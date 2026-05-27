"""
snirh_fetch_drought.py — Monthly drought index by region.

Scrapes the SNIRH drought bulletin page and extracts drought classification
(D0–D4) and precipitation anomaly percentage by hydrological region.

Output: drought_index.csv (append-only).
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

BULLETIN_URL = f"{BASE_URL}/index.php?idMain=1&idItem=9.6"
# Sub-section with piezometric / groundwater regional breakdown
SECAID_GROUNDWATER_URL = f"{BASE_URL}/index.php?idMain=1&idItem=9.6&secaid=4"
SECAID_RUNOFF_URL = f"{BASE_URL}/index.php?idMain=1&idItem=9.6&secaid=2"
CSV_NAME = "drought_index.csv"
COLS = [
    "region",
    "year",
    "month",
    "drought_class",
    "precip_anomaly_pct",
    "piezo_anomaly_pct",
    "fetched_at",
]

# Portuguese month names → int
PT_MONTHS = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8,
    "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
}

# Drought classification text → code
DROUGHT_CLASS_MAP = {
    "normal": "D0",
    "seco": "D1",
    "moderado": "D2",
    "severo": "D3",
    "extremo": "D4",
    "excecional": "D4",
    "excepcional": "D4",
    "fraco": "D1",
    "muito seco": "D3",
    "muito severo": "D4",
}

# Known Portuguese hydrological basins / regions
BASIN_KEYWORDS = [
    "minho", "lima", "cávado", "ave", "douro", "vouga", "mondego",
    "lis", "tejo", "sado", "guadiana", "mira", "arade", "algarve",
    "ribeiras do oeste", "ribeiras do alentejo", "continente",
    "norte", "centro", "sul", "alentejo",
]


def _extract_bulletin_date(soup: BeautifulSoup) -> tuple[int, int]:
    """Extract year/month from bulletin text. Falls back to today."""
    today = date.today()
    text = soup.get_text(separator=" ", strip=True).lower()

    for pt_month, month_num in PT_MONTHS.items():
        pattern = rf"{pt_month}\s+(?:de\s+)?(\d{{4}})"
        m = re.search(pattern, text)
        if m:
            return int(m.group(1)), month_num

    m = re.search(r"(\d{4})[/-](\d{2})", text)
    if m:
        return int(m.group(1)), int(m.group(2))

    logger.warning("Cannot extract bulletin date; using today")
    return today.year, today.month


def _classify_drought(text: str) -> str:
    """Map a Portuguese drought classification string to D0–D4 code."""
    text_lower = text.lower().strip()
    for keyword, code in DROUGHT_CLASS_MAP.items():
        if keyword in text_lower:
            return code
    # Check for already-coded Dx pattern
    m = re.search(r"d([0-4])", text_lower)
    if m:
        return f"D{m.group(1)}"
    return "D0"  # default: normal


def _extract_anomaly_pct(text: str) -> float | None:
    """Extract first percentage value found in a string."""
    m = re.search(r"([+-]?\d+(?:[.,]\d+)?)\s*%", text)
    if m:
        return float(m.group(1).replace(",", "."))
    return None


def _parse_drought_tables(
    soup: BeautifulSoup, year: int, month: int
) -> pd.DataFrame:
    """
    Parse drought data from all tables on the page.
    Looks for tables that contain basin/region names and drought terminology.
    Region names must be ≤ 80 characters (excludes intro paragraph cells).
    """
    rows = []
    tables = soup.find_all("table")

    for table in tables:
        all_text = table.get_text(separator=" ", strip=True).lower()
        # Skip tables that don't appear drought-related
        if not any(k in all_text for k in ["seca", "drought", "bacia", "região", "anomalia", "d0", "d1", "d2", "d3", "d4", "escoamento", "variação"]):
            continue

        for tr in table.find_all("tr"):
            cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
            region = cells[0].strip()
            # Skip header rows, empty rows, and long paragraph cells
            if not region or len(region) > 80:
                continue
            if any(h in region.lower() for h in ["nome", "albufeira", "bacia", "estação", "avaliação"]):
                continue
            row_text = " ".join(cells).lower()

            # Check if this row contains a recognisable drought class or basin keyword
            has_basin = any(k in region.lower() for k in BASIN_KEYWORDS)
            has_drought_term = any(
                k in row_text for k in list(DROUGHT_CLASS_MAP.keys()) + ["d0", "d1", "d2", "d3", "d4"]
            )
            if not (has_basin or has_drought_term):
                continue

            drought_class = "D0"
            precip_anom = None
            piezo_anom = None

            # Extract drought class from any cell
            for cell in cells[1:]:
                if any(k in cell.lower() for k in DROUGHT_CLASS_MAP):
                    drought_class = _classify_drought(cell)
                    break
                dm = re.search(r"[Dd]([0-4])", cell)
                if dm:
                    drought_class = f"D{dm.group(1)}"
                    break

            # Extract anomaly percentages — first two numeric % found
            pct_vals: list[float] = []
            for cell in cells:
                v = _extract_anomaly_pct(cell)
                if v is not None:
                    pct_vals.append(v)
            if pct_vals:
                precip_anom = pct_vals[0]
            if len(pct_vals) > 1:
                piezo_anom = pct_vals[1]

            rows.append(
                {
                    "region": region,
                    "year": year,
                    "month": month,
                    "drought_class": drought_class,
                    "precip_anomaly_pct": precip_anom,
                    "piezo_anomaly_pct": piezo_anom,
                }
            )

    if not rows:
        logger.warning("No drought rows extracted from tables; trying free-text extraction")
        rows = _parse_drought_freetext(soup, year, month)

    return pd.DataFrame(rows, columns=COLS[:-1]) if rows else pd.DataFrame(columns=COLS[:-1])


def _parse_drought_freetext(
    soup: BeautifulSoup, year: int, month: int
) -> list[dict]:
    """
    Fallback: scan short-text elements for drought-related sentences.
    Only considers text blocks ≤ 150 chars to avoid grabbing intro paragraphs.
    Creates one row per recognisable basin mention.
    """
    rows = []
    seen: set[str] = set()

    # Prefer cell/header text — usually shorter and more structured
    candidates = soup.find_all(["td", "th", "li", "h2", "h3", "h4", "label"])
    for elem in candidates:
        block = elem.get_text(separator=" ", strip=True)
        if not block or len(block) > 150:
            continue
        block_lower = block.lower()
        for basin in BASIN_KEYWORDS:
            if basin not in block_lower:
                continue
            drought_class = _classify_drought(block)
            precip_anom = _extract_anomaly_pct(block)
            key = basin.lower()
            if key not in seen:
                seen.add(key)
                rows.append(
                    {
                        "region": basin.title(),
                        "year": year,
                        "month": month,
                        "drought_class": drought_class,
                        "precip_anomaly_pct": precip_anom,
                        "piezo_anomaly_pct": None,
                    }
                )
            break  # one row per element

    return rows


def fetch_drought_index(session: requests.Session | None = None) -> pd.DataFrame:
    """
    Fetch the SNIRH drought bulletin and extract drought classifications.

    Tries the main bulletin page first, then the groundwater sub-section for
    regional structure (secaid=4 lists Algarve, Alentejo, Lisboa e Vale do
    Tejo, Centro). Also parses the runoff table (secaid=2) for basin anomalies.

    Returns new rows DataFrame (may be empty on failure).
    """
    if session is None:
        session = get_session()
    if session is None:
        logger.error("Cannot fetch drought index: no session")
        return pd.DataFrame(columns=COLS)

    # --- Fetch main page for bulletin date ---
    try:
        resp = session.get(BULLETIN_URL, timeout=30)
        resp.raise_for_status()
        logger.info("Drought bulletin fetched (status %s, %d bytes)", resp.status_code, len(resp.content))
    except requests.RequestException as exc:
        logger.error("Failed to fetch drought bulletin: %s", exc)
        return pd.DataFrame(columns=COLS)

    main_soup = BeautifulSoup(resp.text, "lxml")
    year, month = _extract_bulletin_date(main_soup)
    logger.info("Bulletin date: %d/%02d", year, month)

    # --- Try groundwater sub-page for region list ---
    all_rows: list[dict] = []
    try:
        resp2 = session.get(SECAID_GROUNDWATER_URL, timeout=30)
        resp2.raise_for_status()
        sw_soup = BeautifulSoup(resp2.text, "lxml")
        df_gw = _parse_drought_tables(sw_soup, year, month)
        if not df_gw.empty:
            all_rows.extend(df_gw.to_dict("records"))
            logger.info("Groundwater sub-page yielded %d rows", len(df_gw))
    except requests.RequestException as exc:
        logger.warning("Groundwater sub-page fetch failed: %s", exc)

    # --- Try runoff sub-page for basin anomaly data ---
    try:
        resp3 = session.get(SECAID_RUNOFF_URL, timeout=30)
        resp3.raise_for_status()
        ro_soup = BeautifulSoup(resp3.text, "lxml")
        df_ro = _parse_drought_tables(ro_soup, year, month)
        if not df_ro.empty:
            all_rows.extend(df_ro.to_dict("records"))
            logger.info("Runoff sub-page yielded %d rows", len(df_ro))
    except requests.RequestException as exc:
        logger.warning("Runoff sub-page fetch failed: %s", exc)

    # --- Fallback: main page freetext ---
    if not all_rows:
        all_rows = _parse_drought_freetext(main_soup, year, month)
        logger.info("Freetext fallback yielded %d rows", len(all_rows))

    if not all_rows:
        logger.warning("No drought data extracted for %d/%02d", year, month)
        _write_empty_csv()
        return pd.DataFrame(columns=COLS)

    df = pd.DataFrame(all_rows, columns=COLS[:-1])
    # Deduplicate by region keeping last
    df.drop_duplicates(subset=["region"], keep="last", inplace=True)

    now_iso = datetime.now(timezone.utc).isoformat()
    df["fetched_at"] = now_iso

    cache_dir = get_cache_dir()
    csv_path = cache_dir / CSV_NAME

    if csv_path.exists():
        existing = pd.read_csv(csv_path, dtype=str)
        combined = pd.concat([existing, df.astype(str)], ignore_index=True)
        combined.drop_duplicates(
            subset=["region", "year", "month"], keep="last", inplace=True
        )
    else:
        combined = df.astype(str)

    combined.to_csv(csv_path, index=False)
    logger.info(
        "Drought index saved → %s (%d rows for %d/%02d, %d total)",
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
    logger.info("Fetching drought index from %s", BULLETIN_URL)
    session = get_session()
    result = fetch_drought_index(session)
    if result.empty:
        print("Warning: drought index returned no rows (check logs)")
    else:
        print(f"Fetched {len(result)} drought rows:")
        print(result.to_string(index=False))
    sys.exit(0)
