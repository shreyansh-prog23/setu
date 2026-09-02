"""
Combines all 4 hazard engines (landslide, earthquake, flood, cyclone) into
one corridor-level risk verdict.

overall_risk_score is the MINIMUM safety score across the 4 hazards, not an
average - a corridor is only as safe as its worst hazard (a route that's
perfectly clear of landslide/earthquake/flood risk but sits in a live
cyclone's path is not "75% safe on average", it's dangerous right now).
primary_hazard names whichever hazard produced that minimum, so the UI can
say *why* a corridor is flagged, not just that it is.

Each hazard's own assess_*_risk() already degrades gracefully if its model
failed to load (a rule-based fallback, or in cyclone's case, no model at
all - see each engine's docstring) - a hazard is only skipped entirely here
if its engine failed to load AT ALL (a missing/corrupt joblib with no
fallback path), which keeps a partial data-source outage from taking down
every hazard's assessment.

Known limitation: each hazard's safety_score comes from an independently
trained classifier with its own class boundaries, so the numeric scores
aren't perfectly calibrated against each other - e.g. a flood MODERATE at
28.6 can rank "worse" (lower score) than a cyclone HIGH at 47.0, since
flood's decision boundaries sit at different score thresholds than
cyclone's. min(safety_score) is still directionally correct *within* the
picture of "which hazard is this specific corridor's biggest problem right
now", just not a perfectly apples-to-apples comparison across hazards.
"""
from __future__ import annotations

import asyncio
from typing import List, Optional, Tuple

import cyclone_engine
import earthquake_engine
import flood_engine
import risk_engine
from ml.weather import (
    get_elevations,
    get_live_rainfall,
    get_live_rainfall_72h,
    get_live_river_discharge,
    get_live_soil_moisture,
    get_live_wind_pressure,
    get_rainfall_forecast,
    get_wind_forecast_peak,
)

TREND_SAFETY_SCORE_THRESHOLD = 4.0  # min change in ai_safety_score (0-100) to call it RISING/FALLING instead of STABLE - keeps ordinary noise from reading as a trend
_TREND_CAPABLE_HAZARDS = ("landslide", "flood", "cyclone")  # earthquake excluded below - no meaningful weather-based forecast for a fault-line hazard
from schemas import Coordinate


def evaluate_corridor(
    coords: List[Tuple[float, float]],
    hazards: List[Coordinate],
    live_rainfall_24h_mm: Optional[float] = None,
    live_rainfall_72h_mm: Optional[float] = None,
    live_elevation_gradient_pct: Optional[float] = None,
    live_elevation_m: Optional[float] = None,
    live_soil_moisture_idx: Optional[float] = None,
    live_river_discharge_m3s: Optional[float] = None,
    live_wind_kmh: Optional[float] = None,
    live_pressure_hpa: Optional[float] = None,
) -> dict:
    """Returns {overall_risk_score, overall_risk_level, primary_hazard, hazard_breakdown}."""
    if not coords:
        coords = [(26.1445, 91.7362)]  # Guwahati - degenerate/empty-route fallback, same as risk_engine.py
    mid_lat, mid_lon = coords[len(coords) // 2]

    breakdown = {
        "landslide": risk_engine.assess_route_risk(
            coords, hazards,
            live_rainfall_mm=live_rainfall_24h_mm,
            live_elevation_gradient_pct=live_elevation_gradient_pct,
            live_soil_moisture_idx=live_soil_moisture_idx,
        ),
        "earthquake": earthquake_engine.assess_earthquake_risk(mid_lat, mid_lon, coords=coords),
        "flood": flood_engine.assess_flood_risk(
            mid_lat, mid_lon,
            live_rainfall_72h_mm=live_rainfall_72h_mm,
            live_river_discharge_m3s=live_river_discharge_m3s,
            live_elevation_m=live_elevation_m,
            coords=coords,
        ),
        "cyclone": cyclone_engine.assess_cyclone_risk(
            mid_lat, mid_lon,
            live_wind_kmh=live_wind_kmh,
            live_pressure_hpa=live_pressure_hpa,
        ),
    }

    primary_hazard, worst = min(breakdown.items(), key=lambda kv: kv[1]["ai_safety_score"])

    return {
        "overall_risk_score": worst["ai_safety_score"],
        "overall_risk_level": worst["ai_risk_level"],
        "primary_hazard": primary_hazard,
        "hazard_breakdown": breakdown,
    }


async def evaluate_point(lat: float, lon: float) -> dict:
    """Single-point version of evaluate_corridor, for the "you're entering a
    hazard-prone area" live GPS check (see /api/hazard-check in main.py) -
    no TomTom call needed, just the same live weather fetches routing.py
    already makes for one corridor midpoint, applied to exactly this point."""
    (
        rain24, rain72, soil, discharge, wind_pressure, elevs,
    ) = await asyncio.gather(
        get_live_rainfall(lat, lon),
        get_live_rainfall_72h(lat, lon),
        get_live_soil_moisture(lat, lon),
        get_live_river_discharge(lat, lon),
        get_live_wind_pressure(lat, lon),
        get_elevations([(lat, lon)]),
    )
    wind_kmh, pressure_hpa = wind_pressure if wind_pressure else (None, None)
    elevation_m = elevs[0] if elevs else None

    return evaluate_corridor(
        [(lat, lon)], [],
        live_rainfall_24h_mm=rain24,
        live_rainfall_72h_mm=rain72,
        live_elevation_gradient_pct=None,  # a single point has no gradient - landslide falls back to its winding-ratio proxy
        live_elevation_m=elevation_m,
        live_soil_moisture_idx=soil,
        live_river_discharge_m3s=discharge,
        live_wind_kmh=wind_kmh,
        live_pressure_hpa=pressure_hpa,
    )


def _classify_trend(current_score: float, projected_score: float) -> str:
    delta = projected_score - current_score
    if delta <= -TREND_SAFETY_SCORE_THRESHOLD:
        return "RISING"  # projected safety score meaningfully lower - getting worse
    if delta >= TREND_SAFETY_SCORE_THRESHOLD:
        return "FALLING"  # meaningfully higher - improving
    return "STABLE"


async def evaluate_point_with_trend(lat: float, lon: float) -> dict:
    """evaluate_point, plus a near-term (6h) trend per weather-driven hazard
    (landslide/flood/cyclone) - "is this getting worse or better", not just
    "what is it right now". Reuses evaluate_corridor completely unchanged:
    the "projected" verdict is just a second call to it with forecast-
    shifted rainfall/wind in place of current live values, scored by the
    exact same engines as the current verdict.

    Earthquake always gets trend=None - a fault-line hazard has no
    meaningful weather-based forecast, and that's deliberate, not a gap:
    claiming otherwise would be exactly the kind of overclaim a disaster-
    response system shouldn't make (see earthquake_engine.py's own
    reasoning for why it uses fault proximity, not incident recency).

    Only used by the point-check path (/api/hazard-check, DriverView's live
    GPS watch) - deliberately NOT wired into corridor/route scoring
    (routing.py). That path already has a documented history of an event-
    loop-freeze incident from too many concurrent weather calls per route
    candidate (see get_elevations' docstring in ml/weather.py); doubling its
    weather calls to add trend risks the same class of bug. A single point,
    already throttled client-side to once per 2 minutes, is safe for the
    extra concurrent forecast calls this adds.
    """
    (
        rain24, rain72, soil, discharge, wind_pressure, elevs,
        forecast_rain6h, forecast_wind_peak,
    ) = await asyncio.gather(
        get_live_rainfall(lat, lon),
        get_live_rainfall_72h(lat, lon),
        get_live_soil_moisture(lat, lon),
        get_live_river_discharge(lat, lon),
        get_live_wind_pressure(lat, lon),
        get_elevations([(lat, lon)]),
        get_rainfall_forecast(lat, lon),
        get_wind_forecast_peak(lat, lon),
    )
    wind_kmh, pressure_hpa = wind_pressure if wind_pressure else (None, None)
    elevation_m = elevs[0] if elevs else None

    current = evaluate_corridor(
        [(lat, lon)], [],
        live_rainfall_24h_mm=rain24, live_rainfall_72h_mm=rain72,
        live_elevation_gradient_pct=None, live_elevation_m=elevation_m,
        live_soil_moisture_idx=soil, live_river_discharge_m3s=discharge,
        live_wind_kmh=wind_kmh, live_pressure_hpa=pressure_hpa,
    )

    # Rainfall accumulates, so "projected" = current window + incoming rain.
    # Wind doesn't accumulate - the forecast peak IS the projected reading,
    # not something added to the current one.
    projected_rain24 = rain24 + forecast_rain6h if rain24 is not None and forecast_rain6h is not None else None
    projected_rain72 = rain72 + forecast_rain6h if rain72 is not None and forecast_rain6h is not None else None
    projected_wind_kmh, projected_pressure_hpa = forecast_wind_peak if forecast_wind_peak else (None, None)

    projected = evaluate_corridor(
        [(lat, lon)], [],
        live_rainfall_24h_mm=projected_rain24, live_rainfall_72h_mm=projected_rain72,
        live_elevation_gradient_pct=None, live_elevation_m=elevation_m,
        live_soil_moisture_idx=soil, live_river_discharge_m3s=discharge,
        live_wind_kmh=projected_wind_kmh, live_pressure_hpa=projected_pressure_hpa,
    )

    have_forecast = {
        "landslide": projected_rain24 is not None,
        "flood": projected_rain72 is not None,
        "cyclone": projected_wind_kmh is not None,
    }

    for hazard, entry in current["hazard_breakdown"].items():
        if hazard in _TREND_CAPABLE_HAZARDS and have_forecast.get(hazard):
            projected_score = projected["hazard_breakdown"][hazard]["ai_safety_score"]
            entry["trend"] = _classify_trend(entry["ai_safety_score"], projected_score)
            entry["projected_safety_score"] = projected_score
        else:
            entry["trend"] = None
            entry["projected_safety_score"] = None

    return current
