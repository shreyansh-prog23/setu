"""
Builds a real, historically-grounded training set for a cyclone risk model
from ../india_ibtracs_cyclones.csv (16,908 real 3-hourly track points from
NOAA's IBTrACS, North Indian Ocean basin, 1980-present - see
fetch_cyclone_catalog.py).

Unlike the other 3 hazard models, IBTrACS doesn't need a separate
positive/pseudo-absence sampling scheme: a storm's own real track already
covers its full intensity range from formation (weak) through peak through
dissipation (weak again), so every risk tier already has genuine historical
examples without inventing "non-event" dates.

First reduces the 3-hourly points to one row per (storm, calendar day) -
the day's peak-wind observation - since consecutive 3-hourly points of the
same storm are near-duplicates and would otherwise dominate the training
set. All features are computed using ONLY prior days' storms relative to
each row's date - no lookahead.

pressure_hpa is missing on ~28% of storm-days even at peak wind (agencies
don't always report both) - filled via a linear wind->pressure fit learned
from the ~72% of rows that have both (a well-established empirical
relationship - lower pressure ~ higher wind - not an arbitrary guess).

Run directly (fast - single local CSV, no network calls):
    cd backend && python ml/build_cyclone_training_data.py

Writes ml/cyclone_training_data.csv, consumed by train_cyclone_risk_model.py.
"""
from __future__ import annotations

import csv
import math
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import List, Optional

CSV_PATH = Path(__file__).parent.parent / "india_ibtracs_cyclones.csv"
OUTPUT_PATH = Path(__file__).parent / "cyclone_training_data.csv"

RADIUS_KM = 150.0  # a cyclone's damaging wind/rain field extends well beyond its exact track point
STORM_WIND_THRESHOLD_KT = 34.0  # IMD/WMO's own "Cyclonic Storm" cutoff - below this, a system isn't a named cyclone
SEVERE_WIND_THRESHOLD_KT = 64.0  # IMD's "Very Severe Cyclonic Storm" cutoff (~hurricane-equivalent)
CYCLONE_SEASON_MONTHS = {4, 5, 6, 10, 11, 12}  # India's pre-monsoon and post-monsoon cyclone windows
MIN_HISTORY_YEARS = 1.0

RISK_LABELS = {0: "SAFE", 1: "MODERATE", 2: "HIGH_CYCLONE_RISK"}
FEATURE_NAMES = [
    "sustained_wind_kmh",
    "pressure_hpa",
    "distance_to_coast_km",
    "historical_cyclone_density",
    "cyclone_season",
]


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(d_lambda / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _load_daily_peaks() -> List[dict]:
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    by_storm_day: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        if not r["wind_kt"].strip():
            continue
        day = r["time"][:10]
        by_storm_day[(r["sid"], day)].append(r)

    peaks = []
    for (sid, day), day_rows in by_storm_day.items():
        peak = max(day_rows, key=lambda r: float(r["wind_kt"]))
        peaks.append({
            "sid": sid,
            "date": datetime.strptime(day, "%Y-%m-%d"),
            "lat": float(peak["latitude"]),
            "lon": float(peak["longitude"]),
            "wind_kt": float(peak["wind_kt"]),
            "pressure_hpa": float(peak["pressure_hpa"]) if peak["pressure_hpa"].strip() else None,
            "dist2land_km": float(peak["dist2land_km"]),
        })
    peaks.sort(key=lambda p: p["date"])
    return peaks


def _fit_wind_pressure_regression(peaks: List[dict]) -> tuple[float, float]:
    """Simple least-squares fit of pressure_hpa ~ a + b * wind_kt over rows
    that have both, used only to fill in the rows that are missing pressure."""
    pairs = [(p["wind_kt"], p["pressure_hpa"]) for p in peaks if p["pressure_hpa"] is not None]
    n = len(pairs)
    sum_x = sum(x for x, _ in pairs)
    sum_y = sum(y for _, y in pairs)
    sum_xy = sum(x * y for x, y in pairs)
    sum_xx = sum(x * x for x, _ in pairs)
    b = (n * sum_xy - sum_x * sum_y) / (n * sum_xx - sum_x * sum_x)
    a = (sum_y - b * sum_x) / n
    return a, b


def _historical_cyclone_density(lat: float, lon: float, sample_date: datetime, peaks: List[dict], catalog_start: datetime) -> float:
    history_years = max((sample_date - catalog_start).days / 365.25, MIN_HISTORY_YEARS)
    hits = sum(
        1 for p in peaks
        if p["date"] < sample_date and p["wind_kt"] >= STORM_WIND_THRESHOLD_KT
        and _haversine_km(lat, lon, p["lat"], p["lon"]) <= RADIUS_KM
    )
    return round(hits / history_years, 3)


def _risk_label(wind_kt: float) -> int:
    if wind_kt >= SEVERE_WIND_THRESHOLD_KT:
        return 2
    if wind_kt >= STORM_WIND_THRESHOLD_KT:
        return 1
    return 0


def main() -> None:
    peaks = _load_daily_peaks()
    catalog_start = peaks[0]["date"]
    print(f"Loaded {len(peaks)} real storm-days (daily peak wind) from {CSV_PATH.name}, {catalog_start.date()} onward")

    intercept, slope = _fit_wind_pressure_regression(peaks)
    print(f"Wind->pressure fill regression: pressure_hpa = {intercept:.1f} + {slope:.3f} * wind_kt")

    rows = []
    for p in peaks:
        pressure = p["pressure_hpa"] if p["pressure_hpa"] is not None else round(intercept + slope * p["wind_kt"], 1)
        density = _historical_cyclone_density(p["lat"], p["lon"], p["date"], peaks, catalog_start)
        rows.append({
            "sustained_wind_kmh": round(p["wind_kt"] * 1.852, 1),
            "pressure_hpa": pressure,
            "distance_to_coast_km": round(p["dist2land_km"], 1),
            "historical_cyclone_density": density,
            "cyclone_season": 1 if p["date"].month in CYCLONE_SEASON_MONTHS else 0,
            "label": _risk_label(p["wind_kt"]),
        })

    with open(OUTPUT_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[*FEATURE_NAMES, "label"])
        writer.writeheader()
        writer.writerows(rows)

    label_counts = Counter(r["label"] for r in rows)
    print(f"\nAssembled {len(rows)} rows to {OUTPUT_PATH.name}")
    print("Label distribution:", {RISK_LABELS[k]: v for k, v in sorted(label_counts.items())})


if __name__ == "__main__":
    main()
