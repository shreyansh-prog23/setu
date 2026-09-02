"""
Cyclone risk assessment for a corridor point - wraps ml/cyclone_risk_rules.py's
deterministic IMD wind-scale classifier (not a trained model - see that
module's docstring for why a trained model would be circular here).

sustained_wind_kmh and pressure_hpa come from live Open-Meteo conditions AT
THE CORRIDOR'S OWN COORDINATES - if a cyclone is actually affecting this
point right now, its wind/pressure readings already show it directly, no
need to track a distant storm's position.

distance_to_coast_km comes from a real coastline geometry (Natural Earth,
see ml/fetch_coastline.py) - nearest-vertex haversine distance, same
technique fetch_earthquake_geophysical.py uses for fault-line distance.
historical_cyclone_density comes from the real IBTrACS catalog
(india_ibtracs_cyclones.csv - the same dataset cyclone_risk_rules.py's
HISTORICALLY_ACTIVE_DENSITY constant was calibrated against).
"""
from __future__ import annotations

import csv
import json
import logging
import math
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Tuple

from ml.cyclone_risk_rules import HISTORICALLY_ACTIVE_DENSITY, classify_cyclone_risk

logger = logging.getLogger("cyclone_engine")

CYCLONE_CSV_PATH = Path(__file__).parent / "india_ibtracs_cyclones.csv"
COASTLINE_PATH = Path(__file__).parent / "india_coastline.json"

DENSITY_RADIUS_KM = 150.0  # matches ml/build_cyclone_training_data.py's RADIUS_KM
STORM_WIND_THRESHOLD_KT = 34.0
DEFAULT_DISTANCE_TO_COAST_KM = 300.0  # used only if the coastline dataset failed to load


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(d_lambda / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


@lru_cache
def _load_coastline() -> List[List[Tuple[float, float]]]:
    try:
        return json.loads(COASTLINE_PATH.read_text())
    except Exception as exc:
        logger.warning("Coastline dataset load failed (%s): %s", COASTLINE_PATH, exc)
        return []


def _distance_to_coast_km(lat: float, lon: float) -> float:
    lines = _load_coastline()
    if not lines:
        return DEFAULT_DISTANCE_TO_COAST_KM
    return min(_haversine_km(lat, lon, clat, clon) for line in lines for clat, clon in line)


@lru_cache
def _load_cyclone_days() -> List[dict]:
    """One row per (storm, day) - daily peak wind, same reduction
    build_cyclone_training_data.py's _load_daily_peaks() does, so
    historical_cyclone_density matches the density figures the model/rule
    thresholds were actually calibrated against."""
    try:
        with open(CYCLONE_CSV_PATH, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception as exc:
        logger.warning("IBTrACS cyclone catalog load failed (%s): %s", CYCLONE_CSV_PATH, exc)
        return []

    by_storm_day: dict = defaultdict(list)
    for r in rows:
        if not r["wind_kt"].strip():
            continue
        day = r["time"][:10]
        by_storm_day[(r["sid"], day)].append(r)

    peaks = []
    for (_sid, _day), day_rows in by_storm_day.items():
        peak = max(day_rows, key=lambda r: float(r["wind_kt"]))
        peaks.append({"lat": float(peak["latitude"]), "lon": float(peak["longitude"]), "wind_kt": float(peak["wind_kt"])})
    return peaks


def _historical_cyclone_density(lat: float, lon: float) -> float:
    peaks = _load_cyclone_days()
    if not peaks:
        return 0.0
    catalog_years = 46.0  # 1980-2026, see fetch_cyclone_catalog.py's MIN_SEASON
    hits = sum(
        1 for p in peaks
        if p["wind_kt"] >= STORM_WIND_THRESHOLD_KT and _haversine_km(lat, lon, p["lat"], p["lon"]) <= DENSITY_RADIUS_KM
    )
    return round(hits / catalog_years, 3)


def is_loaded() -> bool:
    """Cyclone risk is rule-based, not a trained model - always available
    as long as the coastline/catalog data loaded (checked lazily via the
    lru_cache'd loaders' own fallbacks, so this simply reports the module
    imported cleanly)."""
    return True


def assess_cyclone_risk(lat: float, lon: float, live_wind_kmh: Optional[float] = None, live_pressure_hpa: Optional[float] = None) -> dict:
    """Returns {ai_safety_score, ai_risk_level, risk_factors} for a corridor point."""
    wind_kmh = live_wind_kmh if live_wind_kmh is not None else 15.0  # calm-day default
    distance_to_coast = _distance_to_coast_km(lat, lon)
    density = _historical_cyclone_density(lat, lon)

    result = classify_cyclone_risk(wind_kmh, distance_to_coast, historical_cyclone_density=density)

    if density >= HISTORICALLY_ACTIVE_DENSITY and "Historically Active Cyclone Corridor" not in result["risk_factors"]:
        result["risk_factors"].append("Historically Active Cyclone Corridor")

    result["risk_segment"] = None  # only landslide localizes a specific risky stretch so far - see risk_engine.py
    return result
