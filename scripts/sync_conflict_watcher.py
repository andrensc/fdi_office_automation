#!/usr/bin/env python3
"""
OFFICE-SYNC-CONFLICTS — real-time conflict watcher daemon

Watches the SIG folder for new sync.com CONFLICT files.
When one appears, it immediately:
  1. Runs a deep diff (gpkg → feature-level, qgs → semantic sections)
  2. Sends a macOS desktop notification (osascript)
  3. Sends an email alert if SMTP is configured in .env
  4. Writes a structured JSON + plain-text report to logs/

The daemon tracks which conflicts it has already reported (in
logs/conflict_watcher_state.json) so restarts don't spam you.

Usage:
    # Start in foreground (Ctrl-C to stop)
    python3 sync_conflict_watcher.py

    # Start and keep running even after terminal closes
    nohup python3 sync_conflict_watcher.py >> logs/cron.log 2>&1 &

    # Also scan for existing un-reported conflicts on startup
    python3 sync_conflict_watcher.py --scan-on-startup

    # Override watch path (default: $SIG_BASE)
    python3 sync_conflict_watcher.py --watch-path /Users/g/Sync/FdI

    # Silence desktop notifications (email only)
    python3 sync_conflict_watcher.py --no-desktop

launchd plist (auto-start on login) — see bottom of this file.
"""

import os
import re
import sys
import json
import time
import signal
import smtplib
import hashlib
import sqlite3
import subprocess
import xml.etree.ElementTree as ET
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from datetime import datetime
from typing import Optional

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
except ImportError:
    print("watchdog required: pip install watchdog")
    sys.exit(1)

# Load .env from repo root
_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))
_env_file = _REPO_ROOT / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

try:
    from modelos.helpers import logger
    import logging
except Exception:
    import logging
    logger = logging.getLogger("sync_conflict_watcher")
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

_CONFLICT_RE = re.compile(r'^(.+?)-CONFLICT-(\d+)(\.[^/\\]+)?$', re.IGNORECASE)
_SETTLE_SECONDS = 8   # wait after file creation before analysing (sync still writing)
_MAX_CHECKSUM_BYTES = 50 * 1024 * 1024


# ──────────────────────────────────────────────────────────────────────────────
#  Diff helpers (self-contained — no import of analyser script needed)
# ──────────────────────────────────────────────────────────────────────────────

def _col_names(db, table):
    return [r[1] for r in db.execute(f'PRAGMA table_info("{table}")').fetchall()]

def _fid_set(db, table):
    return {r[0] for r in db.execute(f'SELECT fid FROM "{table}"').fetchall()}

def _attr_hash(db, table, cols):
    attr = [c for c in cols if c not in ('fid', 'geom')]
    if not attr:
        return {}
    col_expr = ', '.join(f'CAST("{c}" AS TEXT)' for c in attr)
    rows = db.execute(f'SELECT fid, {col_expr} FROM "{table}"').fetchall()
    return {r[0]: hashlib.md5('|'.join('' if v is None else str(v) for v in r[1:]).encode()).hexdigest()
            for r in rows}

def _sample_row(db, table, fid, cols):
    attr = [c for c in cols if c not in ('geom',)]
    if not attr:
        return {}
    col_expr = ', '.join(f'"{c}"' for c in attr)
    row = db.execute(f'SELECT {col_expr} FROM "{table}" WHERE fid=?', (fid,)).fetchone()
    return dict(zip(attr, row)) if row else {}

def _gpkg_layers(db):
    try:
        return [r[0] for r in db.execute(
            "SELECT table_name FROM gpkg_contents ORDER BY table_name").fetchall()]
    except Exception:
        return []

def diff_gpkg(conflict_path: Path, original_path: Path) -> dict:
    """Feature-level diff between two GeoPackage files."""
    result = {"type": "gpkg", "layers": {}, "summary": [], "error": None, "has_diff": False}
    try:
        c_db = sqlite3.connect(f"file:{conflict_path}?mode=ro", uri=True)
        o_db = sqlite3.connect(f"file:{original_path}?mode=ro", uri=True)
    except Exception as e:
        result["error"] = str(e)
        return result
    try:
        c_layers = set(_gpkg_layers(c_db))
        o_layers = set(_gpkg_layers(o_db))
        for table in sorted(c_layers | o_layers):
            ld = {"in_conflict": table in c_layers, "in_original": table in o_layers}
            if table in c_layers and table in o_layers:
                c_cols = _col_names(c_db, table)
                o_cols = _col_names(o_db, table)
                c_fids = _fid_set(c_db, table)
                o_fids = _fid_set(o_db, table)
                only_c = sorted(c_fids - o_fids)
                only_o = sorted(o_fids - c_fids)
                c_hashes = _attr_hash(c_db, table, c_cols)
                o_hashes = _attr_hash(o_db, table, o_cols)
                modified = sorted(f for f in (c_fids & o_fids)
                                   if c_hashes.get(f) != o_hashes.get(f))
                col_added = [c for c in c_cols if c not in o_cols]
                col_removed = [c for c in o_cols if c not in c_cols]
                ld.update({
                    "conflict_count": len(c_fids),
                    "original_count": len(o_fids),
                    "only_in_conflict": only_c,
                    "only_in_original": only_o,
                    "modified_fids": modified[:20],
                    "col_added": col_added,
                    "col_removed": col_removed,
                    "sample_conflict": [_sample_row(c_db, table, f, c_cols) for f in only_c[:3]],
                    "sample_original": [_sample_row(o_db, table, f, o_cols) for f in only_o[:3]],
                })
                if only_c or only_o or modified or col_added or col_removed:
                    result["has_diff"] = True
                    if only_c:
                        result["summary"].append(
                            f"{table}: +{len(only_c)} features only in CONFLICT")
                    if only_o:
                        result["summary"].append(
                            f"{table}: -{len(only_o)} features only in ORIGINAL")
                    if modified:
                        result["summary"].append(
                            f"{table}: {len(modified)} features with modified attributes")
                    if col_added:
                        result["summary"].append(f"{table}: new columns in CONFLICT: {col_added}")
                    if col_removed:
                        result["summary"].append(f"{table}: columns removed in CONFLICT: {col_removed}")
                else:
                    result["summary"].append(f"{table}: identical")
            elif table in c_layers:
                result["has_diff"] = True
                result["summary"].append(f"{table}: entire layer only in CONFLICT")
            else:
                result["has_diff"] = True
                result["summary"].append(f"{table}: entire layer only in ORIGINAL")
            result["layers"][table] = ld
    except Exception as e:
        result["error"] = str(e)
    finally:
        c_db.close()
        o_db.close()
    return result


def _qgs_meta_full(path: Path) -> dict:
    info = {"save_dt": None, "save_user": None, "version": None,
            "layers": {}, "relations": {}, "layouts": [], "groups": [], "crs": None}
    try:
        tree = ET.parse(path)
        root = tree.getroot()
        info["save_dt"] = root.get("saveDateTime")
        info["save_user"] = root.get("saveUser")
        info["version"] = root.get("version")
        desc = root.find(".//projectCrs//description")
        if desc is not None:
            info["crs"] = desc.text
        for ml in root.findall(".//maplayer"):
            lid = ml.get("id", "")
            name_el = ml.find("layername")
            src_el = ml.find("datasource")
            renderer_el = ml.find(".//renderer-v2")
            info["layers"][lid] = {
                "name": name_el.text if name_el is not None else "",
                "source": src_el.text if src_el is not None else "",
                "renderer_type": renderer_el.get("type") if renderer_el is not None else None,
            }
        for rel in root.findall(".//relation"):
            info["relations"][rel.get("id", "")] = rel.get("name", "")
        info["layouts"] = [l.get("name", "") for l in root.findall(".//Layout")]
        info["groups"] = [g.get("name", "") for g in root.findall(".//layer-tree-group")]
    except Exception as e:
        info["parse_error"] = str(e)
    return info


def diff_qgs(conflict_path: Path, original_path: Path) -> dict:
    result = {"type": "qgs", "summary": [], "changes": {}, "has_diff": False,
              "conflict_user": None, "original_user": None,
              "conflict_dt": None, "original_dt": None}
    c = _qgs_meta_full(conflict_path)
    o = _qgs_meta_full(original_path)
    result["conflict_user"] = c["save_user"]
    result["original_user"] = o["save_user"]
    result["conflict_dt"] = c["save_dt"]
    result["original_dt"] = o["save_dt"]

    if c["crs"] != o["crs"]:
        result["has_diff"] = True
        result["changes"]["crs"] = {"conflict": c["crs"], "original": o["crs"]}
        result["summary"].append(f"CRS: CONFLICT='{c['crs']}' vs Original='{o['crs']}'")

    c_lids = set(c["layers"]); o_lids = set(o["layers"])
    only_c = {lid: c["layers"][lid]["name"] for lid in (c_lids - o_lids)}
    only_o = {lid: o["layers"][lid]["name"] for lid in (o_lids - c_lids)}
    if only_c:
        result["has_diff"] = True
        result["changes"]["layers_only_conflict"] = only_c
        result["summary"].append(f"Layers only in CONFLICT: {list(only_c.values())[:5]}")
    if only_o:
        result["has_diff"] = True
        result["changes"]["layers_only_original"] = only_o
        result["summary"].append(f"Layers only in ORIGINAL: {list(only_o.values())[:5]}")

    prop_changes = {}
    for lid in c_lids & o_lids:
        diffs = {f: {"c": c["layers"][lid][f], "o": o["layers"][lid][f]}
                 for f in ("source", "renderer_type")
                 if c["layers"][lid][f] != o["layers"][lid][f]}
        if diffs:
            prop_changes[c["layers"][lid]["name"]] = diffs
    if prop_changes:
        result["has_diff"] = True
        result["changes"]["layer_props"] = prop_changes
        result["summary"].append(f"Layer property changes: {list(prop_changes)[:5]}")

    c_rids = set(c["relations"]); o_rids = set(o["relations"])
    rels_c = {rid: c["relations"][rid] for rid in c_rids - o_rids}
    rels_o = {rid: o["relations"][rid] for rid in o_rids - c_rids}
    if rels_c:
        result["has_diff"] = True
        result["changes"]["relations_only_conflict"] = list(set(rels_c.values()))
        result["summary"].append(f"+{len(rels_c)} relations only in CONFLICT")
    if rels_o:
        result["has_diff"] = True
        result["changes"]["relations_only_original"] = list(set(rels_o.values()))
        result["summary"].append(f"-{len(rels_o)} relations only in ORIGINAL")

    c_lo = set(c["layouts"]); o_lo = set(o["layouts"])
    if c_lo != o_lo:
        result["has_diff"] = True
        if c_lo - o_lo:
            result["summary"].append(f"Layouts only in CONFLICT: {list(c_lo - o_lo)}")
        if o_lo - c_lo:
            result["summary"].append(f"Layouts only in ORIGINAL: {list(o_lo - c_lo)}")

    if not result["summary"]:
        result["summary"] = ["No semantic differences found"]
    return result


def deep_diff(conflict_path: Path, original_path: Path) -> dict:
    ext = conflict_path.suffix.lower()
    if ext == ".gpkg":
        return diff_gpkg(conflict_path, original_path)
    elif ext == ".qgs":
        return diff_qgs(conflict_path, original_path)
    else:
        cs = conflict_path.stat(); os_ = original_path.stat()
        has_diff = cs.st_size != os_.st_size
        return {"type": "generic", "has_diff": has_diff,
                "summary": [f"Size: conflict={cs.st_size}, original={os_.st_size}"] if has_diff
                            else ["Same file size"]}


# ──────────────────────────────────────────────────────────────────────────────
#  Timestamp helpers
# ──────────────────────────────────────────────────────────────────────────────

def _qgs_save_dt(path: Path) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                if s.startswith("<qgis "):
                    m = re.search(r'saveDateTime="([^"]+)"', s)
                    u = re.search(r'saveUser="([^"]*)"', s)
                    return (m.group(1) if m else None,
                            u.group(1) if u else None)
                if s and not s.startswith("<?"):
                    break
    except Exception:
        pass
    return None, None


def _effective_ts(path: Path):
    if path.suffix.lower() == ".qgs":
        dt, user = _qgs_save_dt(path)
        if dt:
            try:
                return datetime.fromisoformat(dt).timestamp(), dt, user
            except ValueError:
                pass
    st = path.stat()
    return st.st_mtime, datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%dT%H:%M:%S"), None


# ──────────────────────────────────────────────────────────────────────────────
#  Notification channels
# ──────────────────────────────────────────────────────────────────────────────

def notify_desktop(title: str, subtitle: str, message: str) -> None:
    """macOS Notification Centre via osascript."""
    try:
        script = (f'display notification {json.dumps(message)} '
                  f'with title {json.dumps(title)} '
                  f'subtitle {json.dumps(subtitle)}')
        subprocess.run(["osascript", "-e", script],
                       capture_output=True, timeout=5)
    except Exception as e:
        logger.warning(f"Desktop notification failed: {e}")


def notify_email(subject: str, body: str) -> None:
    """Send email via SMTP if configured."""
    smtp_server = os.getenv("SMTP_SERVER", "")
    admin_email = os.getenv("ADMIN_EMAIL", "")
    if not smtp_server or not admin_email or admin_email == "admin@example.com":
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = os.getenv("SMTP_USER", admin_email)
        msg["To"] = admin_email
        msg.attach(MIMEText(body, "plain", "utf-8"))
        smtp_port = int(os.getenv("SMTP_PORT", "587"))
        with smtplib.SMTP(smtp_server, smtp_port, timeout=15) as s:
            s.ehlo()
            s.starttls()
            s.login(os.getenv("SMTP_USER", ""), os.getenv("SMTP_PASSWORD", ""))
            s.sendmail(msg["From"], [admin_email], msg.as_string())
        logger.info(f"Email sent: {subject}")
    except Exception as e:
        logger.warning(f"Email notification failed: {e}")


# ──────────────────────────────────────────────────────────────────────────────
#  Core handler
# ──────────────────────────────────────────────────────────────────────────────

class ConflictWatcher:

    def __init__(self, watch_path: str, no_desktop: bool = False,
                 scan_on_startup: bool = False):
        self.watch_path = Path(watch_path)
        self.no_desktop = no_desktop
        self.scan_on_startup = scan_on_startup
        self.log_dir = Path(os.getenv("LOG_DIR", str(_REPO_ROOT / "logs")))
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.log_dir / "conflict_watcher_state.json"
        self.seen: dict = self._load_state()
        self._setup_logging()

    def _setup_logging(self):
        log_file = self.log_dir / f"sync_conflict_watcher_{datetime.now():%Y-%m-%d}.log"
        fh = logging.FileHandler(log_file)
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(fh)
        logger.setLevel(logging.INFO)

    def _load_state(self) -> dict:
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text())
            except Exception:
                pass
        return {}

    def _save_state(self):
        try:
            self.state_file.write_text(json.dumps(self.seen, indent=2))
        except Exception as e:
            logger.warning(f"State save failed: {e}")

    def _is_conflict_file(self, path: Path) -> bool:
        if not path.is_file():
            return False
        if path.suffix.lower() in (".tif", ".tiff", ".png", ".jpg", ".jpeg",
                                    ".pdf", ".bak", ".pyc", ".db-shm", ".db-wal"):
            return False
        if path.name.endswith(".qgs~"):
            return False
        return bool(_CONFLICT_RE.match(path.name))

    def handle_conflict(self, conflict_path: Path):
        """Called when a new CONFLICT file is detected."""
        key = str(conflict_path)

        # Already reported?
        if key in self.seen:
            return

        # Wait for sync to finish writing the file
        logger.info(f"Settling {_SETTLE_SECONDS}s before analysing: {conflict_path.name}")
        time.sleep(_SETTLE_SECONDS)

        if not conflict_path.exists():
            logger.info(f"File disappeared before analysis: {conflict_path.name}")
            return

        # Find original
        m = _CONFLICT_RE.match(conflict_path.name)
        stem, ext = m.group(1), (m.group(3) or "")
        original_path = conflict_path.parent / (stem + ext)

        # Build notification payload
        project = conflict_path.parent.parent.name if "inputs_project" in str(conflict_path) \
                  else conflict_path.parent.name
        filename = conflict_path.name

        c_ts, c_dt, c_user = _effective_ts(conflict_path)
        o_ts, o_dt, o_user = _effective_ts(original_path) if original_path.exists() else (0, "not found", None)
        delta_h = abs(o_ts - c_ts) / 3600
        conflict_newer = c_ts > o_ts

        direction = (f"CONFLICT is {delta_h:.1f}h NEWER (edited by {c_user or 'unknown'})"
                     if conflict_newer
                     else f"original is {delta_h:.1f}h newer (original by {o_user or 'unknown'})")

        # Deep diff
        diff = None
        if original_path.exists():
            try:
                diff = deep_diff(conflict_path, original_path)
            except Exception as e:
                diff = {"type": "error", "error": str(e), "summary": [str(e)], "has_diff": None}

        summary_lines = diff["summary"] if diff else ["(original not found)"]
        has_diff = diff.get("has_diff", True) if diff else True

        # ── Desktop notification ──
        if not self.no_desktop:
            notif_title = "⚠️ SIG Conflict Detected"
            notif_subtitle = f"{project} / {filename}"
            notif_body = direction + "\n" + "; ".join(summary_lines[:3])
            notify_desktop(notif_title, notif_subtitle, notif_body)

        # ── Compose full alert text ──
        w = 68
        alert_lines = [
            "=" * w,
            f"  ⚠️  SYNC.COM CONFLICT DETECTED",
            f"  {datetime.now():%Y-%m-%d %H:%M:%S}",
            "=" * w,
            f"  Project  : {project}",
            f"  File     : {filename}",
            f"  Path     : {conflict_path}",
            f"  Type     : {conflict_path.suffix.lower()}",
            "-" * w,
            f"  CONFLICT : saved {c_dt}  by {c_user or 'unknown'}",
            f"  Original : saved {o_dt}  by {o_user or 'unknown'}",
            f"  Timing   : {direction}",
            "-" * w,
            "  DIFF SUMMARY:",
        ]
        for line in summary_lines:
            alert_lines.append(f"    • {line}")

        if diff and diff.get("type") == "gpkg":
            for layer, ld in diff.get("layers", {}).items():
                only_c = ld.get("only_in_conflict", [])
                only_o = ld.get("only_in_original", [])
                for sample in ld.get("sample_conflict", [])[:2]:
                    non_null = {k: v for k, v in sample.items() if v is not None and k != "fid"}
                    alert_lines.append(f"    → CONFLICT fid {sample.get('fid','?')}: {non_null}")
                for sample in ld.get("sample_original", [])[:2]:
                    non_null = {k: v for k, v in sample.items() if v is not None and k != "fid"}
                    alert_lines.append(f"    → ORIGINAL fid {sample.get('fid','?')}: {non_null}")

        if not has_diff:
            alert_lines.append("  VERDICT: ✅ Content identical — safe to delete CONFLICT")
        elif conflict_newer and has_diff:
            alert_lines.append("  VERDICT: ⚠️  CONFLICT is newer and differs — review before deleting")
        else:
            alert_lines.append("  VERDICT: ⚠️  Original is newer but CONFLICT has extra content — verify before deleting")

        alert_lines.append("=" * w)
        alert_text = "\n".join(alert_lines)

        # ── Log to file ──
        logger.warning(alert_text)
        print(alert_text)

        # ── Append to daily conflict report ──
        report_file = self.log_dir / f"conflict_report_{datetime.now():%Y-%m-%d}.txt"
        with open(report_file, "a") as f:
            f.write(alert_text + "\n\n")

        # ── Write structured JSON ──
        json_file = self.log_dir / f"conflict_report_{datetime.now():%Y-%m-%d}.json"
        records = []
        if json_file.exists():
            try:
                records = json.loads(json_file.read_text())
            except Exception:
                pass
        records.append({
            "detected_at": datetime.now().isoformat(),
            "project": project,
            "conflict_file": str(conflict_path),
            "original_file": str(original_path),
            "conflict_saved": c_dt,
            "conflict_user": c_user,
            "original_saved": o_dt,
            "original_user": o_user,
            "conflict_newer": conflict_newer,
            "delta_hours": round(delta_h, 2),
            "diff": diff,
        })
        json_file.write_text(json.dumps(records, indent=2, default=str))

        # ── Email ──
        subject = f"⚠️ SIG Conflict: {project}/{filename}"
        notify_email(subject, alert_text)

        # ── Mark as seen ──
        self.seen[key] = {"detected_at": datetime.now().isoformat(), "has_diff": has_diff}
        self._save_state()

    def startup_scan(self):
        """Scan for existing unreported conflict files on startup."""
        logger.info(f"Startup scan: looking for existing CONFLICT files in {self.watch_path}")
        found = 0
        for p in sorted(self.watch_path.rglob("*")):
            if self._is_conflict_file(p) and str(p) not in self.seen:
                found += 1
                logger.info(f"Found existing conflict: {p.name}")
                self.handle_conflict(p)
        logger.info(f"Startup scan complete — found {found} unreported conflict(s)")


    def weekly_digest(self) -> str:
        """
        Build and send a weekly email digest.
        Returns the digest text (also printed to stdout).

        Shows:
          • NEW conflicts detected since last digest (or last 7 days)
          • PENDING conflicts whose CONFLICT file is still on disk
        """
        import glob as _glob
        from datetime import timedelta

        log_dir = self.log_dir
        now = datetime.now()
        week_ago = now - timedelta(days=7)

        # Load all JSON reports
        all_records: list[dict] = []
        for json_file in sorted(log_dir.glob("conflict_report_*.json")):
            try:
                records = json.loads(json_file.read_text())
                if isinstance(records, list):
                    all_records.extend(records)
            except Exception:
                pass

        # State file tracks every conflict ever seen
        seen = self._load_state()

        # Determine last digest date (stored in state)
        last_digest_key = "__last_weekly_digest__"
        last_digest_str = seen.get(last_digest_key)
        try:
            last_digest_dt = datetime.fromisoformat(last_digest_str) if last_digest_str else week_ago
        except Exception:
            last_digest_dt = week_ago

        # NEW this week: detected after last digest
        new_records = [
            r for r in all_records
            if datetime.fromisoformat(r["detected_at"]) > last_digest_dt
        ]

        # PENDING: CONFLICT file still on disk
        pending_records = [
            r for r in all_records
            if Path(r["conflict_file"]).exists()
        ]

        # Remove duplicates by conflict_file path (keep latest)
        def dedup(records):
            seen_paths: dict = {}
            for r in sorted(records, key=lambda x: x["detected_at"]):
                seen_paths[r["conflict_file"]] = r
            return list(seen_paths.values())

        new_records = dedup(new_records)
        pending_records = dedup(pending_records)

        w = 70
        lines = [
            "=" * w,
            "  📋  FdI SIG — WEEKLY SYNC CONFLICT DIGEST",
            f"  Generated: {now:%Y-%m-%d %H:%M}",
            f"  Period: {last_digest_dt:%Y-%m-%d} → {now:%Y-%m-%d}",
            "=" * w,
        ]

        # ── NEW this period ─────────────────────────────────────────────────
        lines.append(f"\n  🆕  NEW CONFLICTS THIS PERIOD ({len(new_records)})")
        lines.append("-" * w)
        if new_records:
            for r in sorted(new_records, key=lambda x: x["detected_at"], reverse=True):
                project = r.get("project", "?")
                fname = Path(r["conflict_file"]).name
                c_user = r.get("conflict_user") or "?"
                o_user = r.get("original_user") or "?"
                c_dt = (r.get("conflict_saved") or "?")[:16]
                o_dt = (r.get("original_saved") or "?")[:16]
                still = "⚠️ STILL ON DISK" if Path(r["conflict_file"]).exists() else "✅ resolved"
                lines.append(f"  [{still}] {project} / {fname}")
                lines.append(f"     CONFLICT saved {c_dt} by {c_user}")
                lines.append(f"     Original saved {o_dt} by {o_user}")
                diff = r.get("diff") or {}
                for s in (diff.get("summary") or [])[:3]:
                    lines.append(f"     • {s}")
                lines.append("")
        else:
            lines.append("  No new conflicts this period. 🎉")
            lines.append("")

        # ── PENDING (still on disk) ─────────────────────────────────────────
        lines.append(f"  ⏳  PENDING CONFLICTS STILL ON DISK ({len(pending_records)})")
        lines.append("-" * w)
        if pending_records:
            for r in sorted(pending_records, key=lambda x: x["detected_at"]):
                project = r.get("project", "?")
                fname = Path(r["conflict_file"]).name
                c_user = r.get("conflict_user") or "?"
                detected = r.get("detected_at", "?")[:16]
                diff = r.get("diff") or {}
                has_diff = diff.get("has_diff")
                verdict = "has differences" if has_diff else ("identical" if has_diff is False else "unknown")
                lines.append(f"  {project} / {fname}")
                lines.append(f"     First detected: {detected}  |  by {c_user}  |  {verdict}")
                for s in (diff.get("summary") or [])[:2]:
                    lines.append(f"     • {s}")
                lines.append("")
        else:
            lines.append("  No pending conflicts — all clear! ✅")
            lines.append("")

        lines.append("=" * w)
        lines.append("  Run analyser for full diff detail:")
        lines.append("  python3 scripts/sync_conflict_analyser.py --only-diffs")
        lines.append("=" * w)

        digest_text = "\n".join(lines)
        print(digest_text)
        logger.info("Weekly digest generated")

        # Save to log
        digest_file = log_dir / f"conflict_digest_{now:%Y-%m-%d}.txt"
        digest_file.write_text(digest_text)

        # Send email
        subject = f"📋 SIG Weekly Conflict Digest — {len(new_records)} new, {len(pending_records)} pending"
        notify_email(subject, digest_text)

        # Update last digest timestamp in state
        seen[last_digest_key] = now.isoformat()
        self.seen = seen
        self._save_state()

        return digest_text

    def run(self):
        if self.scan_on_startup:
            self.startup_scan()

        logger.info("=" * 60)
        logger.info("sync_conflict_watcher starting")
        logger.info(f"Watching: {self.watch_path}")
        logger.info("=" * 60)
        print(f"\n✅ sync_conflict_watcher running")
        print(f"   Watching: {self.watch_path}")
        print(f"   Reports : {self.log_dir}")
        print(f"   Ctrl-C to stop\n")

        handler = _FSHandler(self)
        observer = Observer()
        observer.schedule(handler, str(self.watch_path), recursive=True)
        observer.start()

        # Track when we last sent the weekly digest
        _last_digest_check: Optional[str] = None

        try:
            while True:
                time.sleep(1)
                # Check once per hour if it's Sunday and digest hasn't been sent today
                now = datetime.now()
                today_str = now.strftime("%Y-%m-%d")
                if (now.weekday() == 6  # Sunday
                        and now.hour >= 8
                        and _last_digest_check != today_str):
                    _last_digest_check = today_str
                    logger.info("Sunday digest check triggered")
                    self.weekly_digest()
        except KeyboardInterrupt:
            pass
        finally:
            observer.stop()
            observer.join()
            self._save_state()
            logger.info("sync_conflict_watcher stopped")


class _FSHandler(FileSystemEventHandler):
    def __init__(self, watcher: ConflictWatcher):
        self.watcher = watcher

    def on_created(self, event):
        if event.is_directory:
            return
        p = Path(event.src_path)
        if self.watcher._is_conflict_file(p):
            logger.info(f"New CONFLICT file detected: {p.name}")
            self.watcher.handle_conflict(p)

    def on_moved(self, event):
        # sync.com sometimes renames files into place
        if event.is_directory:
            return
        p = Path(event.dest_path)
        if self.watcher._is_conflict_file(p):
            logger.info(f"CONFLICT file moved into place: {p.name}")
            self.watcher.handle_conflict(p)


# ──────────────────────────────────────────────────────────────────────────────
#  CLI
# ──────────────────────────────────────────────────────────────────────────────

def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description="Real-time sync.com conflict watcher — notifies on detection",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--watch-path",
        default=os.getenv("SIG_BASE", "/Users/g/Sync/FdI/SIG"),
        help="Root path to watch recursively (default: $SIG_BASE)",
    )
    parser.add_argument(
        "--scan-on-startup", action="store_true",
        help="Also report any existing unreported CONFLICT files on startup",
    )
    parser.add_argument(
        "--no-desktop", action="store_true",
        help="Suppress macOS desktop notifications (log + email only)",
    )
    parser.add_argument(
        "--weekly-report", action="store_true",
        help="Generate and email weekly digest now, then exit (use with cron/launchd)",
    )
    args = parser.parse_args()

    watcher = ConflictWatcher(
        watch_path=args.watch_path,
        no_desktop=args.no_desktop,
        scan_on_startup=args.scan_on_startup,
    )

    if args.weekly_report:
        watcher.weekly_digest()
        return 0

    watcher.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())


# ──────────────────────────────────────────────────────────────────────────────
#  launchd plist — auto-start on login (macOS)
#
#  Save as ~/Library/LaunchAgents/com.fdi.sync-conflict-watcher.plist
#  Then: launchctl load ~/Library/LaunchAgents/com.fdi.sync-conflict-watcher.plist
#
# <?xml version="1.0" encoding="UTF-8"?>
# <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
#     "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
# <plist version="1.0">
# <dict>
#     <key>Label</key>
#     <string>com.fdi.sync-conflict-watcher</string>
#     <key>ProgramArguments</key>
#     <array>
#         <string>/usr/bin/python3</string>
#         <string>/Users/g/Sync/FdI/fdi_office_automation/scripts/sync_conflict_watcher.py</string>
#         <string>--watch-path</string>
#         <string>/Users/g/Sync/FdI</string>
#         <string>--scan-on-startup</string>
#     </array>
#     <key>RunAtLoad</key>
#     <true/>
#     <key>KeepAlive</key>
#     <true/>
#     <key>StandardOutPath</key>
#     <string>/Users/g/Sync/FdI/fdi_office_automation/logs/conflict_watcher_stdout.log</string>
#     <key>StandardErrorPath</key>
#     <string>/Users/g/Sync/FdI/fdi_office_automation/logs/conflict_watcher_stderr.log</string>
#     <key>WorkingDirectory</key>
#     <string>/Users/g/Sync/FdI/fdi_office_automation</string>
# </dict>
# </plist>
# ──────────────────────────────────────────────────────────────────────────────
