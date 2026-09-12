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
# Render's free-tier outbound network to TomTom is slow and inconsistent -
# measured live, the same query took anywhere from 0.7s to 5.2s+ depending
# on the request, well above what 5s used to allow. That silently killed a
# real (not failed) in-flight lookup and returned an empty list - which the
# frontend can't tell apart from "no matches", so autocomplete looked like
# it just wasn't loading. Frontend's own hard cap (DriverView.jsx's
# GEOCODE_TIMEOUT_MS) is 20s, so there's plenty of room to wait longer here
# before giving up.
REQUEST_TIMEOUT_SECONDS = 12.0

# A fresh httpx.AsyncClient() per call opens a brand new TLS connection to
# TomTom every single time - measured live, that made identical back-to-back
# queries take anywhere from 1s to 11s with no cold-start pattern (fast, then
# slow, then slow, then fast), which matches a per-request handshake cost on
# Render's outbound path far more than actual TomTom slowness (TomTom itself
# answered the same queries in under 2s every time when called directly).
# One shared, lazily-created client reuses its connection pool across calls,
# so only the first request after a cold start pays for a new connection.
_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)
    return _client


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
        resp = await _get_client().get(url, params=params)
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
        resp = await _get_client().get(url, params={"key": get_settings().tomtom_api_key})
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
