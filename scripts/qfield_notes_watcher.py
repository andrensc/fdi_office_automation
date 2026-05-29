#!/usr/bin/env python3
"""
QField Notes Watcher — qfield_notes_watcher.py

Monitors /Users/g/QField/cloud/*/Notas.gpkg for changes and syncs notes into:
  - SIG_[project]/inputs_project/project_vector_data/Notas.gpkg
  - /Users/g/Sync/FdI/SIG/outputs_admin/notas_all_projects.gpkg  (central DB)

Also supports manual pull from QFieldCloud API to get changes from other field users
(requires QFIELDCLOUD_TOKEN env var or QFIELDCLOUD_USER + QFIELDCLOUD_PASS).

Usage:
    # Single sync of all projects now
    python3 qfield_notes_watcher.py --sync-once

    # Watch for local file changes (triggered by QField Desktop sync)
    python3 qfield_notes_watcher.py --watch

    # Pull latest from cloud API for all projects, then sync
    python3 qfield_notes_watcher.py --pull-and-sync

    # Pull + watch (recommended for production: pull on start, then watch)
    python3 qfield_notes_watcher.py --pull-and-watch

    # Sync a single project
    python3 qfield_notes_watcher.py --sync-once --project Florestas_de_Iroko__Rectificacao_Artosas

Environment variables (for API pull — store in .env, never in code):
    QFIELDCLOUD_TOKEN   Authentication token (preferred over user/pass)
    QFIELDCLOUD_USER    Username (fallback if no token)
    QFIELDCLOUD_PASS    Password (fallback if no token)
    QFIELDCLOUD_URL     API URL (default: https://app.qfield.cloud/api/v1/)
    QFIELDCLOUD_PROJECTS  JSON mapping of cloud_folder_name → project_uuid
                          e.g. '{"Florestas_de_Iroko__Rectificacao_Artosas": "uuid-here"}'
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
except ImportError:
    print("Error: watchdog library required. Install with: pip install watchdog")
    sys.exit(1)

# Load .env from repo root if present
_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# Add parent directory for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from modelos.qfield.notes_syncer import NotesSyncer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("qfield_notes_watcher")

CLOUD_BASE = Path("/Users/g/QField/cloud")


# ---------------------------------------------------------------------------
# Watchdog event handler
# ---------------------------------------------------------------------------

class NotasChangeHandler(FileSystemEventHandler):
    def __init__(self, syncer: NotesSyncer, debounce_seconds: float = 5.0):
        self.syncer = syncer
        self.debounce = debounce_seconds
        self._pending: dict[str, float] = {}  # cloud_folder → last event time

    def on_modified(self, event):
        self._handle(event)

    def on_created(self, event):
        self._handle(event)

    def _handle(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)

        # Only care about Notas.gpkg; ignore .qfieldsync working copies
        if path.name != "Notas.gpkg":
            return
        if ".qfieldsync" in path.parts:
            return

        # The cloud folder is the directory directly under CLOUD_BASE
        try:
            cloud_folder = path.relative_to(CLOUD_BASE).parts[0]
        except ValueError:
            return

        now = time.time()
        last = self._pending.get(cloud_folder, 0)
        if now - last < self.debounce:
            return  # debounce — avoid double-firing on WAL writes
        self._pending[cloud_folder] = now

        logger.info(f"Detected change: {path}")
        try:
            result = self.syncer.sync_project(cloud_folder)
            _log_result(result)
        except Exception as e:
            logger.error(f"Sync failed for {cloud_folder}: {e}", exc_info=True)


def _log_result(result: dict):
    project = result["project"]
    for geom, counts in result.get("tables", {}).items():
        c = counts.get("central", {})
        p = counts.get("project", {})
        logger.info(
            f"  [{project}] {geom}: "
            f"central +{c.get('inserted',0)} ~{c.get('updated',0)} | "
            f"project +{p.get('inserted',0)} ~{p.get('updated',0)}"
        )
    for err in result.get("errors", []):
        logger.error(f"  [{project}] ERROR: {err}")


# ---------------------------------------------------------------------------
# API pull support
# ---------------------------------------------------------------------------

def pull_all_projects(syncer: NotesSyncer):
    """Pull latest Notas.gpkg from QFieldCloud API for all configured projects."""
    projects_env = os.getenv("QFIELDCLOUD_PROJECTS", "{}")
    try:
        project_uuids = json.loads(projects_env)
    except json.JSONDecodeError:
        logger.error("QFIELDCLOUD_PROJECTS env var is not valid JSON")
        return

    if not project_uuids:
        logger.warning(
            "QFIELDCLOUD_PROJECTS not set — skipping API pull. "
            "Set it to a JSON map of {cloud_folder_name: project_uuid}"
        )
        return

    for cloud_folder, uuid in project_uuids.items():
        logger.info(f"Pulling {cloud_folder} from QFieldCloud API...")
        try:
            syncer.pull_from_cloud_api(uuid, cloud_folder)
        except Exception as e:
            logger.error(f"Pull failed for {cloud_folder}: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def cmd_sync_once(syncer: NotesSyncer, project: str = None):
    if project:
        result = syncer.sync_project(project)
        _log_result(result)
    else:
        results = syncer.sync_all()
        for r in results:
            _log_result(r)
    logger.info("Sync complete.")


def cmd_watch(syncer: NotesSyncer):
    handler = NotasChangeHandler(syncer)
    observer = Observer()
    observer.schedule(handler, str(CLOUD_BASE), recursive=True)
    observer.start()
    logger.info(f"Watching {CLOUD_BASE} for Notas.gpkg changes... (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Stopping watcher.")
        observer.stop()
    observer.join()


def cmd_pull_and_sync(syncer: NotesSyncer):
    pull_all_projects(syncer)
    syncer.sync_all()
    logger.info("Pull + sync complete.")


def cmd_pull_and_watch(syncer: NotesSyncer, pull_interval_minutes: int = 30):
    """Pull from API + sync, then watch filesystem AND re-pull periodically."""
    pull_all_projects(syncer)
    syncer.sync_all()
    logger.info(f"Initial sync done. Watching filesystem + re-pulling every {pull_interval_minutes} min...")

    handler = NotasChangeHandler(syncer)
    observer = Observer()
    observer.schedule(handler, str(CLOUD_BASE), recursive=True)
    observer.start()

    pull_interval_secs = pull_interval_minutes * 60
    last_pull = time.time()

    try:
        while True:
            time.sleep(1)
            if time.time() - last_pull >= pull_interval_secs:
                logger.info(f"[periodic] Re-pulling from QFieldCloud API...")
                try:
                    pull_all_projects(syncer)
                    syncer.sync_all()
                    logger.info("[periodic] Pull + sync complete.")
                except Exception as e:
                    logger.error(f"[periodic] Pull failed: {e}")
                last_pull = time.time()
    except KeyboardInterrupt:
        logger.info("Stopping watcher.")
        observer.stop()
    observer.join()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="QField Notes Watcher — sync Notas.gpkg to project GPKGs and central DB"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--sync-once",      action="store_true", help="Sync all projects once and exit")
    group.add_argument("--watch",          action="store_true", help="Watch for local file changes")
    group.add_argument("--pull-and-sync",  action="store_true", help="Pull from API, sync all, exit")
    group.add_argument("--pull-and-watch", action="store_true", help="Pull from API, sync, then watch")

    parser.add_argument("--project", help="Limit to one cloud folder name (with --sync-once)")
    parser.add_argument(
        "--config",
        default=None,
        help="Path to qfield_project_mapping.json (default: modelos/config/)",
    )
    parser.add_argument(
        "--pull-interval",
        type=int,
        default=30,
        metavar="MINUTES",
        help="Minutes between API re-pulls in --pull-and-watch mode (default: 30)",
    )
    parser.add_argument("--verbose", action="store_true", help="Debug logging")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    config_path = Path(args.config) if args.config else None
    syncer = NotesSyncer(config_path)

    if args.sync_once:
        cmd_sync_once(syncer, args.project)
    elif args.watch:
        cmd_watch(syncer)
    elif args.pull_and_sync:
        cmd_pull_and_sync(syncer)
    elif args.pull_and_watch:
        cmd_pull_and_watch(syncer, pull_interval_minutes=args.pull_interval)


if __name__ == "__main__":
    main()
