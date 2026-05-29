"""
ipma_fetch_observations.py — Hourly weather observations from IPMA's open REST API.

IPMA (Instituto Português do Mar e da Atmosfera) provides a genuine REST API with
live hourly readings from 222+ weather stations across Portugal. Unlike SNIRH, this
API requires no authentication and returns per-station data.

Endpoint: https://api.ipma.pt/open-data/observation/meteorology/stations/observations.json
Format: { "YYYY-MM-DDTHH:MM": { "station_id": { fields... }, ... }, ... }

Fields per station:
  - temperatura         (°C, current)
  - intensidadeVento    (m/s wind speed)
  - intensidadeVentoKM  (km/h wind speed)
  - idDireccVento       (wind direction code 0-8: N=1,NE=2,E=3,SE=4,S=5,SW=6,W=7,NW=8)
  - descDirVento        (wind direction label e.g. "N","SW")
  - humidade            (% relative humidity)
  - pressao             (hPa atmospheric pressure)
  - radiacao            (W/m² solar radiation)
  - precAcumulada       (mm accumulated precipitation)
  (-99 = no data)

Station catalog: https://api.ipma.pt/open-data/observation/meteorology/stations/stations.json
  222 stations with idEstacao, localEstacao, and coordinates (lon, lat).

Usage:
    python3 -m snirh.ipma_fetch_observations
    python3 -m snirh.ipma_fetch_observations --dry-run
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

logger = logging.getLogger(__name__)

STATIONS_URL = "https://api.ipma.pt/open-data/observation/meteorology/stations/stations.json"
OBS_URL = "https://api.ipma.pt/open-data/observation/meteorology/stations/observations.json"
STATIONS_CSV = "ipma_stations.csv"
OBS_CSV = "ipma_observations.csv"

OBS_COLS = [
    "station_id", "station_name", "lat", "lon",
    "timestamp", "temperatura", "t_min_6h", "t_max_6h",
    "intensidade_vento_ms", "intensidade_vento_kmh", "dir_vento_code", "dir_vento",
    "humidade", "pressao", "radiacao", "prec_acumulada",
    "fetched_at",
]


def _get_cache_dir() -> Path:
    """Return the shared SNIRH/climate cache directory."""
    try:
        from .snirh_session import get_cache_dir
        return get_cache_dir()
    except ImportError:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from snirh.snirh_session import get_cache_dir
        return get_cache_dir()


def fetch_ipma_stations(session: requests.Session | None = None) -> pd.DataFrame:
    """
    Download IPMA station catalog (222 stations with coordinates).
    Saves to ipma_stations.csv and returns DataFrame.
    """
    s = session or requests.Session()
    s.headers.setdefault("User-Agent", "FdI-comercial-maps/1.0")

    logger.info("Fetching IPMA station catalog…")
    r = s.get(STATIONS_URL, timeout=30)
    r.raise_for_status()

    features = r.json()
    rows = []
    for feat in features:
        props = feat.get("properties", {})
        coords = feat.get("geometry", {}).get("coordinates", [None, None])
        rows.append({
            "station_id": props.get("idEstacao"),
            "station_name": props.get("localEstacao", ""),
            "lon": coords[0],
            "lat": coords[1],
        })

    df = pd.DataFrame(rows)
    cache = _get_cache_dir()
    out = cache / STATIONS_CSV
    df.to_csv(out, index=False, encoding="utf-8")
    logger.info(f"IPMA stations: {len(df)} rows → {out}")
    return df


def fetch_ipma_observations(
    session: requests.Session | None = None,
    dry_run: bool = False,
) -> pd.DataFrame:
    """
    Download IPMA current hourly observations for all stations.
    Appends new timestamps to ipma_observations.csv (deduplicated by station_id+timestamp).

    Returns DataFrame of new rows added this run.
    """
    s = session or requests.Session()
    s.headers.setdefault("User-Agent", "FdI-comercial-maps/1.0")

    # Load station lookup
    cache = _get_cache_dir()
    stations_path = cache / STATIONS_CSV
    if stations_path.exists():
        stations_df = pd.read_csv(stations_path, dtype={"station_id": str})
        station_lookup = {
            str(row["station_id"]): (row["station_name"], row["lat"], row["lon"])
            for _, row in stations_df.iterrows()
        }
    else:
        logger.warning("IPMA stations catalog not found — run fetch_ipma_stations first")
        station_lookup = {}

    logger.info("Fetching IPMA hourly observations…")
    r = s.get(OBS_URL, timeout=60)
    r.raise_for_status()
    data = r.json()

    fetched_at = datetime.now(timezone.utc).isoformat()
    rows = []
    for timestamp, stations in data.items():
        for sid, obs in stations.items():
            if obs is None:
                continue
            name, lat, lon = station_lookup.get(str(sid), ("", None, None))
            row = {
                "station_id": sid,
                "station_name": name,
                "lat": lat,
                "lon": lon,
                "timestamp": timestamp,
                "temperatura": obs.get("temperatura"),
                "t_min_6h": None,  # not in this endpoint
                "t_max_6h": None,
                "intensidade_vento_ms": obs.get("intensidadeVento"),
                "intensidade_vento_kmh": obs.get("intensidadeVentoKM"),
                "dir_vento_code": obs.get("idDireccVento"),
                "dir_vento": obs.get("descDirVento", ""),
                "humidade": obs.get("humidade"),
                "pressao": obs.get("pressao"),
                "radiacao": obs.get("radiacao"),
                "prec_acumulada": obs.get("precAcumulada"),
                "fetched_at": fetched_at,
            }
            rows.append(row)

    new_df = pd.DataFrame(rows, columns=OBS_COLS)
    # Replace -99 sentinel with NaN
    numeric_cols = ["temperatura", "intensidade_vento_ms", "intensidade_vento_kmh",
                    "humidade", "pressao", "radiacao", "prec_acumulada"]
    for col in numeric_cols:
        if col in new_df.columns:
            new_df[col] = pd.to_numeric(new_df[col], errors="coerce")
            new_df.loc[new_df[col] <= -98, col] = None

    if dry_run:
        logger.info(f"[DRY RUN] Would write {len(new_df)} observation rows")
        return new_df

    out = cache / OBS_CSV
    if out.exists():
        existing = pd.read_csv(out, dtype=str)
        combined = pd.concat([existing, new_df.astype(str)], ignore_index=True)
        combined.drop_duplicates(subset=["station_id", "timestamp"], keep="last", inplace=True)
        combined.to_csv(out, index=False, encoding="utf-8")
    else:
        new_df.to_csv(out, index=False, encoding="utf-8")

    logger.info(f"IPMA observations: {len(new_df)} rows → {out}")
    return new_df


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fetch IPMA hourly weather observations")
    parser.add_argument("--dry-run", action="store_true", help="Print summary without saving")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    s = requests.Session()
    s.headers["User-Agent"] = "FdI-comercial-maps/1.0"

    fetch_ipma_stations(session=s)
    df = fetch_ipma_observations(session=s, dry_run=args.dry_run)

    print(f"\nFetched {len(df)} observation rows from {df['station_id'].nunique()} stations")
    print(f"Timestamps: {sorted(df['timestamp'].unique())[-3:]}")
    print(df[["station_name", "lat", "lon", "timestamp", "temperatura",
              "intensidade_vento_ms", "dir_vento", "humidade"]].head(10).to_string())
