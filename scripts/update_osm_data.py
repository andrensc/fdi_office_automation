"""
OFFICE-N4 — Weekly OSM regional data refresh

Derives the study area from the regional DEM raster (regional_elevation.tif),
queries the Overpass API for each OSM layer within that bbox, and updates the
target GeoPackages in shared_inputs/vector_data/OSM/.

If the DEM is expanded, the next run automatically covers the expanded region.

Usage:
    python3 scripts/update_osm_data.py --dry-run
    python3 scripts/update_osm_data.py --execute
    python3 scripts/update_osm_data.py --execute --layer roads
    python3 scripts/update_osm_data.py --execute --layer buildings
    python3 scripts/update_osm_data.py --execute --layer pois
    python3 scripts/update_osm_data.py --execute --layer landuse
    python3 scripts/update_osm_data.py --execute --layer power

Cron (1st of each month at 03:00):
    0 3 1 * * cd /Users/g/Sync/FdI/fdi_office_automation && /Applications/GRASS-8.4.app/Contents/Resources/bin/python3 scripts/update_osm_data.py --execute >> logs/cron.log 2>&1
"""

import os
import sys
import json
import time
import shutil
import argparse
import logging
import subprocess
import tempfile
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))
from modelos.helpers import logger


class OsmDataUpdater:

    DEM_PATH = Path(os.getenv(
        "REGIONAL_DEM_PATH",
        "/Users/g/Sync/FdI/SIG/shared_inputs/raster_data/topography/regional_elevation.tif",
    ))
    OSM_DIR = Path(os.getenv(
        "OSM_DATA_DIR",
        "/Users/g/Sync/FdI/SIG/shared_inputs/vector_data/OSM",
    ))
    OSMCONF = Path(__file__).parent.parent / "modelos" / "config" / "osmconf.ini"
    TARGET_CRS = "EPSG:3763"
    NODATA_VALUE = -32768
    OVERPASS_URLS = [
        "https://overpass.kumi.systems/api/interpreter",
        "https://overpass-api.de/api/interpreter",
    ]
    OVERPASS_TIMEOUT = 180
    OVERPASS_RETRY = 3
    OVERPASS_RETRY_DELAY = 30

    LAYERS = [
        {
            "name": "roads",
            "gpkg": "Acessos_Regionais.gpkg",
            "layer": "Acessos Area de Estudo",
            "overpass_type": "way_relation",
            "query_body": '(way["highway"];relation["highway"]["type"="route"];);',
            "ogr_layer": "lines",
            # Locks the output schema so QGIS field-index caches stay valid.
            # Must match the attribute list in osmconf.ini [lines] (osm_id/osm_type excluded).
            "select": (
                "highway,name,ref,surface,lanes,oneway,maxspeed,junction,bridge,tunnel,"
                "lit,service,tracktype,smoothness,foot,bicycle,layer,width,access,"
                "motor_vehicle,destination,int_ref,nat_ref,reg_ref"
            ),
        },
        {
            "name": "buildings",
            "gpkg": "Edificado Regional.gpkg",
            "layer": "Edificado Area de Estudo",
            "overpass_type": "way_relation",
            "query_body": '(way["building"];relation["building"];);',
            "ogr_layer": "multipolygons",
            "select": (
                "building,amenity,name,religion,denomination,historic,heritage,"
                "operator,ref,layer,wikipedia,website,source,alt_name,type,fee,"
                "ele,barrier,ruins,disused,landuse,natural,water"
            ),
        },
        {
            "name": "pois",
            "gpkg": "Lugares de Interesse Regionais.gpkg",
            "layer": "Lugares Interesse",
            "overpass_type": "node",
            "query_body": '(node["amenity"];node["place"];node["natural"];node["historic"];);',
            "ogr_layer": "points",
            # Field order must match osmconf.ini [points] attributes exactly.
            "select": (
                "place,source:population,population:date,population,official_name,"
                "name,capital,alt_name,natural,ele,amenity,historic,related_law"
            ),
        },
        {
            "name": "landuse",
            "gpkg": "Uso do Solo Regionais.gpkg",
            "layer": "Uso do Solo Area de Estudo",
            "overpass_type": "way_relation",
            "query_body": '(way["landuse"];way["natural"];way["water"];relation["landuse"];relation["natural"];relation["water"];);',
            "ogr_layer": "multipolygons",
            "select": (
                "building,amenity,name,religion,denomination,historic,heritage,"
                "operator,ref,layer,wikipedia,website,source,alt_name,type,fee,"
                "ele,barrier,ruins,disused,landuse,natural,water"
            ),
        },
        {
            "name": "power",
            "gpkg": "Power_Infrastructure.gpkg",
            "layer": "power_lines",
            "overpass_type": "way",
            "query_body": '(way["power"~"^(line|cable|minor_line)$"];);',
            "ogr_layer": "lines",
            "select": "power,voltage,circuits,cables,wires,phases,frequency,operator,location,line,name,ref",
        },
        {
            "name": "power_poles",
            "gpkg": "Power_Infrastructure.gpkg",
            "layer": "power_poles",
            "overpass_type": "node",
            "query_body": '(node["power"~"^(tower|pole|substation|transformer)$"];);',
            "ogr_layer": "points",
            "select": "power,voltage,height,operator,ref,substation,material,phases,frequency,transformer,converter,switch,name",
        },
    ]

    def __init__(self, dry_run=False, verbose=False, layer_filter=None):
        self.dry_run = dry_run
        self.layer_filter = layer_filter
        self.log_dir = Path(os.getenv("LOG_DIR", str(Path(__file__).parent.parent / "logs")))
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._setup_file_logging()
        if verbose:
            logger.setLevel(logging.DEBUG)
        self._temp_dir = None
        self._bbox_wgs84 = None  # (south, west, north, east)

    def _setup_file_logging(self):
        date_str = datetime.now().strftime("%Y%m%d")
        log_path = self.log_dir / f"update_osm_data_{date_str}.log"
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(fh)
        logger.debug("File logging initialised → %s", log_path)

    def derive_study_area_bbox(self) -> tuple:
        """Derive WGS84 bbox (south, west, north, east) from the regional DEM."""
        logger.info("Deriving study area bbox from DEM: %s", self.DEM_PATH)

        result = subprocess.run(
            ["gdalinfo", str(self.DEM_PATH)],
            capture_output=True, text=True, check=True,
        )
        output = result.stdout

        upper_left = lower_right = None
        for line in output.splitlines():
            if "Upper Left" in line:
                upper_left = self._parse_projected_coord(line)
            elif "Lower Right" in line:
                lower_right = self._parse_projected_coord(line)

        if upper_left is None or lower_right is None:
            raise RuntimeError("Could not parse DEM corner coordinates from gdalinfo output")

        ul_x, ul_y = upper_left   # west edge, north edge (in 3763)
        lr_x, lr_y = lower_right  # east edge, south edge (in 3763)

        # Convert all 4 corners to WGS84 to capture full extent
        corners_3763 = [
            (ul_x, ul_y),  # upper-left  → (west, north)
            (lr_x, lr_y),  # lower-right → (east, south)
            (ul_x, lr_y),  # lower-left  → (west, south)
            (lr_x, ul_y),  # upper-right → (east, north)
        ]

        lons, lats = [], []
        for x, y in corners_3763:
            lon, lat = self._transform_coord_to_wgs84(x, y)
            lons.append(lon)
            lats.append(lat)

        south = round(min(lats), 4)
        north = round(max(lats), 4)
        west  = round(min(lons), 4)
        east  = round(max(lons), 4)

        self._bbox_wgs84 = (south, west, north, east)
        logger.info("Study area bbox (WGS84): S=%.4f W=%.4f N=%.4f E=%.4f", south, west, north, east)
        return self._bbox_wgs84

    def _parse_projected_coord(self, line: str) -> tuple:
        """Parse projected (x, y) from a gdalinfo corner line like:
        Upper Left  (  -98335.907,   18966.988) (  9d16...
        Returns (x, y) floats.
        """
        import re
        m = re.search(r"\(\s*([\-\d\.]+),\s*([\-\d\.]+)\)", line)
        if not m:
            raise ValueError(f"Cannot parse projected coord from: {line!r}")
        return float(m.group(1)), float(m.group(2))

    def _transform_coord_to_wgs84(self, x: float, y: float) -> tuple:
        """Transform a single EPSG:3763 coordinate to (lon, lat) WGS84."""
        result = subprocess.run(
            ["gdaltransform", "-s_srs", "EPSG:3763", "-t_srs", "EPSG:4326"],
            input=f"{x} {y}\n",
            capture_output=True, text=True, check=True,
        )
        parts = result.stdout.strip().split()
        return float(parts[0]), float(parts[1])  # lon, lat

    def _query_overpass(self, query_body: str, overpass_type: str, bbox: tuple) -> Path:
        """Download OSM data from Overpass API, return path to .osm file."""
        south, west, north, east = bbox

        node_only = overpass_type == "node"
        if node_only:
            query = (
                f"[out:xml][timeout:{self.OVERPASS_TIMEOUT}]"
                f"[bbox:{south},{west},{north},{east}];\n"
                f"{query_body}\n"
                "out body;"
            )
        else:
            query = (
                f"[out:xml][timeout:{self.OVERPASS_TIMEOUT}]"
                f"[bbox:{south},{west},{north},{east}];\n"
                f"{query_body}\n"
                # qt (quadtile/geographic) ordering is fine; we use USE_CUSTOM_INDEXING=NO
                # which stores nodes in SQLite regardless of their ID ordering.
                "out body; >; out skel qt;"
            )

        logger.debug("Overpass query:\n%s", query)

        osm_file = Path(self._temp_dir) / f"osm_{int(time.time())}.osm"
        encoded = urllib.parse.urlencode({"data": query}).encode("utf-8")

        for attempt in range(1, self.OVERPASS_RETRY + 1):
            for url in self.OVERPASS_URLS:
                try:
                    logger.info("Overpass attempt %d/%d via %s …", attempt, self.OVERPASS_RETRY, url)
                    req = urllib.request.Request(
                        url,
                        data=encoded,
                        headers={
                            "Content-Type": "application/x-www-form-urlencoded",
                            "User-Agent": "fdi_office_automation/1.0 (osm data refresh)",
                            "Accept": "application/xml, text/xml",
                        },
                    )
                    with urllib.request.urlopen(req, timeout=self.OVERPASS_TIMEOUT + 30) as resp:
                        with open(osm_file, "wb") as f:
                            shutil.copyfileobj(resp, f)
                    size_kb = osm_file.stat().st_size // 1024
                    logger.info("Downloaded %d KB → %s", size_kb, osm_file.name)
                    return osm_file
                except (urllib.error.URLError, OSError) as exc:
                    logger.warning("  %s failed: %s", url, exc)
            if attempt < self.OVERPASS_RETRY:
                logger.info("All endpoints failed, retrying in %ds …", self.OVERPASS_RETRY_DELAY)
                time.sleep(self.OVERPASS_RETRY_DELAY)
        raise RuntimeError(f"Overpass download failed after {self.OVERPASS_RETRY} attempts on all endpoints")

    def _get_dem_footprint(self) -> Path:
        """Return path to a GeoJSON polygon of the DEM's valid (non-nodata) footprint in WGS84.

        Generated once per run and cached in the temp dir.  The geometry is in
        WGS84 (EPSG:4326) because ogr2ogr -clipsrc clips in the SOURCE dataset's
        CRS (OSM files are always WGS84).
        """
        footprint_path = Path(self._temp_dir) / "dem_footprint.geojson"
        if footprint_path.exists():
            return footprint_path

        logger.info("Generating DEM footprint for clip mask …")
        result = subprocess.run(
            [
                "gdal_footprint",
                "-t_srs", "EPSG:4326",
                "-srcnodata", str(self.NODATA_VALUE),
                "-convex_hull",   # avoid concave cuts that fragment linear features
                str(self.DEM_PATH),
                str(footprint_path),
            ],
            capture_output=True, text=True,
        )
        if result.returncode != 0 or not footprint_path.exists():
            logger.warning("gdal_footprint failed (%s), falling back to bbox polygon", result.stderr.strip())
            # Use the WGS84 bbox already derived from the DEM corners
            bbox = getattr(self, "_bbox_wgs84", None) or self.derive_study_area_bbox()
            south, west, north, east = bbox
            geojson = (
                '{"type":"FeatureCollection","features":[{"type":"Feature","properties":{},'
                '"geometry":{"type":"Polygon",'
                f'"coordinates":[[[{west},{south}],[{east},{south}],[{east},{north}],[{west},{north}],[{west},{south}]]]'
                '}}]}'
            )
            footprint_path.write_text(geojson)

        logger.info("DEM footprint ready: %s", footprint_path)
        return footprint_path

    def _reorder_osm_for_gdal(self, osm_file: Path) -> Path:
        """Reorder an OSM XML file so nodes precede ways and relations.

        Overpass API outputs ways/relations before nodes when using
        'out body; >; out skel qt;'.  GDAL's OSM driver buffers unresolved
        ways in memory; once that buffer is exhausted, remaining ways are
        silently dropped — causing ~88% data loss on a ~500 MB file.  Moving
        all <node> elements before all <way>/<relation> elements means GDAL
        can resolve every geometry in a single forward pass.

        Uses streaming two-pass I/O so the full file is never loaded into memory.
        Returns path to the reordered file (written alongside the original in temp dir).
        """
        # Quick check: if the first data element is already a <node>, skip.
        with open(osm_file, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s.startswith("<node "):
                    logger.debug("OSM file already node-first, skipping reorder.")
                    return osm_file
                if s.startswith("<way ") or s.startswith("<relation "):
                    break  # ways/relations come first — reorder required

        reordered = Path(self._temp_dir) / (osm_file.stem + "_sorted.osm")
        logger.info("Reordering OSM file (nodes before ways/relations) → %s", reordered.name)

        with open(reordered, "w", encoding="utf-8") as out:
            # Pass 1 — write OSM header lines, then all <node> elements.
            with open(osm_file, "r", encoding="utf-8") as f:
                section = "header"
                for line in f:
                    s = line.strip()
                    if section == "header":
                        if s.startswith("<way ") or s.startswith("<relation "):
                            section = "skip_data"
                        elif s.startswith("<node "):
                            section = "nodes"
                            out.write(line)
                        elif "</osm>" not in s:
                            out.write(line)
                    elif section == "skip_data":
                        if s.startswith("<node "):
                            section = "nodes"
                            out.write(line)
                        # way/relation lines skipped here
                    else:  # nodes section
                        if "</osm>" not in s and not s.startswith("<remark"):
                            out.write(line)

            # Pass 2 — write all <way>…</way> and <relation>…</relation> blocks.
            with open(osm_file, "r", encoding="utf-8") as f:
                in_element = False
                close_tag = None
                for line in f:
                    s = line.strip()
                    if not in_element:
                        if s.startswith("<way "):
                            in_element = True
                            close_tag = "</way>"
                            out.write(line)
                        elif s.startswith("<relation "):
                            in_element = True
                            close_tag = "</relation>"
                            out.write(line)
                        elif s.startswith("<node "):
                            break  # all ways/relations already written
                    else:
                        out.write(line)
                        if s == close_tag:
                            in_element = False

            out.write("</osm>\n")

        logger.info("Reorder complete.")
        return reordered

    def _osm_to_gpkg(self, osm_file: Path, layer_cfg: dict) -> Path:
        """Convert .osm file to a temp GeoPackage via ogr2ogr, clipped to DEM footprint."""
        tmp_gpkg = Path(self._temp_dir) / f"{layer_cfg['name']}_{int(time.time())}.gpkg"
        footprint = self._get_dem_footprint()

        # Overpass returns ways before their referenced nodes; GDAL drops ways it cannot
        # immediately resolve, causing ~88% data loss on large files.  Reorder first.
        if "way" in layer_cfg.get("overpass_type", ""):
            osm_file = self._reorder_osm_for_gdal(osm_file)

        cmd = [
            "ogr2ogr",
            "-f", "GPKG",
            str(tmp_gpkg),
            str(osm_file),
            layer_cfg["ogr_layer"],
            "-t_srs", self.TARGET_CRS,
            "-nln", layer_cfg["layer"],
            "-oo", f"CONFIG_FILE={self.OSMCONF}",
            # USE_CUSTOM_INDEXING=NO uses a SQLite temp file keyed by node ID.
            # The reordering step above guarantees nodes appear before ways so GDAL
            # can resolve all geometries in one pass without buffering.
            "-oo", "USE_CUSTOM_INDEXING=NO",
            "--config", "OSM_MAX_TMPFILE_SIZE", "1000",
            "-clipsrc", str(footprint),
        ]
        # Polygon layers (buildings, landuse) mix simple POLYGON and MULTIPOLYGON
        # geometries.  PROMOTE_TO_MULTI prevents the geometry-type-mismatch warning
        # and avoids silently dropped features when ogr2ogr enforces strict schema.
        if layer_cfg.get("ogr_layer") == "multipolygons":
            cmd += ["-nlt", "PROMOTE_TO_MULTI"]
        if layer_cfg.get("select"):
            cmd += ["-select", layer_cfg["select"]]
        logger.debug("ogr2ogr: %s", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"ogr2ogr failed:\n{result.stderr}")
        return tmp_gpkg

    def _replace_gpkg_layer(self, tmp_gpkg: Path, layer_cfg: dict):
        """Overwrite the named layer in the target GeoPackage with converted data.

        ogr2ogr -update is used so that the second write to Power_Infrastructure.gpkg
        (power_poles) appends to the file rather than recreating it.
        """
        target_gpkg = self.OSM_DIR / layer_cfg["gpkg"]
        layer_name = layer_cfg["layer"]

        cmd = [
            "ogr2ogr",
            "-f", "GPKG",
            "-update",
            "-overwrite",
            str(target_gpkg),
            str(tmp_gpkg),
            layer_name,
        ]
        logger.debug("Replacing layer in target: %s", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"ogr2ogr replace failed for '{layer_name}' in {target_gpkg.name}:\n{result.stderr}"
            )
        logger.info("Updated layer '%s' in %s", layer_name, target_gpkg.name)

    def _validate_layer(self, layer_cfg: dict) -> dict:
        """Run ogrinfo to get feature count for the updated layer."""
        target_gpkg = self.OSM_DIR / layer_cfg["gpkg"]
        layer_name = layer_cfg["layer"]
        result = subprocess.run(
            ["ogrinfo", "-al", "-so", str(target_gpkg), layer_name],
            capture_output=True, text=True,
        )
        feature_count = 0
        ok = result.returncode == 0
        for line in result.stdout.splitlines():
            if line.startswith("Feature Count:"):
                try:
                    feature_count = int(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
                break
        status = {"layer": layer_name, "feature_count": feature_count, "ok": ok and feature_count > 0}
        logger.info(
            "Validation — '%s': %d features, ok=%s", layer_name, feature_count, status["ok"]
        )
        return status

    def run(self):
        """Execute the OSM data update pipeline."""
        mode = "DRY RUN" if self.dry_run else "EXECUTE"
        logger.info("=== OsmDataUpdater START [%s] ===", mode)

        self._temp_dir = tempfile.mkdtemp(prefix="osm_update_", dir=str(self.log_dir.parent))
        logger.debug("Temp dir: %s", self._temp_dir)

        try:
            bbox = self.derive_study_area_bbox()

            layers = self.LAYERS
            if self.layer_filter:
                layers = [lyr for lyr in self.LAYERS if lyr["name"] == self.layer_filter]
                if not layers:
                    logger.error("Unknown layer filter: %s", self.layer_filter)
                    return

            results = []
            for layer_cfg in layers:
                name = layer_cfg["name"]
                gpkg = layer_cfg["gpkg"]
                layer = layer_cfg["layer"]

                if self.dry_run:
                    logger.info(
                        "[DRY RUN] Would update layer '%s' in %s | query: %s",
                        layer, gpkg, layer_cfg["query_body"],
                    )
                    continue

                logger.info("--- Processing layer: %s ---", name)
                try:
                    osm_file = self._query_overpass(
                        layer_cfg["query_body"], layer_cfg["overpass_type"], bbox
                    )
                    tmp_gpkg = self._osm_to_gpkg(osm_file, layer_cfg)
                    self._replace_gpkg_layer(tmp_gpkg, layer_cfg)
                    status = self._validate_layer(layer_cfg)
                    results.append(status)
                except Exception as exc:
                    logger.error("Failed to update layer '%s': %s", name, exc, exc_info=True)
                    results.append({"layer": layer, "feature_count": 0, "ok": False})

            if not self.dry_run:
                ok_count = sum(1 for r in results if r["ok"])
                logger.info(
                    "=== OsmDataUpdater DONE — %d/%d layers updated successfully ===",
                    ok_count, len(results),
                )
        finally:
            if self._temp_dir and Path(self._temp_dir).exists():
                shutil.rmtree(self._temp_dir, ignore_errors=True)
                logger.debug("Temp dir cleaned up")


def main():
    parser = argparse.ArgumentParser(
        description="OFFICE-N4: Weekly OSM regional data refresh",
    )
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--dry-run", action="store_true",
        help="Validate environment and show bbox, no download",
    )
    mode_group.add_argument(
        "--execute", action="store_true",
        help="Run the full OSM update",
    )
    parser.add_argument(
        "--layer",
        choices=[lyr["name"] for lyr in OsmDataUpdater.LAYERS],
        default=None,
        help="Update only the specified layer",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    updater = OsmDataUpdater(
        dry_run=args.dry_run,
        verbose=args.verbose,
        layer_filter=args.layer,
    )

    if not OsmDataUpdater.DEM_PATH.exists():
        logger.error("DEM not found: %s", OsmDataUpdater.DEM_PATH)
        sys.exit(1)
    if not OsmDataUpdater.OSMCONF.exists():
        logger.error("osmconf.ini not found: %s", OsmDataUpdater.OSMCONF)
        sys.exit(1)

    updater.run()


if __name__ == "__main__":
    main()
