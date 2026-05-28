#!/usr/bin/env python3
"""
OFFICE-N1 — Overnight predios processor

Scans SIG/Estrutura Projeto Template/_Predios/ for ZIP files and processes them
through the comercial_maps pipeline via docker exec into the running container.

On success:  ZIPs are moved to predios_archive/
On failure:  ZIPs are moved to predios_archive/failed/YYYY-MM-DD/

Usage:
    python3 overnight_predios_processor.py --dry-run
    python3 overnight_predios_processor.py --execute --verbose

Cron:
    0 22 * * * cd /path/to/fdi_office_automation && python3 scripts/overnight_predios_processor.py --execute >> logs/cron.log 2>&1
"""

import os
import sys
import shutil
import argparse
import logging
import json
import sqlite3
import subprocess
import time
from pathlib import Path
from datetime import datetime

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from modelos.helpers import logger


class OvernightPrediosProcessor:
    """Process accumulated property ZIP files overnight via comercial_maps pipeline."""

    def __init__(self, dry_run=False, verbose=False):
        self.dry_run = dry_run

        self.predios_folder = Path(os.getenv(
            'PREDIOS_FOLDER',
            '/Users/g/Sync/FdI/SIG/Estrutura Projeto Template/_Predios'
        ))
        self.archive_folder = Path(os.getenv(
            'PREDIOS_ARCHIVE_FOLDER',
            '/Users/g/Sync/FdI/SIG/Estrutura Projeto Template/predios_archive'
        ))
        self.min_zip_age = int(os.getenv('MIN_ZIP_AGE', '0'))  # seconds; 0 = no age filter
        self.docker_container = os.getenv('DOCKER_CONTAINER_COMERCIAL', 'qgis-comercial-processor')
        self.docker_zip_dir = os.getenv('DOCKER_PREDIOS_DIR', '/projects/_Predios')
        self.docker_timeout = int(os.getenv('DOCKER_EXEC_TIMEOUT', '7200'))  # 2 hours
        self.log_dir = Path(os.getenv('LOG_DIR', str(Path(__file__).parent.parent / 'logs')))
        self.gpkg_path = Path(os.getenv(
            'LIMITE_PROPRIEDADE_GPKG',
            '/Users/g/Sync/FdI/SIG/Estrutura Projeto Template/VectorData/Limite da Propriedade.gpkg'
        ))
        self.gpkg_layer = 'Limite da Propriedade'

        if verbose:
            logger.setLevel(logging.DEBUG)

        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._setup_file_logging()

    def _setup_file_logging(self):
        log_file = self.log_dir / f'overnight_predios_{datetime.now():%Y-%m-%d}.log'
        fh = logging.FileHandler(log_file)
        fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
        logger.addHandler(fh)

    def _get_today_processed_names(self):
        """
        Return a set of normalised feature names (strip+lower) whose created_at
        was stamped today.  Used to skip ZIPs already processed manually during
        the day, preventing duplicate-geometry creation inside the pipeline.
        """
        today = datetime.now().strftime('%Y-%m-%d')
        processed = set()

        if not self.gpkg_path.exists():
            return processed

        try:
            conn = sqlite3.connect(str(self.gpkg_path))
            cur = conn.cursor()
            cur.execute(
                f'SELECT name FROM "{self.gpkg_layer}" WHERE created_at LIKE ?',
                (f'{today}%',),
            )
            for (name,) in cur.fetchall():
                if name:
                    processed.add(name.strip().lower())
            conn.close()
        except Exception as exc:
            logger.warning(f"Could not query GeoPackage for today's processed names: {exc}")

        return processed

    def discover_zip_files(self):
        """
        Return ZIP files in predios_folder older than min_zip_age, sorted oldest-first.

        ZIPs whose GeoPackage feature already has created_at stamped today are skipped
        — they were processed manually during the day and re-submitting them would risk
        creating a duplicate project with a different geometry inside the pipeline.
        """
        if not self.predios_folder.exists():
            logger.error(f"_Predios folder not found: {self.predios_folder}")
            return []

        today_processed = self._get_today_processed_names()
        if today_processed:
            logger.info(f"Features already processed today (will skip matching ZIPs): {today_processed}")

        now = time.time()
        zips = []
        skipped = []
        for z in self.predios_folder.glob('*.zip'):
            age = now - z.stat().st_mtime
            if self.min_zip_age > 0 and age < self.min_zip_age:
                continue

            if z.stem.strip().lower() in today_processed:
                logger.info(f"  SKIPPED (already processed today): {z.name}")
                skipped.append(z)
                continue

            zips.append(z)

        zips.sort(key=lambda z: z.stat().st_mtime)
        logger.info(f"Discovered {len(zips)} ZIP(s) to process, {len(skipped)} skipped")
        for z in zips:
            logger.info(f"  {z.name}")
        return zips

    def run_comercial_maps(self):
        """
        Call docker exec into the running comercial_maps container.
        The agent scans --zip-dir and processes all ZIPs found there.

        Returns:
            (returncode, stdout, stderr)
        """
        cmd = [
            'docker', 'exec', self.docker_container,
            'python3', '-u',
            '/workspace/modelos/tasks/agents/comercial_maps_agent.py',
            '--zip-dir', self.docker_zip_dir,
        ]
        logger.info(f"Running: {' '.join(cmd)}")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=self.docker_timeout,
        )
        return result.returncode, result.stdout, result.stderr

    def archive_zips(self, zip_files, success):
        """Move ZIPs to archive (success) or failed subfolder (failure)."""
        if success:
            dest = self.archive_folder
        else:
            dest = self.archive_folder / 'failed' / datetime.now().strftime('%Y-%m-%d')

        dest.mkdir(parents=True, exist_ok=True)

        for z in zip_files:
            target = dest / z.name
            # Avoid overwriting existing file in archive
            if target.exists():
                stem = z.stem
                suffix = z.suffix
                target = dest / f"{stem}_{datetime.now():%H%M%S}{suffix}"
            shutil.move(str(z), target)
            logger.info(f"{'Archived' if success else 'Failed-archived'}: {z.name} → {target}")

    def stamp_timestamps(self, zip_files):
        """
        Stamp created_at and updated_at on GeoPackage features matching processed ZIPs.

        Matching is done by normalising both the ZIP stem and the 'name' field
        (strip + lower-case).  If no feature is found a warning is logged but
        processing continues — the ZIP has already been archived successfully.
        """
        now = datetime.now().isoformat()

        if not self.gpkg_path.exists():
            logger.warning(f"GeoPackage not found — skipping timestamp stamping: {self.gpkg_path}")
            return

        try:
            conn = sqlite3.connect(str(self.gpkg_path))
            cur = conn.cursor()

            cur.execute(f'SELECT fid, name FROM "{self.gpkg_layer}"')
            name_to_fid = {(r[1] or '').strip().lower(): r[0] for r in cur.fetchall()}

            for z in zip_files:
                key = z.stem.strip().lower()
                fid = name_to_fid.get(key)
                if fid is None:
                    logger.warning(f"No GeoPackage feature matched '{z.stem}' — timestamp not stamped")
                    continue

                cur.execute(
                    f'UPDATE "{self.gpkg_layer}" SET created_at = ?, updated_at = ? WHERE fid = ?',
                    (now, now, fid),
                )
                logger.info(f"Timestamps stamped for '{z.stem}' (fid={fid}): {now}")

            conn.commit()
            conn.close()
        except Exception as exc:
            logger.error(f"stamp_timestamps failed: {exc}")

    def run(self):
        """Main processing entry point."""
        logger.info("=" * 60)
        logger.info("OFFICE-N1: Overnight predios processor starting")
        logger.info(f"Mode: {'DRY-RUN' if self.dry_run else 'EXECUTE'}")
        logger.info(f"_Predios folder: {self.predios_folder}")
        logger.info(f"Archive folder:  {self.archive_folder}")
        logger.info("=" * 60)

        summary = {
            "timestamp": datetime.now().isoformat(),
            "mode": "dry-run" if self.dry_run else "execute",
            "zips_discovered": 0,
            "pipeline_success": None,
            "zips_archived": 0,
            "zips_failed": 0,
            "docker_returncode": None,
        }

        zip_files = self.discover_zip_files()
        summary["zips_discovered"] = len(zip_files)

        if not zip_files:
            logger.info("No ZIP files found — nothing to process.")
            return summary

        if self.dry_run:
            logger.info(f"[DRY-RUN] Would process {len(zip_files)} ZIP(s) via docker exec {self.docker_container}")
            for z in zip_files:
                logger.info(f"  [DRY-RUN] {z.name}")
            summary["pipeline_success"] = True
            return summary

        try:
            returncode, stdout, stderr = self.run_comercial_maps()
        except subprocess.TimeoutExpired:
            logger.error(f"docker exec timed out after {self.docker_timeout}s")
            summary["pipeline_success"] = False
            summary["zips_failed"] = len(zip_files)
            self.archive_zips(zip_files, success=False)
            return summary
        except Exception as exc:
            logger.error(f"docker exec raised exception: {exc}")
            summary["pipeline_success"] = False
            summary["zips_failed"] = len(zip_files)
            self.archive_zips(zip_files, success=False)
            return summary

        summary["docker_returncode"] = returncode

        # Log all container output
        if stdout:
            for line in stdout.splitlines():
                logger.info(f"[container] {line}")
        if stderr:
            for line in stderr.splitlines():
                logger.warning(f"[container:err] {line}")

        if returncode == 0:
            logger.info(f"comercial_maps pipeline succeeded (exit 0)")
            summary["pipeline_success"] = True
            summary["zips_archived"] = len(zip_files)
            self.stamp_timestamps(zip_files)
            self.archive_zips(zip_files, success=True)
        else:
            logger.error(f"comercial_maps pipeline FAILED (exit {returncode})")
            summary["pipeline_success"] = False
            summary["zips_failed"] = len(zip_files)
            self.archive_zips(zip_files, success=False)

        logger.info("=" * 60)
        logger.info(f"Completed. ZIPs discovered: {summary['zips_discovered']}, "
                    f"archived: {summary['zips_archived']}, failed: {summary['zips_failed']}")
        logger.info("=" * 60)

        return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--dry-run', action='store_true', help='Show what would run without executing')
    parser.add_argument('--execute', action='store_true', help='Actually process ZIPs')
    parser.add_argument('--verbose', action='store_true', help='Verbose logging')
    args = parser.parse_args()

    if not (args.dry_run or args.execute):
        args.dry_run = True  # Safety default

    processor = OvernightPrediosProcessor(dry_run=args.dry_run, verbose=args.verbose)
    summary = processor.run()

    summary_path = processor.log_dir / f'overnight_predios_{datetime.now():%Y-%m-%d}.json'
    with open(summary_path, 'w') as f:
        json.dump(summary, f, indent=2)
    logger.info(f"Summary → {summary_path}")

    failed = summary.get('zips_failed', 0)
    return 1 if failed > 0 else 0


if __name__ == '__main__':
    sys.exit(main())
