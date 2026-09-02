"""
Downloads a real historical earthquake catalog for the India region from
USGS's FDSNWS Event API (earthquake.usgs.gov) - free, no API key, and
(unlike the NASA Global Landslide Catalog used for the landslide model)
both the historical archive AND the live feed come from this same source.

Bounding box covers India plus immediate neighbors (Pakistan, Nepal,
Bangladesh, Myanmar, Afghanistan) since seismic zones don't respect borders
and cross-border events still shake Indian corridors. M>=4.0, 1970-present
(the modern well-instrumented era) - paginated in yearly chunks since the
API caps a single query at 20,000 events and the full range returns ~23,600.

Writes ../india_usgs_earthquakes.csv, consumed by build_earthquake_training_data.py.
Run directly:
    cd backend && python ml/fetch_earthquake_catalog.py
"""
from __future__ import annotations

import csv
import time
from pathlib import Path

import httpx

OUTPUT_PATH = Path(__file__).parent.parent / "india_usgs_earthquakes.csv"
QUERY_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"

MIN_MAGNITUDE = 4.0
START_YEAR = 1970
END_YEAR = 2026
BBOX = {"minlatitude": 6, "maxlatitude": 38, "minlongitude": 68, "maxlongitude": 98}

FIELDNAMES = ["event_id", "time", "latitude", "longitude", "depth_km", "magnitude", "place"]


def _fetch_year(client: httpx.Client, year: int) -> list[dict]:
    resp = client.get(
        QUERY_URL,
        params={
            "format": "geojson",
            "starttime": f"{year}-01-01",
            "endtime": f"{year + 1}-01-01",
            "minmagnitude": MIN_MAGNITUDE,
            **BBOX,
        },
        timeout=60,
    )
    resp.raise_for_status()
    features = resp.json()["features"]
    rows = []
    for f in features:
        props, geom = f["properties"], f["geometry"]
        lon, lat, depth = geom["coordinates"][:3]
        rows.append({
            "event_id": f["id"],
            "time": props["time"],  # epoch ms UTC
            "latitude": lat,
            "longitude": lon,
            "depth_km": depth,
            "magnitude": props["mag"],
            "place": props.get("place") or "",
        })
    return rows


def main() -> None:
    all_rows = []
    with httpx.Client() as client:
        for year in range(START_YEAR, END_YEAR + 1):
            rows = _fetch_year(client, year)
            all_rows.extend(rows)
            print(f"  {year}: {len(rows)} events (running total {len(all_rows)})")
            time.sleep(0.3)

    with open(OUTPUT_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(all_rows)
    print(f"\nWrote {len(all_rows)} earthquakes (M>={MIN_MAGNITUDE}, {START_YEAR}-{END_YEAR}) to {OUTPUT_PATH.name}")


if __name__ == "__main__":
    main()
