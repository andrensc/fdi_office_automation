#!/usr/bin/env python3
"""
OFFICE-SYNC-CONFLICTS — sync.com conflict file resolver

Scans the SIG folder for files created by sync.com when two devices modify
the same file simultaneously. sync.com renames the losing version to:

    filename-CONFLICT-N.ext   (e.g. "Projeto QGIS-CONFLICT-1.qgs")

This script:
  1. Discovers all CONFLICT files under --scan-path
  2. Analyses each pair (CONFLICT vs original):
       - For .qgs files: reads saveDateTime and saveUser from XML header
       - For all files:  compares mtime and file size
       - Small files (< 100 MB): computes MD5 to detect identical copies
  3. Classifies each conflict as:
       SAFE_DELETE  — CONFLICT is older than original → safe to delete
       REVIEW       — CONFLICT is NEWER than original → admin decision needed
       IDENTICAL    — same content, safe to delete CONFLICT
       ORPHAN       — original file no longer exists
  4. In --report mode prints a summary table (no changes)
  5. In --resolve mode deletes SAFE_DELETE / IDENTICAL conflicts;
     writes a JSON log of every action taken

Usage:
    # Dry-run report across whole SIG folder
    python3 sync_conflict_resolver.py --scan-path /Users/g/Sync/FdI/SIG --report

    # Preview what would be deleted (dry-run)
    python3 sync_conflict_resolver.py --scan-path /Users/g/Sync/FdI/SIG --resolve --dry-run

    # Actually resolve (deletes SAFE_DELETE and IDENTICAL conflicts)
    python3 sync_conflict_resolver.py --scan-path /Users/g/Sync/FdI/SIG --resolve --execute

    # Scan parent FdI folder (includes ESCOAMENTO etc.)
    python3 sync_conflict_resolver.py --scan-path /Users/g/Sync/FdI --resolve --dry-run

Cron (optional — run after overnight sync settles):
    30 7 * * * cd /path/to/fdi_office_automation && python3 scripts/sync_conflict_resolver.py \\
        --scan-path /Users/g/Sync/FdI/SIG --resolve --execute >> logs/cron.log 2>&1
"""

import os
import re
import sys
import json
import hashlib
import argparse
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

# Fallback logger if modelos helpers not available
try:
    from modelos.helpers import logger
except Exception:
    logger = logging.getLogger("sync_conflict_resolver")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# sync.com conflict pattern:  basename-CONFLICT-N.ext
_CONFLICT_RE = re.compile(r'^(.+?)-CONFLICT-(\d+)(\.[^/\\]+)?$', re.IGNORECASE)

# Files larger than this are NOT checksummed (performance guard)
_MAX_CHECKSUM_BYTES = 100 * 1024 * 1024  # 100 MB


class ConflictRecord:
    def __init__(self, conflict_path: Path, original_path: Optional[Path]):
        self.conflict_path = conflict_path
        self.original_path = original_path
        self.conflict_mtime: Optional[float] = None
        self.original_mtime: Optional[float] = None
        self.conflict_size: Optional[int] = None
        self.original_size: Optional[int] = None
        self.conflict_save_dt: Optional[str] = None
        self.original_save_dt: Optional[str] = None
        self.conflict_save_user: Optional[str] = None
        self.original_save_user: Optional[str] = None
        self.identical: Optional[bool] = None
        self.status: str = "UNKNOWN"
        self.note: str = ""


class SyncConflictResolver:

    def __init__(self, scan_path: str, dry_run: bool = True, verbose: bool = False):
        self.scan_path = Path(scan_path)
        self.dry_run = dry_run
        self.log_dir = Path(os.getenv("LOG_DIR", str(Path(__file__).parent.parent / "logs")))
        if verbose:
            logger.setLevel(logging.DEBUG)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._setup_file_logging()

    def _setup_file_logging(self):
        log_file = self.log_dir / f"sync_conflict_resolver_{datetime.now():%Y-%m-%d}.log"
        fh = logging.FileHandler(log_file)
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(fh)

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover(self) -> list:
        records = []
        logger.info(f"Scanning {self.scan_path} for CONFLICT files ...")
        for p in sorted(self.scan_path.rglob("*")):
            if not p.is_file():
                continue
            m = _CONFLICT_RE.match(p.name)
            if not m:
                continue
            stem, ext = m.group(1), (m.group(3) or "")
            original = p.parent / (stem + ext)
            records.append(ConflictRecord(p, original if original.exists() else None))
        logger.info(f"Found {len(records)} CONFLICT file(s)")
        return records

    # ------------------------------------------------------------------
    # Analysis
    # ------------------------------------------------------------------

    def analyse(self, records: list) -> None:
        for rec in records:
            self._collect_stats(rec)
            self._classify(rec)

    def _collect_stats(self, rec: ConflictRecord) -> None:
        s = rec.conflict_path.stat()
        rec.conflict_mtime = s.st_mtime
        rec.conflict_size = s.st_size

        if rec.original_path is None:
            return

        s = rec.original_path.stat()
        rec.original_mtime = s.st_mtime
        rec.original_size = s.st_size

        ext = rec.conflict_path.suffix.lower()
        if ext == ".qgs":
            rec.conflict_save_dt, rec.conflict_save_user = self._qgs_meta(rec.conflict_path)
            rec.original_save_dt, rec.original_save_user = self._qgs_meta(rec.original_path)

        if rec.conflict_size < _MAX_CHECKSUM_BYTES:
            rec.identical = (
                rec.conflict_size == rec.original_size
                and self._md5(rec.conflict_path) == self._md5(rec.original_path)
            )

    def _classify(self, rec: ConflictRecord) -> None:
        if rec.original_path is None:
            rec.status = "ORPHAN"
            rec.note = "Original file not found"
            return
        if rec.identical is True:
            rec.status = "IDENTICAL"
            rec.note = "Byte-for-byte identical to original"
            return
        c_ts = self._effective_ts(rec.conflict_save_dt, rec.conflict_mtime)
        o_ts = self._effective_ts(rec.original_save_dt, rec.original_mtime)
        delta_h = (o_ts - c_ts) / 3600
        if delta_h >= 0:
            rec.status = "SAFE_DELETE"
            user = f" (saved by {rec.original_save_user})" if rec.original_save_user else ""
            rec.note = f"Original is {delta_h:.1f}h newer{user}"
        else:
            rec.status = "REVIEW"
            user = f" (saved by {rec.conflict_save_user})" if rec.conflict_save_user else ""
            rec.note = f"CONFLICT is {abs(delta_h):.1f}h newer — manual review needed{user}"

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------

    def report(self, records: list) -> None:
        safe = [r for r in records if r.status == "SAFE_DELETE"]
        identical = [r for r in records if r.status == "IDENTICAL"]
        review = [r for r in records if r.status == "REVIEW"]
        orphan = [r for r in records if r.status == "ORPHAN"]

        w = 70
        print("\n" + "="*w)
        print(f"  SYNC.COM CONFLICT REPORT — {datetime.now():%Y-%m-%d %H:%M}")
        print(f"  Scan path : {self.scan_path}")
        print("="*w)
        print(f"  Total conflicts  : {len(records)}")
        print(f"  SAFE_DELETE      : {len(safe)}   (original is newer — ok to remove CONFLICT)")
        print(f"  IDENTICAL        : {len(identical)}   (same content — ok to remove CONFLICT)")
        print(f"  REVIEW           : {len(review)}   (conflict is NEWER — do not delete!)")
        print(f"  ORPHAN           : {len(orphan)}   (no matching original file)")
        print("="*w)

        sections = [
            ("✅  SAFE TO DELETE", safe + identical),
            ("⚠️   REVIEW REQUIRED (conflict is newer than original!)", review),
            ("🔴  ORPHAN (no original found)", orphan),
        ]
        for title, group in sections:
            if not group:
                continue
            print(f"\n{title}")
            print("-"*w)
            for rec in group:
                try:
                    rel = rec.conflict_path.relative_to(self.scan_path)
                except ValueError:
                    rel = rec.conflict_path
                sz = (rec.conflict_size or 0) / 1024**2
                print(f"  [{rec.status:12s}] {rel}  ({sz:.1f} MB)")
                print(f"               {rec.note}")
                if rec.conflict_save_dt or rec.original_save_dt:
                    print(f"               CONFLICT: {rec.conflict_save_dt or '?'}  |  Original: {rec.original_save_dt or '?'}")
                print()
        print("="*w + "\n")

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def resolve(self, records: list) -> dict:
        deleted, skipped_review, skipped_orphan, errors = [], [], [], []
        mode = "DRY-RUN" if self.dry_run else "EXECUTE"

        for rec in records:
            if rec.status in ("SAFE_DELETE", "IDENTICAL"):
                try:
                    if self.dry_run:
                        logger.info(f"[DRY-RUN] Would delete: {rec.conflict_path}")
                    else:
                        rec.conflict_path.unlink()
                        logger.info(f"Deleted: {rec.conflict_path}")
                    deleted.append(str(rec.conflict_path))
                except Exception as e:
                    logger.error(f"Error deleting {rec.conflict_path}: {e}")
                    errors.append({"path": str(rec.conflict_path), "error": str(e)})
            elif rec.status == "REVIEW":
                logger.warning(f"REVIEW skipped: {rec.conflict_path.name} — {rec.note}")
                skipped_review.append(str(rec.conflict_path))
            elif rec.status == "ORPHAN":
                logger.warning(f"ORPHAN skipped: {rec.conflict_path}")
                skipped_orphan.append(str(rec.conflict_path))

        summary = {
            "timestamp": datetime.now().isoformat(),
            "mode": mode,
            "scan_path": str(self.scan_path),
            "deleted": deleted,
            "skipped_review": skipped_review,
            "skipped_orphan": skipped_orphan,
            "errors": errors,
        }
        log_file = self.log_dir / f"sync_conflict_resolver_{datetime.now():%Y-%m-%d}.json"
        with open(log_file, "w") as f:
            json.dump(summary, f, indent=2)
        logger.info(f"Summary written to {log_file}")

        print(f"\n[{mode}] Deleted: {len(deleted)} | Review: {len(skipped_review)} | "
              f"Orphan: {len(skipped_orphan)} | Errors: {len(errors)}")
        if skipped_review:
            print("\n⚠️  These conflict files are NEWER than the original — manual review required:")
            for p in skipped_review:
                print(f"   {p}")
        return summary

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _qgs_meta(path: Path):
        """Extract (saveDateTime, saveUser) from the root <qgis> element."""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    stripped = line.strip()
                    if stripped.startswith("<qgis "):
                        dt = re.search(r'saveDateTime="([^"]+)"', stripped)
                        usr = re.search(r'saveUser="([^"]+)"', stripped)
                        return (
                            dt.group(1) if dt else None,
                            usr.group(1) if usr else None,
                        )
                    if stripped and not stripped.startswith("<?"):
                        break
        except Exception:
            pass
        return None, None

    @staticmethod
    def _effective_ts(save_dt: Optional[str], mtime: Optional[float]) -> float:
        if save_dt:
            try:
                return datetime.fromisoformat(save_dt).timestamp()
            except ValueError:
                pass
        return mtime or 0.0

    @staticmethod
    def _md5(path: Path) -> str:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resolve sync.com CONFLICT files in SIG project folders",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--scan-path",
        default=os.getenv("SIG_BASE", "/Users/g/Sync/FdI/SIG"),
        help="Root folder to scan (default: $SIG_BASE or /Users/g/Sync/FdI/SIG)",
    )
    parser.add_argument("--report", action="store_true",
                        help="Print analysis report without making any changes")
    parser.add_argument("--resolve", action="store_true",
                        help="Resolve conflicts (combine with --dry-run or --execute)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be deleted, no changes made")
    parser.add_argument("--execute", action="store_true",
                        help="Actually delete resolved CONFLICT files")
    parser.add_argument("--verbose", action="store_true")

    args = parser.parse_args()

    if not args.report and not args.resolve:
        args.report = True  # safe default

    if args.resolve and not args.execute:
        args.dry_run = True  # safety default

    resolver = SyncConflictResolver(
        scan_path=args.scan_path,
        dry_run=not args.execute,
        verbose=args.verbose,
    )

    records = resolver.discover()
    resolver.analyse(records)
    resolver.report(records)

    if args.resolve:
        resolver.resolve(records)

    return 0


if __name__ == "__main__":
    sys.exit(main())
