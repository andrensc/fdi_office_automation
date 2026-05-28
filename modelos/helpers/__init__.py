"""
helpers — Shared utility modules

Core modules:
- docker_executor: Docker exec wrapper with logging
- log_manager: Centralized logging and metric tracking
- notification: Email alerts and notifications
"""

import logging
from pathlib import Path

# Configure root logger for helpers
LOG_DIR = Path(__file__).parent.parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger("fdi_office_automation")
if not logger.handlers:
    handler = logging.FileHandler(LOG_DIR / "office_automation.log")
    formatter = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

__all__ = [
    "docker_executor",
    "log_manager",
    "notification",
    "logger",
]
