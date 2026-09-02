"""
Builds a real, historically-grounded training set for an earthquake risk
model from ../india_usgs_earthquakes.csv (23,612 real M>=4.0 events across
India + neighbors, 1970-2026, from USGS FDSNWS - see
fetch_earthquake_catalog.py).

Unlike the landslide model (which needs an external daily-weather trigger -
rainfall - fetched per sample), every feature here is derivable straight
from the earthquake catalog itself: no other API calls needed. This models
seismic hazard the way real hazard maps do - regions with denser historical
activity, larger past magnitudes, and recent upticks in activity are more
earthquake-prone - rather than trying to "predict" the exact day of a quake.

Positive samples: every recorded M>=5.5 event (a "significant" quake, the
same cutoff USGS itself uses for its own significant-earthquake feed), at
its real (lat, lon, date). Negative samples: the same locations at a random
other date, far from any M>=5.5 event there. All features for a sample are
computed using ONLY catalog events strictly before that sample's date - no
lookahead/leakage from data that wouldn't have been known at the time.

Two features come from real geophysical layers instead of recorded-quake
history - see fetch_earthquake_geophysical.py:
  - fault_dist_km: distance to the nearest known active fault (GEM GAF-DB) -
    a location can be fault-adjacent and hazardous even with a quiet
    recorded history ("seismic quiescence" before a major rupture is real
    and is exactly what pure incident-density features miss).
  - seismic_zone_factor: India's official IS 1893:2016 Z-factor (0.10-0.36)
    for whichever BIS seismic zone (II-V) the point falls in - the same
    input India's own building codes use, independent of this app's data.

Run directly (fast - local CSVs/JSON, no network calls, unlike the
landslide builder):
    cd backend && python ml/build_earthquake_training_data.py

Writes ml/earthquake_training_data.csv, consumed by train_earthquake_risk_model.py.
"""
from __future__ import annotations

import csv
import json
import math
import random
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

CSV_PATH = Path(__file__).parent.parent / "india_usgs_earthquakes.csv"
FAULTS_PATH = Path(__file__).parent.parent / "india_active_faults.json"
ZONES_PATH = Path(__file__).parent.parent / "india_seismic_zones.json"
OUTPUT_PATH = Path(__file__).parent / "earthquake_training_data.csv"
DEFAULT_ZONE_FACTOR = 0.16  # Zone III (moderate) - used only for the rare point outside every digitized zone ring

SIGNIFICANT_MAGNITUDE = 5.5  # USGS's own "significant earthquake" cutoff
BBOX = {"minlat": 6, "maxlat": 38, "minlon": 68, "maxlon": 98}  # same South Asia box fetch_earthquake_catalog.py used
SPATIAL_NEGATIVE_COUNT = 600  # random locations anywhere in the bbox, not just at quake sites - see main()'s docstring note
RADIUS_KM = 100.0  # regional shaking/hazard radius - wider than the landslide model's 15km, since a quake's felt/damage area is much larger than a single slope failure
RECENT_WINDOW_DAYS = 30
NEGATIVE_DATE_EXCLUSION_DAYS = 90  # keep negative-sample dates well clear of any M>=5.5 event at that same location
MAX_DAYS_SINCE_MAJOR = 3650.0  # cap "days since last major quake" at ~10y when none found, so it doesn't blow up the feature scale
MIN_HISTORY_YEARS = 1.0  # avoid dividing by a near-zero history window early in the catalog

RISK_LABELS = {0: "SAFE", 1: "MODERATE", 2: "HIGH_EARTHQUAKE_RISK"}
FEATURE_NAMES = [
    "local_seismic_density",
    "max_magnitude_nearby",
    "avg_depth_km",
    "recent_activity_rate_30d",
    "days_since_major_quake",
    "fault_dist_km",
    "seismic_zone_factor",
]


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(d_lambda / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _load_faults() -> List[List[tuple]]:
    return json.loads(FAULTS_PATH.read_text())


def _load_zones() -> List[dict]:
    """Precomputes each zone ring's bounding box once, so a per-sample point
    can cheaply skip the ~278k-vertex ray-casting cost for every ring whose
    bbox obviously doesn't contain it - only the (usually 1-2) candidate
    rings actually get the full point-in-polygon test."""
    zones = json.loads(ZONES_PATH.read_text())
    for z in zones:
        lats = [p[0] for p in z["ring"]]
        lons = [p[1] for p in z["ring"]]
        z["bbox"] = (min(lats), max(lats), min(lons), max(lons))
        z["centroid"] = (sum(lats) / len(lats), sum(lons) / len(lons))
    return zones


def _fault_dist_km(lat: float, lon: float, faults: List[List[tuple]]) -> float:
    return min(_haversine_km(lat, lon, flat, flon) for line in faults for flat, flon in line)


def _point_in_ring(lat: float, lon: float, ring: List[tuple]) -> bool:
    """Standard ray-casting point-in-polygon test (lon=x, lat=y)."""
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


def _seismic_zone_factor(lat: float, lon: float, zones: List[dict]) -> float:
    candidates = [z for z in zones if z["bbox"][0] <= lat <= z["bbox"][1] and z["bbox"][2] <= lon <= z["bbox"][3]]
    for z in candidates:
        if _point_in_ring(lat, lon, z["ring"]):
            return z["z_factor"]
    # Point fell in a gap between digitized rings (coastline/border slivers) -
    # fall back to the nearest zone's centroid rather than a hardcoded guess.
    if zones:
        nearest = min(zones, key=lambda z: _haversine_km(lat, lon, *z["centroid"]))
        return nearest["z_factor"]
    return DEFAULT_ZONE_FACTOR


def _load_catalog() -> List[dict]:
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


def _nearby_before(events: List[dict], lat: float, lon: float, before: datetime, radius_km: float = RADIUS_KM) -> List[dict]:
    return [e for e in events if e["time"] < before and _haversine_km(lat, lon, e["lat"], e["lon"]) <= radius_km]


def _features_at(
    events: List[dict], lat: float, lon: float, sample_date: datetime, catalog_start: datetime,
    faults: List[List[tuple]], zones: List[dict],
) -> Optional[dict]:
    nearby = _nearby_before(events, lat, lon, sample_date)

    history_years = max((sample_date - catalog_start).days / 365.25, MIN_HISTORY_YEARS)
    local_seismic_density = round(len(nearby) / history_years, 3)

    max_magnitude_nearby = round(max((e["mag"] for e in nearby), default=0.0), 2)
    avg_depth_km = round(sum(e["depth_km"] for e in nearby) / len(nearby), 1) if nearby else 35.0  # 35km ~ typical continental crust depth, used only when no nearby history exists yet

    recent_cutoff = sample_date - timedelta(days=RECENT_WINDOW_DAYS)
    recent_activity_rate_30d = sum(1 for e in nearby if e["time"] >= recent_cutoff)

    major_nearby = [e for e in nearby if e["mag"] >= SIGNIFICANT_MAGNITUDE]
    if major_nearby:
        days_since_major_quake = min((sample_date - max(e["time"] for e in major_nearby)).days, MAX_DAYS_SINCE_MAJOR)
    else:
        days_since_major_quake = MAX_DAYS_SINCE_MAJOR

    return {
        "local_seismic_density": local_seismic_density,
        "max_magnitude_nearby": max_magnitude_nearby,
        "avg_depth_km": avg_depth_km,
        "recent_activity_rate_30d": recent_activity_rate_30d,
        "days_since_major_quake": days_since_major_quake,
        "fault_dist_km": round(_fault_dist_km(lat, lon, faults), 1),
        "seismic_zone_factor": _seismic_zone_factor(lat, lon, zones),
    }


def _severity_label() -> int:
    # A quake's eventual magnitude isn't predictable from the seismicity that
    # preceded it - a well-documented limit in seismology, not a modeling
    # shortcut - so every positive sample (preceded a real M>=5.5 event) gets
    # the same HIGH label; only negatives are split by recent activity level.
    return 2


def _pick_negative_date(
    lat: float, lon: float, majors: List[dict], date_min: datetime, span_days: int
) -> Optional[datetime]:
    """A negative-sample date only needs to steer clear of major quakes AT
    THIS LOCATION - not every major quake in the whole region, which (given
    734 of them spread across the catalog) would exclude almost the entire
    calendar."""
    local_major_dates = [e["time"] for e in majors if _haversine_km(lat, lon, e["lat"], e["lon"]) <= RADIUS_KM]
    for _ in range(15):
        d = date_min + timedelta(days=random.randint(0, span_days))
        if all(abs((d - other).days) > NEGATIVE_DATE_EXCLUSION_DAYS for other in local_major_dates):
            return d
    return None


def main() -> None:
    events = _load_catalog()
    catalog_start = events[0]["time"]
    print(f"Loaded {len(events)} real recorded earthquakes (M>=4.0) from {CSV_PATH.name}, {catalog_start.date()} onward")

    faults = _load_faults()
    zones = _load_zones()
    print(f"Loaded {len(faults)} fault-line segments and {len(zones)} seismic zone polygons")

    majors = [e for e in events if e["mag"] >= SIGNIFICANT_MAGNITUDE]
    print(f"{len(majors)} are 'significant' (M>={SIGNIFICANT_MAGNITUDE}) - these become positive samples")

    date_min = min(e["time"] for e in majors)
    date_max = max(e["time"] for e in events)
    span_days = (date_max - date_min).days
    random.seed(42)

    # year/grid_cell aren't model features - they're written alongside so
    # train_earthquake_risk_model.py can run GroupKFold (grouped by year, to
    # keep an aftershock sequence from splitting across train/validation) and
    # a spatial holdout (grouped by grid_cell, a ~1deg/~111km bucket) instead
    # of only a shuffled fold, which can silently overstate accuracy.
    rows = []
    skipped_negative = 0
    for e in majors:
        feats = _features_at(events, e["lat"], e["lon"], e["time"], catalog_start, faults, zones)
        rows.append({**feats, "label": _severity_label(), "year": e["time"].year, "grid_cell": f"{int(e['lat'])}_{int(e['lon'])}"})

    for e in majors:
        neg_date = _pick_negative_date(e["lat"], e["lon"], majors, date_min, span_days)
        if neg_date is None or neg_date <= catalog_start:
            skipped_negative += 1
            continue
        feats = _features_at(events, e["lat"], e["lon"], neg_date, catalog_start, faults, zones)
        # A negative sample can still land in a moderately active window (elevated
        # background seismicity without a significant quake) - reflect that rather
        # than forcing every non-event day to SAFE, mirroring the landslide
        # builder's rainfall-threshold-based negative relabeling.
        label = 1 if feats["recent_activity_rate_30d"] >= 3 else 0
        rows.append({**feats, "label": label, "year": neg_date.year, "grid_cell": f"{int(e['lat'])}_{int(e['lon'])}"})

    # Every sample above (positive AND location-tied negative) comes from a
    # location that had a real M>=5.5 quake at some point - fault_dist_km and
    # seismic_zone_factor are static/date-independent, so a positive and its
    # paired negative share identical values for both. That means the model
    # never sees what a genuinely calm, far-from-any-fault location (most of
    # peninsular India) looks like, and extrapolates unpredictably - even
    # backwards - when asked to score one live. Add real negative samples at
    # random locations across the whole bbox (not tied to any quake site) so
    # the model has actual training exposure to low-seismicity geography.
    random.seed(43)
    for _ in range(SPATIAL_NEGATIVE_COUNT):
        lat = random.uniform(BBOX["minlat"], BBOX["maxlat"])
        lon = random.uniform(BBOX["minlon"], BBOX["maxlon"])
        sample_date = date_min + timedelta(days=random.randint(0, span_days))
        if sample_date <= catalog_start:
            continue
        feats = _features_at(events, lat, lon, sample_date, catalog_start, faults, zones)
        label = 1 if feats["recent_activity_rate_30d"] >= 3 else 0
        rows.append({**feats, "label": label, "year": sample_date.year, "grid_cell": f"{int(lat)}_{int(lon)}"})

    with open(OUTPUT_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[*FEATURE_NAMES, "label", "year", "grid_cell"])
        writer.writeheader()
        writer.writerows(rows)

    label_counts = Counter(r["label"] for r in rows)
    print(f"\nAssembled {len(rows)} rows to {OUTPUT_PATH.name} ({skipped_negative} negative samples skipped - too close to catalog start)")
    print("Label distribution:", {RISK_LABELS[k]: v for k, v in sorted(label_counts.items())})


if __name__ == "__main__":
    main()
