"""
Centralised path resolution for fdi_office_automation.

All scripts import from here so SYNC_ROOT only needs to be set once in .env.
Usage:
    from modelos.paths import SYNC_ROOT, SIG_BASE, EPT, PATHS
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from repo root (two levels up from this file)
load_dotenv(Path(__file__).parent.parent / ".env", override=False)

# ── Root ─────────────────────────────────────────────────────────────────────
SYNC_ROOT = Path(os.getenv("SYNC_ROOT", str(Path.home() / "Sync" / "FdI")))
SIG_BASE  = Path(os.getenv("SIG_BASE",  str(SYNC_ROOT / "SIG")))

# Estrutura Projeto Template
EPT = SIG_BASE / "Estrutura Projeto Template"

# ── Convenience paths ─────────────────────────────────────────────────────────
PATHS = {
    "PREDIOS_FOLDER":          Path(os.getenv("PREDIOS_FOLDER",          str(EPT / "_Predios"))),
    "PREDIOS_ARCHIVE_FOLDER":  Path(os.getenv("PREDIOS_ARCHIVE_FOLDER",  str(EPT / "predios_archive"))),
    "LIMITE_PROPRIEDADE_GPKG": Path(os.getenv("LIMITE_PROPRIEDADE_GPKG", str(EPT / "VectorData" / "Limite da Propriedade.gpkg"))),
    "REGIONAL_DEM_PATH":       Path(os.getenv("REGIONAL_DEM_PATH",       str(SIG_BASE / "shared_inputs" / "raster_data" / "topography" / "regional_elevation.tif"))),
    "OSM_DATA_DIR":            Path(os.getenv("OSM_DATA_DIR",            str(SIG_BASE / "shared_inputs" / "vector_data" / "OSM"))),
    "QFIELD_CLOUD_BASE":       Path(os.getenv("QFIELD_CLOUD_BASE",       str(Path.home() / "QField" / "cloud"))),
    "SNIRH_CACHE_DIR":         Path(os.getenv("SNIRH_CACHE_DIR",         str(SIG_BASE / "shared_inputs" / "snirh_cache"))),
    "LOG_DIR":                 Path(os.getenv("LOG_DIR",                 str(Path(__file__).parent.parent / "logs"))),
}
