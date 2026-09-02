"""
Flood risk assessment for a corridor point - wraps the RandomForestClassifier
trained offline by ml/train_flood_risk_model.py.

rainfall_mm_72h and river_discharge_m3s come from live Open-Meteo lookups
(same vendor as risk_engine.py's landslide rainfall, reusing the archive API
with a wider 72h window plus the dedicated flood-discharge API).
elevation_m is reused from whatever routing.py already fetched for the
landslide model's gradient calculation - no extra API call needed.
historical_flood_density comes from the real Dartmouth Flood Observatory
catalog (india_dfo_floods.csv - the same dataset the model was trained on).
"""
from __future__ import annotations

import csv
import logging
import math
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Tuple

import joblib
import numpy as np

logger = logging.getLogger("flood_engine")

CSV_PATH = Path(__file__).parent / "india_dfo_floods.csv"
MODEL_PATH = Path(__file__).parent / "ml" / "flood_risk_model.joblib"

NEARBY_EVENT_RADIUS_KM = 50.0
REFERENCE_ROUTE_KM = 200.0
MONSOON_MONTHS = {6, 7, 8, 9}
DEFAULT_ELEVATION_M = 200.0  # used only if the caller has no live elevation reading at all

FEATURE_NAMES = [
    "rainfall_mm_72h",
    "river_discharge_m3s",
    "elevation_m",
    "historical_flood_density",
    "monsoon_month",
]

RISK_LABELS = {0: "SAFE", 1: "MODERATE", 2: "HIGH_FLOOD_RISK"}

FEATURE_DRIVER_LABELS = {
    "rainfall_mm_72h": "72h Rainfall",
    "river_discharge_m3s": "River Discharge",
    "elevation_m": "Low Elevation / Floodplain",
    "historical_flood_density": "Historical Flood Density",
    "monsoon_month": "Active Monsoon Conditions",
}

_model = None
_feature_importances: Optional[np.ndarray] = None


def load_flood_model() -> bool:
    global _model, _feature_importances
    try:
        bundle = joblib.load(MODEL_PATH)
        _model = bundle["model"]
        _feature_importances = np.asarray(_model.feature_importances_)
        logger.info("Flood risk model loaded from %s", MODEL_PATH)
        return True
    except Exception as exc:
        _model = None
        _feature_importances = None
        logger.warning(
            "Flood risk model unavailable (%s) - flood hazard will be skipped. "
            "Run `python ml/train_flood_risk_model.py` to generate %s.",
            exc,
            MODEL_PATH,
        )
        return False


def is_loaded() -> bool:
    return _model is not None


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(d_lambda / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


@lru_cache
def _flood_points() -> List[Tuple[float, float]]:
    try:
        with open(CSV_PATH, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        return [(float(r["latitude"]), float(r["longitude"])) for r in rows]
    except Exception as exc:
        logger.warning("DFO flood catalog load failed (%s): %s", CSV_PATH, exc)
        return []


def _historical_flood_density(lat: float, lon: float) -> float:
    points = _flood_points()
    hits = sum(1 for plat, plon in points if _haversine_km(lat, lon, plat, plon) <= NEARBY_EVENT_RADIUS_KM)
    return round((hits / REFERENCE_ROUTE_KM) * 100.0, 2)


_LOCALIZE_MAX_SAMPLES = 40  # bounded sample, not the full polyline - see risk_engine.py's _nearby_incident_count for why


def _sample_for_localize(coords: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    if len(coords) <= _LOCALIZE_MAX_SAMPLES:
        return coords
    step = len(coords) / _LOCALIZE_MAX_SAMPLES
    return [coords[int(i * step)] for i in range(_LOCALIZE_MAX_SAMPLES)]


def _localize_flood_risk(coords: List[Tuple[float, float]]) -> Optional[dict]:
    """Same technique as risk_engine.py's _localize_landslide_risk: a single
    midpoint reading gets applied to the WHOLE corridor otherwise, which for
    a long route means one river crossing's discharge tars 175km of highway
    with the same score. Finds where along the route it passes closest to a
    real recorded DFO flood, so a HIGH/MODERATE verdict can point at the
    actual risky stretch instead of the whole corridor."""
    points = _flood_points()
    if not coords or not points:
        return None
    sampled = _sample_for_localize(coords)
    if len(sampled) < 2:
        return None
    cumulative = [0.0]
    for i in range(1, len(sampled)):
        cumulative.append(cumulative[-1] + _haversine_km(*sampled[i - 1], *sampled[i]))
    total_km = cumulative[-1] if cumulative[-1] > 0 else 1.0

    best_km, best_dist = None, None
    for i, (lat, lon) in enumerate(sampled):
        nearest = min((_haversine_km(lat, lon, plat, plon) for plat, plon in points), default=None)
        if nearest is not None and nearest <= NEARBY_EVENT_RADIUS_KM and (best_dist is None or nearest < best_dist):
            best_dist, best_km = nearest, cumulative[i]
    if best_km is None:
        return None
    return {"km_from_origin": round(best_km), "fraction": round(best_km / total_km, 3)}


def estimate_flood_features(
    lat: float, lon: float,
    live_rainfall_72h_mm: Optional[float] = None,
    live_river_discharge_m3s: Optional[float] = None,
    live_elevation_m: Optional[float] = None,
) -> dict:
    now_month = datetime.now().month
    monsoon_month = 1 if now_month in MONSOON_MONTHS else 0

    rainfall = max(0.0, live_rainfall_72h_mm) if live_rainfall_72h_mm is not None else (120.0 if monsoon_month else 20.0)
    discharge = max(0.0, live_river_discharge_m3s) if live_river_discharge_m3s is not None else 10.0
    elevation = live_elevation_m if live_elevation_m is not None else DEFAULT_ELEVATION_M

    return {
        "rainfall_mm_72h": round(rainfall, 1),
        "river_discharge_m3s": round(discharge, 2),
        "elevation_m": round(elevation, 1),
        "historical_flood_density": _historical_flood_density(lat, lon),
        "monsoon_month": monsoon_month,
    }


def _rule_based_fallback(features: dict) -> Tuple[int, np.ndarray]:
    """Used only if flood_risk_model.joblib hasn't been trained/loaded yet."""
    score = (
        min(features["rainfall_mm_72h"] / 200.0, 1.0) * 0.35
        + min(features["river_discharge_m3s"] / 150.0, 1.0) * 0.4
        + max(0.0, 1.0 - features["elevation_m"] / 1000.0) * 0.15
        + min(features["historical_flood_density"] / 2.0, 1.0) * 0.1
    )
    if score < 0.35:
        return 0, np.array([0.8, 0.15, 0.05])
    if score < 0.62:
        return 1, np.array([0.2, 0.6, 0.2])
    return 2, np.array([0.05, 0.25, 0.7])


def assess_flood_risk(
    lat: float, lon: float,
    live_rainfall_72h_mm: Optional[float] = None,
    live_river_discharge_m3s: Optional[float] = None,
    live_elevation_m: Optional[float] = None,
    coords: Optional[List[Tuple[float, float]]] = None,
) -> dict:
    """Returns {ai_safety_score, ai_risk_level, risk_factors, risk_segment}
    for a corridor point. `coords` (the route's polyline, if passed) is used
    only for risk_segment localization - the live features above are still
    evaluated at the single (lat, lon) point, same as before."""
    features = estimate_flood_features(lat, lon, live_rainfall_72h_mm, live_river_discharge_m3s, live_elevation_m)
    vector = [features[name] for name in FEATURE_NAMES]

    if _model is not None:
        proba = _model.predict_proba([vector])[0]
        predicted = int(np.argmax(proba))
        importances = _feature_importances
    else:
        predicted, proba = _rule_based_fallback(features)
        importances = np.array([0.35, 0.4, 0.15, 0.1, 0.0])

    safety_score = round(float(proba[0] * 100 + proba[1] * 50), 1)

    norms = {
        "rainfall_mm_72h": min(features["rainfall_mm_72h"] / 200.0, 1.0),
        "river_discharge_m3s": min(features["river_discharge_m3s"] / 150.0, 1.0),
        "elevation_m": max(0.0, 1.0 - features["elevation_m"] / 1000.0),
        "historical_flood_density": min(features["historical_flood_density"] / 2.0, 1.0),
        "monsoon_month": float(features["monsoon_month"]),
    }
    contributions = sorted(
        ((name, importances[i] * norms[name]) for i, name in enumerate(FEATURE_NAMES)),
        key=lambda kv: kv[1],
        reverse=True,
    )
    risk_factors = [FEATURE_DRIVER_LABELS[name] for name, contrib in contributions[:2] if contrib > 0]

    risk_segment = _localize_flood_risk(coords) if predicted > 0 and coords else None

    return {
        "ai_safety_score": safety_score,
        "ai_risk_level": RISK_LABELS[predicted],
        "risk_factors": risk_factors,
        "risk_segment": risk_segment,
    }
