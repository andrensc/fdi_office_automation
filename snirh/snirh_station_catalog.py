"""
snirh_station_catalog.py — Station catalog scraper (quarterly refresh).

Tries the AJAX endpoint first; falls back to parsing the HTML station list.
Output: stations_catalog.csv (append-only, deduplicated by station_code + fetched_at date).
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from .snirh_session import BASE_URL, get_cache_dir, get_session

load_dotenv()
logger = logging.getLogger(__name__)

CATALOG_URL = f"{BASE_URL}/index.php?idMain=2&idItem=1"
AJAX_URL = f"{BASE_URL}/snirh/_dadosbase/site/paraAjax/getStations.php"
CSV_NAME = "stations_catalog.csv"
COLS = ["station_code", "station_name", "network", "lat", "lon", "altitude", "active", "fetched_at"]


def _parse_ajax(data: list[dict]) -> pd.DataFrame:
    rows = []
    for item in data:
        rows.append(
            {
                "station_code": str(item.get("codigo", item.get("id", ""))).strip(),
                "station_name": str(item.get("nome", item.get("name", ""))).strip(),
                "network": str(item.get("rede", item.get("network", ""))).strip(),
                "lat": item.get("latitude", item.get("lat", None)),
                "lon": item.get("longitude", item.get("lon", None)),
                "altitude": item.get("altitude", None),
                "active": item.get("activa", item.get("active", True)),
            }
        )
    return pd.DataFrame(rows, columns=COLS[:-1])


def _parse_html(html: str) -> pd.DataFrame:
    soup = BeautifulSoup(html, "lxml")
    rows = []
    table = soup.find("table")
    if not table:
        logger.warning("No table found in station catalog HTML")
        return pd.DataFrame(columns=COLS[:-1])

    headers: list[str] = []
    thead = table.find("thead") or table
    for th in thead.find_all("th"):
        headers.append(th.get_text(strip=True).lower())

    for tr in table.find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if not cells:
            continue
        # Map by position if we have enough cells; otherwise build partial row
        row: dict = {
            "station_code": cells[0] if len(cells) > 0 else "",
            "station_name": cells[1] if len(cells) > 1 else "",
            "network": cells[2] if len(cells) > 2 else "",
            "lat": cells[3] if len(cells) > 3 else None,
            "lon": cells[4] if len(cells) > 4 else None,
            "altitude": cells[5] if len(cells) > 5 else None,
            "active": True,
        }
        if row["station_code"]:
            rows.append(row)

    return pd.DataFrame(rows, columns=COLS[:-1])


def fetch_station_catalog(session: requests.Session | None = None) -> pd.DataFrame:
    """
    Fetch the SNIRH station catalog and append new records to CSV.

    Returns the new rows DataFrame (may be empty on failure).
    """
    if session is None:
        session = get_session()
    if session is None:
        logger.error("Cannot fetch station catalog: no session")
        return pd.DataFrame(columns=COLS)

    df = pd.DataFrame(columns=COLS[:-1])

    # Try AJAX first
    try:
        resp = session.get(AJAX_URL, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        if isinstance(payload, list) and payload:
            df = _parse_ajax(payload)
            logger.info("Fetched %d stations via AJAX", len(df))
        elif isinstance(payload, dict):
            items = payload.get("data", payload.get("estacoes", []))
            if items:
                df = _parse_ajax(items)
                logger.info("Fetched %d stations via AJAX (dict payload)", len(df))
    except (requests.RequestException, json.JSONDecodeError, ValueError) as exc:
        logger.warning("AJAX station endpoint failed (%s); falling back to HTML", exc)

    # Fallback: HTML
    if df.empty:
        try:
            resp = session.get(CATALOG_URL, timeout=30)
            resp.raise_for_status()
            df = _parse_html(resp.text)
            logger.info("Fetched %d stations via HTML fallback", len(df))
        except requests.RequestException as exc:
            logger.error("HTML fallback also failed: %s", exc)
            return pd.DataFrame(columns=COLS)

    if df.empty:
        logger.warning("Station catalog returned no rows")
        return pd.DataFrame(columns=COLS)

    now_iso = datetime.now(timezone.utc).isoformat()
    df["fetched_at"] = now_iso

    cache_dir = get_cache_dir()
    csv_path = cache_dir / CSV_NAME

    if csv_path.exists():
        existing = pd.read_csv(csv_path, dtype=str)
        combined = pd.concat([existing, df.astype(str)], ignore_index=True)
        combined.drop_duplicates(subset=["station_code", "fetched_at"], keep="last", inplace=True)
    else:
        combined = df.astype(str)

    combined.to_csv(csv_path, index=False)
    logger.info("Station catalog saved → %s (%d total rows)", csv_path, len(combined))
    return df


if __name__ == "__main__":
    import sys
    from .snirh_session import configure_logging

    configure_logging()
    session = get_session()
    result = fetch_station_catalog(session)
    print(f"Fetched {len(result)} station rows")
    sys.exit(0 if not result.empty else 0)
