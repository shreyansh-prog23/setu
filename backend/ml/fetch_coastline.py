"""
Downloads Natural Earth's public-domain 10m coastline dataset and filters it
to a wide South Asia bounding box - used for a real distance_to_coast_km
feature (how far a corridor point is from the actual coast), the same
"real geometry, not a heuristic" approach fetch_earthquake_geophysical.py
uses for fault-line distance.

Free, no key, direct GeoJSON download (public domain, github.com/nvkelso/
natural-earth-vector). Saved as light local JSON (raw vertex lists, no GIS
library needed to read it back).

Writes ../india_coastline.json.
Run directly:
    cd backend && python ml/fetch_coastline.py
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx

OUTPUT_PATH = Path(__file__).parent.parent / "india_coastline.json"
SOURCE_URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_coastline.geojson"

# Wider than the earthquake/flood bbox on purpose - a corridor point near
# India's coast still needs a correct nearest-coast distance even if the
# actual nearest coastline point is technically just outside India (Sri
# Lanka, Bangladesh's coast, etc).
BBOX = {"minlat": 0.0, "maxlat": 40.0, "minlon": 60.0, "maxlon": 100.0}


def _in_bbox(lat: float, lon: float) -> bool:
    return BBOX["minlat"] <= lat <= BBOX["maxlat"] and BBOX["minlon"] <= lon <= BBOX["maxlon"]


def main() -> None:
    resp = httpx.get(SOURCE_URL, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    lines = []
    for f in data["features"]:
        geom = f["geometry"]
        if geom["type"] == "LineString":
            rings = [geom["coordinates"]]
        elif geom["type"] == "MultiLineString":
            rings = geom["coordinates"]
        else:
            continue
        for ring in rings:
            points = [(lat, lon) for lon, lat, *_ in ring]
            if any(_in_bbox(lat, lon) for lat, lon in points):
                lines.append(points)

    OUTPUT_PATH.write_text(json.dumps(lines))
    print(f"Wrote {len(lines)} coastline segments (South Asia bbox) to {OUTPUT_PATH.name}")


if __name__ == "__main__":
    main()
