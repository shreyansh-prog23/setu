"""
Downloads two real, free, pre-digitized geophysical layers to give the
earthquake model actual seismology (not just density-of-past-quakes):

  - GEM Global Active Faults Database (GAF-DB) - github.com/GEMScienceTools/
    gem-global-active-faults, CC-BY-SA 4.0, ~13,500 fault traces worldwide.
    fault_dist_km (distance to nearest known active fault) is a standard
    real seismic-hazard input independent of recorded earthquake history.
  - India's BIS IS 1893:2016 seismic zone map (Zones II-V, each with an
    official "Z-factor" used in earthquake-resistant design codes) -
    digitized GeoJSON via bharatlas.com/data.gov.in (GODL-India license).
    Only 17 of its 173 polygons carry a zone label - the other 156 are tiny
    (~1km) unlabeled artifacts, almost certainly leftover digitization
    slivers, not real zone boundaries - so those are dropped here rather
    than silently treated as "unknown zone".

Both are filtered to the same South Asia bounding box used elsewhere and
saved as light local JSON (raw vertex lists, no GIS library needed to read
them back - see build_earthquake_training_data.py).

Writes ../india_active_faults.json and ../india_seismic_zones.json.
Run directly:
    cd backend && python ml/fetch_earthquake_geophysical.py
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx

FAULTS_OUTPUT = Path(__file__).parent.parent / "india_active_faults.json"
ZONES_OUTPUT = Path(__file__).parent.parent / "india_seismic_zones.json"

GAF_DB_URL = "https://raw.githubusercontent.com/GEMScienceTools/gem-global-active-faults/master/geojson/gem_active_faults.geojson"
BIS_ZONES_URL = "https://pub-0429b8e3b5a946e69ea007df844a6f1c.r2.dev/environment/seismic/Seismic_Zones.geojson"

BBOX = {"minlat": 6, "maxlat": 38, "minlon": 68, "maxlon": 98}

# Official Z-factors from IS 1893 (Part 1):2016, Table 3.
ZONE_FACTORS = {
    "Seismic Zone-II": 0.10,
    "Seismic Zone-III": 0.16,
    "Seismic Zone-IV": 0.24,
    "Seismic Zone-V": 0.36,
}


def _in_bbox(lat: float, lon: float) -> bool:
    return BBOX["minlat"] <= lat <= BBOX["maxlat"] and BBOX["minlon"] <= lon <= BBOX["maxlon"]


def fetch_faults() -> None:
    resp = httpx.get(GAF_DB_URL, timeout=60)
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

    FAULTS_OUTPUT.write_text(json.dumps(lines))
    print(f"Wrote {len(lines)} fault-line segments (South Asia bbox) to {FAULTS_OUTPUT.name}")


def fetch_zones() -> None:
    resp = httpx.get(BIS_ZONES_URL, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    zones = []
    dropped_unlabeled = 0
    for f in data["features"]:
        label = f["properties"].get("seismic_zo")
        if not label or label not in ZONE_FACTORS:
            dropped_unlabeled += 1
            continue
        geom = f["geometry"]
        rings = [geom["coordinates"][0]] if geom["type"] == "Polygon" else [poly[0] for poly in geom["coordinates"]]
        for ring in rings:
            zones.append({"zone": label, "z_factor": ZONE_FACTORS[label], "ring": [(lat, lon) for lon, lat, *_ in ring]})

    ZONES_OUTPUT.write_text(json.dumps(zones))
    print(f"Wrote {len(zones)} labeled seismic zone polygons to {ZONES_OUTPUT.name} ({dropped_unlabeled} unlabeled artifact polygons dropped)")


def main() -> None:
    fetch_faults()
    fetch_zones()


if __name__ == "__main__":
    main()
