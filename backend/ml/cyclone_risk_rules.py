"""
Deterministic cyclone risk classifier - IMD's own 3-minute sustained wind
scale (IS-published categories: Depression / Deep Depression / Cyclonic
Storm / Severe Cyclonic Storm / ...), not a trained model.

This replaces an earlier RandomForest attempt that scored a misleading
100% cross-validated accuracy: its label was a direct threshold function of
wind_kt, and wind_kt (as sustained_wind_kmh) was also a model input, so the
"model" was just re-deriving a lookup table it was handed - no real
predictive skill, just restating the IMD scale through an ML detour. A
storm's current wind speed and pressure directly ARE the hazard (unlike,
say, an earthquake's eventual magnitude, which isn't determined by its
precursor seismicity) - classifying that is a real-world rule-based
problem, not a statistical-learning one, so a rule engine is the honest fit.

Historical density and distance-to-coast still matter for a CORRIDOR risk
call specifically (not just "how strong is this storm") - a storm at
Cat-2 strength but 400km out at sea doesn't yet threaten an inland highway
the way the same storm at landfall does - so those two adjust the score and
surface as risk_factors without changing what the wind-speed category itself
means.
"""
from __future__ import annotations

from typing import List, Optional

# IMD's official 3-min sustained wind categories (km/h), collapsed to this
# app's 3-tier schema: Depression/Deep Depression -> SAFE, Cyclonic Storm ->
# MODERATE, Severe Cyclonic Storm and above -> HIGH.
CS_THRESHOLD_KMH = 62.0
SCS_THRESHOLD_KMH = 89.0

# A storm's wind field doesn't meaningfully threaten an inland corridor until
# it's within its own gale-force wind radius of the coast - beyond this, cap
# the assessment at MODERATE regardless of the storm's out-at-sea intensity.
COASTAL_RELEVANCE_KM = 300.0

# 75th percentile of historical_cyclone_density across ml/cyclone_training_data.csv
# (2,539 real IBTrACS storm-days, North Indian Ocean, 1980-present) - a
# location above this has real above-average historical cyclone exposure,
# not an arbitrary cutoff.
HISTORICALLY_ACTIVE_DENSITY = 0.369

RISK_LABELS = {0: "SAFE", 1: "MODERATE", 2: "HIGH_CYCLONE_RISK"}


def classify_cyclone_risk(
    sustained_wind_kmh: float,
    distance_to_coast_km: float,
    historical_cyclone_density: Optional[float] = None,
) -> dict:
    """Returns {ai_safety_score, ai_risk_level, risk_factors} for a corridor
    point given live storm conditions - same response shape as the other 3
    hazards' assess_*_risk functions for consistency once these are wired
    into a shared endpoint."""
    if sustained_wind_kmh >= SCS_THRESHOLD_KMH:
        category = 2
    elif sustained_wind_kmh >= CS_THRESHOLD_KMH:
        category = 1
    else:
        category = 0

    # A storm too far offshore to matter yet is capped at MODERATE even at
    # high intensity - it's a real risk signal (worth watching), just not
    # yet a HIGH one for a specific land corridor.
    if category == 2 and distance_to_coast_km > COASTAL_RELEVANCE_KM:
        category = 1

    if category == 0:
        safety_score = round(max(0.0, 100.0 - (sustained_wind_kmh / CS_THRESHOLD_KMH) * 20.0), 1)
    elif category == 1:
        span = max(SCS_THRESHOLD_KMH - CS_THRESHOLD_KMH, 1.0)
        progress = min(max((sustained_wind_kmh - CS_THRESHOLD_KMH) / span, 0.0), 1.0)
        safety_score = round(80.0 - progress * 30.0, 1)
    else:
        over = min((sustained_wind_kmh - SCS_THRESHOLD_KMH) / SCS_THRESHOLD_KMH, 1.0)
        safety_score = round(max(5.0, 50.0 - over * 45.0), 1)

    risk_factors: List[str] = []
    if sustained_wind_kmh >= CS_THRESHOLD_KMH:
        risk_factors.append("High Sustained Wind Speed")
    if distance_to_coast_km <= 100.0:
        risk_factors.append("Coastal Proximity")
    if historical_cyclone_density is not None and historical_cyclone_density >= HISTORICALLY_ACTIVE_DENSITY:
        risk_factors.append("Historically Active Cyclone Corridor")

    return {
        "ai_safety_score": safety_score,
        "ai_risk_level": RISK_LABELS[category],
        "risk_factors": risk_factors,
    }
