"""
fdi_office_automation

Office orchestration for overnight backlog processing, project rebuilds, and real-time QField synchronization.

Version: 1.0 (May 2026)
Tier: 4-5 (office automation + admin dashboard)
"""

__version__ = "1.0.0"
__author__ = "FdI Office Automation"
__description__ = "Multi-project office coordination and QField synchronization"

import os
from pathlib import Path

# Project root directory
PROJECT_ROOT = Path(__file__).parent.absolute()

# Key directories
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
MODELOS_DIR = PROJECT_ROOT / "modelos"
CONFIG_DIR = MODELOS_DIR / "config"
LOGS_DIR = PROJECT_ROOT / "logs"

# Ensure key directories exist
LOGS_DIR.mkdir(exist_ok=True)

__all__ = [
    "PROJECT_ROOT",
    "SCRIPTS_DIR",
    "MODELOS_DIR",
    "CONFIG_DIR",
    "LOGS_DIR",
]
