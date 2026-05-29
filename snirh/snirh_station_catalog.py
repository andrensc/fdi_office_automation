"""
snirh_station_catalog.py — Station catalog scraper (quarterly refresh).

Uses the xml_listaestacoes.php endpoint — the real data source behind the JS map UI.
Each network requires a POST to set PHP session state, then GET of the XML endpoint.

Output: stations_catalog.csv (append-only, deduplicated by site_id).
"""

import logging
import re
from datetime import datetime, timezone
from html import unescape
import xml.etree.ElementTree as ET

import pandas as pd
import requests
from dotenv import load_dotenv

from .snirh_session import BASE_URL, get_cache_dir, get_session

load_dotenv()
logger = logging.getLogger(__name__)

CATALOG_URL = f"{BASE_URL}/index.php?idMain=2&idItem=1"
XML_URL = f"{BASE_URL}/snirh/_dadosbase/site/xml/xml_listaestacoes.php"
CSV_NAME = "stations_catalog.csv"
COLS = ["station_code", "station_name", "network", "site_id", "lat", "lon", "active", "fetched_at"]

# Network IDs extracted from the SNIRH form (f_redes_todas[] option values)
NETWORKS = {
    "920123704": "Meteorológica",
    "920123705": "Hidrométrica",
    "100290946": "Piezometria",
    "458192970": "ETA",
}

_CODE_RE = re.compile(r'\(([^)]+)\)\s*$')


def _parse_xml_markers(xml_text: str, network_name: str) -> list[dict]:
    """Parse <markers><marker .../></markers> XML into row dicts."""
    rows = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("XML parse error for %s: %s", network_name, exc)
        return rows

    for marker in root.findall("marker"):
        estacao = unescape(marker.get("estacao", ""))
        # Station code is in parentheses at end: "■ ABRANTES (17H/01C)"
        m = _CODE_RE.search(estacao)
        code = m.group(1) if m else marker.get("site", "")
        name = estacao[: m.start()].strip().lstrip("■ ").strip() if m else estacao.strip()
        rows.append(
            {
                "station_code": code,
                "station_name": name,
                "network": network_name,
                "site_id": marker.get("site", ""),
                "lat": marker.get("lat", ""),
                "lon": marker.get("lng", ""),
                "active": marker.get("activa", "0") == "1",
            }
        )
    return rows


def fetch_station_catalog(session: requests.Session | None = None) -> pd.DataFrame:
    """
    Fetch the SNIRH station catalog for all networks and append new records to CSV.

    For each network: POST the filter form to set the PHP session state, then GET
    xml_listaestacoes.php which returns the map markers as XML with lat/lon.

    Returns the new rows DataFrame (may be empty on failure).
    """
    if session is None:
        session = get_session()
    if session is None:
        logger.error("Cannot fetch station catalog: no session")
        return pd.DataFrame(columns=COLS)

    all_rows: list[dict] = []

    for net_id, net_name in NETWORKS.items():
        try:
            # POST sets the PHP session's network filter so the XML reflects it
            session.post(
                CATALOG_URL,
                data={"f_redes_seleccao[]": net_id, "aplicar_filtro": "1", "f_tipo_de_mapa": "3"},
                timeout=30,
            )
            resp = session.get(XML_URL, timeout=30)
            resp.raise_for_status()
            rows = _parse_xml_markers(resp.text, net_name)
            logger.info("Fetched %d stations for network %s", len(rows), net_name)
            all_rows.extend(rows)
        except requests.RequestException as exc:
            logger.warning("Failed to fetch network %s (%s): %s", net_name, net_id, exc)

    if not all_rows:
        logger.warning("Station catalog returned no rows from any network")
        return pd.DataFrame(columns=COLS)

    df = pd.DataFrame(all_rows, columns=COLS[:-1])
    # Each network POST resets the filter, so the same physical station can appear
    # in multiple network views — deduplicate by internal site_id
    df.drop_duplicates(subset=["site_id"], keep="first", inplace=True)
    df.reset_index(drop=True, inplace=True)

    if df.empty:
        logger.warning("Station catalog empty after deduplication")
        return pd.DataFrame(columns=COLS)

    now_iso = datetime.now(timezone.utc).isoformat()
    df["fetched_at"] = now_iso

    cache_dir = get_cache_dir()
    csv_path = cache_dir / CSV_NAME

    if csv_path.exists():
        existing = pd.read_csv(csv_path, dtype=str)
        combined = pd.concat([existing, df.astype(str)], ignore_index=True)
        # Keep latest fetch per station_code (quarterly refresh pattern)
        combined.sort_values("fetched_at", inplace=True)
        combined.drop_duplicates(subset=["station_code"], keep="last", inplace=True)
        combined.reset_index(drop=True, inplace=True)
    else:
        combined = df.astype(str)

    combined.to_csv(csv_path, index=False)
    logger.info("Station catalog saved → %s (%d total rows)", csv_path, len(combined))
    return df


if __name__ == "__main__":
    import sys
    from .snirh_session import configure_logging

    configure_logging()
    session = get_session()
    result = fetch_station_catalog(session)
    print(f"Fetched {len(result)} station rows")
    sys.exit(0 if not result.empty else 0)
