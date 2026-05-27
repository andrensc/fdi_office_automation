"""
snirh_session.py — Session and cookie management for the SNIRH website.

SNIRH uses PHP session cookies. We establish a session by GETting the
main page before any scraping calls.
"""

import logging
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

BASE_URL = "https://snirh.apambiente.pt"
INIT_URL = f"{BASE_URL}/index.php?idMain=1"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def get_session(timeout: int = 30) -> requests.Session | None:
    """
    Create and return a requests.Session with SNIRH PHP session cookie set.

    Returns None if the initial connection fails.
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    try:
        resp = session.get(INIT_URL, timeout=timeout)
        resp.raise_for_status()
        logger.info("SNIRH session established (status %s)", resp.status_code)
        return session
    except requests.RequestException as exc:
        logger.error("Failed to establish SNIRH session: %s", exc)
        return None


def get_cache_dir() -> Path:
    """Return the cache directory, creating it if necessary."""
    cache_dir = Path(os.getenv("SNIRH_CACHE_DIR", "./snirh_cache/"))
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def configure_logging() -> None:
    """Configure root logger: file (if SNIRH_LOG_DIR set) + console."""
    log_dir_env = os.getenv("SNIRH_LOG_DIR")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    if log_dir_env:
        log_dir = Path(log_dir_env)
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_dir / "snirh.log", encoding="utf-8"))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )
