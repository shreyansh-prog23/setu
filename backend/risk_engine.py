"""
Lightweight ML Predictive Corridor Risk Engine.

Wraps the RandomForestClassifier trained offline by ml/train_risk_model.py
to score a route corridor's landslide/disruption risk from 5 terrain +
monsoon features. rainfall_mm_24h, elevation_gradient_pct, and
soil_saturation_idx all come from live Open-Meteo/Open-Topo-Data lookups
(routing.py) when available, and historical_incident_rate is computed from
real recorded history: NASA's Global Landslide Catalog (2,389 pan-India
incidents - the same dataset the model itself was trained on, see
ml/build_real_training_data.py) merged with this app's own persisted hazard
+ SOS reports in SQLite. Only the monsoon-window fallback rainfall/soil
estimates (used when a live lookup fails) remain coordinate-based heuristics.
"""
from __future__ import annotations

import csv
import logging
import math
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Tuple

import joblib
import numpy as np

import database
from schemas import Coordinate

logger = logging.getLogger("risk_engine")

NASA_LANDSLIDE_CSV_PATH = Path(__file__).parent / "india_nasa_landslides.csv"

MODEL_PATH = Path(__file__).parent / "ml" / "risk_model.joblib"

FEATURE_NAMES = [
    "rainfall_mm_24h",
    "elevation_gradient_pct",
    "soil_saturation_idx",
    "historical_incident_rate",
    "monsoon_month",
]

RISK_LABELS = {0: "SAFE", 1: "MODERATE", 2: "HIGH_LANDSLIDE_RISK"}

# Human-readable driver names surfaced in risk_factors / the UI's "key risk driver" line.
FEATURE_DRIVER_LABELS = {
    "rainfall_mm_24h": "24h Rainfall",
    "elevation_gradient_pct": "High Slope Gradient",
    "soil_saturation_idx": "Soil Saturation",
    "historical_incident_rate": "Historical Incident Density",
    "monsoon_month": "Active Monsoon Conditions",
}

_MONSOON_MONTHS = {6, 7, 8, 9}  # Jun-Sep, India's primary monsoon window

FLAT_TERRAIN_GRADIENT_THRESHOLD_PCT = 5.0  # below this, the ground is structurally too flat for a landslide, full stop - see assess_route_risk's terrain sanity override
_NEARBY_INCIDENT_RADIUS_KM = 15.0

_model = None
_feature_importances: Optional[np.ndarray] = None


def load_risk_model() -> bool:
    """Loads the trained model at API startup. A missing/unreadable model
    degrades the engine to its rule-based fallback rather than failing
    startup - run `python ml/train_risk_model.py` to generate it."""
    global _model, _feature_importances
    try:
        bundle = joblib.load(MODEL_PATH)
        _model = bundle["model"]
        _feature_importances = np.asarray(_model.feature_importances_)
        logger.info("Corridor risk model loaded from %s", MODEL_PATH)
        return True
    except Exception as exc:  # missing file, corrupt joblib, sklearn version skew, etc.
        _model = None
        _feature_importances = None
        logger.warning(
            "Corridor risk model unavailable (%s) - falling back to rule-based risk scoring. "
            "Run `python ml/train_risk_model.py` to generate %s.",
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


def _route_length_km(coords: List[Tuple[float, float]]) -> float:
    return sum(
        _haversine_km(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1])
        for i in range(len(coords) - 1)
    )


_INCIDENT_CHECK_MAX_SAMPLES = 40  # see _nearby_incident_count's docstring


def _sample_for_incident_check(coords: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    if len(coords) <= _INCIDENT_CHECK_MAX_SAMPLES:
        return coords
    step = len(coords) / _INCIDENT_CHECK_MAX_SAMPLES
    return [coords[int(i * step)] for i in range(_INCIDENT_CHECK_MAX_SAMPLES)]


def _nearby_incident_count(
    coords: List[Tuple[float, float]], points: List[Tuple[float, float]], radius_km: float = _NEARBY_INCIDENT_RADIUS_KM
) -> int:
    """Real incident (production) outage: this used to check every point
    against the FULL route polyline - fine at 320 NE-only incidents and
    short NE corridors, but the pan-India catalog (2,420 incidents) and
    long pan-India highways (a polyline can have thousands of vertices)
    multiplied the O(incidents x polyline_len) haversine cost into tens of
    millions of calls per candidate route, all synchronous - which blocked
    the entire asyncio event loop for minutes and made even unrelated
    endpoints unresponsive. A 15km-radius proximity check doesn't need
    every polyline vertex - a bounded, evenly-spaced sample is just as
    accurate for this purpose."""
    if not coords or not points:
        return 0
    sampled = _sample_for_incident_check(coords)
    hit = 0
    for plat, plon in points:
        if any(_haversine_km(lat, lon, plat, plon) <= radius_km for lat, lon in sampled):
            hit += 1
    return hit


@lru_cache
def _nasa_landslide_points() -> List[Tuple[float, float]]:
    """(lat, lon) of every recorded incident in NASA's Global Landslide
    Catalog for pan-India - loaded once, since the file doesn't change at
    runtime. Same source used to train the model itself."""
    try:
        with open(NASA_LANDSLIDE_CSV_PATH, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        points = []
        for r in rows:
            try:
                points.append((float(r["latitude"]), float(r["longitude"])))
            except (ValueError, KeyError):
                continue
        return points
    except Exception as exc:
        logger.warning("NASA landslide catalog load failed (%s): %s", NASA_LANDSLIDE_CSV_PATH, exc)
        return []


def _historical_incident_points() -> List[Tuple[float, float]]:
    """All-time (lat, lon) of every real recorded NASA landslide across
    India, plus every hazard/SOS record this app has persisted to SQLite -
    real accumulated history, not a simulated placeholder."""
    try:
        app_points = database.get_incident_points()
    except Exception as exc:
        logger.warning("Historical incident lookup from database failed: %s", exc)
        app_points = []
    return _nasa_landslide_points() + app_points


def estimate_corridor_features(
    coords: List[Tuple[float, float]],
    hazards: List[Coordinate],
    live_rainfall_mm: Optional[float] = None,
    live_elevation_gradient_pct: Optional[float] = None,
    live_soil_moisture_idx: Optional[float] = None,
) -> dict:
    """
    Derives the model's 5 input features from the route corridor:

      - monsoon_month: whether "now" falls in India's Jun-Sep monsoon window
      - rainfall_mm_24h: live Open-Meteo precipitation when available
        (live_rainfall_mm), else a monsoon-scaled heuristic jittered per-corridor
      - elevation_gradient_pct: live Open-Meteo elevation-derived steepest grade
        when available (live_elevation_gradient_pct), else a winding-ratio proxy
      - soil_saturation_idx: live Open-Meteo soil_moisture_0_to_7cm when
        available (live_soil_moisture_idx), else a rainfall+monsoon-derived proxy
      - historical_incident_rate: this app's own persisted hazard/SOS history
        (SQLite) near the corridor, plus any request-supplied hazards, per
        100km of route
    """
    if not coords:
        coords = [(26.1445, 91.7362)]  # Guwahati - degenerate/empty-route fallback

    now_month = datetime.now(timezone.utc).month
    monsoon_month = 1 if now_month in _MONSOON_MONTHS else 0

    lat0, lon0 = coords[0]
    lat1, lon1 = coords[-1]
    straight_km = max(_haversine_km(lat0, lon0, lat1, lon1), 0.1)
    actual_km = max(_route_length_km(coords), straight_km)
    winding_ratio = actual_km / straight_km

    if live_rainfall_mm is not None:
        rainfall_mm_24h = max(0.0, live_rainfall_mm)
    else:
        # Deterministic per-corridor jitter (same route -> same score) instead
        # of pure randomness, seeded from the route's midpoint coordinate.
        mid_lat, mid_lon = coords[len(coords) // 2]
        jitter = abs(math.modf(math.sin(mid_lat * 12.9898 + mid_lon * 78.233) * 43758.5453)[0])
        base_rainfall = 165.0 if monsoon_month else 35.0
        rainfall_mm_24h = max(0.0, base_rainfall + (jitter - 0.5) * 60.0)

    if live_elevation_gradient_pct is not None:
        elevation_gradient_pct = min(60.0, max(0.0, live_elevation_gradient_pct))
    else:
        elevation_gradient_pct = min(60.0, max(2.0, (winding_ratio - 1.0) * 90.0))

    if live_soil_moisture_idx is not None:
        soil_saturation_idx = min(1.0, max(0.0, live_soil_moisture_idx))
    else:
        soil_saturation_idx = min(
            1.0,
            max(0.05, 0.25 + (rainfall_mm_24h / 250.0) * 0.55 + (0.12 if monsoon_month else 0.0)),
        )

    incident_points = [(hz.lat, hz.lon) for hz in hazards] + _historical_incident_points()
    incident_hits = _nearby_incident_count(coords, incident_points)
    historical_incident_rate = (incident_hits / max(actual_km, 1.0)) * 100.0

    return {
        "rainfall_mm_24h": round(rainfall_mm_24h, 1),
        "elevation_gradient_pct": round(elevation_gradient_pct, 1),
        "soil_saturation_idx": round(soil_saturation_idx, 3),
        "historical_incident_rate": round(historical_incident_rate, 2),
        "monsoon_month": monsoon_month,
    }


def _localize_landslide_risk(
    coords: List[Tuple[float, float]], incident_points: List[Tuple[float, float]], radius_km: float = _NEARBY_INCIDENT_RADIUS_KM
) -> Optional[dict]:
    """Finds WHERE along a (possibly very long) corridor the route passes
    closest to a real recorded landslide - a HIGH-risk verdict on a
    3,000km highway is much more useful with "risk concentrated ~180km in"
    than just a single corridor-wide number. None if no incident is within
    radius_km of any sampled point (reuses the same bounded sample as
    _nearby_incident_count - see its docstring for why full-polyline
    iteration isn't safe here)."""
    if not coords or not incident_points:
        return None
    sampled = _sample_for_incident_check(coords)
    if len(sampled) < 2:
        return None
    cumulative = [0.0]
    for i in range(1, len(sampled)):
        cumulative.append(cumulative[-1] + _haversine_km(*sampled[i - 1], *sampled[i]))
    total_km = cumulative[-1] if cumulative[-1] > 0 else 1.0

    best_km, best_dist = None, None
    for i, (lat, lon) in enumerate(sampled):
        nearest = min((_haversine_km(lat, lon, plat, plon) for plat, plon in incident_points), default=None)
        if nearest is not None and nearest <= radius_km and (best_dist is None or nearest < best_dist):
            best_dist, best_km = nearest, cumulative[i]
    if best_km is None:
        return None
    return {"km_from_origin": round(best_km), "fraction": round(best_km / total_km, 3)}


def _rule_based_fallback(features: dict) -> Tuple[int, np.ndarray]:
    """Used only if risk_model.joblib hasn't been trained/loaded yet - a
    simple weighted-sum version of the same signal the model learns."""
    score = (
        features["rainfall_mm_24h"] / 250.0 * 0.35
        + features["elevation_gradient_pct"] / 60.0 * 0.3
        + features["soil_saturation_idx"] * 0.2
        + min(features["historical_incident_rate"] / 5.0, 1.0) * 0.1
        + features["monsoon_month"] * 0.05
    )
    if score < 0.35:
        return 0, np.array([0.8, 0.15, 0.05])
    if score < 0.62:
        return 1, np.array([0.2, 0.6, 0.2])
    return 2, np.array([0.05, 0.25, 0.7])


def assess_route_risk(
    coords: List[Tuple[float, float]],
    hazards: List[Coordinate],
    live_rainfall_mm: Optional[float] = None,
    live_elevation_gradient_pct: Optional[float] = None,
    live_soil_moisture_idx: Optional[float] = None,
) -> dict:
    """Returns {ai_safety_score, ai_risk_level, risk_factors} for a route corridor."""
    features = estimate_corridor_features(coords, hazards, live_rainfall_mm, live_elevation_gradient_pct, live_soil_moisture_idx)
    vector = [features[name] for name in FEATURE_NAMES]

    if _model is not None:
        proba = _model.predict_proba([vector])[0]
        predicted = int(np.argmax(proba))
        importances = _feature_importances
    else:
        predicted, proba = _rule_based_fallback(features)
        importances = np.array([0.32, 0.28, 0.2, 0.12, 0.08])

    # Terrain sanity override: even after rebalancing the training data with
    # more spatially-varied negatives, the trained model still relies on
    # rainfall/soil far more than elevation_gradient_pct (a real, measured
    # weakness - see ml/train_risk_model.py's own feature_importances_
    # output), so it can call MODERATE/HIGH landslide risk on genuinely flat
    # ground purely from rain. A landslide cannot happen on ground this flat,
    # full stop - if that same rain floods it, that's flood_engine.py's job
    # to flag, not this one's. Only overrides when a REAL measured gradient
    # was supplied (routing.py's live terrain sampling), never the winding-
    # ratio proxy used when no live terrain data is available - overriding on
    # a guess would be worse than not overriding at all.
    if live_elevation_gradient_pct is not None and live_elevation_gradient_pct < FLAT_TERRAIN_GRADIENT_THRESHOLD_PCT:
        predicted = 0
        proba = np.array([1.0, 0.0, 0.0])

    # Weighted safety score: SAFE counts fully, MODERATE half, HIGH not at all.
    safety_score = round(float(proba[0] * 100 + proba[1] * 50), 1)

    # Rank which of the 5 features drove this specific corridor's score
    # (importance x normalized feature value) for the human-readable driver text.
    norms = {
        "rainfall_mm_24h": features["rainfall_mm_24h"] / 250.0,
        "elevation_gradient_pct": features["elevation_gradient_pct"] / 60.0,
        "soil_saturation_idx": features["soil_saturation_idx"],
        "historical_incident_rate": min(features["historical_incident_rate"] / 5.0, 1.0),
        "monsoon_month": float(features["monsoon_month"]),
    }
    contributions = sorted(
        ((name, importances[i] * norms[name]) for i, name in enumerate(FEATURE_NAMES)),
        key=lambda kv: kv[1],
        reverse=True,
    )
    risk_factors = [FEATURE_DRIVER_LABELS[name] for name, contrib in contributions[:2] if contrib > 0]

    # Only bother localizing a segment when the corridor-wide verdict is
    # actually MODERATE/HIGH - a SAFE route has no "risky stretch" to point at.
    risk_segment = None
    if predicted > 0:
        incident_points = [(hz.lat, hz.lon) for hz in hazards] + _historical_incident_points()
        risk_segment = _localize_landslide_risk(coords, incident_points)

    return {
        "ai_safety_score": safety_score,
        "ai_risk_level": RISK_LABELS[predicted],
        "risk_factors": risk_factors,
        "risk_segment": risk_segment,
    }
