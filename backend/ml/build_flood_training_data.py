"""
Builds a real, historically-grounded training set for a flood risk model
from ../india_dfo_floods.csv (120 recorded South Asia flood events,
1985-2010, from the Dartmouth Flood Observatory - see fetch_flood_catalog.py).

For each recorded flood (a positive example) and a matched random
non-flood date at the same real location (pseudo-absence, same technique
build_real_training_data.py uses for landslides), this fetches REAL
historical rainfall and river discharge from Open-Meteo's free archive/
flood APIs, and REAL elevation from Open-Topo-Data. historical_flood_density
is computed directly from the 120 recorded events - no API call needed for
that one.

Positive-sample severity uses DFO's own expert-assigned "severity" class
(1 = large event, ~10-20yr return period; 1.5 = very large, ~20-100yr;
2 = extreme, >100yr) rather than a magnitude-median split - DFO's analysts
already did the severity judgment call, no need to re-derive it from their
continuous magnitude index. 1.0 maps to MODERATE, 1.5/2.0 both map to HIGH
(this app only has 3 risk tiers, not DFO's 3 positive-severity + none); one
record in the source data has an out-of-range 11.5 (a data-entry typo, all
3,999 other rows are clean 1/1.5/2), clamped to the HIGH end rather than
dropped.

The "still risky, no flood" threshold for negative samples is the 80th
percentile of the real discharge values seen across this run's own negative
samples (mirroring the landslide builder's percentile-based rainfall
threshold) instead of a guessed constant.

Every location above (positive AND location-tied negative) comes from a
real DFO flood site - elevation_m and historical_flood_density are static/
date-independent, so a positive and its paired negative share identical
values for both (same issue the earthquake builder had). The model would
never see what a genuinely low-flood-risk location (high elevation, no
nearby flood history - most of the Deccan plateau, say) looks like, and
would extrapolate unpredictably when scoring one live. SPATIAL_NEGATIVE_COUNT
random locations across the full pan-India bbox (not tied to any flood
site) fix that, same technique build_earthquake_training_data.py uses.

Concurrent (bounded by CONCURRENCY, retries on 429, resumable via a local
cache) - same fix build_real_training_data.py needed once soil moisture
made sequential fetching impractically slow. Run directly:
    cd backend && python ml/build_flood_training_data.py

Writes ml/flood_training_data.csv, consumed by train_flood_risk_model.py.
"""
from __future__ import annotations

import asyncio
import csv
import json
import math
import random
import statistics
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

import httpx

CSV_PATH = Path(__file__).parent.parent / "india_dfo_floods.csv"
OUTPUT_PATH = Path(__file__).parent / "flood_training_data.csv"
CACHE_PATH = Path(__file__).parent / "flood_training_cache.json"

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FLOOD_URL = "https://flood-api.open-meteo.com/v1/flood"
ELEVATION_URL = "https://api.opentopodata.org/v1/srtm30m"
CONCURRENCY = 8
MAX_RETRIES = 3

NEARBY_EVENT_RADIUS_KM = 50.0  # DFO centroids are already regional aggregates, so a wider radius than the landslide model's 15km point-incidents
REFERENCE_ROUTE_KM = 200.0  # same NE-India-corridor stand-in unit build_real_training_data.py uses, kept consistent for train/serve parity
MONSOON_MONTHS = {6, 7, 8, 9}
NEGATIVE_DATE_EXCLUSION_DAYS = 30
ELEVATED_DISCHARGE_PERCENTILE = 80  # a negative day still counts MODERATE if its discharge is in the top 20% of this run's own negative samples - see main()
MAX_VALID_SEVERITY = 2.0  # DFO's real scale tops out at 2.0 ("extreme") - one row has a 11.5 data-entry typo, clamped here rather than dropped
BBOX = {"minlat": 8.0, "maxlat": 37.0, "minlon": 68.0, "maxlon": 97.5}  # same pan-India box fetch_landslide_catalog.py uses
SPATIAL_NEGATIVE_COUNT = 300

RISK_LABELS = {0: "SAFE", 1: "MODERATE", 2: "HIGH_FLOOD_RISK"}
FEATURE_NAMES = [
    "rainfall_mm_72h",
    "river_discharge_m3s",
    "elevation_m",
    "historical_flood_density",
    "monsoon_month",
]


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(d_lambda / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _load_events() -> List[dict]:
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    events = []
    for r in rows:
        try:
            date = datetime.strptime(r["began"].strip(), "%Y%m%d")
            lat, lon = float(r["latitude"]), float(r["longitude"])
            severity = min(float(r["severity"]), MAX_VALID_SEVERITY)
        except (ValueError, KeyError):
            continue
        events.append({"date": date, "lat": lat, "lon": lon, "severity": severity})
    return events


def _load_cache() -> dict:
    return json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}


def _save_cache(cache: dict) -> None:
    CACHE_PATH.write_text(json.dumps(cache))


async def _fetch_with_retry(client: httpx.AsyncClient, url: str, params: dict) -> Optional[dict]:
    for attempt in range(MAX_RETRIES):
        try:
            resp = await client.get(url, params=params, timeout=20)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                await asyncio.sleep(2.0 * (attempt + 1))
                continue
            return None
        except httpx.HTTPError:
            await asyncio.sleep(1.0)
    return None


async def _rainfall_mm_72h(client: httpx.AsyncClient, lat: float, lon: float, date: datetime) -> Optional[float]:
    start = (date - timedelta(days=2)).strftime("%Y-%m-%d")
    end = date.strftime("%Y-%m-%d")
    data = await _fetch_with_retry(
        client, ARCHIVE_URL,
        {"latitude": lat, "longitude": lon, "start_date": start, "end_date": end, "daily": "precipitation_sum", "timezone": "auto"},
    )
    if not data:
        return None
    values = [v for v in data.get("daily", {}).get("precipitation_sum", []) if v is not None]
    return sum(values) if values else None


async def _river_discharge(client: httpx.AsyncClient, lat: float, lon: float, date: datetime) -> Optional[float]:
    date_str = date.strftime("%Y-%m-%d")
    data = await _fetch_with_retry(
        client, FLOOD_URL,
        {"latitude": lat, "longitude": lon, "start_date": date_str, "end_date": date_str, "daily": "river_discharge"},
    )
    if not data:
        return None
    values = data.get("daily", {}).get("river_discharge", [])
    return values[0] if values and values[0] is not None else None


async def _elevation_m(client: httpx.AsyncClient, lat: float, lon: float) -> Optional[float]:
    data = await _fetch_with_retry(client, ELEVATION_URL, {"locations": f"{lat:.5f},{lon:.5f}"})
    results = (data or {}).get("results")
    if not results:
        return None
    return results[0]["elevation"]


def _historical_flood_density(lat: float, lon: float, all_points: List[Tuple[float, float]], exclude_index: int) -> float:
    hits = sum(
        1 for i, (plat, plon) in enumerate(all_points)
        if i != exclude_index and _haversine_km(lat, lon, plat, plon) <= NEARBY_EVENT_RADIUS_KM
    )
    return round((hits / REFERENCE_ROUTE_KM) * 100.0, 2)


def _severity_label(severity: float) -> int:
    return 1 if severity <= 1.0 else 2  # DFO 1.0 ("large") -> MODERATE; 1.5/2.0 ("very large"/"extreme") -> HIGH


def _pick_negative_date(lat: float, lon: float, events: List[dict], date_min: datetime, span_days: int) -> Optional[datetime]:
    local_dates = [e["date"] for e in events if _haversine_km(lat, lon, e["lat"], e["lon"]) <= NEARBY_EVENT_RADIUS_KM]
    for _ in range(15):
        d = date_min + timedelta(days=random.randint(0, span_days))
        if all(abs((d - other).days) > NEGATIVE_DATE_EXCLUSION_DAYS for other in local_dates):
            return d
    return None


def _plan_samples(events: List[dict]) -> List[dict]:
    date_min, date_max = min(e["date"] for e in events), max(e["date"] for e in events)
    span_days = (date_max - date_min).days
    random.seed(42)

    samples = []
    for idx, e in enumerate(events):
        samples.append({"key": f"pos_{idx}", "lat": e["lat"], "lon": e["lon"], "date": e["date"], "kind": "pos", "severity": e["severity"]})
    for idx, e in enumerate(events):
        neg_date = _pick_negative_date(e["lat"], e["lon"], events, date_min, span_days)
        if neg_date is None:
            continue
        samples.append({"key": f"neg_{idx}", "lat": e["lat"], "lon": e["lon"], "date": neg_date, "kind": "neg", "severity": None})

    # Random locations across the whole bbox, not tied to any DFO flood site -
    # see module docstring. Labeled "spatial" (not "neg") only so assemble_dataset
    # can compute historical_flood_density without excluding a positive's own
    # index (these have no corresponding positive to exclude).
    random.seed(43)
    for i in range(SPATIAL_NEGATIVE_COUNT):
        lat = random.uniform(BBOX["minlat"], BBOX["maxlat"])
        lon = random.uniform(BBOX["minlon"], BBOX["maxlon"])
        sample_date = date_min + timedelta(days=random.randint(0, span_days))
        samples.append({"key": f"spatial_{i}", "lat": lat, "lon": lon, "date": sample_date, "kind": "spatial", "severity": None})
    return samples


async def fetch_pass(samples: List[dict]) -> None:
    """Single resumable pass fetching all 3 external values per row, up to
    CONCURRENCY requests in flight - unlike the landslide builder's split
    rainfall/elevation passes, flood/discharge and rainfall share the same
    endpoint cadence here so one combined pass is enough."""
    cache = _load_cache()
    pending = [s for s in samples if not all(f"{p}_{s['key']}" in cache for p in ("rain", "discharge", "elev"))]
    print(f"  {len(samples) - len(pending)}/{len(samples)} already cached, fetching {len(pending)} remaining...")
    semaphore = asyncio.Semaphore(CONCURRENCY)
    done = 0

    async def _worker(client: httpx.AsyncClient, s: dict) -> None:
        nonlocal done
        keys = (f"rain_{s['key']}", f"discharge_{s['key']}", f"elev_{s['key']}")
        async with semaphore:
            if keys[0] not in cache:
                rain = await _rainfall_mm_72h(client, s["lat"], s["lon"], s["date"])
                if rain is not None:
                    cache[keys[0]] = rain
                    _save_cache(cache)
            if keys[1] not in cache:
                discharge = await _river_discharge(client, s["lat"], s["lon"], s["date"])
                if discharge is not None:
                    cache[keys[1]] = discharge
                    _save_cache(cache)
            if keys[2] not in cache:
                elev = await _elevation_m(client, s["lat"], s["lon"])
                if elev is not None:
                    cache[keys[2]] = elev
                    _save_cache(cache)
        done += 1
        missing = [k for k in keys if k not in cache]
        if done % 50 == 0 or done == len(pending) or missing:
            status = "complete" if not missing else f"missing {missing}, will retry on next run"
            print(f"  [{done}/{len(pending)}] {s['key']}: {status}")

    async with httpx.AsyncClient() as client:
        await asyncio.gather(*(_worker(client, s) for s in pending))
    print("Fetch pass complete.")


def assemble_dataset(samples: List[dict], all_points: List[Tuple[float, float]]) -> None:
    cache = _load_cache()
    index_by_key = {s["key"]: idx for idx, s in enumerate(s for s in samples if s["kind"] == "pos")}

    # The "still risky, no flood" negative-sample threshold is derived from
    # this run's own negative-sample discharge distribution (80th percentile)
    # rather than a guessed constant - same technique the landslide builder
    # uses for its rainfall threshold.
    negative_discharges = [cache[f"discharge_{s['key']}"] for s in samples if s["kind"] in ("neg", "spatial") and f"discharge_{s['key']}" in cache]
    elevated_discharge_threshold = statistics.quantiles(negative_discharges, n=100)[ELEVATED_DISCHARGE_PERCENTILE - 1]
    print(f"Elevated-discharge threshold (P{ELEVATED_DISCHARGE_PERCENTILE} of {len(negative_discharges)} real negative-day readings): {elevated_discharge_threshold:.1f} m3/s")

    rows = []
    incomplete = 0
    for s in samples:
        rain = cache.get(f"rain_{s['key']}")
        discharge = cache.get(f"discharge_{s['key']}")
        elev = cache.get(f"elev_{s['key']}")
        if rain is None or discharge is None or elev is None:
            incomplete += 1
            continue
        monsoon = 1 if s["date"].month in MONSOON_MONTHS else 0
        if s["kind"] == "pos":
            exclude_index = index_by_key.get(s["key"])
        elif s["kind"] == "neg":
            exclude_index = int(s["key"].split("_")[1])  # neg_N shares pos_N's location, exclude it from its own density count
        else:
            exclude_index = -1  # spatial sample has no corresponding positive to exclude
        density = _historical_flood_density(s["lat"], s["lon"], all_points, exclude_index=exclude_index)
        label = _severity_label(s["severity"]) if s["kind"] == "pos" else (1 if discharge >= elevated_discharge_threshold else 0)
        rows.append({
            "rainfall_mm_72h": round(rain, 1),
            "river_discharge_m3s": round(discharge, 2),
            "elevation_m": round(elev, 1),
            "historical_flood_density": density,
            "monsoon_month": monsoon,
            "label": label,
            # Not model features - written for train_flood_risk_model.py's
            # GroupKFold/spatial-holdout validation, same rationale as the
            # earthquake builder's year/grid_cell columns.
            "year": s["date"].year,
            "grid_cell": f"{int(s['lat'])}_{int(s['lon'])}",
        })

    with open(OUTPUT_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[*FEATURE_NAMES, "label", "year", "grid_cell"])
        writer.writeheader()
        writer.writerows(rows)

    label_counts = Counter(r["label"] for r in rows)
    print(f"\nAssembled {len(rows)} rows to {OUTPUT_PATH.name} ({incomplete} rows still missing a fetched value)")
    print("Label distribution:", {RISK_LABELS[k]: v for k, v in sorted(label_counts.items())})


async def _main() -> None:
    events = _load_events()
    print(f"Loaded {len(events)} real recorded floods from {CSV_PATH.name}")
    all_points = [(e["lat"], e["lon"]) for e in events]
    samples = _plan_samples(events)
    print(
        f"Planned {len(samples)} training rows "
        f"({sum(1 for s in samples if s['kind'] == 'pos')} positive, "
        f"{sum(1 for s in samples if s['kind'] == 'neg')} location-tied negative, "
        f"{sum(1 for s in samples if s['kind'] == 'spatial')} spatial negative)"
    )

    await fetch_pass(samples)
    assemble_dataset(samples, all_points)


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
