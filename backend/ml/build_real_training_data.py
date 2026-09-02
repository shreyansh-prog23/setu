"""
Builds a real, historically-grounded training set for the corridor risk
model from NASA's Global Landslide Catalog (../india_nasa_landslides.csv -
2,389 recorded pan-India landslides, 1970-2019 - see
fetch_landslide_catalog.py; was 320 NE-India-only records before the
pan-India pivot).

For each recorded landslide (a positive example) and a matched random
non-incident date at the same real location (a negative/pseudo-absence
example - a standard technique in real landslide-susceptibility modeling),
this fetches REAL historical daily rainfall, REAL soil_moisture_0_to_7cm
(averaged over the day, normalized against a typical saturated-soil
reference value - same normalization risk_engine.py's live lookup uses, see
ml/weather.py), and REAL local terrain gradient, all from Open-Meteo's free
archive/elevation APIs. Rainfall and soil moisture come from the same
archive request (one daily + one hourly parameter on one call) rather than
a separate round of API calls. historical_incident_rate is computed
directly from real distances between the 2,389 recorded incidents.

historical_incident_rate is scaled against REFERENCE_ROUTE_KM to roughly
match risk_engine.py's live definition (incident hits per corridor, divided
by the corridor's actual length) - a single point has no "route length",
so a representative inter-city highway corridor span is used as a stand-in.

Concurrent (bounded by RAINFALL_CONCURRENCY/ELEVATION_CONCURRENCY, retries on 429, resumable via a local
cache) - the pan-India expansion (2,389 incidents, ~4,780 sample rows, up
from 320/640) made the old one-request-at-a-time version impractically slow
once soil moisture's heavier hourly query was added (~4s/request observed,
5+ hours projected) - bounded concurrency cuts this to minutes. Run directly:
    cd backend && python ml/build_real_training_data.py

Writes ml/real_training_data.csv, consumed by train_risk_model.py.
"""
from __future__ import annotations

import asyncio
import csv
import json
import math
import random
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

import httpx

CSV_PATH = Path(__file__).parent.parent / "india_nasa_landslides.csv"
OUTPUT_PATH = Path(__file__).parent / "real_training_data.csv"
CACHE_PATH = Path(__file__).parent / "real_training_cache.json"

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
ELEVATION_URL = "https://api.opentopodata.org/v1/srtm30m"  # separate provider/quota from the rainfall archive above
RAINFALL_CONCURRENCY = 10  # Open-Meteo's archive API handled this fine
ELEVATION_CONCURRENCY = 3  # Open-Topo-Data's free tier rate-limited hard at 10 concurrent - a 3,678/4,778 rain pass finished clean, but elevation only got 1,719/4,778 before failing en masse (each failure retrying with backoff is what actually burned the 3.5 real hours, not raw request time)
MAX_RETRIES = 3

SOIL_SATURATION_REFERENCE_M3M3 = 0.45  # same reference ml/weather.py's live get_live_soil_moisture uses - typical saturated volumetric water content for loam/clay-loam soils
NEARBY_INCIDENT_RADIUS_KM = 15.0
REFERENCE_ROUTE_KM = 200.0  # typical inter-city highway corridor length - see module docstring
MONSOON_MONTHS = {6, 7, 8, 9}
MODERATE_RAIN_NO_INCIDENT_THRESHOLD_MM = 1.9  # ~75th percentile of real negative-day rainfall - 100mm was unreachable, so this rule never fired before
NEGATIVE_DATE_EXCLUSION_DAYS = 14  # keep negative-sample dates well clear of any recorded incident date
BBOX = {"minlat": 8.0, "maxlat": 37.0, "minlon": 68.0, "maxlon": 97.5}  # same pan-India box fetch_landslide_catalog.py uses
SPATIAL_NEGATIVE_COUNT = 1200  # was 600 - even after that first fix, the deployed model still learned almost nothing from elevation_gradient_pct (6.6% feature importance, vs 54.8% for rainfall_mm_24h alone) because 600 was still a small minority against ~4,760 same-site pos/neg pairs. Doubled first as a cheaper/faster test of whether this actually moves feature importance before spending more real API quota/time - every existing sample (rain/soil/grad, this file's on-disk cache) is reused as-is, so a later increase only fetches the new delta, not from scratch. See _plan_samples' docstring for why this matters.

RISK_LABELS = {0: "SAFE", 1: "MODERATE", 2: "HIGH_LANDSLIDE_RISK"}
FEATURE_NAMES = [
    "rainfall_mm_24h",
    "elevation_gradient_pct",
    "soil_saturation_idx",
    "historical_incident_rate",
    "monsoon_month",
]


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(d_lambda / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _load_incidents() -> List[dict]:
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    incidents = []
    for r in rows:
        try:
            date = datetime.strptime(r["event_date"].strip(), "%m/%d/%Y %I:%M:%S %p")
            lat, lon = float(r["latitude"]), float(r["longitude"])
        except (ValueError, KeyError):
            continue
        incidents.append({"date": date, "lat": lat, "lon": lon, "size": (r.get("landslide_size") or "unknown").strip().lower()})
    return incidents


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


async def _rainfall_and_soil(client: httpx.AsyncClient, lat: float, lon: float, date: datetime) -> Tuple[Optional[float], Optional[float]]:
    """One archive-API call returns both real values for the day: summed
    daily rainfall and the day's hourly soil-moisture readings (averaged and
    normalized into the same 0-1 index ml/weather.py's live lookup uses)."""
    date_str = date.strftime("%Y-%m-%d")
    data = await _fetch_with_retry(
        client, ARCHIVE_URL,
        {
            "latitude": lat, "longitude": lon, "start_date": date_str, "end_date": date_str,
            "daily": "precipitation_sum", "hourly": "soil_moisture_0_to_7cm", "timezone": "auto",
        },
    )
    if not data:
        return None, None
    daily_values = data.get("daily", {}).get("precipitation_sum", [])
    rain = daily_values[0] if daily_values and daily_values[0] is not None else None
    hourly_values = [v for v in data.get("hourly", {}).get("soil_moisture_0_to_7cm", []) if v is not None]
    soil = round(min(1.0, (sum(hourly_values) / len(hourly_values)) / SOIL_SATURATION_REFERENCE_M3M3), 3) if hourly_values else None
    return rain, soil


async def _elevation_gradient_pct(client: httpx.AsyncClient, lat: float, lon: float) -> Optional[float]:
    offset = 0.03  # ~3km - a small local neighborhood around the incident point
    points = [(lat, lon), (lat + offset, lon), (lat - offset, lon), (lat, lon + offset), (lat, lon - offset)]
    data = await _fetch_with_retry(client, ELEVATION_URL, {"locations": "|".join(f"{p[0]:.5f},{p[1]:.5f}" for p in points)})
    results = (data or {}).get("results")
    elevations = [r["elevation"] for r in results] if results else None
    # SRTM30m returns null for ocean points - a real possibility now that
    # spatial negative samples are randomly placed across the whole bbox
    # (some land in the Bay of Bengal/Arabian Sea), not just at real
    # incident locations on land.
    if not elevations or len(elevations) < 2 or elevations[0] is None:
        return None
    center_elev = elevations[0]
    steepest = 0.0
    for (plat, plon), elev in zip(points[1:], elevations[1:]):
        if elev is None:
            continue
        run_km = _haversine_km(lat, lon, plat, plon)
        if run_km <= 0:
            continue
        steepest = max(steepest, (abs(elev - center_elev) / (run_km * 1000.0)) * 100.0)
    return round(min(steepest, 60.0), 1)


def _historical_incident_rate(lat: float, lon: float, all_points: List[Tuple[float, float]], exclude_index: int) -> float:
    hits = sum(
        1 for i, (plat, plon) in enumerate(all_points)
        if i != exclude_index and _haversine_km(lat, lon, plat, plon) <= NEARBY_INCIDENT_RADIUS_KM
    )
    return round((hits / REFERENCE_ROUTE_KM) * 100.0, 2)


def _severity_label(size: str) -> int:
    if size == "small":
        return 1
    if size in ("medium", "large", "very_large"):
        return 2
    return 1  # unknown size -> moderate, a cautious default rather than guessing high or low


def _pick_negative_date(inc_date: datetime, incident_dates: List[datetime], date_min: datetime, span_days: int) -> Optional[datetime]:
    for _ in range(15):
        d = date_min + timedelta(days=random.randint(0, span_days))
        if all(abs((d - other).days) > NEGATIVE_DATE_EXCLUSION_DAYS for other in incident_dates):
            return d
    return None


def _plan_samples(incidents: List[dict]) -> List[dict]:
    """One entry per training row (positive, location-tied negative, or
    spatial negative), with the date/label inputs already resolved - kept
    separate from the network fetch loops below so rainfall and elevation
    can be fetched independently and either can be resumed/retried without
    recomputing the other.

    Every positive AND location-tied negative sits at a real recorded
    landslide site - elevation_gradient_pct and historical_incident_rate are
    largely static per location, so they carry almost no signal between a
    positive and its paired negative (same bug build_earthquake_training_data.py
    and build_flood_training_data.py had). SPATIAL_NEGATIVE_COUNT random
    locations across the pan-India bbox (not tied to any landslide site) give
    the model real exposure to genuinely low-terrain-risk geography."""
    incident_dates = [i["date"] for i in incidents]
    date_min, date_max = min(incident_dates), max(incident_dates)
    span_days = (date_max - date_min).days
    random.seed(42)

    samples = []
    for idx, inc in enumerate(incidents):
        samples.append({"key": f"pos_{idx}", "lat": inc["lat"], "lon": inc["lon"], "date": inc["date"], "kind": "pos", "size": inc["size"]})
    for idx, inc in enumerate(incidents):
        neg_date = _pick_negative_date(inc["date"], incident_dates, date_min, span_days)
        if neg_date is None:
            continue
        samples.append({"key": f"neg_{idx}", "lat": inc["lat"], "lon": inc["lon"], "date": neg_date, "kind": "neg", "size": None})

    random.seed(43)
    for i in range(SPATIAL_NEGATIVE_COUNT):
        lat = random.uniform(BBOX["minlat"], BBOX["maxlat"])
        lon = random.uniform(BBOX["minlon"], BBOX["maxlon"])
        sample_date = date_min + timedelta(days=random.randint(0, span_days))
        samples.append({"key": f"spatial_{i}", "lat": lat, "lon": lon, "date": sample_date, "kind": "spatial", "size": None})
    return samples


async def fetch_rainfall_pass(samples: List[dict]) -> None:
    """Pass 1: fetch real historical rainfall + soil moisture for every
    planned row (one archive-API call gives both), up to RAINFALL_CONCURRENCY
    requests in flight at once. Independent of the elevation endpoint's
    quota. Cache is saved after every completed request (not batched), so a
    Ctrl-C or crash mid-pass loses at most one in-flight request."""
    cache = _load_cache()
    pending = [s for s in samples if not (f"rain_{s['key']}" in cache and f"soil_{s['key']}" in cache)]
    print(f"  {len(samples) - len(pending)}/{len(samples)} already cached, fetching {len(pending)} remaining...")
    semaphore = asyncio.Semaphore(RAINFALL_CONCURRENCY)
    done = 0

    async def _worker(client: httpx.AsyncClient, s: dict) -> None:
        nonlocal done
        async with semaphore:
            rain, soil = await _rainfall_and_soil(client, s["lat"], s["lon"], s["date"])
        done += 1
        if rain is None or soil is None:
            print(f"  [rain {done}/{len(pending)}] {s['key']}: fetch failed, will retry on next run")
            return
        cache[f"rain_{s['key']}"] = rain
        cache[f"soil_{s['key']}"] = soil
        _save_cache(cache)
        if done % 100 == 0 or done == len(pending):
            print(f"  [rain {done}/{len(pending)}] {s['key']}: {rain}mm, soil={soil}")

    async with httpx.AsyncClient() as client:
        await asyncio.gather(*(_worker(client, s) for s in pending))
    print("Rainfall pass complete.")


async def fetch_elevation_pass(samples: List[dict]) -> None:
    """Pass 2: fetch real local terrain gradient for every planned row,
    same bounded-concurrency approach as fetch_rainfall_pass. Separate pass
    so it can be retried on its own without re-fetching rainfall."""
    cache = _load_cache()
    pending = [s for s in samples if f"grad_{s['key']}" not in cache]
    print(f"  {len(samples) - len(pending)}/{len(samples)} already cached, fetching {len(pending)} remaining...")
    semaphore = asyncio.Semaphore(ELEVATION_CONCURRENCY)
    done = 0

    async def _worker(client: httpx.AsyncClient, s: dict) -> None:
        nonlocal done
        async with semaphore:
            grad = await _elevation_gradient_pct(client, s["lat"], s["lon"])
        done += 1
        if grad is None:
            print(f"  [grad {done}/{len(pending)}] {s['key']}: fetch failed, will retry on next run")
            return
        cache[f"grad_{s['key']}"] = grad
        _save_cache(cache)
        if done % 100 == 0 or done == len(pending):
            print(f"  [grad {done}/{len(pending)}] {s['key']}: {grad}%")

    async with httpx.AsyncClient() as client:
        await asyncio.gather(*(_worker(client, s) for s in pending))
    print("Elevation pass complete.")


def assemble_dataset(samples: List[dict], all_points: List[Tuple[float, float]]) -> None:
    """Combines whatever rainfall + elevation values have been cached so far
    into the final training CSV - safe to re-run repeatedly as more of the
    two passes complete."""
    cache = _load_cache()
    index_by_key = {s["key"]: idx for idx, s in enumerate(s for s in samples if s["kind"] == "pos")}
    rows = []
    incomplete = 0
    for s in samples:
        rain = cache.get(f"rain_{s['key']}")
        soil = cache.get(f"soil_{s['key']}")
        grad = cache.get(f"grad_{s['key']}")
        if rain is None or soil is None or grad is None:
            incomplete += 1
            continue
        monsoon = 1 if s["date"].month in MONSOON_MONTHS else 0
        if s["kind"] == "pos":
            exclude_index = index_by_key.get(s["key"])
        elif s["kind"] == "neg":
            exclude_index = int(s["key"].split("_")[1])  # neg_N shares pos_N's location, exclude it from its own density count
        else:
            exclude_index = -1  # spatial sample has no corresponding positive to exclude
        rate = _historical_incident_rate(s["lat"], s["lon"], all_points, exclude_index=exclude_index)
        label = _severity_label(s["size"]) if s["kind"] == "pos" else (1 if rain >= MODERATE_RAIN_NO_INCIDENT_THRESHOLD_MM else 0)
        rows.append({
            "rainfall_mm_24h": round(rain, 1),
            "elevation_gradient_pct": grad,
            "soil_saturation_idx": soil,
            "historical_incident_rate": rate,
            "monsoon_month": monsoon,
            "label": label,
            # Not model features - written for train_risk_model.py's
            # GroupKFold/spatial-holdout validation, same rationale as the
            # earthquake/flood builders' year/grid_cell columns.
            "year": s["date"].year,
            "grid_cell": f"{int(s['lat'])}_{int(s['lon'])}",
        })

    with open(OUTPUT_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[*FEATURE_NAMES, "label", "year", "grid_cell"])
        writer.writeheader()
        writer.writerows(rows)

    label_counts = Counter(r["label"] for r in rows)
    print(f"\nAssembled {len(rows)} rows to {OUTPUT_PATH.name} ({incomplete} rows still missing rainfall and/or elevation)")
    print("Label distribution:", {RISK_LABELS[k]: v for k, v in sorted(label_counts.items())})


async def _main() -> None:
    incidents = _load_incidents()
    print(f"Loaded {len(incidents)} real recorded landslides from {CSV_PATH.name}")
    all_points = [(i["lat"], i["lon"]) for i in incidents]
    samples = _plan_samples(incidents)
    print(
        f"Planned {len(samples)} training rows "
        f"({sum(1 for s in samples if s['kind'] == 'pos')} positive, "
        f"{sum(1 for s in samples if s['kind'] == 'neg')} location-tied negative, "
        f"{sum(1 for s in samples if s['kind'] == 'spatial')} spatial negative)"
    )

    await fetch_rainfall_pass(samples)
    await fetch_elevation_pass(samples)
    assemble_dataset(samples, all_points)


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
