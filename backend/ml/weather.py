"""
Live precipitation (Open-Meteo) + elevation (Open-Topo-Data) lookups, both
free and keyless. Feeds real rainfall_mm_24h and elevation_gradient_pct
values into the corridor risk model in place of its coordinate-based
heuristic proxies.

Elevation uses a separate provider from rainfall on purpose - they're
independent free-tier quotas, so exhausting one (as happened during testing)
doesn't take down the other.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import httpx

logger = logging.getLogger("weather")

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_TOPO_DATA_ELEVATION_URL = "https://api.opentopodata.org/v1/srtm30m"
REQUEST_TIMEOUT_SECONDS = 5.0

# Open-Topo-Data's free tier has a strict rate limit - a periodic refresh
# across many watched corridors (each scoring several TomTom route
# candidates concurrently) can otherwise fire many simultaneous requests at
# it, which cascades into 429s and (on this single-process async server)
# can stall the whole event loop, not just elevation lookups. Real fix is
# the elevation cache below; this semaphore just keeps a cold cache's first
# burst of misses from hitting the API all at once.
_elevation_semaphore = asyncio.Semaphore(3)


async def get_live_rainfall(lat: float, lon: float) -> Optional[float]:
    """
    Returns the summed precipitation (mm) over the trailing 24 hourly
    readings at (lat, lon), or None if the live lookup fails - callers
    should fall back to the heuristic rainfall estimate in that case.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "precipitation",
        "past_days": 1,
        "forecast_days": 1,
    }
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            resp = await client.get(OPEN_METEO_URL, params=params)
        resp.raise_for_status()
        values = resp.json()["hourly"]["precipitation"]
        last_24h = values[-24:] if len(values) >= 24 else values
        return round(sum(v for v in last_24h if v is not None), 2)
    except Exception as exc:
        logger.warning("Open-Meteo rainfall lookup failed for (%.4f, %.4f): %s", lat, lon, exc)
        return None


SOIL_SATURATION_REFERENCE_M3M3 = 0.45  # typical saturated volumetric water content for loam/clay-loam soils - normalizes the raw m3/m3 reading into a 0-1 "how saturated is this soil" index


async def get_live_soil_moisture(lat: float, lon: float) -> Optional[float]:
    """
    Returns a 0-1 soil saturation index from Open-Meteo's real
    soil_moisture_0_to_7cm field (ERA5-Land reanalysis, m3/m3 volumetric
    water content), averaged over the trailing 24 hourly readings - real
    measured data in place of the rainfall-derived heuristic this replaced.
    None if the live lookup fails - callers should fall back to the
    heuristic in that case.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "soil_moisture_0_to_7cm",
        "past_days": 1,
        "forecast_days": 1,
    }
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            resp = await client.get(OPEN_METEO_URL, params=params)
        resp.raise_for_status()
        values = [v for v in resp.json()["hourly"]["soil_moisture_0_to_7cm"][-24:] if v is not None]
        if not values:
            return None
        return round(min(1.0, (sum(values) / len(values)) / SOIL_SATURATION_REFERENCE_M3M3), 3)
    except Exception as exc:
        logger.warning("Open-Meteo soil moisture lookup failed for (%.4f, %.4f): %s", lat, lon, exc)
        return None


FLOOD_API_URL = "https://flood-api.open-meteo.com/v1/flood"


async def get_live_rainfall_72h(lat: float, lon: float) -> Optional[float]:
    """
    Returns summed precipitation (mm) over the trailing 72 hourly readings -
    the flood model's antecedent-rainfall window (floods build up over
    multi-day accumulation, unlike landslide's 24h trigger). None if the
    live lookup fails - callers should fall back to a monsoon-scaled estimate.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "precipitation",
        "past_days": 3,
        "forecast_days": 1,
    }
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            resp = await client.get(OPEN_METEO_URL, params=params)
        resp.raise_for_status()
        values = resp.json()["hourly"]["precipitation"]
        last_72h = values[-72:] if len(values) >= 72 else values
        return round(sum(v for v in last_72h if v is not None), 2)
    except Exception as exc:
        logger.warning("Open-Meteo 72h rainfall lookup failed for (%.4f, %.4f): %s", lat, lon, exc)
        return None


async def get_live_river_discharge(lat: float, lon: float) -> Optional[float]:
    """
    Returns today's simulated river discharge (m3/s) from Open-Meteo's free
    Flood API (GloFAS-based), or None if the live lookup fails - callers
    should fall back to a fixed baseline estimate in that case.
    """
    params = {"latitude": lat, "longitude": lon, "daily": "river_discharge", "forecast_days": 1}
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            resp = await client.get(FLOOD_API_URL, params=params)
        resp.raise_for_status()
        values = resp.json().get("daily", {}).get("river_discharge", [])
        return round(values[0], 2) if values and values[0] is not None else None
    except Exception as exc:
        logger.warning("Open-Meteo river discharge lookup failed for (%.4f, %.4f): %s", lat, lon, exc)
        return None


def _find_current_hour_index(times: List[str]) -> Optional[int]:
    """Locates "now" in an Open-Meteo hourly.time array by exact match on
    the truncated-to-hour timestamp, rather than assuming a fixed array
    offset - the trailing-window functions above (values[-24:] etc.) get
    away with an offset guess because they only look backward from the end
    of the array; a forecast function looks forward from "now", which could
    be anywhere in the array depending on time of day, so it has to actually
    find the right starting point."""
    now_hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00")
    return times.index(now_hour) if now_hour in times else None


async def get_rainfall_forecast(lat: float, lon: float, hours_ahead: int = 6) -> Optional[float]:
    """
    Returns summed FORECAST precipitation (mm) over the next hours_ahead
    hours from now - the forward-looking counterpart to get_live_rainfall/
    get_live_rainfall_72h above, used to detect "rain is coming" before it
    shows up in trailing-window totals. None if the lookup fails.
    """
    params = {
        "latitude": lat, "longitude": lon, "hourly": "precipitation",
        "past_days": 0, "forecast_days": 2,  # 2 days of runway so hours_ahead always fits regardless of time of day
    }
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            resp = await client.get(OPEN_METEO_URL, params=params)
        resp.raise_for_status()
        hourly = resp.json()["hourly"]
        start = _find_current_hour_index(hourly["time"])
        if start is None:
            return None
        window = [v for v in hourly["precipitation"][start:start + hours_ahead] if v is not None]
        return round(sum(window), 2) if window else None
    except Exception as exc:
        logger.warning("Open-Meteo rainfall forecast lookup failed for (%.4f, %.4f): %s", lat, lon, exc)
        return None


async def get_wind_forecast_peak(lat: float, lon: float, hours_ahead: int = 6) -> Optional[Tuple[float, float]]:
    """
    Returns (peak_wind_kmh, pressure_hpa_at_that_hour) over the next
    hours_ahead hours from now - the forward-looking counterpart to
    get_live_wind_pressure below. Cyclone risk keys off peak wind directly
    (see cyclone_engine.py), so this tracks the peak of the window, not an
    average. None if the lookup fails.
    """
    params = {
        "latitude": lat, "longitude": lon, "hourly": "wind_speed_10m,surface_pressure",
        "past_days": 0, "forecast_days": 2,
    }
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            resp = await client.get(OPEN_METEO_URL, params=params)
        resp.raise_for_status()
        hourly = resp.json()["hourly"]
        start = _find_current_hour_index(hourly["time"])
        if start is None:
            return None
        window = [
            (w, p) for w, p in zip(
                hourly["wind_speed_10m"][start:start + hours_ahead],
                hourly["surface_pressure"][start:start + hours_ahead],
            ) if w is not None and p is not None
        ]
        if not window:
            return None
        peak_wind, pressure_at_peak = max(window, key=lambda wp: wp[0])
        return round(peak_wind, 1), round(pressure_at_peak, 1)
    except Exception as exc:
        logger.warning("Open-Meteo wind forecast lookup failed for (%.4f, %.4f): %s", lat, lon, exc)
        return None


async def get_live_wind_pressure(lat: float, lon: float) -> Optional[Tuple[float, float]]:
    """
    Returns (sustained_wind_kmh, pressure_hpa) for right now, from Open-
    Meteo's standard forecast API. If a cyclone is actually affecting this
    point, its current wind/pressure will already reflect that directly -
    no need to track a distant storm's position, just read the conditions
    at the corridor itself. None if the live lookup fails.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "wind_speed_10m,surface_pressure",
        "past_days": 0,
        "forecast_days": 1,
    }
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            resp = await client.get(OPEN_METEO_URL, params=params)
        resp.raise_for_status()
        hourly = resp.json()["hourly"]
        wind_values = [v for v in hourly["wind_speed_10m"] if v is not None]
        pressure_values = [v for v in hourly["surface_pressure"] if v is not None]
        if not wind_values or not pressure_values:
            return None
        return round(wind_values[0], 1), round(pressure_values[0], 1)
    except Exception as exc:
        logger.warning("Open-Meteo wind/pressure lookup failed for (%.4f, %.4f): %s", lat, lon, exc)
        return None


_elevation_cache: dict = {}  # (lat, lon) rounded to 5dp -> elevation meters; terrain is static, so this never needs a TTL/invalidation


async def get_elevations(points: List[Tuple[float, float]]) -> Optional[List[float]]:
    """Returns real elevations (meters) for the given (lat, lon) points, via
    Open-Topo-Data's free SRTM30m API, or None if the lookup fails.

    Cached in-process by rounded coordinate - elevation is static terrain
    data, so re-fetching it on every periodic corridor refresh (every 60s,
    for the same fixed ROUTES_SEED waypoints, forever) was pure waste and
    the actual cause of a real incident: expanding from 5 to 12 watched
    corridors multiplied the concurrent load on Open-Topo-Data's strict
    free-tier rate limit past what it would tolerate, which triggered
    cascading 429s and (because this is a single-process asyncio server)
    saturated the event loop badly enough that even unrelated simple GET
    endpoints stopped responding. Caching makes every refresh after the
    first a pure cache hit for any point already seen, for zero ongoing
    external load on a fixed corridor list.
    """
    if not points:
        return None
    rounded = [(round(lat, 5), round(lon, 5)) for lat, lon in points]
    missing = [p for p in rounded if p not in _elevation_cache]
    if missing:
        params = {"locations": "|".join(f"{lat:.5f},{lon:.5f}" for lat, lon in missing)}
        try:
            async with _elevation_semaphore:
                async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
                    resp = await client.get(OPEN_TOPO_DATA_ELEVATION_URL, params=params)
            resp.raise_for_status()
            elevations = [r["elevation"] for r in resp.json()["results"]]
            for p, e in zip(missing, elevations):
                _elevation_cache[p] = e
        except Exception as exc:
            logger.warning("Open-Topo-Data elevation lookup failed for %d point(s): %s", len(missing), exc)
            return None
    return [_elevation_cache[p] for p in rounded]
