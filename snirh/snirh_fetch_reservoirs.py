"""
snirh_fetch_reservoirs.py — Monthly reservoir fill data (albufeiras).

Parses the SNIRH albufeiras bulletin page for reservoir storage data.
Output: albufeiras_fill.csv (append-only).
"""

import logging
import re
from datetime import datetime, timezone, date

import pandas as pd
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from .snirh_session import BASE_URL, get_cache_dir, get_session

load_dotenv()
logger = logging.getLogger(__name__)

BULLETIN_URL = f"{BASE_URL}/index.php?idMain=1&idItem=1.3"
CSV_NAME = "albufeiras_fill.csv"
COLS = [
    "albufeira_code",
    "albufeira_nome",
    "year",
    "month",
    "volume_hm3",
    "pct_capacidade",
    "fetched_at",
]

# Portuguese month names → int
PT_MONTHS = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8,
    "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
}


def _extract_bulletin_date(soup: BeautifulSoup) -> tuple[int, int]:
    """Try to extract year/month from bulletin page text. Falls back to today."""
    today = date.today()
    text = soup.get_text(separator=" ", strip=True).lower()

    for pt_month, month_num in PT_MONTHS.items():
        pattern = rf"{pt_month}\s+de\s+(\d{{4}})"
        m = re.search(pattern, text)
        if m:
            return int(m.group(1)), month_num

    # Try numeric date patterns like "2024-03" or "03/2024"
    m = re.search(r"(\d{4})[/-](\d{2})", text)
    if m:
        return int(m.group(1)), int(m.group(2))

    logger.warning("Could not extract bulletin date; using today (%d/%02d)", today.year, today.month)
    return today.year, today.month


def _parse_reservoir_table(soup: BeautifulSoup, year: int, month: int) -> pd.DataFrame:
    """
    Parse all tables on the page and return the one that looks like reservoir data.
    Expected columns contain: nome/albufeira, capacidade, volume, %
    """
    rows = []
    tables = soup.find_all("table")

    for table in tables:
        headers_raw = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        headers_all = " ".join(headers_raw)
        # Look for table containing reservoir-related headers
        if not any(k in headers_all for k in ["volume", "capacidade", "albufeira", "nome"]):
            # Also check first row TDs as headers
            first_row = table.find("tr")
            if first_row:
                cells = [td.get_text(strip=True).lower() for td in first_row.find_all("td")]
                headers_all = " ".join(cells)
                if not any(k in headers_all for k in ["volume", "capacidade", "albufeira", "nome"]):
                    continue

        for tr in table.find_all("tr"):
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cells) < 3:
                continue
            # Skip header rows
            if any(h in cells[0].lower() for h in ["nome", "albufeira", "bacia"]):
                continue
            nome = cells[0].strip()
            if not nome:
                continue

            # Try to extract numeric values for volume and %
            volume = None
            pct = None
            for cell in cells[1:]:
                clean = cell.replace(",", ".").replace("%", "").strip()
                try:
                    val = float(clean)
                    if "%" in cell or (0 <= val <= 100 and pct is None):
                        pct = val
                    elif volume is None:
                        volume = val
                except ValueError:
                    pass

            rows.append(
                {
                    "albufeira_code": re.sub(r"\s+", "_", nome.lower()),
                    "albufeira_nome": nome,
                    "year": year,
                    "month": month,
                    "volume_hm3": volume,
                    "pct_capacidade": pct,
                }
            )

    if not rows:
        logger.warning("No reservoir rows extracted from page")
    return pd.DataFrame(rows, columns=COLS[:-1]) if rows else pd.DataFrame(columns=COLS[:-1])


def fetch_reservoir_fill(session: requests.Session | None = None) -> pd.DataFrame:
    """
    Fetch current SNIRH albufeiras bulletin and extract reservoir fill data.

    Returns new rows DataFrame (may be empty on failure).
    """
    if session is None:
        session = get_session()
    if session is None:
        logger.error("Cannot fetch reservoir fill: no session")
        return pd.DataFrame(columns=COLS)

    try:
        resp = session.get(BULLETIN_URL, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Failed to fetch albufeiras bulletin: %s", exc)
        return pd.DataFrame(columns=COLS)

    soup = BeautifulSoup(resp.text, "lxml")
    year, month = _extract_bulletin_date(soup)
    df = _parse_reservoir_table(soup, year, month)

    if df.empty:
        logger.warning("No reservoir data extracted for %d/%02d", year, month)
        _write_empty_csv()
        return pd.DataFrame(columns=COLS)

    now_iso = datetime.now(timezone.utc).isoformat()
    df["fetched_at"] = now_iso

    cache_dir = get_cache_dir()
    csv_path = cache_dir / CSV_NAME

    if csv_path.exists():
        existing = pd.read_csv(csv_path, dtype=str)
        combined = pd.concat([existing, df.astype(str)], ignore_index=True)
        combined.drop_duplicates(
            subset=["albufeira_code", "year", "month"], keep="last", inplace=True
        )
    else:
        combined = df.astype(str)

    combined.to_csv(csv_path, index=False)
    logger.info(
        "Reservoir fill saved → %s (%d rows for %d/%02d, %d total)",
        csv_path,
        len(df),
        year,
        month,
        len(combined),
    )
    return df


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
