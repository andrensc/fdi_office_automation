"""
snirh_refresh_all.py — Orchestrator: runs all SNIRH scrapers.

Usage:
    python3 snirh/snirh_refresh_all.py
    python3 snirh/snirh_refresh_all.py --dry-run
    python3 snirh/snirh_refresh_all.py --watch
    python3 snirh/snirh_refresh_all.py --near-gpkg /path/to/property.gpkg --radius-km 50
    python3 snirh/snirh_refresh_all.py --bbox -9.2 38.6 -8.9 38.9
    python3 snirh/snirh_refresh_all.py --skip temperature --years-back 2
"""

import argparse
import logging
import math
import os
import sqlite3
import sys
import time
from pathlib import Path

if __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import snirh  # noqa: F401
    from snirh.snirh_session import configure_logging, get_session, get_cache_dir
    from snirh.snirh_station_catalog import fetch_station_catalog
    from snirh.snirh_fetch_temperature import fetch_temperature
    from snirh.snirh_fetch_reservoirs import fetch_reservoir_fill
    from snirh.snirh_fetch_drought import fetch_drought_index
else:
    from .snirh_session import configure_logging, get_session, get_cache_dir
    from .snirh_station_catalog import fetch_station_catalog
    from .snirh_fetch_temperature import fetch_temperature
    from .snirh_fetch_reservoirs import fetch_reservoir_fill
    from .snirh_fetch_drought import fetch_drought_index

import pandas as pd

logger = logging.getLogger(__name__)

STEPS = ["stations", "temperature", "reservoirs", "drought"]


# ---------------------------------------------------------------------------
# Spatial helpers
# ---------------------------------------------------------------------------

def _bbox_from_gpkg(gpkg_path: str, radius_km: float) -> tuple[float, float, float, float]:
    """
    Read the bounding box of the first geometry layer in a GeoPackage using
    sqlite3 (no GDAL required). Expands by radius_km on all sides.
    Returns (minx, miny, maxx, maxy) in WGS84 degrees.
    """
    conn = sqlite3.connect(gpkg_path)
    cur = conn.cursor()

    # Get first geometry column info
    cur.execute("SELECT table_name, column_name FROM gpkg_geometry_columns LIMIT 1")
    row = cur.fetchone()
    if not row:
        raise ValueError(f"No geometry columns found in {gpkg_path}")
    table, geom_col = row

    # Try to get SRS to decide if we need to reproject
    cur.execute(
        "SELECT organization_coordsys_id FROM gpkg_spatial_ref_sys "
        "WHERE srs_id = (SELECT srs_id FROM gpkg_geometry_columns WHERE table_name=?)",
        (table,),
    )
    srs_row = cur.fetchone()
    epsg = srs_row[0] if srs_row else None

    # Use gpkg_contents for quick extent first
    cur.execute(
        "SELECT min_x, min_y, max_x, max_y FROM gpkg_contents WHERE table_name=?",
        (table,),
    )
    ext = cur.fetchone()
    conn.close()

    if not ext or None in ext:
        raise ValueError(f"Cannot read extent from {gpkg_path}:{table}")

    minx, miny, maxx, maxy = ext

    # If EPSG is metric (e.g. 3763 ETRS89-PT-TM06) convert to degrees
    if epsg and epsg not in (4326, 4258, 4269):
        logger.info("Detected CRS EPSG:%s — reprojecting bbox to WGS84 for station filter", epsg)
        try:
            from pyproj import Transformer
            t = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
            minx, miny = t.transform(minx, miny)
            maxx, maxy = t.transform(maxx, maxy)
        except ImportError:
            logger.warning("pyproj not installed — assuming bbox is already in degrees")

    # Expand by radius_km
    lat_mid = (miny + maxy) / 2
    deg_lat = radius_km / 111.0
    deg_lon = radius_km / (111.0 * math.cos(math.radians(lat_mid)))

    return (minx - deg_lon, miny - deg_lat, maxx + deg_lon, maxy + deg_lat)


def _filter_stations_by_bbox(
    catalog_csv: Path, bbox: tuple[float, float, float, float]
) -> tuple[list[str], dict[str, str]]:
    """
    Return (station_codes, station_names) for stations within bbox.
    bbox = (minx, miny, maxx, maxy) in WGS84 degrees.
    """
    if not catalog_csv.exists():
        logger.warning("stations_catalog.csv not found — cannot filter by bbox")
        return [], {}

    df = pd.read_csv(catalog_csv, dtype=str)
    minx, miny, maxx, maxy = bbox

    def in_bbox(row):
        try:
            lon = float(row.get("lon", "nan"))
            lat = float(row.get("lat", "nan"))
            return minx <= lon <= maxx and miny <= lat <= maxy
        except (ValueError, TypeError):
            return False

    mask = df.apply(in_bbox, axis=1)
    filtered = df[mask]
    codes = filtered["station_code"].dropna().unique().tolist()
    names = dict(zip(filtered["station_code"], filtered.get("station_name", pd.Series(dtype=str))))
    logger.info(
        "Spatial filter: %d/%d stations within bbox (%.4f,%.4f)-(%.4f,%.4f)",
        len(codes), len(df), minx, miny, maxx, maxy,
    )
    return codes, names


# ---------------------------------------------------------------------------
# Dry-run helpers
# ---------------------------------------------------------------------------

def _dry_run_report(
    years_back: int,
    skip: set[str],
    bbox: tuple | None,
    gpkg_path: str | None,
    radius_km: float,
) -> None:
    """Print what would be fetched without making any HTTP requests or writes."""
    from datetime import date
    today = date.today()
    start_year = today.year - years_back

    print("\n=== SNIRH Dry Run — what would be fetched ===\n")
    print(f"  Years back  : {years_back}  ({start_year}–{today.year})")
    print(f"  Skip steps  : {', '.join(skip) if skip else 'none'}")

    if gpkg_path:
        try:
            resolved_bbox = _bbox_from_gpkg(gpkg_path, radius_km)
            print(f"  Near GPKG   : {gpkg_path}")
            print(f"  Radius      : {radius_km} km")
            print(f"  Bbox (WGS84): {resolved_bbox[0]:.4f},{resolved_bbox[1]:.4f} → {resolved_bbox[2]:.4f},{resolved_bbox[3]:.4f}")
        except Exception as exc:
            print(f"  Near GPKG   : {gpkg_path}  ⚠ could not read bbox: {exc}")
    elif bbox:
        print(f"  Bbox (WGS84): {bbox[0]:.4f},{bbox[1]:.4f} → {bbox[2]:.4f},{bbox[3]:.4f}")

    cache_dir = get_cache_dir()
    catalog_path = cache_dir / "stations_catalog.csv"

    print("\n  Steps:")
    for step in STEPS:
        if step in skip:
            print(f"    — {step:12s} SKIPPED")
        elif step == "temperature":
            if catalog_path.exists():
                cat = pd.read_csv(catalog_path, dtype=str)
                n_total = len(cat["station_code"].dropna().unique())
                if bbox or gpkg_path:
                    resolved_bbox = bbox
                    if gpkg_path:
                        try:
                            resolved_bbox = _bbox_from_gpkg(gpkg_path, radius_km)
                        except Exception:
                            pass
                    if resolved_bbox:
                        codes, _ = _filter_stations_by_bbox(catalog_path, resolved_bbox)
                        n_filtered = len(codes)
                        months = years_back * 12
                        requests_est = n_filtered * months * 2  # TX + TN
                        print(f"    ✓ {step:12s} {n_filtered}/{n_total} stations × {months} months × 2 params ≈ {requests_est:,} requests")
                    else:
                        months = years_back * 12
                        print(f"    ✓ {step:12s} {n_total} stations × {months} months × 2 params ≈ {n_total * months * 2:,} requests")
                else:
                    months = years_back * 12
                    print(f"    ✓ {step:12s} {n_total} stations × {months} months × 2 params ≈ {n_total * months * 2:,} requests  ⚠ consider --near-gpkg to reduce")
            else:
                print(f"    ✓ {step:12s} (station catalog not yet cached — run stations step first)")
        elif step == "stations":
            print(f"    ✓ {step:12s} fetch national catalog → stations_catalog.csv")
        elif step == "reservoirs":
            print(f"    ✓ {step:12s} fetch national albufeiras bulletin → albufeiras_fill.csv  (no spatial filter needed)")
        elif step == "drought":
            print(f"    ✓ {step:12s} fetch national drought bulletin → drought_index.csv  (no spatial filter needed)")

    print(f"\n  Output dir  : {cache_dir}")
    print("\n  → Re-run without --dry-run to execute.\n")


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run_all(
    years_back: int,
    skip: set[str],
    bbox: tuple | None,
    dry_run: bool,
) -> dict[str, bool]:
    """Run all scrapers in sequence. Returns {step: success} map."""
    results: dict[str, bool] = {}

    session = get_session()
    if session is None:
        logger.error("Could not establish SNIRH session — aborting all steps")
        return {step: False for step in STEPS}

    cache_dir = get_cache_dir()

    # Step 1: Station catalog
    if "stations" not in skip:
        logger.info("=== Step 1/4: Station catalog ===")
        t0 = time.monotonic()
        try:
            df = fetch_station_catalog(session)
            results["stations"] = not df.empty
            logger.info("Stations done in %.1fs — %d rows", time.monotonic() - t0, len(df))
        except Exception as exc:
            logger.error("Station catalog failed: %s", exc, exc_info=True)
            results["stations"] = False
    else:
        logger.info("Skipping stations")
        results["stations"] = True

    # Step 2: Temperature extremes (with optional spatial filter)
    if "temperature" not in skip:
        logger.info("=== Step 2/4: Temperature extremes (years_back=%d) ===", years_back)
        t0 = time.monotonic()
        try:
            station_codes = None
            station_names = None
            if bbox:
                catalog_path = cache_dir / "stations_catalog.csv"
                station_codes, station_names = _filter_stations_by_bbox(catalog_path, bbox)
                if not station_codes:
                    logger.warning("No stations in bbox — skipping temperature fetch")
                    results["temperature"] = True
                else:
                    df = fetch_temperature(
                        station_codes=station_codes,
                        station_names=station_names,
                        years_back=years_back,
                        session=session,
                    )
                    results["temperature"] = True
                    logger.info("Temperature done in %.1fs — %d rows", time.monotonic() - t0, len(df))
            else:
                df = fetch_temperature(years_back=years_back, session=session)
                results["temperature"] = True
                logger.info("Temperature done in %.1fs — %d rows", time.monotonic() - t0, len(df))
        except Exception as exc:
            logger.error("Temperature fetch failed: %s", exc, exc_info=True)
            results["temperature"] = False
    else:
        logger.info("Skipping temperature")
        results["temperature"] = True

    # Step 3: Reservoir fill
    if "reservoirs" not in skip:
        logger.info("=== Step 3/4: Reservoir fill (albufeiras) ===")
        t0 = time.monotonic()
        try:
            df = fetch_reservoir_fill(session)
            results["reservoirs"] = True
            logger.info("Reservoirs done in %.1fs — %d rows", time.monotonic() - t0, len(df))
        except Exception as exc:
            logger.error("Reservoir fetch failed: %s", exc, exc_info=True)
            results["reservoirs"] = False
    else:
        logger.info("Skipping reservoirs")
        results["reservoirs"] = True

    # Step 4: Drought index
    if "drought" not in skip:
        logger.info("=== Step 4/4: Drought index ===")
        t0 = time.monotonic()
        try:
            df = fetch_drought_index(session)
            results["drought"] = True
            logger.info("Drought done in %.1fs — %d rows", time.monotonic() - t0, len(df))
        except Exception as exc:
            logger.error("Drought fetch failed: %s", exc, exc_info=True)
            results["drought"] = False
    else:
        logger.info("Skipping drought")
        results["drought"] = True

    return results


def main() -> int:
    configure_logging()

    parser = argparse.ArgumentParser(
        description="Refresh all SNIRH data caches",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--years-back", type=int, default=None,
                        help="Override SNIRH_YEARS_BACK env var")
    parser.add_argument("--skip", nargs="+", choices=STEPS, default=[], metavar="STEP",
                        help=f"Steps to skip. Choices: {STEPS}")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be fetched without making HTTP requests or writing files")
    parser.add_argument("--watch", action="store_true",
                        help="Poll SNIRH until available, then run the refresh")
    parser.add_argument("--interval", type=int, default=60,
                        help="Seconds between poll attempts when --watch is active")
    parser.add_argument("--near-gpkg", metavar="GPKG_PATH",
                        help="Path to a property GeoPackage — filter temperature stations by proximity")
    parser.add_argument("--radius-km", type=float, default=50.0,
                        help="Radius in km around property for station spatial filter (used with --near-gpkg)")
    parser.add_argument("--bbox", nargs=4, type=float, metavar=("MINX", "MINY", "MAXX", "MAXY"),
                        help="WGS84 bounding box to filter temperature stations (minx miny maxx maxy)")
    args = parser.parse_args()

    years_back = args.years_back or int(os.getenv("SNIRH_YEARS_BACK", "5"))
    skip = set(args.skip)

    # Resolve bbox
    bbox = None
    if args.near_gpkg:
        gpkg_path = args.near_gpkg
        try:
            bbox = _bbox_from_gpkg(gpkg_path, args.radius_km)
            logger.info("Using bbox from %s + %.0f km: %s", gpkg_path, args.radius_km, bbox)
        except Exception as exc:
            logger.error("Could not read bbox from GPKG: %s", exc)
            return 1
    elif args.bbox:
        bbox = tuple(args.bbox)
        logger.info("Using explicit bbox: %s", bbox)

    # Dry run — no HTTP, no writes
    if args.dry_run:
        _dry_run_report(
            years_back=years_back,
            skip=skip,
            bbox=bbox,
            gpkg_path=args.near_gpkg,
            radius_km=args.radius_km,
        )
        return 0

    # Watch mode — poll until available
    if args.watch:
        attempt = 0
        from snirh.snirh_check import check_once
        while True:
            attempt += 1
            ts = time.strftime("%H:%M:%S")
            print(f"\n[Attempt {attempt} @ {ts}] Checking SNIRH connectivity...")
            if check_once(verbose=False):
                print("✅ SNIRH is available — starting refresh...\n")
                break
            else:
                next_ts = time.strftime("%H:%M:%S", time.localtime(time.time() + args.interval))
                print(f"  ✗ Not available. Next attempt at {next_ts} (Ctrl+C to stop)")
                try:
                    time.sleep(args.interval)
                except KeyboardInterrupt:
                    print("\nStopped by user.")
                    return 1

    logger.info(
        "Starting SNIRH refresh (years_back=%d, skip=%s, bbox=%s)",
        years_back, skip or "none", bbox or "national",
    )
    results = run_all(years_back=years_back, skip=skip, bbox=bbox, dry_run=False)

    print("\n=== SNIRH Refresh Summary ===")
    all_ok = True
    for step in STEPS:
        status = "✓" if results.get(step, False) else "✗"
        print(f"  {status} {step}")
        if not results.get(step, False) and step not in skip:
            all_ok = False

    if all_ok:
        logger.info("All SNIRH scrapers completed successfully")
        return 0
    else:
        logger.warning("Some SNIRH scrapers had failures — check logs")
        return 1


if __name__ == "__main__":
    sys.exit(main())
