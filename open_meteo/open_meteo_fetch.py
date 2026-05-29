"""
open_meteo_fetch.py — Historical climate data from Open-Meteo ERA5 reanalysis.

Open-Meteo provides FREE access to ERA5 climate reanalysis (1940–present) at any
coordinate worldwide. No account or API key required.

Data source: ERA5 (ECMWF Reanalysis v5) — ~9km horizontal resolution, globally
recognised standard for historical climate analysis.

Why ERA5 over station data:
  - Available at ANY coordinate (not just near stations)
  - Continuous 1940–present with no gaps
  - Includes wind direction & speed (unavailable from SNIRH/IPMA without login)
  - Absolute Tmax/Tmin per day per exact property location

Daily variables fetched:
  Temperature : t_max, t_min, t_mean, t_apparent_max, t_apparent_min (C)
  Wind        : wind_max_kmh, wind_gusts_kmh, wind_dir_deg (dominant direction)
  Precipitation: precip_mm, rain_mm, snow_cm, precip_hours
  Solar       : radiation_mj_m2, et0_mm (FAO evapotranspiration)
  Daylight    : sunrise, sunset, daylight_s, sunshine_s

Outputs (saved to open_meteo_cache/):
  daily_{lat}_{lon}.parquet   — raw daily data (append-on-rerun)
  monthly_{lat}_{lon}.csv     — monthly statistics (mean/min/max/p10/p90)
  extremes_{lat}_{lon}.csv    — all-time absolute records

Usage:
  # Single coordinate
  python3 -m open_meteo.open_meteo_fetch --lat 38.5 --lon -8.1

  # From GeoPackage (uses centroid of Limite da Propriedade layer)
  python3 -m open_meteo.open_meteo_fetch --gpkg /path/to/property.gpkg

  # Dry-run (check what would be fetched without downloading)
  python3 -m open_meteo.open_meteo_fetch --lat 38.5 --lon -8.1 --dry-run
"""

import argparse
import logging
import time
from datetime import date
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://archive-api.open-meteo.com/v1/archive"

DAILY_VARS = [
    "temperature_2m_max",
    "temperature_2m_min",
    "temperature_2m_mean",
    "apparent_temperature_max",
    "apparent_temperature_min",
    "precipitation_sum",
    "rain_sum",
    "snowfall_sum",
    "precipitation_hours",
    "windspeed_10m_max",
    "windgusts_10m_max",
    "winddirection_10m_dominant",
    "shortwave_radiation_sum",
    "et0_fao_evapotranspiration",
    "sunrise",
    "sunset",
    "daylight_duration",
    "sunshine_duration",
]

RENAME = {
    "temperature_2m_max": "t_max",
    "temperature_2m_min": "t_min",
    "temperature_2m_mean": "t_mean",
    "apparent_temperature_max": "t_apparent_max",
    "apparent_temperature_min": "t_apparent_min",
    "precipitation_sum": "precip_mm",
    "rain_sum": "rain_mm",
    "snowfall_sum": "snow_cm",
    "precipitation_hours": "precip_hours",
    "windspeed_10m_max": "wind_max_kmh",
    "windgusts_10m_max": "wind_gusts_kmh",
    "winddirection_10m_dominant": "wind_dir_deg",
    "shortwave_radiation_sum": "radiation_mj_m2",
    "et0_fao_evapotranspiration": "et0_mm",
    "sunrise": "sunrise",
    "sunset": "sunset",
    "daylight_duration": "daylight_s",
    "sunshine_duration": "sunshine_s",
}

ERA5_START = date(1940, 1, 1)  # Full ERA5 record
CLIMATE_NORMAL_START = date(1991, 1, 1)  # WMO 30-year climate normal (1991-2020)
CHUNK_YEARS = 10


def _cache_dir() -> Path:
    base = Path.home() / "Sync/FdI/SIG/shared_inputs/open_meteo_cache"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _coord_key(lat: float, lon: float) -> str:
    lat_str = f"{abs(lat):.4f}{'S' if lat < 0 else 'N'}"
    lon_str = f"{abs(lon):.4f}{'W' if lon < 0 else 'E'}"
    return f"{lat_str}_{lon_str}"


def fetch_daily(
    lat: float,
    lon: float,
    start: date = CLIMATE_NORMAL_START,
    end: Optional[date] = None,
    dry_run: bool = False,
    session: Optional[requests.Session] = None,
) -> pd.DataFrame:
    """
    Fetch daily ERA5 data for a coordinate.
    Resumes from cached data if available.
    Returns DataFrame with daily climate values.
    """
    if end is None:
        end = date.today()

    s = session or requests.Session()
    s.headers.setdefault("User-Agent", "FdI-comercial-maps/1.0 (climate-data)")

    cache = _cache_dir()
    key = _coord_key(lat, lon)
    parquet_path = cache / f"daily_{key}.parquet"

    # Load existing cache and determine what to fetch
    existing = pd.DataFrame()
    if parquet_path.exists():
        try:
            existing = pd.read_parquet(parquet_path)
            existing["date"] = pd.to_datetime(existing["date"]).dt.date
            if not existing.empty:
                last_cached = existing["date"].max()
                if last_cached >= end:
                    logger.info(f"Cache up to date ({len(existing)} days cached)")
                    return existing
                start = (pd.Timestamp(last_cached) + pd.Timedelta(days=1)).date()
                logger.info(f"Resuming from {start} ({len(existing)} days already cached)")
        except Exception as e:
            logger.warning(f"Cache read failed ({e}), re-fetching all")
            existing = pd.DataFrame()

    if dry_run:
        days = (end - start).days + 1
        n_chunks = days // (CHUNK_YEARS * 365) + 1
        logger.info(f"[DRY RUN] Would fetch ({lat:.4f}, {lon:.4f}): {start} → {end}")
        logger.info(f"  ~{days:,} days in {n_chunks} chunk(s) of {CHUNK_YEARS} years")
        return existing

    all_chunks = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(
            date(chunk_start.year + CHUNK_YEARS, chunk_start.month, chunk_start.day),
            end,
        )
        logger.info(f"Chunk {chunk_start} → {chunk_end}…")

        params = {
            "latitude": lat,
            "longitude": lon,
            "start_date": chunk_start.isoformat(),
            "end_date": chunk_end.isoformat(),
            "daily": ",".join(DAILY_VARS),
            "timezone": "Europe/Lisbon",
        }

        for attempt in range(5):
            try:
                r = s.get(BASE_URL, params=params, timeout=90)
                if r.status_code == 429:
                    body = {}
                    try:
                        body = r.json()
                    except Exception:
                        pass
                    reason = body.get("reason", "")
                    if "next hour" in reason.lower():
                        # Sleep until the top of the next hour
                        import math
                        now = time.time()
                        secs_to_next_hour = 3600 - (now % 3600)
                        wait = int(secs_to_next_hour) + 5
                        logger.warning(f"Hourly API limit hit. Sleeping {wait//60}m {wait%60}s until next hour…")
                        time.sleep(wait)
                    else:
                        wait = 30 * (2 ** attempt)
                        logger.warning(f"Rate limited (attempt {attempt+1}). Retry in {wait}s…")
                        time.sleep(wait)
                    continue
                r.raise_for_status()
                break
            except requests.exceptions.HTTPError:
                if attempt == 4:
                    raise
            except Exception as exc:
                if attempt == 4:
                    raise
                wait = 30 * (2 ** attempt)
                logger.warning(f"Attempt {attempt + 1} failed: {exc}. Retry in {wait}s…")
                time.sleep(wait)

        data = r.json()
        if "error" in data:
            raise ValueError(f"Open-Meteo API error: {data.get('reason', data)}")

        df = pd.DataFrame(data["daily"])
        df.rename(columns=RENAME, inplace=True)
        all_chunks.append(df)

        # Save incrementally so progress survives 429 interruptions
        partial = pd.concat(all_chunks, ignore_index=True)
        partial["lat"] = lat
        partial["lon"] = lon
        if not existing.empty:
            partial = pd.concat([existing, partial], ignore_index=True)
            partial.drop_duplicates(subset=["date"], keep="last", inplace=True)
            partial.sort_values("date", inplace=True, ignore_index=True)
        partial.to_parquet(parquet_path, index=False)
        logger.info(f"  Progress saved ({len(partial):,} days total)")

        next_date = pd.Timestamp(chunk_end) + pd.Timedelta(days=1)
        chunk_start = next_date.date()
        time.sleep(2.0)

    if not all_chunks:
        return existing

    new_data = pd.concat(all_chunks, ignore_index=True)
    new_data["lat"] = lat
    new_data["lon"] = lon

    combined = pd.concat([existing, new_data], ignore_index=True) if not existing.empty else new_data
    combined.drop_duplicates(subset=["date"], keep="last", inplace=True)
    combined.sort_values("date", inplace=True, ignore_index=True)

    combined.to_parquet(parquet_path, index=False)
    logger.info(f"Saved {len(combined):,} days → {parquet_path.name}")
    return combined


def compute_monthly_stats(df: pd.DataFrame, lat: float, lon: float) -> pd.DataFrame:
    """
    Compute monthly climate statistics from daily ERA5 data.

    Returns DataFrame indexed 1–12 (months) with:
      - mean, p10, p90, abs_max, abs_min for each numeric variable
      - circular mean wind direction per month
    """
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df["month"] = df["date"].dt.month
    df["year"] = df["date"].dt.year

    numeric_cols = [
        "t_max", "t_min", "t_mean", "t_apparent_max", "t_apparent_min",
        "precip_mm", "rain_mm", "snow_cm", "precip_hours",
        "wind_max_kmh", "wind_gusts_kmh",
        "radiation_mj_m2", "et0_mm",
    ]
    numeric_cols = [c for c in numeric_cols if c in df.columns]

    result = pd.DataFrame({"month": range(1, 13)})

    for col in numeric_cols:
        g = df.groupby("month")[col]
        result[f"{col}_mean"] = g.mean().values
        result[f"{col}_p10"]  = g.quantile(0.10).values
        result[f"{col}_p90"]  = g.quantile(0.90).values
        result[f"{col}_max"]  = g.max().values
        result[f"{col}_min"]  = g.min().values

    # Circular mean for wind direction
    if "wind_dir_deg" in df.columns:
        rad = np.radians(df["wind_dir_deg"])
        df["_sin"] = np.sin(rad)
        df["_cos"] = np.cos(rad)
        sin_m = df.groupby("month")["_sin"].mean()
        cos_m = df.groupby("month")["_cos"].mean()
        result["wind_dir_mean_deg"] = (np.degrees(np.arctan2(sin_m.values, cos_m.values)) % 360).round(1)

    result["lat"] = lat
    result["lon"] = lon
    result["data_years"] = df["year"].nunique()
    result["data_start"] = df["date"].min().date().isoformat()
    result["data_end"] = df["date"].max().date().isoformat()
    return result


def compute_extremes(df: pd.DataFrame, lat: float, lon: float) -> pd.DataFrame:
    """
    Compute all-time absolute records from daily ERA5 data.
    Returns one row per tracked variable with record values and dates.
    """
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])

    tracked = [
        ("t_max",          "Absolute maximum temperature (°C)"),
        ("t_min",          "Absolute minimum temperature (°C)"),
        ("wind_max_kmh",   "Maximum wind speed (km/h)"),
        ("wind_gusts_kmh", "Maximum wind gusts (km/h)"),
        ("precip_mm",      "Maximum daily precipitation (mm)"),
        ("rain_mm",        "Maximum daily rainfall (mm)"),
        ("et0_mm",         "Maximum daily ET0 evapotranspiration (mm)"),
    ]

    rows = []
    for col, label in tracked:
        if col not in df.columns:
            continue
        valid = df[["date", col]].dropna(subset=[col])
        if valid.empty:
            continue
        max_i = valid[col].idxmax()
        min_i = valid[col].idxmin()
        rows.append({
            "variable": label,
            "column": col,
            "record_max": round(float(valid.loc[max_i, col]), 2),
            "record_max_date": valid.loc[max_i, "date"].date().isoformat(),
            "record_min": round(float(valid.loc[min_i, col]), 2),
            "record_min_date": valid.loc[min_i, "date"].date().isoformat(),
        })

    result = pd.DataFrame(rows)
    result["lat"] = lat
    result["lon"] = lon
    return result


def fetch_and_summarise(
    lat: float,
    lon: float,
    dry_run: bool = False,
    session: Optional[requests.Session] = None,
    start: date = CLIMATE_NORMAL_START,
) -> dict:
    """
    Full pipeline for one property coordinate:
      1. Fetch (or resume) daily ERA5 data
      2. Compute monthly statistics
      3. Compute all-time extremes
      4. Save CSVs to open_meteo_cache/

    Returns dict with keys: daily, monthly, extremes (all DataFrames).
    """
    cache = _cache_dir()
    key = _coord_key(lat, lon)

    daily_df = fetch_daily(lat, lon, start=start, dry_run=dry_run, session=session)

    if daily_df.empty:
        logger.warning("No data returned — check network or dry-run flag")
        return {"daily": daily_df, "monthly": pd.DataFrame(), "extremes": pd.DataFrame()}

    monthly_df = compute_monthly_stats(daily_df, lat, lon)
    extremes_df = compute_extremes(daily_df, lat, lon)

    if not dry_run:
        monthly_df.to_csv(cache / f"monthly_{key}.csv", index=False)
        extremes_df.to_csv(cache / f"extremes_{key}.csv", index=False)
        logger.info(f"Monthly stats → monthly_{key}.csv")
        logger.info(f"Extremes      → extremes_{key}.csv")

    return {"daily": daily_df, "monthly": monthly_df, "extremes": extremes_df}


def coords_from_gpkg(gpkg_path: str, layer: str = "Limite da Propriedade") -> tuple:
    """
    Extract WGS84 centroid from a GeoPackage property boundary layer.
    Reprojects from EPSG:3763 (PT-TM06) automatically via pyproj if installed.
    """
    import sqlite3

    conn = sqlite3.connect(gpkg_path)
    cur = conn.cursor()

    cur.execute(
        "SELECT table_name FROM gpkg_geometry_columns WHERE table_name LIKE ?",
        (f"%{layer.split()[-1]}%",),
    )
    row = cur.fetchone()
    if not row:
        cur.execute("SELECT table_name FROM gpkg_geometry_columns LIMIT 1")
        row = cur.fetchone()
    if not row:
        raise ValueError(f"No geometry layers found in {gpkg_path}")
    table = row[0]

    cur.execute(
        "SELECT srs_id FROM gpkg_geometry_columns WHERE table_name=?", (table,)
    )
    srs_row = cur.fetchone()
    srs_id = srs_row[0] if srs_row else None

    cur.execute(
        "SELECT organization_coordsys_id FROM gpkg_spatial_ref_sys WHERE srs_id=?",
        (srs_id,),
    )
    epsg_row = cur.fetchone()
    epsg = epsg_row[0] if epsg_row else None

    cur.execute(
        "SELECT min_x, min_y, max_x, max_y FROM gpkg_contents WHERE table_name=?",
        (table,),
    )
    ext = cur.fetchone()
    conn.close()

    if not ext:
        raise ValueError(f"No bounding box found for layer '{table}'")

    cx = (ext[0] + ext[2]) / 2
    cy = (ext[1] + ext[3]) / 2

    if epsg == 3763:
        try:
            from pyproj import Transformer
            t = Transformer.from_crs("EPSG:3763", "EPSG:4326", always_xy=True)
            lon_r, lat_r = t.transform(cx, cy)
            logger.info(f"Reprojected EPSG:3763 → WGS84: ({lat_r:.6f}, {lon_r:.6f})")
            return round(lat_r, 6), round(lon_r, 6)
        except ImportError:
            logger.warning("pyproj not available — cannot reproject from EPSG:3763")

    return round(cy, 6), round(cx, 6)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="Fetch ERA5 historical climate data from Open-Meteo (1940–present)"
    )
    coord_group = parser.add_mutually_exclusive_group(required=True)
    coord_group.add_argument("--lat", type=float, help="Latitude in WGS84")
    coord_group.add_argument("--gpkg", help="GeoPackage path (uses property boundary centroid)")
    parser.add_argument("--lon", type=float, help="Longitude in WGS84 (required with --lat)")
    parser.add_argument("--layer", default="Limite da Propriedade", help="GPKG layer name")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be fetched, no download")
    parser.add_argument(
        "--start-year", type=int, default=1991,
        help="First year to fetch (default: 1991 = WMO 30-yr normal; use 1940 for full ERA5 record)",
    )
    args = parser.parse_args()

    if args.gpkg:
        lat, lon = coords_from_gpkg(args.gpkg, args.layer)
        logger.info(f"Property centroid: {lat:.6f}, {lon:.6f}")
    else:
        if args.lon is None:
            parser.error("--lon is required with --lat")
        lat, lon = args.lat, args.lon

    start_date = date(args.start_year, 1, 1)
    logger.info(f"Climate period: {start_date} → today")
    result = fetch_and_summarise(lat, lon, dry_run=args.dry_run, start=start_date)

    if not result["extremes"].empty:
        print("\n── Absolute climate records (ERA5 1940–present) ──────────────────────────")
        for _, row in result["extremes"].iterrows():
            print(f"  {row['variable']:<48}  MAX {row['record_max']:>7.1f}  ({row['record_max_date']})")
            print(f"  {'':48}  MIN {row['record_min']:>7.1f}  ({row['record_min_date']})")

    if not result["monthly"].empty:
        m = result["monthly"]
        MN = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
        print("\n── Monthly climate summary ────────────────────────────────────────────────")
        print(f"  {'Mth':<5} {'Tmax':>5} {'Tmin':>5} {'Tmean':>6} {'Wind':>6} {'Gust':>6} {'Dir°':>5} {'Precip':>7} {'ET0':>5} {'Rad':>6}")
        for _, row in m.iterrows():
            mn = MN[int(row["month"]) - 1]
            print(
                f"  {mn:<5}"
                f" {row.get('t_max_mean', 0):>5.1f}"
                f" {row.get('t_min_mean', 0):>5.1f}"
                f" {row.get('t_mean_mean', 0):>6.1f}"
                f" {row.get('wind_max_kmh_mean', 0):>6.1f}"
                f" {row.get('wind_gusts_kmh_mean', 0):>6.1f}"
                f" {row.get('wind_dir_mean_deg', 0):>5.0f}"
                f" {row.get('precip_mm_mean', 0):>7.1f}"
                f" {row.get('et0_mm_mean', 0):>5.1f}"
                f" {row.get('radiation_mj_m2_mean', 0):>6.1f}"
            )
        print(f"\n  ERA5 data: {m['data_start'].iloc[0]} → {m['data_end'].iloc[0]}"
              f"  ({m['data_years'].iloc[0]} years)")
