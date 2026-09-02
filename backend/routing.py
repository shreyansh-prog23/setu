"""
TomTom Routing API integration for the pan-India disaster logistics dashboard.

Wraps TomTom's `calculateRoute` endpoint (v1) with:
  - live traffic (traffic=true) and optional predictive traffic (departAt)
  - truck travel mode, for freight-appropriate routing
  - optional unpaved-road avoidance
  - terrain hazard avoidance: the returned route(s) are checked against
    caller-supplied hazard coordinates, and the service automatically
    switches to a TomTom-computed alternative (requested via
    maxAlternatives) if the primary corridor passes within HAZARD_RADIUS_KM
    of a hazard.

Endpoint reference (verified against TomTom's docs at
https://docs.tomtom.com/routing-api/documentation/tomtom-maps/calculate-route):
  GET https://api.tomtom.com/routing/1/calculateRoute/{locations}/json
  - {locations} is a colon-separated list of "lat,lon" points; the first and
    last are origin/destination, anything between them is a forced waypoint.
  - traffic=true                enables live traffic delay data
  - travelMode=truck             routes for a freight/logistics vehicle
  - avoid=unpavedRoads            (optional) avoid unsealed roads
  - departAt=<ISO 8601>            (optional) predictive traffic for a future departure
  - maxAlternatives=<0-5>          requests additional alternative routes

TomTom does not currently document a generic "avoid these coordinates"
parameter for calculateRoute, so hazard avoidance here is implemented the
same way the app's OSRM-based frontend does it: sample the route polyline,
haversine-check it against known hazard points, and fall back to the best
available TomTom alternative route when the primary is unsafe.
"""
from __future__ import annotations

import asyncio
import logging
import math
from typing import List, Optional, Tuple

import httpx

import database
from config import get_settings
from ml.weather import (
    get_elevations,
    get_live_rainfall,
    get_live_rainfall_72h,
    get_live_river_discharge,
    get_live_soil_moisture,
    get_live_wind_pressure,
)
from multi_hazard import evaluate_corridor
from schemas import Coordinate, CongestionLevel, HazardBreakdown, RouteRequest, RouteResponse

logger = logging.getLogger("routing")

MAX_ALTERNATIVES = 3
HAZARD_RADIUS_KM = 20.0

# Driver-reported hazards (see POST /api/alerts in main.py) are precise point
# reports rather than broad risk zones, so they're checked with a tighter
# radius than the caller-supplied hazard_avoid_coords above.
ACTIVE_HAZARD_RADIUS_KM = 1.0


def _active_hazard_coords() -> List[Coordinate]:
    """Queried fresh from SQLite on every call (not cached) so an expired
    hazard report - see database.get_active_hazards()'s TTL filter - stops
    affecting routes immediately rather than only after a server restart."""
    return [Coordinate(lat=h["latitude"], lon=h["longitude"]) for h in database.get_active_hazards()]

# Congestion classification thresholds, as a fraction of trafficDelayInSeconds
# over travelTimeInSeconds. Tunable business logic, not a TomTom-provided value.
CONGESTION_MODERATE_RATIO = 0.10
CONGESTION_SEVERE_RATIO = 0.30


class RoutingServiceError(Exception):
    """Raised for any unrecoverable TomTom API call or response-parsing failure."""


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(d_lambda / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _classify_congestion(delay_seconds: float, travel_time_seconds: float) -> CongestionLevel:
    if travel_time_seconds <= 0:
        return CongestionLevel.LOW
    ratio = delay_seconds / travel_time_seconds
    if ratio >= CONGESTION_SEVERE_RATIO:
        return CongestionLevel.SEVERE
    if ratio >= CONGESTION_MODERATE_RATIO:
        return CongestionLevel.MODERATE
    return CongestionLevel.LOW


def _flatten_points(route: dict) -> List[Tuple[float, float]]:
    """TomTom nests polyline points per-leg: routes[].legs[].points[].{latitude,longitude}."""
    coords: List[Tuple[float, float]] = []
    for leg in route.get("legs", []):
        for pt in leg.get("points", []):
            coords.append((pt["latitude"], pt["longitude"]))
    return coords


def _route_hits_hazards(
    coords: List[Tuple[float, float]], hazards: List[Coordinate], radius_km: float = HAZARD_RADIUS_KM
) -> bool:
    # Checks every polyline vertex (not a stride-sampled subset): a hazard-safety
    # check that can silently skip a nearby vertex is worse than the negligible
    # extra cost of an O(points x hazards) haversine pass.
    if not hazards or not coords:
        return False
    for lat, lon in coords:
        for hz in hazards:
            if _haversine_km(lat, lon, hz.lat, hz.lon) <= radius_km:
                return True
    return False


def _build_locations_path(origin: Coordinate, destination: Coordinate) -> str:
    return f"{origin.lat},{origin.lon}:{destination.lat},{destination.lon}"


def _sample_points(coords: List[Tuple[float, float]], max_samples: int = 24) -> List[Tuple[float, float]]:
    if len(coords) <= max_samples:
        return coords
    step = len(coords) / max_samples
    return [coords[int(i * step)] for i in range(max_samples)]


def _elevation_gradient_pct(
    points: List[Tuple[float, float]], elevations: Optional[List[float]]
) -> Tuple[Optional[float], Optional[int]]:
    """Steepest grade (%) between consecutive sampled points, from real elevation
    data, plus the index of that steepest gap (i.e. between elevations[index]
    and elevations[index+1]) - lets the UI point at exactly where the risky
    climb is instead of only showing one number for the whole corridor."""
    if not elevations or len(elevations) < 2:
        return None, None
    steepest = 0.0
    steepest_index = None
    for i in range(len(elevations) - 1):
        rise_m = abs(elevations[i + 1] - elevations[i])
        run_km = _haversine_km(points[i][0], points[i][1], points[i + 1][0], points[i + 1][1])
        if run_km <= 0:
            continue
        grade = (rise_m / (run_km * 1000.0)) * 100.0
        if grade > steepest:
            steepest = grade
            steepest_index = i
    return round(min(steepest, 60.0), 1), steepest_index


async def _call_tomtom(
    locations_path: str,
    request: RouteRequest,
    api_key: str,
    base_url: str,
    timeout_seconds: float,
) -> dict:
    params: dict = {
        "key": api_key,
        "traffic": "true",
        "travelMode": "truck",
        "maxAlternatives": MAX_ALTERNATIVES,
    }
    if request.avoid_unpaved:
        params["avoid"] = "unpavedRoads"
    if request.depart_at:
        params["departAt"] = request.depart_at

    url = f"{base_url}/{locations_path}/json"

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.get(url, params=params)
    except httpx.TimeoutException as exc:
        raise RoutingServiceError("TomTom routing request timed out") from exc
    except httpx.HTTPError as exc:
        raise RoutingServiceError(f"TomTom routing request failed: {exc}") from exc

    if resp.status_code != 200:
        raise RoutingServiceError(f"TomTom API returned HTTP {resp.status_code}: {resp.text[:300]}")

    try:
        data = resp.json()
    except ValueError as exc:
        raise RoutingServiceError("TomTom API returned a non-JSON response") from exc

    if not data.get("routes"):
        raise RoutingServiceError("TomTom API returned no routes for the given locations")

    return data


async def calculate_route(request: RouteRequest) -> RouteResponse:
    """
    Compute a live-traffic truck route between origin and destination.

    Every TomTom candidate route (primary + up to MAX_ALTERNATIVES) is scored
    by all 4 hazard models (landslide, earthquake, flood, cyclone - see
    multi_hazard.py), fed live weather at the corridor's midpoint and this
    app's own persisted hazard/SOS history. Hazard-free candidates are
    preferred, and among those the UNIFIED score (worst of the 4 hazards,
    not landslide alone) picks the safest - closing the loop from "detect
    risk" to "actually choose the safest corridor" instead of only labeling
    the default TomTom pick after the fact.
    """
    settings = get_settings()
    hazards: List[Coordinate] = request.hazard_avoid_coords or []

    locations_path = _build_locations_path(request.origin, request.destination)
    data = await _call_tomtom(
        locations_path, request, settings.tomtom_api_key, settings.tomtom_base_url, settings.request_timeout_seconds
    )

    active_hazards = _active_hazard_coords()

    routes = data["routes"]
    coords_list = [_flatten_points(r) for r in routes]
    hazard_flags = [
        _route_hits_hazards(c, hazards) or _route_hits_hazards(c, active_hazards, ACTIVE_HAZARD_RADIUS_KM)
        for c in coords_list
    ]
    midpoints = [c[len(c) // 2] if c else (request.origin.lat, request.origin.lon) for c in coords_list]
    sample_sets = [_sample_points(c) for c in coords_list]

    n = len(routes)
    live_results = await asyncio.gather(
        *(get_live_rainfall(lat, lon) for lat, lon in midpoints),
        *(get_live_rainfall_72h(lat, lon) for lat, lon in midpoints),
        *(get_live_soil_moisture(lat, lon) for lat, lon in midpoints),
        *(get_live_river_discharge(lat, lon) for lat, lon in midpoints),
        *(get_live_wind_pressure(lat, lon) for lat, lon in midpoints),
        *(get_elevations(pts) for pts in sample_sets),
    )
    (
        rainfall_24h_values, rainfall_72h_values, soil_moisture_values,
        discharge_values, wind_pressure_values, elevation_sets,
    ) = (live_results[i * n:(i + 1) * n] for i in range(6))
    gradient_results = [_elevation_gradient_pct(pts, elevs) for pts, elevs in zip(sample_sets, elevation_sets)]
    gradient_values = [g for g, _ in gradient_results]
    steepest_indices = [idx for _, idx in gradient_results]
    midpoint_elevations = [elevs[0] if elevs else None for elevs in elevation_sets]

    multi_risks = [
        evaluate_corridor(
            c, hazards,
            live_rainfall_24h_mm=rain24, live_rainfall_72h_mm=rain72,
            live_elevation_gradient_pct=grad, live_elevation_m=mid_elev,
            live_soil_moisture_idx=soil, live_river_discharge_m3s=discharge,
            live_wind_kmh=wind_pressure[0] if wind_pressure else None,
            live_pressure_hpa=wind_pressure[1] if wind_pressure else None,
        )
        for c, rain24, rain72, grad, mid_elev, soil, discharge, wind_pressure in zip(
            coords_list, rainfall_24h_values, rainfall_72h_values, gradient_values,
            midpoint_elevations, soil_moisture_values, discharge_values, wind_pressure_values,
        )
    ]

    candidates = [
        {"route": r, "coords": c, "hazard": h, "risk": risk, "elevations": elevs, "gradient_pct": grad, "steepest_index": idx}
        for r, c, h, risk, elevs, grad, idx in zip(routes, coords_list, hazard_flags, multi_risks, elevation_sets, gradient_values, steepest_indices)
    ]

    primary = candidates[0]
    # Prefer hazard-free candidates; the unified multi-hazard score then
    # selects the safest of those (falling back to the full pool if every
    # candidate is hazardous).
    pool = [c for c in candidates if not c["hazard"]] or candidates
    best = max(pool, key=lambda c: c["risk"]["overall_risk_score"])

    rerouted = best is not primary and primary["hazard"]
    ai_selected_safest = best is not primary and not primary["hazard"]

    if primary["hazard"] and best is primary:
        logger.warning(
            "Primary route %s -> %s intersects a hazard zone and no hazard-clear "
            "TomTom alternative was available; returning primary route flagged as hazardous.",
            request.origin,
            request.destination,
        )

    summary = best["route"]["summary"]
    travel_time_seconds = summary["travelTimeInSeconds"]
    delay_seconds = summary.get("trafficDelayInSeconds", 0)
    risk = best["risk"]
    breakdown = risk["hazard_breakdown"]

    return RouteResponse(
        distance_km=round(summary["lengthInMeters"] / 1000.0, 2),
        travel_time_minutes=round(travel_time_seconds / 60.0, 1),
        traffic_delay_minutes=round(delay_seconds / 60.0, 1),
        congestion_level=_classify_congestion(delay_seconds, travel_time_seconds),
        coordinates=best["coords"],
        hazard_detected=primary["hazard"],
        rerouted=rerouted,
        ai_safety_score=risk["overall_risk_score"],
        ai_risk_level=risk["overall_risk_level"],
        risk_factors=breakdown[risk["primary_hazard"]]["risk_factors"],
        risk_segment=breakdown[risk["primary_hazard"]]["risk_segment"],
        primary_hazard=risk["primary_hazard"],
        hazard_breakdown=HazardBreakdown(**breakdown),
        ai_route_label="AI Selected: Safest Corridor" if ai_selected_safest else None,
        elevation_profile=[round(e, 1) for e in best["elevations"]] if best["elevations"] else [],
        max_gradient_pct=best["gradient_pct"] if best["gradient_pct"] is not None else 0.0,
        steepest_segment_index=best["steepest_index"],
    )
