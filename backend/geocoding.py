"""
Real place-name -> coordinates lookup, shared by the voice SOS triage
pipeline (voice_service.py's spoken-location resolution) and the frontend's
location search (DriverView.jsx's origin/destination fields, via GET
/api/geocode/search in main.py).

Uses TomTom's Geocoding API - the same TOMTOM_API_KEY already in use for
routing.py's route calculation, so no new credentials are needed. Replaces
what used to be two separate hardcoded place-name dicts (voice_service.py's
4-town FALLBACK_LOCATIONS, DriverView.jsx's 7-town HUBS) that only worked
for Northeast India place names.
"""
from __future__ import annotations

import logging
from typing import List, Optional, Tuple
from urllib.parse import quote

import httpx

from config import get_settings

logger = logging.getLogger("geocoding")

GEOCODE_URL = "https://api.tomtom.com/search/2/geocode"
REVERSE_GEOCODE_URL = "https://api.tomtom.com/search/2/reverseGeocode"
REQUEST_TIMEOUT_SECONDS = 5.0


async def geocode_search(query: str, limit: int = 5) -> List[dict]:
    """Returns up to `limit` real matches for a free-text place search,
    restricted to India: [{"name": "...", "lat": ..., "lon": ...}, ...].
    Empty list if the query is blank or the lookup fails - callers should
    treat that as "no matches", not necessarily an error."""
    query = (query or "").strip()
    if not query:
        return []
    settings = get_settings()
    url = f"{GEOCODE_URL}/{quote(query)}.json"
    params = {"key": settings.tomtom_api_key, "limit": limit, "countrySet": "IN"}
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            resp = await client.get(url, params=params)
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except Exception as exc:
        logger.warning("TomTom geocoding search failed for %r: %s", query, exc)
        return []
    return [
        {"name": r.get("address", {}).get("freeformAddress", query), "lat": r["position"]["lat"], "lon": r["position"]["lon"]}
        for r in results
        if "position" in r
    ]


async def geocode_one(query: str) -> Optional[Tuple[float, float]]:
    """Convenience wrapper for callers that just need one best-match
    coordinate pair (voice_service.py's spoken-location resolution) rather
    than a full suggestion list."""
    results = await geocode_search(query, limit=1)
    return (results[0]["lat"], results[0]["lon"]) if results else None


async def reverse_geocode(lat: float, lon: float) -> Optional[dict]:
    """Coordinates -> a real city/state name, for labeling map markers with
    something more readable than raw lat/lon (see GET /api/geocode/reverse
    in main.py). Returns {"city", "state"} or None if the lookup fails."""
    url = f"{REVERSE_GEOCODE_URL}/{lat},{lon}.json"
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            resp = await client.get(url, params={"key": get_settings().tomtom_api_key})
        resp.raise_for_status()
        addresses = resp.json().get("addresses", [])
    except Exception as exc:
        logger.warning("TomTom reverse geocoding failed for (%.4f, %.4f): %s", lat, lon, exc)
        return None
    if not addresses:
        return None
    addr = addresses[0]["address"]
    city = addr.get("municipality") or addr.get("countrySecondarySubdivision") or addr.get("localName")
    state = addr.get("countrySubdivisionName")
    if not city:
        return None
    return {"city": city, "state": state}
