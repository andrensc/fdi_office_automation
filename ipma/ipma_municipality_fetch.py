"""
IPMA Municipality Climate Data Fetcher
======================================
Downloads recent (last ~60-90 days) daily climate data from IPMA's public API
for a given municipality in mainland Portugal.

Available variables:
  - temperature-max  : daily Tmax (°C) — gridded interpolated, per municipality
  - temperature-min  : daily Tmin (°C)
  - precipitation-total : daily precipitation (mm)
  - evapotranspiration  : daily ET0 (mm)
  - mpdsi            : monthly Palmer Drought Severity Index

Each CSV has columns: date, minimum, maximum, range, mean, std
  - minimum/maximum : spatial range within the municipality
  - mean            : municipality-wide average

Usage:
    python3 -m ipma.ipma_municipality_fetch --lat 37.01 --lon -7.93
    python3 -m ipma.ipma_municipality_fetch --lat 38.71 --lon -9.14
    python3 -m ipma.ipma_municipality_fetch --gpkg /path/to/property.gpkg
    python3 -m ipma.ipma_municipality_fetch --lat 37.01 --lon -7.93 --dry-run
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional
from unicodedata import normalize

import requests

CACHE_DIR = Path.home() / "Sync" / "FdI" / "SIG" / "shared_inputs" / "ipma_cache"
BASE_URL = "https://api.ipma.pt/open-data/observation/climate"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"

VARIABLES = {
    "temperature-max": ("mtxmx", "°C", "Maximum temperature"),
    "temperature-min": ("mtnmn", "°C", "Minimum temperature"),
    "precipitation-total": ("mrrto", "mm", "Precipitation"),
    "evapotranspiration": ("et0", "mm", "Evapotranspiration (ET0)"),
    "mpdsi": ("mpdsi", "index", "Palmer Drought Severity Index"),
}

DISTRICTS = [
    "aveiro", "beja", "braga", "braganca", "castelo-branco",
    "coimbra", "evora", "faro", "guarda", "leiria", "lisboa",
    "portalegre", "porto", "santarem", "setubal",
    "viana-do-castelo", "vila-real", "viseu",
]

INDEX_CACHE_FILE = CACHE_DIR / "municipality_index.json"
INDEX_MAX_AGE_DAYS = 30  # rebuild index if older than this


def _slugify(name: str) -> str:
    """Convert name to lowercase ascii slug."""
    name = normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return name.lower().replace(" ", "-").replace("_", "-")


def _load_index() -> dict:
    """Return cached municipality index, rebuilding if stale or missing."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if INDEX_CACHE_FILE.exists():
        age = time.time() - INDEX_CACHE_FILE.stat().st_mtime
        if age < INDEX_MAX_AGE_DAYS * 86400:
            return json.loads(INDEX_CACHE_FILE.read_text())
    return _build_index()


def _build_index() -> dict:
    """
    Build municipality index by scanning all district directory listings.
    Returns: {dico: {district, slug, name}} where dico is 4-digit code.
    """
    import re
    print("Building IPMA municipality index...", file=sys.stderr)
    index = {}
    for dist in DISTRICTS:
        r = requests.get(f"{BASE_URL}/temperature-max/{dist}/", timeout=15)
        r.raise_for_status()
        for match in re.finditer(r'href="(mtxmx-(\d{4})-([^"]+)\.csv)"', r.text):
            _, dico, slug = match.groups()
            name = slug.replace("-", " ").title()
            index[dico] = {"district": dist, "slug": slug, "name": name}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_CACHE_FILE.write_text(json.dumps(index, ensure_ascii=False, indent=2))
    print(f"  Indexed {len(index)} municipalities.", file=sys.stderr)
    return index


def lookup_municipality(lat: float, lon: float) -> dict:
    """
    Reverse-geocode lat/lon to Portuguese municipality using Nominatim.
    Returns dict with keys: district, dico, slug, name (IPMA index entry).
    Raises ValueError if municipality not found in IPMA index.
    """
    r = requests.get(
        NOMINATIM_URL,
        params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 10},
        headers={"User-Agent": "fdi_office_automation/1.0 climate-fetch"},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()

    addr = data.get("address", {})
    # Nominatim uses 'municipality' or 'city' or 'town' for Portuguese concelhos
    mun_name = addr.get("municipality") or addr.get("city") or addr.get("town") or addr.get("village", "")
    county = addr.get("county", "")  # sometimes has 'Concelho de X'
    if county.startswith("Concelho de "):
        mun_name = county[len("Concelho de "):]

    if not mun_name:
        raise ValueError(f"Could not determine municipality for ({lat}, {lon}). Nominatim response: {data}")

    index = _load_index()
    slug_query = _slugify(mun_name)

    # Direct slug match
    for dico, entry in index.items():
        if entry["slug"] == slug_query or _slugify(entry["name"]) == slug_query:
            return {"dico": dico, **entry, "nominatim_name": mun_name}

    # Partial match fallback
    for dico, entry in index.items():
        if slug_query in entry["slug"] or entry["slug"] in slug_query:
            return {"dico": dico, **entry, "nominatim_name": mun_name}

    raise ValueError(
        f"Municipality '{mun_name}' (slug: {slug_query}) not found in IPMA index. "
        f"Nearest IPMA entries: {[e['name'] for e in index.values() if slug_query[:4] in e['slug']][:5]}"
    )


def fetch_variable(district: str, dico: str, slug: str, variable: str) -> list[dict]:
    """
    Download CSV for one variable, return list of {date, minimum, maximum, mean, std} dicts.
    Returns [] on 404 (variable not available for this municipality).
    """
    prefix, _, _ = VARIABLES[variable]
    url = f"{BASE_URL}/{variable}/{district}/{prefix}-{dico}-{slug}.csv"
    r = requests.get(url, timeout=30)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    rows = []
    lines = r.text.strip().split("\n")
    if len(lines) < 2:
        return []
    header = [h.strip() for h in lines[0].split(",")]
    for line in lines[1:]:
        parts = line.strip().split(",")
        if len(parts) < 2:
            continue
        row = dict(zip(header, parts))
        rows.append({
            "date": row.get("date", ""),
            "minimum": float(row.get("minimum", "nan")),
            "maximum": float(row.get("maximum", "nan")),
            "mean": float(row.get("mean", "nan")),
            "std": float(row.get("std", "nan") or "nan"),
        })
    return rows


def compute_stats(rows: list[dict]) -> dict:
    """Compute period statistics from a list of daily rows."""
    if not rows:
        return {}
    means = [r["mean"] for r in rows if r["mean"] == r["mean"]]  # nan check
    maxs = [r["maximum"] for r in rows if r["maximum"] == r["maximum"]]
    mins = [r["minimum"] for r in rows if r["minimum"] == r["minimum"]]
    return {
        "days": len(rows),
        "period_start": rows[0]["date"],
        "period_end": rows[-1]["date"],
        "abs_max": max(maxs) if maxs else None,
        "abs_min": min(mins) if mins else None,
        "period_mean": round(sum(means) / len(means), 2) if means else None,
        "period_max_mean": round(max(means), 2) if means else None,
        "period_min_mean": round(min(means), 2) if means else None,
    }


def fetch_and_summarise(lat: float, lon: float, dry_run: bool = False) -> dict:
    """
    Complete pipeline: reverse-geocode → download all variables → compute stats.
    Returns summary dict with municipality info and stats per variable.
    Caches results to avoid redundant downloads.
    """
    mun = lookup_municipality(lat, lon)
    print(f"Municipality: {mun['name']} ({mun['district']}, DICO {mun['dico']})", file=sys.stderr)

    cache_file = CACHE_DIR / f"ipma_{mun['dico']}_{mun['slug']}.json"
    if cache_file.exists():
        age = time.time() - cache_file.stat().st_mtime
        if age < 86400:  # valid for 24h
            print("  (using cached data)", file=sys.stderr)
            return json.loads(cache_file.read_text())

    if dry_run:
        print("  [dry-run] Would download:", file=sys.stderr)
        for var, (prefix, unit, label) in VARIABLES.items():
            url = f"{BASE_URL}/{var}/{mun['district']}/{prefix}-{mun['dico']}-{mun['slug']}.csv"
            print(f"    {label}: {url}", file=sys.stderr)
        return {"municipality": mun, "dry_run": True}

    result = {"municipality": mun, "variables": {}}
    for var, (_, unit, label) in VARIABLES.items():
        print(f"  Fetching {label}...", file=sys.stderr)
        rows = fetch_variable(mun["district"], mun["dico"], mun["slug"], var)
        stats = compute_stats(rows)
        result["variables"][var] = {"unit": unit, "label": label, **stats}

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def coords_from_gpkg(gpkg_path: str) -> tuple[float, float]:
    """Extract WGS84 centroid coordinates from a GeoPackage."""
    try:
        from osgeo import ogr, osr
    except ImportError:
        raise ImportError("GDAL/OGR not available. Install gdal or run inside the Docker container.")
    ds = ogr.Open(str(gpkg_path))
    if not ds:
        raise ValueError(f"Cannot open GeoPackage: {gpkg_path}")
    layer = ds.GetLayer(0)
    feature = layer.GetNextFeature()
    geom = feature.GetGeometryRef()
    centroid = geom.Centroid()
    src_srs = layer.GetSpatialRef()
    wgs84 = osr.SpatialReference()
    wgs84.ImportFromEPSG(4326)
    transform = osr.CoordinateTransformation(src_srs, wgs84)
    centroid.Transform(transform)
    return centroid.GetY(), centroid.GetX()  # lat, lon


def _print_summary(result: dict) -> None:
    mun = result.get("municipality", {})
    print(f"\nIPMA Climate Data — {mun.get('name', '?')} ({mun.get('district', '?').title()})")
    print("=" * 60)
    for var, stats in result.get("variables", {}).items():
        if not stats.get("days"):
            continue
        label = stats["label"]
        unit = stats["unit"]
        print(f"\n{label} ({unit})  [{stats['period_start']} → {stats['period_end']}, {stats['days']} days]")
        if stats.get("abs_max") is not None:
            print(f"  Absolute max : {stats['abs_max']:.2f}")
        if stats.get("abs_min") is not None:
            print(f"  Absolute min : {stats['abs_min']:.2f}")
        if stats.get("period_mean") is not None:
            print(f"  Period mean  : {stats['period_mean']:.2f}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch IPMA per-municipality climate data (last ~60-90 days)"
    )
    coord_group = parser.add_mutually_exclusive_group(required=True)
    coord_group.add_argument("--lat", type=float, help="Latitude (WGS84)")
    coord_group.add_argument("--gpkg", type=str, help="Path to GeoPackage (centroid used)")
    parser.add_argument("--lon", type=float, help="Longitude (WGS84, required with --lat)")
    parser.add_argument("--dry-run", action="store_true", help="Show URLs without downloading")
    parser.add_argument("--json", action="store_true", help="Output raw JSON result")
    parser.add_argument("--rebuild-index", action="store_true", help="Force rebuild municipality index")
    args = parser.parse_args()

    if args.rebuild_index:
        INDEX_CACHE_FILE.unlink(missing_ok=True)
        _build_index()
        return

    if args.gpkg:
        lat, lon = coords_from_gpkg(args.gpkg)
    else:
        if args.lon is None:
            parser.error("--lon is required with --lat")
        lat, lon = args.lat, args.lon

    result = fetch_and_summarise(lat, lon, dry_run=args.dry_run)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_summary(result)


if __name__ == "__main__":
    main()
