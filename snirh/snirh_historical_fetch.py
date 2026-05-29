"""
SNIRH Historical Data Fetcher
==============================
Downloads historical climate observations from SNIRH (Sistema Nacional de
Informação de Recursos Hídricos) without authentication.

Works by:
  1. Loading simplex.php to initialise the PHP session for a station
  2. Downloading CSV directly from paraCSV/dados_csv.php

Available parameters (meteorological network 920123704):
  - '1857'       : Direcção do vento horária (°)
  - '100750606'  : Velocidade do vento horária (m/s)
  - '490270858'  : Velocidade do vento média diária (m/s)
  - '100750612'  : Velocidade do vento máxima horária (m/s)
  - '1852'       : Temperatura do ar máxima diária (°C)
  - '1853'       : Temperatura do ar mínima diária (°C)
  - '413026594'  : Precipitação diária (mm)
  - '100744007'  : Precipitação horária (mm)
  - '1436794570' : Precipitação mensal (mm)
  - '4237'       : Precipitação anual (mm)

Usage:
    python3 -m snirh.snirh_historical_fetch --site 920685416 --par 100750606 \\
        --tmin 01/01/2010 --tmax 31/12/2020

    python3 -m snirh.snirh_historical_fetch --lat 38.77 --lon -8.38 \\
        --pars wind --tmin 01/01/2015 --tmax 31/12/2024

    python3 -m snirh.snirh_historical_fetch --lat 38.77 --lon -8.38 \\
        --pars all --tmin 01/01/2000 --tmax 31/12/2024 --output /path/to/dir
"""

import argparse
import csv
import io
import json
import math
import re
import sys
import time
from pathlib import Path
from typing import Optional

import requests

CACHE_DIR = Path.home() / "Sync" / "FdI" / "SIG" / "shared_inputs" / "snirh_cache"
BASE = "https://snirh.apambiente.pt"
SIMPLEX_URL = f"{BASE}/snirh/_dadosbase/site/simplex.php"
CSV_URL = f"{BASE}/snirh/_dadosbase/site/paraCSV/dados_csv.php"
XML_URL = f"{BASE}/snirh/_dadosbase/site/xml/xml_listaestacoes.php"
SEARCH_URL = f"{BASE}/index.php"

# Meteorological network cover ID
MET_COVER = "920123704"

# Known parameter codes for meteorological network
PARAMS = {
    "wind_dir_h":     ("1857",        "Direcção do vento horária", "°"),
    "wind_speed_h":   ("100750606",   "Velocidade do vento horária", "m/s"),
    "wind_speed_d":   ("490270858",   "Velocidade do vento média diária", "m/s"),
    "wind_speed_max": ("100750612",   "Velocidade do vento máxima horária", "m/s"),
    "tmax_d":         ("1852",        "Temperatura máxima diária", "°C"),
    "tmin_d":         ("1853",        "Temperatura mínima diária", "°C"),
    "precip_d":       ("413026594",   "Precipitação diária", "mm"),
    "precip_h":       ("100744007",   "Precipitação horária", "mm"),
    "precip_m":       ("1436794570",  "Precipitação mensal", "mm"),
    "precip_y":       ("4237",        "Precipitação anual", "mm"),
}

PARAM_GROUPS = {
    "wind":    ["wind_dir_h", "wind_speed_h", "wind_speed_d", "wind_speed_max"],
    "temp":    ["tmax_d", "tmin_d"],
    "precip":  ["precip_d", "precip_m", "precip_h"],
    "all":     list(PARAMS.keys()),
}

STATION_CATALOG_FILE = CACHE_DIR / "met_station_catalog.json"


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return distance in km between two WGS84 points."""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": f"{BASE}/snirh/_dadosbase/site/simplex.php",
    })
    return s


def load_station_catalog(cover: str = MET_COVER, force_rebuild: bool = False) -> list[dict]:
    """
    Load meteorological station catalog with lat/lon from SNIRH XML endpoint.
    Caches to disk. Returns list of {site, cover, code, name, lat, lon} dicts.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if STATION_CATALOG_FILE.exists() and not force_rebuild:
        age = time.time() - STATION_CATALOG_FILE.stat().st_mtime
        if age < 7 * 86400:  # rebuild weekly
            return json.loads(STATION_CATALOG_FILE.read_text())

    print("Fetching SNIRH station catalog...", file=sys.stderr)
    s = _make_session()
    s.get(f"{BASE}/index.php?idMain=2&idItem=1", timeout=15)
    s.post(f"{BASE}/index.php?idMain=2&idItem=1",
           data={"f_redes_seleccao[]": cover, "aplicar_filtro": "1", "f_tipo_de_mapa": "3"},
           timeout=15)
    r = s.get(XML_URL, timeout=30)
    r.raise_for_status()

    import html as _html
    stations = []
    for m in re.finditer(r'<marker\s+(.*?)/>', r.text, re.S):
        raw = m.group(1)
        site_m = re.search(r'site="(\d+)"', raw)
        lat_m  = re.search(r'lat="([^"]+)"', raw)
        lng_m  = re.search(r'lng="([^"]+)"', raw)
        cov_m  = re.search(r'cover="([^"]+)"', raw)
        activa = re.search(r'activa="([^"]+)"', raw)
        estacao_m = re.search(r'\bestacao="([^"]*)"', raw)
        if not (site_m and lat_m and lng_m):
            continue
        site = site_m.group(1)
        cov  = cov_m.group(1) if cov_m else cover
        # Parse name+code from estacao attribute: "■ NAME (CODE)"
        name, code = "", ""
        if estacao_m:
            val = _html.unescape(estacao_m.group(1)).strip().lstrip("\u25a0").strip()
            cm = re.match(r'(.+?)\s*\(([^)]+)\)\s*$', val)
            if cm:
                name = cm.group(1).strip()
                code = cm.group(2).strip()
            else:
                name = val
        try:
            stations.append({
                "site": site,
                "cover": cov,
                "code": code,
                "name": name,
                "lat": float(lat_m.group(1)),
                "lon": float(lng_m.group(1)),
                "active": activa.group(1) == "1" if activa else False,
            })
        except ValueError:
            continue

    STATION_CATALOG_FILE.write_text(json.dumps(stations, ensure_ascii=False, indent=2))
    print(f"  {len(stations)} meteorological stations cached.", file=sys.stderr)
    return stations


def find_nearest_stations(lat: float, lon: float, n: int = 5,
                           cover: str = MET_COVER) -> list[dict]:
    """Return the n nearest meteorological stations to (lat, lon)."""
    stations = load_station_catalog(cover)
    for st in stations:
        st["distance_km"] = _haversine(lat, lon, st["lat"], st["lon"])
    return sorted(stations, key=lambda s: s["distance_km"])[:n]


def _init_session_for_station(s: requests.Session, site: str, cover: str = MET_COVER,
                               bacia: str = "17") -> None:
    """Load simplex.php to initialise the PHP session for a station."""
    s.get(SIMPLEX_URL, params={
        "OBJINFO": "DADOS",
        "FILTRA_BACIA": bacia,
        "FILTRA_COVER": cover,
        "FILTRA_SITE": site,
    }, timeout=15)


def download_csv(site: str, par_code: str, tmin: str, tmax: str,
                 cover: str = MET_COVER, bacia: str = "17",
                 formato: str = "csv") -> str:
    """
    Download raw CSV from SNIRH for one station/parameter/period.
    tmin/tmax: dd/mm/yyyy strings.
    Returns decoded CSV text (iso-8859-1).
    """
    s = _make_session()
    _init_session_for_station(s, site, cover, bacia)
    r = s.get(CSV_URL, params={
        "sites": site,
        "pars": par_code,
        "tmin": tmin,
        "tmax": tmax,
        "formato": formato,
    }, timeout=60)
    r.raise_for_status()
    return r.content.decode("iso-8859-1")


def parse_csv(raw: str) -> list[dict]:
    """
    Parse SNIRH CSV export into list of {datetime_str, value, flag} dicts.
    Skips header rows and legend footnotes.
    """
    rows = []
    lines = raw.strip().split("\n")
    data_start = False
    par_name = ""
    unit = ""
    for line in lines:
        line = line.rstrip("\r")
        if line.startswith("SNIRH") or not line.strip():
            continue
        if line.startswith("DATA,"):
            # Next line has parameter name
            continue
        if line.startswith(",") and not data_start:
            # Parameter name row: ",Velocidade do vento horária (m/s),FLAG,"
            par_match = re.match(r",([^(]+)\(([^)]+)\)", line)
            if par_match:
                par_name = par_match.group(1).strip()
                unit = par_match.group(2).strip()
            data_start = True
            continue
        if line.startswith("(") or not data_start:
            continue
        parts = line.split(",")
        if len(parts) < 2:
            continue
        dt = parts[0].strip()
        if not dt or not (dt[0].isdigit()):
            continue
        try:
            val_str = parts[1].strip()
            val = float(val_str) if val_str else None
            flag = parts[2].strip() if len(parts) > 2 else ""
            rows.append({"datetime": dt, "value": val, "flag": flag,
                         "par_name": par_name, "unit": unit})
        except (ValueError, IndexError):
            continue
    return rows


def fetch_and_cache(site: str, par_key: str, tmin: str, tmax: str,
                    cover: str = MET_COVER, force: bool = False) -> list[dict]:
    """
    Fetch parameter data for a station, using disk cache.
    Cache file: snirh_cache/{site}_{par_code}_{tmin_yyyymmdd}_{tmax_yyyymmdd}.json
    """
    par_code = PARAMS[par_key][0]
    tmin_slug = tmin.replace("/", "")
    tmax_slug = tmax.replace("/", "")
    cache_file = CACHE_DIR / f"{site}_{par_code}_{tmin_slug}_{tmax_slug}.json"

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if cache_file.exists() and not force:
        return json.loads(cache_file.read_text())

    label = PARAMS[par_key][1]
    print(f"  Downloading {label} for site {site} ({tmin}–{tmax})...", file=sys.stderr)
    raw = download_csv(site, par_code, tmin, tmax, cover)
    rows = parse_csv(raw)
    cache_file.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    return rows


def fetch_for_property(lat: float, lon: float, par_keys: list[str],
                       tmin: str, tmax: str, n_stations: int = 1,
                       cover: str = MET_COVER) -> dict:
    """
    Find nearest meteorological station(s) and download requested parameters.
    Returns {station_info, data: {par_key: [rows]}} for each station.
    """
    stations = find_nearest_stations(lat, lon, n=n_stations, cover=cover)
    results = []
    for st in stations:
        print(f"Station: {st['name']} ({st.get('code','?')}) — {st['distance_km']:.1f} km away",
              file=sys.stderr)
        data = {}
        for pk in par_keys:
            rows = fetch_and_cache(st["site"], pk, tmin, tmax, st["cover"])
            data[pk] = rows
            n = len(rows)
            non_null = sum(1 for r in rows if r["value"] is not None)
            print(f"  {PARAMS[pk][1]}: {n} records, {non_null} valid", file=sys.stderr)
        results.append({"station": st, "data": data})
    return results


def compute_wind_stats(speed_rows: list[dict], dir_rows: list[dict]) -> dict:
    """
    Compute wind statistics from hourly speed + direction data.
    Returns: abs_max_speed, mean_speed, dominant_dir, speed_percentiles, dir_distribution.
    """
    speeds = [r["value"] for r in speed_rows if r["value"] is not None]
    dirs = [r["value"] for r in dir_rows if r["value"] is not None and r["value"] >= 0]

    if not speeds:
        return {}

    speeds_sorted = sorted(speeds)
    n = len(speeds_sorted)

    # Direction distribution (8 sectors of 45°)
    sector_names = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    sector_counts = [0] * 8
    for d in dirs:
        sector = int((d + 22.5) / 45) % 8
        sector_counts[sector] += 1

    dominant_idx = sector_counts.index(max(sector_counts))

    return {
        "n_records": n,
        "abs_max": round(max(speeds), 2),
        "p95": round(speeds_sorted[int(0.95 * n)], 2),
        "p90": round(speeds_sorted[int(0.90 * n)], 2),
        "mean": round(sum(speeds) / n, 2),
        "dominant_dir": sector_names[dominant_idx],
        "dir_distribution": {sector_names[i]: round(sector_counts[i] / max(1, len(dirs)), 3)
                             for i in range(8)},
    }


def save_csv(rows: list[dict], path: Path) -> None:
    """Save parsed rows to a clean CSV."""
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["datetime", "value", "flag"])
        writer.writeheader()
        writer.writerows({"datetime": r["datetime"], "value": r["value"], "flag": r["flag"]}
                         for r in rows)


def search_station(name_or_code: str) -> list[dict]:
    """
    Search SNIRH for stations matching name or code.
    Returns list of {site, cover, bacia, code, name, rede} dicts.
    """
    s = _make_session()
    s.get(f"{BASE}/index.php?idMain=2&idItem=3", timeout=15)
    r = s.post(SEARCH_URL, params={"idMain": "2", "idItem": "3"},
               data={"form_estacao": name_or_code, "accao": "go", "tipo_entrada": "0"},
               timeout=15)
    results = []
    # Extract AbreVersaoSimplex calls paired with station info
    # Each station block has: INFO/DADOS calls then name/bacia/rede cells
    blocks = re.split(r'(?=AbreVersaoSimplex\(\'INFO\')', r.text)
    for block in blocks[1:]:
        dados_m = re.search(r"AbreVersaoSimplex\('DADOS',(\d+),(\d+),(\d+)\)", block)
        if not dados_m:
            continue
        bacia, cover, site = dados_m.groups()
        # Get station code from the block (e.g. 21G/02UG)
        code_m = re.search(r'\b(\d{2}[A-Z]/\d{2}[A-Z]+)\b', block)
        code = code_m.group(1) if code_m else ""
        # Get name and rede from table cells
        cells = re.findall(r'<td[^>]*>\s*([^<\s][^<]*)\s*</td>', block)
        name = cells[0].strip() if cells else ""
        rede = cells[2].strip() if len(cells) > 2 else ""
        results.append({"site": site, "cover": cover, "bacia": bacia,
                        "code": code, "name": name, "rede": rede})
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download SNIRH historical climate data (no credentials required)"
    )
    loc_group = parser.add_mutually_exclusive_group()
    loc_group.add_argument("--site", help="SNIRH internal site ID (e.g. 920685416)")
    loc_group.add_argument("--lat", type=float, help="Latitude (WGS84) — finds nearest station")
    loc_group.add_argument("--search", help="Search stations by name or code (e.g. 'Lavre')")
    parser.add_argument("--lon", type=float, help="Longitude (WGS84), required with --lat")
    parser.add_argument("--cover", default=MET_COVER,
                        help="Network cover ID (default: meteorological)")
    parser.add_argument("--bacia", default="17", help="Basin ID (default: 17)")
    parser.add_argument("--par", help="Parameter code (e.g. 100750606) or key (e.g. wind_speed_h)")
    parser.add_argument("--pars", choices=["wind", "temp", "precip", "all"],
                        help="Predefined parameter group")
    parser.add_argument("--tmin", default="01/01/2000", help="Start date dd/mm/yyyy")
    parser.add_argument("--tmax", default="31/12/2024", help="End date dd/mm/yyyy")
    parser.add_argument("--output", help="Output directory for CSV files")
    parser.add_argument("--n-stations", type=int, default=1,
                        help="Number of nearest stations to fetch (with --lat)")
    parser.add_argument("--list-params", action="store_true", help="List available parameters")
    parser.add_argument("--rebuild-catalog", action="store_true",
                        help="Force rebuild station catalog")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be downloaded without downloading")
    args = parser.parse_args()

    if args.list_params:
        print("Available parameter keys:")
        for k, (code, label, unit) in PARAMS.items():
            print(f"  {k:18s} code={code:12s} {label} ({unit})")
        print("\nParameter groups: wind, temp, precip, all")
        return

    if args.rebuild_catalog:
        load_station_catalog(force_rebuild=True)
        return

    if args.search:
        results = search_station(args.search)
        print(f"Found {len(results)} station(s):")
        for r in results:
            print(f"  site={r['site']:>12} cover={r['cover']:>10} bacia={r['bacia']}"
                  f"  code={r['code']:10} name={r['name']:30} rede={r['rede']}")
        return

    # Resolve parameters
    par_keys = []
    if args.pars:
        par_keys = PARAM_GROUPS[args.pars]
    elif args.par:
        if args.par in PARAMS:
            par_keys = [args.par]
        else:
            # Assume it's a raw code — find matching key
            match = [k for k, (c, _, _) in PARAMS.items() if c == args.par]
            if match:
                par_keys = match
            else:
                # Unknown code — add as-is
                PARAMS["_custom"] = (args.par, "Custom parameter", "?")
                par_keys = ["_custom"]
    else:
        par_keys = PARAM_GROUPS["wind"]  # default: wind
        print(f"No --par/--pars specified. Fetching wind parameters.", file=sys.stderr)

    if args.dry_run:
        if args.lat is not None:
            if args.lon is None:
                parser.error("--lon required with --lat")
            stations = find_nearest_stations(args.lat, args.lon, n=args.n_stations)
        else:
            stations = [{"site": args.site, "cover": args.cover, "name": "?", "distance_km": 0}]
        print("[dry-run] Would download:")
        for st in stations:
            print(f"  Station: {st.get('name','?')} (site={st['site']})")
            for pk in par_keys:
                code = PARAMS[pk][0]
                url = f"{CSV_URL}?sites={st['site']}&pars={code}&tmin={args.tmin}&tmax={args.tmax}&formato=csv"
                print(f"    {PARAMS[pk][1]}: {url}")
        return

    # Fetch data
    output_dir = Path(args.output) if args.output else CACHE_DIR / "exports"

    if args.lat is not None:
        if args.lon is None:
            parser.error("--lon required with --lat")
        results = fetch_for_property(args.lat, args.lon, par_keys, args.tmin, args.tmax,
                                     n_stations=args.n_stations)
    else:
        # Direct site ID
        station = {"site": args.site, "cover": args.cover, "name": args.site, "distance_km": 0}
        data = {}
        for pk in par_keys:
            rows = fetch_and_cache(args.site, pk, args.tmin, args.tmax, args.cover)
            data[pk] = rows
        results = [{"station": station, "data": data}]

    # Output results
    for res in results:
        st = res["station"]
        st_dir = output_dir / f"{st['site']}_{st.get('code','').replace('/', '-')}"
        for pk, rows in res["data"].items():
            if args.output:
                save_csv(rows, st_dir / f"{pk}.csv")
                print(f"Saved {len(rows)} rows → {st_dir / f'{pk}.csv'}")

    # Print wind stats if wind was requested
    for res in results:
        st = res["station"]
        data = res["data"]
        if "wind_speed_h" in data and data["wind_speed_h"]:
            print(f"\nWind statistics — {st.get('name', st['site'])}:")
            dir_rows = data.get("wind_dir_h", [])
            stats = compute_wind_stats(data["wind_speed_h"], dir_rows)
            for k, v in stats.items():
                if k != "dir_distribution":
                    print(f"  {k}: {v}")
            if stats.get("dir_distribution"):
                print("  Direction distribution:")
                for d, p in sorted(stats["dir_distribution"].items(),
                                   key=lambda x: -x[1]):
                    bar = "█" * int(p * 40)
                    print(f"    {d:2s} {bar} {p:.1%}")


if __name__ == "__main__":
    main()
