"""
Earthquake risk assessment for a corridor point - wraps the
RandomForestClassifier trained offline by ml/train_earthquake_risk_model.py.

Unlike landslide/flood, every feature here comes from static local files
already downloaded once (india_usgs_earthquakes.csv, india_active_faults.json,
india_seismic_zones.json - see ml/fetch_earthquake_catalog.py and
ml/fetch_earthquake_geophysical.py) rather than a live API call per request -
seismic history, fault proximity, and BIS zoning don't change minute to
minute, so there's nothing to fetch live. The one real limitation: "recent
activity" reflects the catalog's last fetch date, not literally this second -
acceptable for a corridor-planning tool, not for real-time seismic monitoring.

Mirrors risk_engine.py's shape: assess_earthquake_risk() returns
{ai_safety_score, ai_risk_level, risk_factors} for a single point (a
corridor's midpoint, same sampling risk_engine.py's live rainfall lookup uses -
earthquake hazard doesn't vary meaningfully across one highway corridor's
length the way rainfall does, so one point is a reasonable proxy for the
whole route).
"""
from __future__ import annotations

import csv
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Tuple

import joblib
import numpy as np

logger = logging.getLogger("earthquake_engine")

CSV_PATH = Path(__file__).parent / "india_usgs_earthquakes.csv"
FAULTS_PATH = Path(__file__).parent / "india_active_faults.json"
ZONES_PATH = Path(__file__).parent / "india_seismic_zones.json"
MODEL_PATH = Path(__file__).parent / "ml" / "earthquake_risk_model.joblib"

SIGNIFICANT_MAGNITUDE = 5.5
RADIUS_KM = 100.0
RECENT_WINDOW_DAYS = 30
MAX_DAYS_SINCE_MAJOR = 3650.0
MIN_HISTORY_YEARS = 1.0
DEFAULT_ZONE_FACTOR = 0.16

FEATURE_NAMES = [
    "local_seismic_density",
    "max_magnitude_nearby",
    "avg_depth_km",
    "recent_activity_rate_30d",
    "days_since_major_quake",
    "fault_dist_km",
    "seismic_zone_factor",
]

RISK_LABELS = {0: "SAFE", 1: "MODERATE", 2: "HIGH_EARTHQUAKE_RISK"}

FEATURE_DRIVER_LABELS = {
    "local_seismic_density": "Historical Seismic Density",
    "max_magnitude_nearby": "Nearby Major Quake History",
    "avg_depth_km": "Regional Quake Depth",
    "recent_activity_rate_30d": "Recent Seismic Activity",
    "days_since_major_quake": "Time Since Last Major Quake",
    "fault_dist_km": "Active Fault Proximity",
    "seismic_zone_factor": "BIS Seismic Zone Rating",
}

_model = None
_feature_importances: Optional[np.ndarray] = None
_high_risk_threshold = 0.35


def load_earthquake_model() -> bool:
    """Loads the trained model (+ its saved decision threshold) at API
    startup. A missing/unreadable model disables this hazard's scoring
    rather than failing startup - main.py logs a warning and the multi-hazard
    engine skips earthquake in that case."""
    global _model, _feature_importances, _high_risk_threshold
    try:
        bundle = joblib.load(MODEL_PATH)
        _model = bundle["model"]
        _feature_importances = np.asarray(_model.feature_importances_)
        _high_risk_threshold = bundle.get("high_risk_decision_threshold", 0.35)
        logger.info("Earthquake risk model loaded from %s", MODEL_PATH)
        return True
    except Exception as exc:
        _model = None
        _feature_importances = None
        logger.warning(
            "Earthquake risk model unavailable (%s) - earthquake hazard will be skipped. "
            "Run `python ml/train_earthquake_risk_model.py` to generate %s.",
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
def _load_catalog() -> List[dict]:
    try:
        with open(CSV_PATH, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        events = []
        for r in rows:
            events.append({
                "time": datetime.fromtimestamp(int(r["time"]) / 1000.0, tz=timezone.utc),
                "lat": float(r["latitude"]),
                "lon": float(r["longitude"]),
                "depth_km": float(r["depth_km"]),
                "mag": float(r["magnitude"]),
            })
        events.sort(key=lambda e: e["time"])
        return events
    except Exception as exc:
        logger.warning("USGS earthquake catalog load failed (%s): %s", CSV_PATH, exc)
        return []


@lru_cache
def _load_faults() -> List[List[Tuple[float, float]]]:
    try:
        return json.loads(FAULTS_PATH.read_text())
    except Exception as exc:
        logger.warning("Active fault dataset load failed (%s): %s", FAULTS_PATH, exc)
        return []


@lru_cache
def _load_zones() -> List[dict]:
    try:
        zones = json.loads(ZONES_PATH.read_text())
    except Exception as exc:
        logger.warning("Seismic zone dataset load failed (%s): %s", ZONES_PATH, exc)
        return []
    for z in zones:
        lats = [p[0] for p in z["ring"]]
        lons = [p[1] for p in z["ring"]]
        z["bbox"] = (min(lats), max(lats), min(lons), max(lons))
        z["centroid"] = (sum(lats) / len(lats), sum(lons) / len(lons))
    return zones


def _fault_dist_km(lat: float, lon: float) -> float:
    faults = _load_faults()
    if not faults:
        return 500.0  # no fault data loaded - a large, clearly-a-fallback distance rather than a false "very close"
    return min(_haversine_km(lat, lon, flat, flon) for line in faults for flat, flon in line)


_LOCALIZE_MAX_SAMPLES = 40  # bounded sample, not the full polyline - see risk_engine.py's _nearby_incident_count for why
_FAULT_PROXIMITY_KM = 50.0  # "close enough to a fault to call this stretch the risky one"


def _sample_for_localize(coords: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    if len(coords) <= _LOCALIZE_MAX_SAMPLES:
        return coords
    step = len(coords) / _LOCALIZE_MAX_SAMPLES
    return [coords[int(i * step)] for i in range(_LOCALIZE_MAX_SAMPLES)]


def _localize_earthquake_risk(coords: List[Tuple[float, float]]) -> Optional[dict]:
    """Same technique as risk_engine.py's landslide localization and
    flood_engine.py's flood localization, but against real FAULT LINES
    (GEM GAF-DB) rather than past recorded quakes - a fault is the actual
    structural hazard (it's still there whether or not it has ruptured
    recently), so "closest to a known fault" localizes the risky stretch
    more meaningfully here than "closest to where a quake once struck"."""
    faults = _load_faults()
    if not coords or not faults:
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
        nearest = min((_haversine_km(lat, lon, flat, flon) for line in faults for flat, flon in line), default=None)
        if nearest is not None and nearest <= _FAULT_PROXIMITY_KM and (best_dist is None or nearest < best_dist):
            best_dist, best_km = nearest, cumulative[i]
    if best_km is None:
        return None
    return {"km_from_origin": round(best_km), "fraction": round(best_km / total_km, 3)}


def _point_in_ring(lat: float, lon: float, ring: List[Tuple[float, float]]) -> bool:
    inside = False
    n = len(ring)
    for i in range(n):
        lat1, lon1 = ring[i]
        lat2, lon2 = ring[(i + 1) % n]
        if (lat1 > lat) != (lat2 > lat):
            x_intersect = lon1 + (lat - lat1) / (lat2 - lat1) * (lon2 - lon1)
            if lon < x_intersect:
                inside = not inside
    return inside


def _seismic_zone_factor(lat: float, lon: float) -> float:
    zones = _load_zones()
    candidates = [z for z in zones if z["bbox"][0] <= lat <= z["bbox"][1] and z["bbox"][2] <= lon <= z["bbox"][3]]
    for z in candidates:
        if _point_in_ring(lat, lon, z["ring"]):
            return z["z_factor"]
    if zones:
        nearest = min(zones, key=lambda z: _haversine_km(lat, lon, *z["centroid"]))
        return nearest["z_factor"]
    return DEFAULT_ZONE_FACTOR


def estimate_earthquake_features(lat: float, lon: float) -> dict:
    events = _load_catalog()
    now = datetime.now(timezone.utc)
    nearby = [e for e in events if _haversine_km(lat, lon, e["lat"], e["lon"]) <= RADIUS_KM]

    catalog_start = events[0]["time"] if events else now - timedelta(days=365)
    history_years = max((now - catalog_start).days / 365.25, MIN_HISTORY_YEARS)
    local_seismic_density = round(len(nearby) / history_years, 3)

    max_magnitude_nearby = round(max((e["mag"] for e in nearby), default=0.0), 2)
    avg_depth_km = round(sum(e["depth_km"] for e in nearby) / len(nearby), 1) if nearby else 35.0

    recent_cutoff = now - timedelta(days=RECENT_WINDOW_DAYS)
    recent_activity_rate_30d = sum(1 for e in nearby if e["time"] >= recent_cutoff)

    major_nearby = [e for e in nearby if e["mag"] >= SIGNIFICANT_MAGNITUDE]
    if major_nearby:
        days_since_major_quake = min((now - max(e["time"] for e in major_nearby)).days, MAX_DAYS_SINCE_MAJOR)
    else:
        days_since_major_quake = MAX_DAYS_SINCE_MAJOR

    return {
        "local_seismic_density": local_seismic_density,
        "max_magnitude_nearby": max_magnitude_nearby,
        "avg_depth_km": avg_depth_km,
        "recent_activity_rate_30d": recent_activity_rate_30d,
        "days_since_major_quake": days_since_major_quake,
        "fault_dist_km": round(_fault_dist_km(lat, lon), 1),
        "seismic_zone_factor": _seismic_zone_factor(lat, lon),
    }


def _rule_based_fallback(features: dict) -> Tuple[int, np.ndarray]:
    """Used only if earthquake_risk_model.joblib hasn't been trained/loaded yet."""
    score = (
        min(features["local_seismic_density"] / 20.0, 1.0) * 0.25
        + min(features["fault_dist_km"] / 200.0, 1.0) * -0.25 + 0.25  # closer fault -> higher score contribution
        + features["seismic_zone_factor"] / 0.36 * 0.25
        + (1.0 if features["days_since_major_quake"] < 365 else 0.0) * 0.25
    )
    if score < 0.35:
        return 0, np.array([0.8, 0.15, 0.05])
    if score < 0.62:
        return 1, np.array([0.2, 0.6, 0.2])
    return 2, np.array([0.05, 0.25, 0.7])


def assess_earthquake_risk(lat: float, lon: float, coords: Optional[List[Tuple[float, float]]] = None) -> dict:
    """Returns {ai_safety_score, ai_risk_level, risk_factors, risk_segment}
    for a corridor point. `coords` (the route's polyline, if passed) is used
    only for risk_segment localization - the live features above are still
    evaluated at the single (lat, lon) point, same as before."""
    features = estimate_earthquake_features(lat, lon)
    vector = [features[name] for name in FEATURE_NAMES]

    if _model is not None:
        proba = _model.predict_proba([vector])[0]
        predicted = 2 if proba[2] >= _high_risk_threshold else int(np.argmax(proba[:2]))
        importances = _feature_importances
    else:
        predicted, proba = _rule_based_fallback(features)
        importances = np.array([0.2, 0.15, 0.05, 0.15, 0.1, 0.2, 0.15])

    safety_score = round(float(proba[0] * 100 + proba[1] * 50), 1)

    norms = {
        "local_seismic_density": min(features["local_seismic_density"] / 20.0, 1.0),
        "max_magnitude_nearby": features["max_magnitude_nearby"] / 8.0,
        "avg_depth_km": 1.0 - min(features["avg_depth_km"] / 70.0, 1.0),  # shallower -> higher surface-risk contribution
        "recent_activity_rate_30d": min(features["recent_activity_rate_30d"] / 10.0, 1.0),
        "days_since_major_quake": 1.0 - min(features["days_since_major_quake"] / MAX_DAYS_SINCE_MAJOR, 1.0),
        "fault_dist_km": 1.0 - min(features["fault_dist_km"] / 200.0, 1.0),
        "seismic_zone_factor": features["seismic_zone_factor"] / 0.36,
    }
    contributions = sorted(
        ((name, importances[i] * norms[name]) for i, name in enumerate(FEATURE_NAMES)),
        key=lambda kv: kv[1],
        reverse=True,
    )
    risk_factors = [FEATURE_DRIVER_LABELS[name] for name, contrib in contributions[:2] if contrib > 0]

    risk_segment = _localize_earthquake_risk(coords) if predicted > 0 and coords else None

    return {
        "ai_safety_score": safety_score,
        "ai_risk_level": RISK_LABELS[predicted],
        "risk_factors": risk_factors,
        "risk_segment": risk_segment,
    }
