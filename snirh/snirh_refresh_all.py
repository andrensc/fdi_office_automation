"""
snirh_refresh_all.py — Orchestrator: runs all SNIRH scrapers.

Usage:
    python3 snirh_refresh_all.py [--years-back N] [--skip stations|temperature|reservoirs|drought]
"""

import argparse
import logging
import sys
import time
from pathlib import Path

# Allow running as a top-level script outside the package
if __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import snirh  # noqa: F401 — ensure package is importable
    from snirh.snirh_session import configure_logging, get_session
    from snirh.snirh_station_catalog import fetch_station_catalog
    from snirh.snirh_fetch_temperature import fetch_temperature_extremes
    from snirh.snirh_fetch_reservoirs import fetch_reservoir_fill
    from snirh.snirh_fetch_drought import fetch_drought_index
else:
    from .snirh_session import configure_logging, get_session
    from .snirh_station_catalog import fetch_station_catalog
    from .snirh_fetch_temperature import fetch_temperature_extremes
    from .snirh_fetch_reservoirs import fetch_reservoir_fill
    from .snirh_fetch_drought import fetch_drought_index

logger = logging.getLogger(__name__)

STEPS = ["stations", "temperature", "reservoirs", "drought"]


def run_all(years_back: int, skip: set[str]) -> dict[str, bool]:
    """Run all scrapers in sequence. Returns {step: success} map."""
    results: dict[str, bool] = {}

    session = get_session()
    if session is None:
        logger.error("Could not establish SNIRH session — aborting all steps")
        return {step: False for step in STEPS}

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

    # Step 2: Temperature extremes
    if "temperature" not in skip:
        logger.info("=== Step 2/4: Temperature extremes (years_back=%d) ===", years_back)
        t0 = time.monotonic()
        try:
            df = fetch_temperature_extremes(years_back=years_back, session=session)
            results["temperature"] = True  # empty is OK — inactive stations
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
    parser.add_argument(
        "--years-back",
        type=int,
        default=None,
        help="Override SNIRH_YEARS_BACK env var",
    )
    parser.add_argument(
        "--skip",
        nargs="+",
        choices=STEPS,
        default=[],
        metavar="STEP",
        help=f"Steps to skip. Choices: {STEPS}",
    )
    args = parser.parse_args()

    import os
    years_back = args.years_back or int(os.getenv("SNIRH_YEARS_BACK", "5"))
    skip = set(args.skip)

    logger.info("Starting SNIRH refresh (years_back=%d, skip=%s)", years_back, skip or "none")
    results = run_all(years_back=years_back, skip=skip)

    # Summary
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
