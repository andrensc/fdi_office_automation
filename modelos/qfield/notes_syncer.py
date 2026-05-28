#!/usr/bin/env python3
"""
QField Notes Syncer — notes_syncer.py

Syncs Notas.gpkg from QField Cloud mirror folders into:
  1. The project's SIG_[project]/inputs_project/project_vector_data/Notas.gpkg
  2. The central notas_all_projects.gpkg (Comercial Maps notes layer)

Sync is uuid-based (upsert). Handles schema differences gracefully:
tables may be named "Pontos Notaveis" or "pontos_notaveis" — both are resolved.

Write-back: when note_fixed=1 in the project GPKG, propagates back to cloud GPKG.

Usage:
    from modelos.qfield.notes_syncer import NotesSyncer
    syncer = NotesSyncer()
    syncer.sync_project("Florestas_de_Iroko__Rectificacao_Artosas")
    syncer.sync_all()
"""

import json
import logging
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# mod_spatialite is required to write to cloud GPKGs (they have ST_IsEmpty triggers)
_SPATIALITE_CANDIDATES = [
    "/opt/homebrew/lib/mod_spatialite",
    "/opt/homebrew/lib/mod_spatialite.dylib",
    "/usr/lib/x86_64-linux-gnu/mod_spatialite.so",
    "/usr/lib/mod_spatialite.so",
]

def _open_gpkg(path: str, write: bool = False) -> sqlite3.Connection:
    """Open a GeoPackage, loading spatialite if needed for write access."""
    conn = sqlite3.connect(path)
    if write:
        try:
            conn.enable_load_extension(True)
            for candidate in _SPATIALITE_CANDIDATES:
                try:
                    conn.load_extension(candidate)
                    break
                except Exception:
                    continue
        except Exception as e:
            logger.debug(f"Could not load spatialite ({e}) — write-back to cloud GPKGs may fail")
    return conn

# ---------------------------------------------------------------------------
# Table name resolution — cloud files may use CamelCase or snake_case
# ---------------------------------------------------------------------------
POINTS_NAMES  = ["pontos_notaveis", "Pontos Notaveis"]
LINES_NAMES   = ["linhas_notaveis", "Linhas Notaveis"]
AREAS_NAMES   = ["areas", "Areas Notaveis"]
PHOTOS_NAMES  = ["notas_photo_gallery"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_tables(cur) -> list[str]:
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite%' AND name NOT LIKE 'gpkg_%' AND name NOT LIKE 'rtree%'"
    )
    return [r[0] for r in cur.fetchall()]


def _resolve_table(available: list[str], candidates: list[str]) -> Optional[str]:
    for c in candidates:
        if c in available:
            return c
    return None


def _get_columns(cur, table: str) -> list[str]:
    cur.execute(f'PRAGMA table_info("{table}")')
    return [r[1] for r in cur.fetchall()]


def _add_column_if_missing(cur, table: str, col: str, col_type: str = "TEXT"):
    cols = _get_columns(cur, table)
    if col not in cols:
        cur.execute(f'ALTER TABLE "{table}" ADD COLUMN {col} {col_type}')
        logger.debug(f"Added column {col} to {table}")


def _ensure_central_columns(cur, table: str):
    """Ensure project_code and synced_at columns exist in target table."""
    for col, typ in [("project_code", "TEXT"), ("synced_at", "TEXT")]:
        _add_column_if_missing(cur, table, col, typ)


def _upsert_features(
    src_cur,
    src_table: str,
    dst_conn,
    dst_table: str,
    project_code: str,
    write_back_conn=None,
    write_back_table: str = None,
    uuid_col: str = "uuid",
):
    """
    Upsert features from src_table into dst_table by uuid.
    Returns (inserted, updated) counts.
    """
    dst_cur = dst_conn.cursor()
    _ensure_central_columns(dst_cur, dst_table)
    dst_conn.commit()

    src_cols = _get_columns(src_cur, src_table)
    dst_cols = _get_columns(dst_cur, dst_table)

    # Shared columns (excluding fid which is autoincrement in dst)
    shared = [c for c in src_cols if c in dst_cols and c != "fid"]

    if uuid_col not in shared:
        logger.warning(f"No {uuid_col} column in {src_table} — skipping upsert")
        return 0, 0

    src_cur.execute(f'SELECT {", ".join(shared)} FROM "{src_table}"')
    rows = src_cur.fetchall()

    inserted = updated = 0
    synced_at = _now_iso()

    for row in rows:
        values = dict(zip(shared, row))
        uuid = values.get(uuid_col)
        if not uuid:
            continue

        values["project_code"] = project_code
        values["synced_at"] = synced_at

        # Check if exists
        dst_cur.execute(f'SELECT fid FROM "{dst_table}" WHERE {uuid_col} = ?', (uuid,))
        existing = dst_cur.fetchone()

        cols_to_write = [c for c in values if c in dst_cols]
        vals = [values[c] for c in cols_to_write]

        if existing:
            set_clause = ", ".join(f"{c} = ?" for c in cols_to_write if c != "fid")
            set_vals = [values[c] for c in cols_to_write if c != "fid"]
            dst_cur.execute(
                f'UPDATE "{dst_table}" SET {set_clause} WHERE {uuid_col} = ?',
                set_vals + [uuid],
            )
            updated += 1
        else:
            placeholders = ", ".join("?" for _ in cols_to_write)
            dst_cur.execute(
                f'INSERT INTO "{dst_table}" ({", ".join(cols_to_write)}) VALUES ({placeholders})',
                vals,
            )
            inserted += 1

    dst_conn.commit()

    # Write-back: propagate note_fixed from dst back to cloud source
    if write_back_conn and write_back_table and "note_fixed" in dst_cols:
        wb_cur = write_back_conn.cursor()
        wb_cols = _get_columns(wb_cur, write_back_table)
        if "note_fixed" in wb_cols and "uuid" in wb_cols:
            dst_cur.execute(
                f'SELECT {uuid_col}, note_fixed, note_fixed_notes FROM "{dst_table}" '
                f'WHERE project_code = ? AND note_fixed = 1',
                (project_code,),
            )
            closed = dst_cur.fetchall()
            for uuid, nf, nf_notes in closed:
                has_nfn = "note_fixed_notes" in wb_cols
                if has_nfn:
                    wb_cur.execute(
                        f'UPDATE "{write_back_table}" SET note_fixed = ?, note_fixed_notes = ? WHERE uuid = ?',
                        (nf, nf_notes, uuid),
                    )
                else:
                    wb_cur.execute(
                        f'UPDATE "{write_back_table}" SET note_fixed = ? WHERE uuid = ?',
                        (nf, uuid),
                    )
            write_back_conn.commit()
            if closed:
                logger.info(f"Write-back: {len(closed)} closed notes → {write_back_table}")

    return inserted, updated


# ---------------------------------------------------------------------------
# Main syncer class
# ---------------------------------------------------------------------------

class NotesSyncer:
    def __init__(self, config_path: Optional[Path] = None):
        if config_path is None:
            config_path = (
                Path(__file__).parent.parent / "config" / "qfield_project_mapping.json"
            )
        with open(config_path) as f:
            self.cfg = json.load(f)

        self.cloud_base = Path(self.cfg["cloud_base"])
        self.sig_base   = Path(self.cfg["sig_base"])
        self.central_gpkg = Path(self.cfg["central_notas_gpkg"])
        self.notas_rel  = self.cfg["notas_gpkg_relative"]
        self.log_dir    = Path(self.cfg["log_dir"])
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self._setup_logging()

    def _setup_logging(self):
        log_file = self.log_dir / f"qfield_notes_sync_{datetime.now().strftime('%Y-%m-%d')}.log"
        fh = logging.FileHandler(str(log_file))
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(fh)
        if not logger.handlers or not any(
            isinstance(h, logging.StreamHandler) for h in logger.handlers
        ):
            sh = logging.StreamHandler()
            sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logger.addHandler(sh)
        logger.setLevel(logging.INFO)

    # ------------------------------------------------------------------

    def _resolve_project(self, cloud_folder_name: str) -> Optional[dict]:
        """Return project config dict or None if not found."""
        projects = self.cfg.get("projects", {})
        if cloud_folder_name in projects:
            return projects[cloud_folder_name]
        # Fallback: read property_boundaries.gpkg on the fly
        cloud_dir = self.cloud_base / cloud_folder_name
        pb = cloud_dir / "property_boundaries.gpkg"
        if pb.exists():
            try:
                conn = sqlite3.connect(str(pb))
                cur = conn.cursor()
                tables = _get_tables(cur)
                for t in tables:
                    cur.execute(f'SELECT name FROM "{t}" LIMIT 1')
                    row = cur.fetchone()
                    if row:
                        name = row[0]
                        # Search SIG folder (normalize to NFC — macOS filesystem uses NFD)
                        name_nfc = unicodedata.normalize("NFC", name)
                        for d in self.sig_base.iterdir():
                            d_nfc = unicodedata.normalize("NFC", d.name)
                            if d_nfc.startswith("SIG_") and name_nfc.lower() in d_nfc.lower():
                                conn.close()
                                return {"property_name": name, "sig_project_folder": str(d)}
                conn.close()
            except Exception as e:
                logger.warning(f"Could not read property_boundaries for {cloud_folder_name}: {e}")
        return None

    # ------------------------------------------------------------------

    def sync_project(self, cloud_folder_name: str) -> dict:
        """
        Sync one project's Notas.gpkg.
        Returns a result dict with counts per table.
        """
        logger.info(f"=== Syncing {cloud_folder_name} ===")
        result = {"project": cloud_folder_name, "tables": {}, "errors": []}

        project = self._resolve_project(cloud_folder_name)
        if not project:
            msg = f"No mapping found for {cloud_folder_name}"
            logger.error(msg)
            result["errors"].append(msg)
            return result

        property_code = project.get("property_name", cloud_folder_name)
        cloud_notas   = self.cloud_base / cloud_folder_name / "Notas.gpkg"
        sig_notas     = Path(project["sig_project_folder"]) / self.notas_rel

        if not cloud_notas.exists():
            msg = f"Cloud Notas.gpkg not found: {cloud_notas}"
            logger.warning(msg)
            result["errors"].append(msg)
            return result

        src_conn     = _open_gpkg(str(cloud_notas), write=True)   # write=True for note_fixed write-back
        central_conn = sqlite3.connect(str(self.central_gpkg))
        dst_conn     = _open_gpkg(str(sig_notas), write=True) if sig_notas.exists() else None
        tmpl_path    = self.cfg.get("template_notas_gpkg")
        tmpl_conn    = _open_gpkg(tmpl_path, write=True) if tmpl_path and Path(tmpl_path).exists() else None

        src_cur   = src_conn.cursor()
        src_tables = _get_tables(src_cur)

        table_candidates = [
            ("points", POINTS_NAMES),
            ("lines",  LINES_NAMES),
            ("areas",  AREAS_NAMES),
        ]

        for geom_type, candidates in table_candidates:
            src_table = _resolve_table(src_tables, candidates)
            if not src_table:
                logger.debug(f"  No {geom_type} table in {cloud_folder_name}")
                continue

            # Determine central target table name
            central_table = candidates[0]  # always snake_case in central

            # Sync → central GPKG
            ins_c, upd_c = _upsert_features(
                src_cur, src_table,
                central_conn, central_table,
                property_code,
                write_back_conn=src_conn if dst_conn else None,
                write_back_table=src_table,
            )

            # Sync → project SIG GPKG
            ins_p = upd_p = 0
            if dst_conn:
                dst_cur_tmp = dst_conn.cursor()
                dst_tables = _get_tables(dst_cur_tmp)
                dst_table = _resolve_table(dst_tables, candidates)
                if dst_table:
                    ins_p, upd_p = _upsert_features(
                        src_cur, src_table,
                        dst_conn, dst_table,
                        property_code,
                        write_back_conn=src_conn,
                        write_back_table=src_table,
                    )

            # Sync → template GPKG (all projects aggregate here)
            ins_t = upd_t = 0
            if tmpl_conn:
                tmpl_cur_tmp = tmpl_conn.cursor()
                tmpl_tables = _get_tables(tmpl_cur_tmp)
                tmpl_table = _resolve_table(tmpl_tables, candidates)
                if tmpl_table:
                    ins_t, upd_t = _upsert_features(
                        src_cur, src_table,
                        tmpl_conn, tmpl_table,
                        property_code,
                    )

            logger.info(
                f"  {geom_type}: central +{ins_c} ~{upd_c} | project +{ins_p} ~{upd_p} | template +{ins_t} ~{upd_t}"
            )
            result["tables"][geom_type] = {
                "central": {"inserted": ins_c, "updated": upd_c},
                "project": {"inserted": ins_p, "updated": upd_p},
                "template": {"inserted": ins_t, "updated": upd_t},
            }

        # Photos
        photo_table = _resolve_table(src_tables, PHOTOS_NAMES)
        if photo_table:
            ins_c, upd_c = _upsert_features(
                src_cur, photo_table,
                central_conn, "notas_photo_gallery",
                property_code,
                write_back_conn=None,
                uuid_col="notes_photo_uuid",
            )
            ins_p = upd_p = 0
            if dst_conn:
                dst_cur_tmp = dst_conn.cursor()
                dst_tables = _get_tables(dst_cur_tmp)
                dst_pt = _resolve_table(dst_tables, PHOTOS_NAMES)
                if dst_pt:
                    ins_p, upd_p = _upsert_features(
                        src_cur, photo_table,
                        dst_conn, dst_pt,
                        property_code,
                        uuid_col="notes_photo_uuid",
                    )
            # Sync photos → template
            ins_t = upd_t = 0
            if tmpl_conn:
                tmpl_cur_tmp = tmpl_conn.cursor()
                tmpl_tables = _get_tables(tmpl_cur_tmp)
                tmpl_pt = _resolve_table(tmpl_tables, PHOTOS_NAMES)
                if tmpl_pt:
                    ins_t, upd_t = _upsert_features(
                        src_cur, photo_table,
                        tmpl_conn, tmpl_pt,
                        property_code,
                        uuid_col="notes_photo_uuid",
                    )
            logger.info(f"  photos: central +{ins_c} ~{upd_c} | project +{ins_p} ~{upd_p} | template +{ins_t} ~{upd_t}")
            result["tables"]["photos"] = {
                "central": {"inserted": ins_c, "updated": upd_c},
                "project": {"inserted": ins_p, "updated": upd_p},
                "template": {"inserted": ins_t, "updated": upd_t},
            }

        src_conn.close()
        central_conn.close()
        if dst_conn:
            dst_conn.close()
        if tmpl_conn:
            tmpl_conn.close()

        logger.info(f"=== Done: {cloud_folder_name} ===")
        return result

    # ------------------------------------------------------------------

    def sync_all(self) -> list[dict]:
        """Sync all projects — configured ones plus any cloud folder with Notas.gpkg."""
        # Start with explicitly configured projects (preserves order)
        configured = set(self.cfg.get("projects", {}).keys())
        folders = list(configured)

        # Auto-discover any cloud folder that has a Notas.gpkg but isn't in config
        if self.cloud_base.exists():
            for d in sorted(self.cloud_base.iterdir()):
                if d.is_dir() and d.name not in configured:
                    if (d / "Notas.gpkg").exists():
                        folders.append(d.name)

        results = []
        for cloud_folder in folders:
            r = self.sync_project(cloud_folder)
            results.append(r)
        return results

    # ------------------------------------------------------------------

    def pull_from_cloud_api(self, project_id: str, cloud_folder_name: str):
        """
        Pull latest Notas.gpkg from QFieldCloud API using qfieldcloud-sdk.
        Requires QFIELDCLOUD_TOKEN env var (or QFIELDCLOUD_USER + QFIELDCLOUD_PASS).

        Args:
            project_id: QFieldCloud project UUID
            cloud_folder_name: local folder name under cloud_base
        """
        try:
            from qfieldcloud_sdk import sdk
        except ImportError:
            logger.error("qfieldcloud-sdk not installed. Run: pip install qfieldcloud-sdk")
            return False

        import os
        token   = os.getenv("QFIELDCLOUD_TOKEN")
        user    = os.getenv("QFIELDCLOUD_USER")
        passwd  = os.getenv("QFIELDCLOUD_PASS")
        url     = os.getenv("QFIELDCLOUD_URL", "https://app.qfield.cloud/api/v1/")

        client = sdk.Client(url=url)
        if token:
            client.token = token
        else:
            client.login(username=user, password=passwd)

        local_dir = self.cloud_base / cloud_folder_name
        logger.info(f"Pulling Notas.gpkg for project {project_id} → {local_dir}")
        client.download_files(
            project_id,
            str(local_dir),
            filter_glob="Notas.gpkg",
            force_download=True,
        )
        return True
