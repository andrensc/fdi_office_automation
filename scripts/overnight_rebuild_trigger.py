#!/usr/bin/env python3
"""
OFFICE-N2 — Overnight project creator trigger

Reads features from "Limites da Propriedade" (master property layer) where
runOvernight_project_creator = True, then runs project_creation_agent inside
the phase1 Docker container for each one.

On success:  runOvernight_project_creator is cleared to 0 for that feature
On failure:  feature is left flagged so it retries next night

Usage:
    python3 overnight_rebuild_trigger.py --dry-run
    python3 overnight_rebuild_trigger.py --execute --verbose
    python3 overnight_rebuild_trigger.py --execute --project "Artosas"

Cron:
    0 6 * * * cd /path/to/fdi_office_automation && python3 scripts/overnight_rebuild_trigger.py --execute >> logs/cron.log 2>&1
"""

import os
import sys
import sqlite3
import argparse
import logging
import json
import subprocess
from pathlib import Path
from datetime import datetime

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from modelos.helpers import logger

# GeoPackage table name (as stored in the GPKG)
LIMITE_TABLE = "Limite da Propriedade"


class OvernightRebuildTrigger:
    """
    Read the master property layer for properties flagged for overnight project creation,
    then run project_creation_agent inside the phase1 container for each.
    """

    def __init__(self, dry_run=False, verbose=False, target_project=None):
        self.dry_run = dry_run
        self.target_project = target_project

        self.gpkg_path = Path(os.getenv(
            'LIMITE_PROPRIEDADE_GPKG',
            '/Users/g/Sync/FdI/SIG/Estrutura Projeto Template/VectorData/Limite da Propriedade.gpkg'
        ))
        self.docker_container = os.getenv('DOCKER_CONTAINER_PHASE1', 'qgis-py-phase1')
        self.docker_timeout = int(os.getenv('DOCKER_EXEC_TIMEOUT', '14400'))  # 4 hours per project
        self.log_dir = Path(os.getenv('LOG_DIR', str(Path(__file__).parent.parent / 'logs')))

        if verbose:
            logger.setLevel(logging.DEBUG)

        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._setup_file_logging()

    def _setup_file_logging(self):
        log_file = self.log_dir / f'overnight_rebuild_{datetime.now():%Y-%m-%d}.log'
        fh = logging.FileHandler(log_file)
        fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
        logger.addHandler(fh)

    def get_pending_projects(self):
        """
        Query the master GeoPackage for features with runOvernight_project_creator = 1.

        Returns:
            list of str: property names (the 'name' field)
        """
        if not self.gpkg_path.exists():
            logger.error(f"GeoPackage not found: {self.gpkg_path}")
            return []

        conn = sqlite3.connect(str(self.gpkg_path))
        try:
            rows = conn.execute(
                f'SELECT name FROM "{LIMITE_TABLE}" WHERE runOvernight_project_creator = 1'
            ).fetchall()
        finally:
            conn.close()

        names = [r[0] for r in rows if r[0] and r[0].strip()]
        logger.info(f"Found {len(names)} project(s) flagged for overnight creation:")
        for n in names:
            logger.info(f"  {n}")
        return names

    def create_project(self, project_name):
        """
        Run project_creation_agent inside the phase1 Docker container.

        Args:
            project_name: value from the 'name' field (e.g. 'Artosas')

        Returns:
            (success: bool, stdout: str, stderr: str)
        """
        cmd = [
            'docker', 'exec', self.docker_container,
            'python3', '-u',
            '/workspace/modelos/phase1/tasks/new_project/project_creation_agent.py',
            '--project-name', project_name,
        ]
        logger.info(f"Running: {' '.join(cmd)}")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=self.docker_timeout,
        )
        return result.returncode == 0, result.stdout, result.stderr

    def clear_flag(self, project_name):
        """Set runOvernight_project_creator = 0 for the given property name."""
        conn = sqlite3.connect(str(self.gpkg_path))
        try:
            conn.execute(
                f'UPDATE "{LIMITE_TABLE}" SET runOvernight_project_creator = 0 WHERE name = ?',
                (project_name,)
            )
            conn.commit()
            logger.info(f"Cleared runOvernight_project_creator flag for: {project_name}")
        finally:
            conn.close()

    def run(self):
        """Main loop — process each pending project."""
        logger.info("=" * 60)
        logger.info("OFFICE-N2: Overnight project creator trigger starting")
        logger.info(f"Mode: {'DRY-RUN' if self.dry_run else 'EXECUTE'}")
        logger.info(f"GeoPackage: {self.gpkg_path}")
        if self.target_project:
            logger.info(f"Forced target: {self.target_project}")
        logger.info("=" * 60)

        summary = {
            "timestamp": datetime.now().isoformat(),
            "mode": "dry-run" if self.dry_run else "execute",
            "projects_discovered": 0,
            "projects_succeeded": 0,
            "projects_failed": 0,
            "details": [],
        }

        if self.target_project:
            pending = [self.target_project]
        else:
            pending = self.get_pending_projects()

        summary["projects_discovered"] = len(pending)

        if not pending:
            logger.info("No projects flagged for overnight creation — nothing to do.")
            return summary

        for project_name in pending:
            result_entry = {"project": project_name, "success": False, "error": None}

            if self.dry_run:
                logger.info(f"[DRY-RUN] Would create project: {project_name}")
                result_entry["success"] = True
                summary["projects_succeeded"] += 1
                summary["details"].append(result_entry)
                continue

            logger.info(f"--- Creating project: {project_name} ---")
            try:
                success, stdout, stderr = self.create_project(project_name)
            except subprocess.TimeoutExpired:
                logger.error(f"Timed out creating project '{project_name}' after {self.docker_timeout}s")
                result_entry["error"] = "timeout"
                summary["projects_failed"] += 1
                summary["details"].append(result_entry)
                continue
            except Exception as exc:
                logger.error(f"Exception creating project '{project_name}': {exc}")
                result_entry["error"] = str(exc)
                summary["projects_failed"] += 1
                summary["details"].append(result_entry)
                continue

            # Log container output
            if stdout:
                for line in stdout.splitlines():
                    logger.info(f"  [container] {line}")
            if stderr:
                for line in stderr.splitlines():
                    logger.warning(f"  [container:err] {line}")

            if success:
                logger.info(f"✓ Project created successfully: {project_name}")
                self.clear_flag(project_name)
                result_entry["success"] = True
                summary["projects_succeeded"] += 1
            else:
                logger.error(f"✗ Project creation FAILED: {project_name} (flag left set for retry)")
                result_entry["error"] = "docker exec returned non-zero"
                summary["projects_failed"] += 1

            summary["details"].append(result_entry)

        logger.info("=" * 60)
        logger.info(
            f"Completed. Succeeded: {summary['projects_succeeded']}, "
            f"Failed: {summary['projects_failed']}"
        )
        logger.info("=" * 60)
        return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--dry-run', action='store_true', help='Show what would run without executing')
    parser.add_argument('--execute', action='store_true', help='Actually create projects')
    parser.add_argument('--project', help='Force a specific project name (ignores the flag filter)')
    parser.add_argument('--verbose', action='store_true', help='Verbose logging')
    args = parser.parse_args()

    if not (args.dry_run or args.execute):
        args.dry_run = True  # Safety default

    trigger = OvernightRebuildTrigger(
        dry_run=args.dry_run,
        verbose=args.verbose,
        target_project=args.project,
    )
    summary = trigger.run()

    summary_path = trigger.log_dir / f'overnight_rebuild_{datetime.now():%Y-%m-%d}.json'
    with open(summary_path, 'w') as f:
        json.dump(summary, f, indent=2)
    logger.info(f"Summary → {summary_path}")

    return 1 if summary['projects_failed'] > 0 else 0


if __name__ == '__main__':
    sys.exit(main())
