# SNIRH Monthly Data Cache — `fdi_office_automation`

Automated scraper module for caching monthly hydrological and meteorological data from the Portuguese [SNIRH](https://snirh.apambiente.pt) (Sistema Nacional de Informação de Recursos Hídricos) portal.

## Purpose

Provides a reproducible, append-only local CSV cache of SNIRH data for use by the **FdI `comercial_maps` pipeline**, specifically the nearest-station climate lookup during field population.

Data collected:
- **Station catalog** — all monitoring stations with coordinates
- **Temperature extremes** — monthly TX/TN per meteorological station
- **Reservoir fill** — monthly albufeiras storage levels (hm³ + % capacity)
- **Drought index** — regional drought classification (D0–D4) + precipitation anomaly

---

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Edit .env to set SNIRH_CACHE_DIR, SNIRH_YEARS_BACK, SNIRH_LOG_DIR
```

### Environment variables (`.env`)

| Variable | Default | Description |
|---|---|---|
| `SNIRH_CACHE_DIR` | `./snirh_cache/` | Directory for output CSVs |
| `SNIRH_YEARS_BACK` | `5` | Years of historical data to fetch |
| `SNIRH_LOG_DIR` | *(none)* | If set, writes `snirh.log` here |

---

## Usage

### Run all scrapers

```bash
python3 snirh/snirh_refresh_all.py
```

### Skip specific steps

```bash
python3 snirh/snirh_refresh_all.py --skip temperature
python3 snirh/snirh_refresh_all.py --skip temperature reservoirs
```

### Override years back

```bash
python3 snirh/snirh_refresh_all.py --years-back 2
```

### Run individual scrapers

```bash
python3 -m snirh.snirh_fetch_drought
python3 -m snirh.snirh_fetch_reservoirs
python3 -m snirh.snirh_station_catalog
```

---

## Output files

All files written to `SNIRH_CACHE_DIR` (default: `./snirh_cache/`).

### `stations_catalog.csv`

| Column | Type | Description |
|---|---|---|
| `station_code` | str | SNIRH station identifier |
| `station_name` | str | Station name |
| `network` | str | Monitoring network |
| `lat` | float | Latitude (WGS84) |
| `lon` | float | Longitude (WGS84) |
| `altitude` | float | Elevation (m) |
| `active` | bool | Whether station is active |
| `fetched_at` | ISO datetime | Fetch timestamp |

### `temperatura_extremos.csv`

| Column | Type | Description |
|---|---|---|
| `station_code` | str | SNIRH station identifier |
| `station_name` | str | Station name |
| `year` | int | Year |
| `month` | int | Month (1–12) |
| `tx_abs` | float | Absolute maximum temperature (°C) |
| `tn_abs` | float | Absolute minimum temperature (°C) |
| `source_url` | str | API endpoint used |
| `fetched_at` | ISO datetime | Fetch timestamp |

### `albufeiras_fill.csv`

| Column | Type | Description |
|---|---|---|
| `albufeira_code` | str | Slug derived from reservoir name |
| `albufeira_nome` | str | Reservoir name |
| `year` | int | Bulletin year |
| `month` | int | Bulletin month |
| `volume_hm3` | float | Stored volume (hm³) |
| `pct_capacidade` | float | % of total capacity |
| `fetched_at` | ISO datetime | Fetch timestamp |

### `drought_index.csv`

| Column | Type | Description |
|---|---|---|
| `region` | str | Hydrological basin or region |
| `year` | int | Bulletin year |
| `month` | int | Bulletin month |
| `drought_class` | str | D0 (Normal) through D4 (Exceptional) |
| `precip_anomaly_pct` | float | Precipitation anomaly (%) |
| `piezo_anomaly_pct` | float | Piezometric anomaly (%) |
| `fetched_at` | ISO datetime | Fetch timestamp |

#### Drought class mapping

| Code | Portuguese term | Meaning |
|---|---|---|
| D0 | Normal | No drought |
| D1 | Seco / Fraco | Abnormally dry |
| D2 | Moderado | Moderate drought |
| D3 | Severo / Muito Seco | Severe drought |
| D4 | Extremo / Excecional | Extreme/Exceptional drought |

---

## Known quirks

- **PHP sessions**: SNIRH uses PHP session cookies. The session is established by GETting the main page first (`snirh_session.py`). If the cookie expires mid-run, re-run the orchestrator.
- **Temperature time-series**: Many stations return empty data — this is normal for inactive stations. The scraper logs a warning and continues; an empty CSV with headers is written.
- **AJAX endpoints**: The station catalog AJAX endpoint (`getStations.php`) may return an empty payload depending on server load. The HTML fallback parser handles this.
- **Bulletin structure**: The albufeiras and drought bulletin HTML layouts change occasionally. The parsers use multiple strategies (table scan → free-text fallback) to maximise resilience.
- **Rate limiting**: No explicit rate limiting is enforced. Add `time.sleep()` calls between station requests if SNIRH blocks requests.

---

## Integration with `comercial_maps`

The `comercial_maps` pipeline reads from `shared_inputs/snirh_cache/` for nearest-station climate lookup during field population. Point `SNIRH_CACHE_DIR` at that path (or symlink) to keep data in sync:

```bash
# Option A: set env var
SNIRH_CACHE_DIR=/path/to/comercial_maps/shared_inputs/snirh_cache/ python3 snirh/snirh_refresh_all.py

# Option B: symlink
ln -s /path/to/comercial_maps/shared_inputs/snirh_cache ./snirh_cache
```

---

## Module structure

```
snirh/
├── __init__.py                     — Package exports
├── snirh_session.py                — Session/cookie management
├── snirh_station_catalog.py        — Station catalog scraper
├── snirh_fetch_temperature.py      — Monthly TX/TN extremes
├── snirh_fetch_reservoirs.py       — Monthly reservoir fill
├── snirh_fetch_drought.py          — Monthly drought index
└── snirh_refresh_all.py            — Orchestrator
```
