"""
snirh_fetch_temperature.py — Monthly temperature data from SNIRH synthesis page.

NOTE: The SNIRH temperatura/boletim/estacao.php endpoint is a NATIONAL SYNTHESIS page.
It does NOT return per-station data — the cod_estacao parameter is ignored and all
stations return identical values representing a national reference station (approx. Lisbon).

Per-station time series (janela_verdados.php) requires an authenticated SNIRH account.
Anonymous access is limited to: station catalog, reservoir fill, and national synthesis.

For per-station temperature extremes, use:
  - IPMA current observations: ipma_fetch_observations.py (live hourly readings)
  - WorldClim/CHELSA rasters (already used by comercial_maps for climate normals)

This scraper is kept for the historical national synthesis and as a placeholder for when
authenticated access becomes available.

Output: temperature_monthly.csv (append-only, deduplicated by station_code+hydro_year+month).
"""

import logging
import re
from datetime import datetime, timezone, date
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from .snirh_session import BASE_URL, get_cache_dir, get_session
from .snirh_station_catalog import CSV_NAME as CATALOG_CSV

load_dotenv()
logger = logging.getLogger(__name__)

TEMP_BOLETIM_URL = f"{BASE_URL}/snirh/_dadossintese/temperatura/boletim/estacao.php"
CSV_NAME = "temperature_monthly.csv"
COLS = [
    "site_id", "station_code", "station_name", "hydro_year",
    "month", "t_mean", "t_mean_hist", "t_min", "t_max", "fetched_at",
]

# Hydrological year months: October starts the year
PT_MONTH_ABBR = {
    "OUT": 10, "NOV": 11, "DEZ": 12,
    "JAN": 1, "FEV": 2, "MAR": 3, "ABR": 4,
    "MAI": 5, "JUN": 6, "JUL": 7, "AGO": 8, "SET": 9,
}

# Portugese row labels → output column name
ROW_LABEL_MAP = {
    "temperatura média mensal histórica": "t_mean_hist",
    "temperatura média mensal": "t_mean",
    "temperatura mensal mínima": "t_min",
    "temperatura mensal máxima": "t_max",
}


def _current_hydro_year() -> int:
    """Return the starting year of the current hydrological year (e.g. 2025 for 2025/26)."""
    today = date.today()
    return today.year if today.month >= 10 else today.year - 1


def _parse_temp_boletim(html: str) -> dict:
    """
    Parse the temperatura boletim page HTML.

    Returns a dict: {month_int: {t_mean, t_mean_hist, t_min, t_max}}
    Only months with at least one non-null value are included.
    """
    soup = BeautifulSoup(html, "lxml")
    tables = soup.find_all("table")
    if not tables:
        return {}

    # The data table is the last one; it has column headers OUT,NOV,...,SET,ANUAL
    data_table = tables[-1]
    rows = data_table.find_all("tr")
    if not rows:
        return {}

    # First row: month headers
    header_cells = [td.get_text(strip=True).upper() for td in rows[0].find_all(["td", "th"])]
    # Map column index → month int (skip first label column and ANUAL)
    col_month = {}
    for i, h in enumerate(header_cells):
        if h in PT_MONTH_ABBR:
            col_month[i] = PT_MONTH_ABBR[h]

    month_data: dict[int, dict] = {}

    for row in rows[1:]:
        cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
        if not cells:
            continue
        label = cells[0].lower()
        # Match label to output column; use longest-prefix matching
        col_key = None
        for pattern, key in ROW_LABEL_MAP.items():
            if pattern in label:
                col_key = key
                break
        if col_key is None:
            continue

        for idx, month in col_month.items():
            if idx < len(cells):
                val_str = cells[idx].strip().replace(",", ".")
                if val_str and val_str.lower() not in ("n/d", "-", ""):
                    try:
                        val = float(val_str)
                        if month not in month_data:
                            month_data[month] = {}
                        month_data[month][col_key] = val
                    except ValueError:
                        pass

    return month_data


def fetch_temperature(
    session: requests.Session | None = None,
    station_filter: list[str] | None = None,
    years_back: int = 1,
) -> pd.DataFrame:
    """
    Fetch monthly temperature data for all (or filtered) meteorological stations.

    station_filter: list of site_id strings to fetch; if None, uses all Meteorológica
                    stations from stations_catalog.csv.
    years_back: number of hydrological years to fetch (default 1 = current only).

    Returns DataFrame of new rows.
    """
    if session is None:
        session = get_session()
    if session is None:
        logger.error("Cannot fetch temperature: no session")
        return pd.DataFrame(columns=COLS)

    # Load station catalog
    cache_dir = get_cache_dir()
    catalog_path = cache_dir / CATALOG_CSV
    if not catalog_path.exists():
        logger.error("Station catalog not found: %s", catalog_path)
        return pd.DataFrame(columns=COLS)

    catalog = pd.read_csv(catalog_path, dtype=str)
    meteo = catalog[catalog["network"].str.contains("Meteorol", na=False, case=False)]
    if station_filter:
        meteo = meteo[meteo["site_id"].isin(station_filter)]

    if meteo.empty:
        logger.warning("No meteorological stations found in catalog")
        return pd.DataFrame(columns=COLS)

    hydro_year_start = _current_hydro_year()
    target_years = list(range(hydro_year_start - years_back + 1, hydro_year_start + 1))
    logger.info(
        "Fetching temperature for %d stations × %d years = up to %d requests",
        len(meteo), len(target_years), len(meteo) * len(target_years),
    )

    all_rows = []
    ok = err = skip = 0

    for _, station in meteo.iterrows():
        site_id = station.get("site_id", "")
        scode = station.get("station_code", "")
        sname = station.get("station_name", "")
        if not site_id:
            skip += 1
            continue

        for yr in target_years:
            url = f"{TEMP_BOLETIM_URL}?prec_anoh={yr}&cod_estacao={site_id}"
            try:
                resp = session.get(url, timeout=20)
                resp.raise_for_status()
                month_data = _parse_temp_boletim(resp.text)
                if not month_data:
                    skip += 1
                    continue

                hydro_label = f"{yr}/{str(yr + 1)[-2:]}"
                for month, vals in month_data.items():
                    all_rows.append(
                        {
                            "site_id": site_id,
                            "station_code": scode,
                            "station_name": sname,
                            "hydro_year": hydro_label,
                            "month": month,
                            "t_mean": vals.get("t_mean"),
                            "t_mean_hist": vals.get("t_mean_hist"),
                            "t_min": vals.get("t_min"),
                            "t_max": vals.get("t_max"),
                        }
                    )
                ok += 1
            except requests.RequestException as exc:
                logger.warning("Failed station %s yr=%d: %s", scode, yr, exc)
                err += 1

    logger.info("Temperature fetch done: %d ok, %d errors, %d skipped", ok, err, skip)

    if not all_rows:
        logger.warning("No temperature data retrieved")
        return pd.DataFrame(columns=COLS)

    df_new = pd.DataFrame(all_rows, columns=COLS[:-1])
    now_iso = datetime.now(timezone.utc).isoformat()
    df_new["fetched_at"] = now_iso

    csv_path = cache_dir / CSV_NAME
    if csv_path.exists():
        existing = pd.read_csv(csv_path, dtype=str)
        combined = pd.concat([existing, df_new.astype(str)], ignore_index=True)
        combined.sort_values("fetched_at", inplace=True)
        combined.drop_duplicates(subset=["site_id", "hydro_year", "month"], keep="last", inplace=True)
        combined.reset_index(drop=True, inplace=True)
    else:
        combined = df_new.astype(str)

    combined.to_csv(csv_path, index=False)
    logger.info(
        "Temperature saved → %s (%d new rows, %d total)",
        csv_path, len(df_new), len(combined),
    )
    return df_new


if __name__ == "__main__":
    import sys
    from .snirh_session import configure_logging

    configure_logging()
    session = get_session()
    result = fetch_temperature(session)
    print(f"Fetched {len(result)} temperature rows")
    sys.exit(0)
