"""
snirh_fetch_temperature.py — Monthly TX/TN temperature extremes per station.

Hits the SNIRH time-series AJAX endpoint for meteorological stations.
Output: temperatura_extremos.csv (append-only).
"""

import logging
import os
from datetime import datetime, timezone, date
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

from .snirh_session import BASE_URL, get_cache_dir, get_session

load_dotenv()
logger = logging.getLogger(__name__)

DADOS_BASE_URL = f"{BASE_URL}/snirh/_dadosbase/site/paraAjax/dadosBase.php"
CSV_NAME = "temperatura_extremos.csv"
COLS = [
    "station_code",
    "station_name",
    "year",
    "month",
    "tx_abs",
    "tn_abs",
    "source_url",
    "fetched_at",
]

YEARS_BACK = int(os.getenv("SNIRH_YEARS_BACK", "5"))


def _build_params(station_code: str, year: int, month: int, parm: str) -> dict:
    start = f"{year}-{month:02d}-01"
    # Last day of the month (approximate — SNIRH accepts over-range dates)
    end = f"{year}-{month:02d}-28"
    return {
        "stationType": "meteorologica",
        "parm": parm,
        "tmin": start,
        "tmax": end,
        "estacoes": station_code,
        "anos": str(year),
    }


def _fetch_parm(
    session: requests.Session,
    station_code: str,
    year: int,
    month: int,
    parm: str,
) -> float | None:
    params = _build_params(station_code, year, month, parm)
    try:
        resp = session.get(DADOS_BASE_URL, params=params, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        # Payload is usually a list of dicts or nested structure
        if isinstance(payload, list) and payload:
            for item in payload:
                val = item.get("valor", item.get("value", item.get("v", None)))
                if val is not None:
                    try:
                        return float(val)
                    except (ValueError, TypeError):
                        pass
        elif isinstance(payload, dict):
            val = payload.get("valor", payload.get("value", None))
            if val is not None:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    pass
    except (requests.RequestException, ValueError) as exc:
        logger.debug("Parm %s station %s %d/%02d failed: %s", parm, station_code, year, month, exc)
    return None


def fetch_temperature_extremes(
    station_codes: list[str] | None = None,
    station_names: dict[str, str] | None = None,
    years_back: int | None = None,
    session: requests.Session | None = None,
) -> pd.DataFrame:
    """
    Fetch monthly TX/TN extremes for each station in station_codes.

    If station_codes is None, tries to read from stations_catalog.csv.
    Returns new rows DataFrame (may be empty if all requests fail).
    """
    if session is None:
        session = get_session()
    if session is None:
        logger.error("Cannot fetch temperature extremes: no session")
        return pd.DataFrame(columns=COLS)

    if years_back is None:
        years_back = YEARS_BACK

    # Load stations from catalog if not provided
    if station_codes is None:
        cache_dir = get_cache_dir()
        catalog_path = cache_dir / "stations_catalog.csv"
        if catalog_path.exists():
            cat = pd.read_csv(catalog_path, dtype=str)
            station_codes = cat["station_code"].dropna().unique().tolist()
            if station_names is None:
                station_names = dict(zip(cat["station_code"], cat["station_name"]))
        else:
            logger.warning("No stations_catalog.csv found; using empty station list")
            station_codes = []

    if not station_codes:
        logger.warning("No stations to fetch temperature data for")
        _write_empty_csv()
        return pd.DataFrame(columns=COLS)

    if station_names is None:
        station_names = {}

    today = date.today()
    start_year = today.year - years_back
    rows = []

    for station_code in station_codes:
        sname = station_names.get(station_code, "")
        for year in range(start_year, today.year + 1):
            max_month = today.month if year == today.year else 12
            for month in range(1, max_month + 1):
                tx = _fetch_parm(session, station_code, year, month, "TX")
                tn = _fetch_parm(session, station_code, year, month, "TN")
                if tx is None and tn is None:
                    logger.debug("No data: station %s %d/%02d", station_code, year, month)
                    continue
                rows.append(
                    {
                        "station_code": station_code,
                        "station_name": sname,
                        "year": year,
                        "month": month,
                        "tx_abs": tx,
                        "tn_abs": tn,
                        "source_url": DADOS_BASE_URL,
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                    }
                )

    df = pd.DataFrame(rows, columns=COLS) if rows else pd.DataFrame(columns=COLS)
    _append_to_csv(df)
    return df


def _write_empty_csv() -> None:
    cache_dir = get_cache_dir()
    csv_path = cache_dir / CSV_NAME
    if not csv_path.exists():
        pd.DataFrame(columns=COLS).to_csv(csv_path, index=False)
        logger.info("Created empty %s", csv_path)


def _append_to_csv(df: pd.DataFrame) -> None:
    cache_dir = get_cache_dir()
    csv_path = cache_dir / CSV_NAME

    if df.empty:
        _write_empty_csv()
        return

    if csv_path.exists():
        existing = pd.read_csv(csv_path, dtype=str)
        combined = pd.concat([existing, df.astype(str)], ignore_index=True)
        combined.drop_duplicates(
            subset=["station_code", "year", "month"], keep="last", inplace=True
        )
    else:
        combined = df.astype(str)

    combined.to_csv(csv_path, index=False)
    logger.info("Temperature extremes saved → %s (%d total rows)", csv_path, len(combined))


if __name__ == "__main__":
    import sys
    from .snirh_session import configure_logging

    configure_logging()
    session = get_session()
    result = fetch_temperature_extremes(session=session)
    print(f"Fetched {len(result)} temperature rows")
    sys.exit(0)
