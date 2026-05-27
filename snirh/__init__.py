"""
SNIRH monthly data cache scraping module.

Scrapes SNIRH bulletins for use by the comercial_maps nearest-station lookup pipeline.
"""

from .snirh_session import get_session
from .snirh_station_catalog import fetch_station_catalog
from .snirh_fetch_temperature import fetch_temperature_extremes
from .snirh_fetch_reservoirs import fetch_reservoir_fill
from .snirh_fetch_drought import fetch_drought_index

__all__ = [
    "get_session",
    "fetch_station_catalog",
    "fetch_temperature_extremes",
    "fetch_reservoir_fill",
    "fetch_drought_index",
]
