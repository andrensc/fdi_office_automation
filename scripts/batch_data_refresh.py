#!/usr/bin/env python3
"""
OFFICE-N3 — Weekly batch data refresh

Refreshes comercial_maps field data for all properties whose updated_at timestamp
is NULL or older than --min-age-days (default 30).  Runs inside the always-on
qgis-comercial-processor container via docker exec.

Properties processed by OFFICE-N1 within the past month are automatically skipped
because N1 stamps both created_at AND updated_at on successful ZIP import.

Usage:
    python3 batch_data_refresh.py --dry-run
    python3 batch_data_refresh.py --execute
    python3 batch_data_refresh.py --execute --min-age-days 14 --verbose

Cron (Saturday 06:00):
    0 6 * * 6 cd /Users/g/Sync/FdI/fdi_office_automation && python3 scripts/batch_data_refresh.py --execute >> logs/cron.log 2>&1
"""

import os
import sys
import argparse
import logging
import json
import sqlite3
import subprocess
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from modelos.helpers import logger


class BatchDataRefresh:
    """Refresh comercial_maps field data for stale properties."""

    DEFAULT_MIN_AGE_DAYS = 30
    POPULATION_SCRIPT = '/workspace/modelos/tasks/subagents/comercial_maps_population.py'
    DOCKER_GPKG = '/projects/VectorData/Limite da Propriedade.gpkg'

    def __init__(self, dry_run=False, verbose=False, min_age_days=None):
        self.dry_run = dry_run
        self.min_age_days = min_age_days if min_age_days is not None else self.DEFAULT_MIN_AGE_DAYS

        self.gpkg_path = Path(os.getenv(
            'LIMITE_PROPRIEDADE_GPKG',
            '/Users/g/Sync/FdI/SIG/Estrutura Projeto Template/VectorData/Limite da Propriedade.gpkg'
        ))
        self.gpkg_layer = 'Limite da Propriedade'
        self.docker_container = os.getenv('DOCKER_CONTAINER_COMERCIAL', 'qgis-comercial-processor')
        self.docker_timeout = int(os.getenv('DOCKER_EXEC_TIMEOUT', '7200'))
        self.log_dir = Path(os.getenv('LOG_DIR', str(Path(__file__).parent.parent / 'logs')))

        if verbose:
            logger.setLevel(logging.DEBUG)

        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._setup_file_logging()

    def _setup_file_logging(self):
        log_file = self.log_dir / f'batch_data_refresh_{datetime.now():%Y-%m-%d}.log'
        fh = logging.FileHandler(log_file)
        fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
        logger.addHandler(fh)

    def get_stale_features(self):
        """
        Query the GeoPackage for features that need a data refresh.

        Returns list of (fid, name) tuples where updated_at IS NULL or older than
        min_age_days.
        """
        if not self.gpkg_path.exists():
            logger.error(f"GeoPackage not found: {self.gpkg_path}")
            return []

        query = f"""
            SELECT fid, name
            FROM "{self.gpkg_layer}"
            WHERE updated_at IS NULL
               OR datetime(updated_at) < datetime('now', '-{self.min_age_days} days')
            ORDER BY fid
        """
        try:
            conn = sqlite3.connect(str(self.gpkg_path))
            cur = conn.cursor()
            cur.execute(query)
            rows = cur.fetchall()
            conn.close()
            return rows
        except Exception as exc:
            logger.error(f"GeoPackage query failed: {exc}")
            return []

    def refresh_feature(self, fid, name):
        """
        Run the population subagent for a single feature via docker exec.

        Returns:
            True on success (exit 0), False on failure.
        """
        cmd = [
            'docker', 'exec', self.docker_container,
            'python3', '-u',
            self.POPULATION_SCRIPT,
            '--feature-id', str(fid),
            '--property-layer', self.DOCKER_GPKG,
        ]
        logger.info(f"  Running: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.docker_timeout,
            )
        except subprocess.TimeoutExpired:
            logger.error(f"  docker exec timed out for fid={fid} '{name}'")
            return False
        except Exception as exc:
            logger.error(f"  docker exec raised exception for fid={fid} '{name}': {exc}")
            return False

        if result.stdout:
            for line in result.stdout.splitlines():
                logger.debug(f"  [container] {line}")
        if result.stderr:
            for line in result.stderr.splitlines():
                logger.debug(f"  [container:err] {line}")

        if result.returncode == 0:
            logger.info(f"  ✓ fid={fid} '{name}' refreshed successfully")
            return True
        else:
            logger.error(f"  ✗ fid={fid} '{name}' FAILED (exit {result.returncode})")
            return False

    def stamp_updated_at(self, fid, timestamp):
        """Write updated_at for a single feature."""
        try:
            conn = sqlite3.connect(str(self.gpkg_path))
            conn.execute(
                f'UPDATE "{self.gpkg_layer}" SET updated_at = ? WHERE fid = ?',
                (timestamp, fid),
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.error(f"stamp_updated_at failed for fid={fid}: {exc}")

    def run(self):
        """Main processing entry point."""
        logger.info("=" * 60)
        logger.info("OFFICE-N3: Batch data refresh starting")
        logger.info(f"Mode: {'DRY-RUN' if self.dry_run else 'EXECUTE'}")
        logger.info(f"Min age: {self.min_age_days} days")
        logger.info(f"GeoPackage: {self.gpkg_path}")
        logger.info("=" * 60)

        summary = {
            "timestamp": datetime.now().isoformat(),
            "mode": "dry-run" if self.dry_run else "execute",
            "min_age_days": self.min_age_days,
            "features_stale": 0,
            "features_refreshed": 0,
            "features_failed": 0,
            "results": [],
        }

        stale = self.get_stale_features()
        summary["features_stale"] = len(stale)

        if not stale:
            logger.info("No stale features found — nothing to refresh.")
            return summary

        logger.info(f"Found {len(stale)} stale feature(s):")
        for fid, name in stale:
            logger.info(f"  fid={fid}  '{name}'")

        if self.dry_run:
            logger.info(f"[DRY-RUN] Would refresh {len(stale)} feature(s) via docker exec")
            summary["results"] = [{"fid": fid, "name": name, "status": "would-run"} for fid, name in stale]
            return summary

        for fid, name in stale:
            logger.info(f"Refreshing fid={fid} '{name}' …")
            success = self.refresh_feature(fid, name)

            if success:
                ts = datetime.now().isoformat()
                self.stamp_updated_at(fid, ts)
                summary["features_refreshed"] += 1
                summary["results"].append({"fid": fid, "name": name, "status": "ok", "updated_at": ts})
            else:
                summary["features_failed"] += 1
                summary["results"].append({"fid": fid, "name": name, "status": "failed"})

        logger.info("=" * 60)
        logger.info(
            f"Completed. Stale: {summary['features_stale']}, "
            f"refreshed: {summary['features_refreshed']}, "
            f"failed: {summary['features_failed']}"
        )
        logger.info("=" * 60)

        return summary


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('--dry-run', action='store_true', help='Show what would run without executing')
    parser.add_argument('--execute', action='store_true', help='Actually refresh stale properties')
    parser.add_argument('--verbose', action='store_true', help='Verbose logging')
    parser.add_argument(
        '--min-age-days', type=int, default=BatchDataRefresh.DEFAULT_MIN_AGE_DAYS,
        help=f'Minimum days since last update to qualify (default: {BatchDataRefresh.DEFAULT_MIN_AGE_DAYS})'
    )
    args = parser.parse_args()

    if not (args.dry_run or args.execute):
        args.dry_run = True  # Safety default

    refresher = BatchDataRefresh(
        dry_run=args.dry_run,
        verbose=args.verbose,
        min_age_days=args.min_age_days,
    )
    summary = refresher.run()

    summary_path = refresher.log_dir / f'batch_data_refresh_{datetime.now():%Y-%m-%d}.json'
    with open(summary_path, 'w') as f:
        json.dump(summary, f, indent=2)
    logger.info(f"Summary → {summary_path}")

    return 1 if summary.get('features_failed', 0) > 0 else 0


if __name__ == '__main__':
    sys.exit(main())
