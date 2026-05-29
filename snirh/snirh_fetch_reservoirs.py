"""
snirh_fetch_reservoirs.py — Monthly reservoir fill data by basin (albufeiras).

Scrapes the tabelageral.php endpoint which returns a basin-level fill % table
for all hydrological years available. This is the data source used by the SNIRH
JS map UI (albuf_funcoes.js) and is actively updated monthly.

Output: albufeiras_fill.csv (append-only, deduplicated by basin+hydrological_year+month).
Columns: basin, hydro_year, month, pct_fill, fetched_at
"""

import logging
import re
from datetime import datetime, timezone

import pandas as pd
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from .snirh_session import BASE_URL, get_cache_dir, get_session

load_dotenv()
logger = logging.getLogger(__name__)

# Base path for the albufeiras synthesis tables
TABELA_BASE = f"{BASE_URL}/snirh/_dadossintese/albufeiras/tabelas/tabelageral.php"
CSV_NAME = "albufeiras_fill.csv"
COLS = ["basin", "hydro_year", "month", "pct_fill", "fetched_at"]

# Portuguese month abbreviation → int (hydrological year starts in October)
PT_MONTH_ABBR = {
    "OUT": 10, "NOV": 11, "DEZ": 12,
    "JAN": 1, "FEV": 2, "MAR": 3, "ABR": 4,
    "MAI": 5, "JUN": 6, "JUL": 7, "AGO": 8, "SET": 9,
}


def _parse_tabelageral(html: str) -> pd.DataFrame:
    """
    Parse tabelageral.php response into rows.

    Table structure:
      Row 0: headers  ["", "", "ARADE", "AVE", ...]
      Row 1: capacity ["Capacidade Total ...", "233.1", ...]
      Remaining rows grouped by hydrological year label then month abbr:
        ["Média (%)","OUT", val, val, ...]   ← first row of group has year+month merged
        ["NOV", val, ...]                    ← subsequent rows have only month
      Year label like "2024/25(%)" appears at the start of a new group.
    """
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table")
    if not table:
        logger.warning("No table found in tabelageral response")
        return pd.DataFrame(columns=COLS[:-1])

    rows = table.find_all("tr")
    if not rows:
        return pd.DataFrame(columns=COLS[:-1])

    # Extract basin names from header row
    header_cells = [td.get_text(strip=True) for td in rows[0].find_all(["td", "th"])]
    # First two columns are label placeholders, rest are basin names
    basins = [c for c in header_cells[2:] if c]

    records = []
    current_year = "Média"  # rows before first explicit year label are multi-year averages

    for row in rows[2:]:  # skip header + capacity row
        cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
        if not cells:
            continue

        # Detect year group header pattern like "2024/25(%)" or "Média\n(%)"
        first = cells[0].strip()
        if re.search(r"\d{4}/\d{2}", first) or "média" in first.lower():
            # Extract year like "2024/25" → "2024/25"
            m = re.search(r"(\d{4}/\d{2})", first)
            current_year = m.group(1) if m else first.split("(")[0].strip()
            month_cell = cells[1] if len(cells) > 1 else ""
            values = cells[2:]
        else:
            month_cell = first
            values = cells[1:]

        month_abbr = month_cell.strip().upper()
        if month_abbr not in PT_MONTH_ABBR:
            continue
        month_num = PT_MONTH_ABBR[month_abbr]

        for i, basin in enumerate(basins):
            if i < len(values):
                val_str = values[i].strip()
                if val_str and val_str.lower() != "n/d":
                    try:
                        pct = float(val_str.replace(",", ".").replace("\xa0", "").replace("\u00a0", ""))
                        records.append({
                            "basin": basin,
                            "hydro_year": current_year,
                            "month": month_num,
                            "pct_fill": pct,
                        })
                    except ValueError:
                        pass

    return pd.DataFrame(records, columns=COLS[:-1]) if records else pd.DataFrame(columns=COLS[:-1])


def fetch_reservoir_fill(
    session: requests.Session | None = None,
    years_back: int = 3,
) -> pd.DataFrame:
    """
    Fetch SNIRH albufeiras fill % by basin for recent hydrological years.

    Fetches tabelageral.php with percOUvolum=0 (percentage mode) for each
    hydrological year from (current - years_back) to current.

    Returns new rows DataFrame.
    """
    if session is None:
        session = get_session()
    if session is None:
        logger.error("Cannot fetch reservoir fill: no session")
        return pd.DataFrame(columns=COLS)

    from datetime import date
    today = date.today()
    # Hydrological year starts October. Current hydro year e.g. Oct 2025 → "2025"
    hydro_year_start = today.year if today.month >= 10 else today.year - 1
    target_years = list(range(hydro_year_start - years_back + 1, hydro_year_start + 2))

    all_frames = []
    for yr in target_years:
        url = f"{TABELA_BASE}?percOUvolum=0&anohi={yr}"
        try:
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
            df = _parse_tabelageral(resp.text)
            if not df.empty:
                logger.info("Parsed %d basin-month rows for anohi=%d", len(df), yr)
                all_frames.append(df)
            else:
                logger.warning("No data for anohi=%d", yr)
        except requests.RequestException as exc:
            logger.warning("Failed to fetch reservoirs for anohi=%d: %s", yr, exc)

    if not all_frames:
        logger.warning("No reservoir data retrieved")
        _write_empty_csv()
        return pd.DataFrame(columns=COLS)

    df_new = pd.concat(all_frames, ignore_index=True)
    now_iso = datetime.now(timezone.utc).isoformat()
    df_new["fetched_at"] = now_iso

    cache_dir = get_cache_dir()
    csv_path = cache_dir / CSV_NAME

    if csv_path.exists():
        existing = pd.read_csv(csv_path, dtype=str)
        combined = pd.concat([existing, df_new.astype(str)], ignore_index=True)
        combined.sort_values("fetched_at", inplace=True)
        combined.drop_duplicates(subset=["basin", "hydro_year", "month"], keep="last", inplace=True)
        combined.reset_index(drop=True, inplace=True)
    else:
        combined = df_new.astype(str)

    combined.to_csv(csv_path, index=False)
    logger.info(
        "Reservoir fill saved → %s (%d new rows, %d total)",
        csv_path, len(df_new), len(combined),
    )
    return df_new


def _write_empty_csv() -> None:
    cache_dir = get_cache_dir()
    csv_path = cache_dir / CSV_NAME
    if not csv_path.exists():
        pd.DataFrame(columns=COLS).to_csv(csv_path, index=False)
        logger.info("Created empty %s", csv_path)


if __name__ == "__main__":
    import sys
    from .snirh_session import configure_logging

    configure_logging()
    session = get_session()
    result = fetch_reservoir_fill(session)
    print(f"Fetched {len(result)} reservoir rows")
    sys.exit(0)
