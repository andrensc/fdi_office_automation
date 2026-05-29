"""
SNIRH monthly data cache scraping module.

Scrapes SNIRH bulletins for use by the comercial_maps nearest-station lookup pipeline.
"""

from .snirh_session import get_session
from .snirh_station_catalog import fetch_station_catalog
from .snirh_fetch_temperature import fetch_temperature
from .snirh_fetch_reservoirs import fetch_reservoir_fill
from .snirh_fetch_drought import fetch_drought_index
from .ipma_fetch_observations import fetch_ipma_stations, fetch_ipma_observations

__all__ = [
    "get_session",
    "fetch_station_catalog",
    "fetch_temperature",
    "fetch_reservoir_fill",
    "fetch_drought_index",
    "fetch_ipma_stations",
    "fetch_ipma_observations",
]
