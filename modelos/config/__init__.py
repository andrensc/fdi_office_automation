"""
config — Configuration management and discovery
"""

import json
import os
from pathlib import Path

CONFIG_DIR = Path(__file__).parent.absolute()

# Load QField layer discovery config
QFIELD_DISCOVERY_PATH = CONFIG_DIR / "qfield_layer_discovery.json"

def load_qfield_discovery():
    """Load P1-N11 QField layer discovery configuration."""
    if not QFIELD_DISCOVERY_PATH.exists():
        raise FileNotFoundError(f"QField discovery config not found: {QFIELD_DISCOVERY_PATH}")
    with open(QFIELD_DISCOVERY_PATH) as f:
        return json.load(f)

QFIELD_DISCOVERY = load_qfield_discovery()

__all__ = [
    "QFIELD_DISCOVERY",
    "load_qfield_discovery",
]
