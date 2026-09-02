"""
Downloads NASA's full Global Landslide Catalog (GLC) - the same catalog
northeast_nasa_landslides.csv was originally sliced from, mirrored on HDX
(CC-BY, free, no key) as a global export, 1970-2019, 11,033 records - and
filters it to a pan-India bounding box instead of just Northeast India.

This is the pivot task from NE-India-only to pan-India scope: 2,389 real
recorded landslides fall in the wider box vs. the 320 in the old NE-only
file - 7.5x more real training data for the same model.

Shipped as an Esri shapefile; only the .dbf attribute table is needed (same
approach as fetch_flood_catalog.py's minimal DBF reader - no GIS dependency).
Output columns match northeast_nasa_landslides.csv exactly, so
build_real_training_data.py only needs its CSV_PATH updated, not its parser.

Writes ../india_nasa_landslides.csv, consumed by build_real_training_data.py.
Run directly:
    cd backend && python ml/fetch_landslide_catalog.py
"""
from __future__ import annotations

import csv
import struct
import zipfile
from io import BytesIO
from pathlib import Path

import httpx

OUTPUT_PATH = Path(__file__).parent.parent / "india_nasa_landslides.csv"
ARCHIVE_URL = (
    "https://data.humdata.org/dataset/1eb911ba-3681-4a96-b025-ae0c33b80a12/"
    "resource/ed703c45-2001-4286-ba16-8248c17fec80/download/"
    "global_landslide_catalog_nasa.zip"
)
DBF_NAME = "global_landslide_catalog_NASA.dbf"

# Pan-India bounding box (was 21-29 lat / 88-98 lon, Northeast-India-only)
BBOX = {"minlat": 8.0, "maxlat": 37.0, "minlon": 68.0, "maxlon": 97.5}

# DBF field names truncated to 10 chars -> the original CSV's real column names.
FIELD_MAP = {
    "source_nam": "source_name", "source_lin": "source_link", "event_id": "event_id",
    "event_date": "event_date", "event_time": "event_time", "event_titl": "event_title",
    "event_desc": "event_description", "location_d": "location_description",
    "location_a": "location_accuracy", "landslide_": "landslide_category",
    "landslid_1": "landslide_trigger", "landslid_2": "landslide_size",
    "landslid_3": "landslide_setting", "fatality_c": "fatality_count",
    "injury_cou": "injury_count", "storm_name": "storm_name", "photo_link": "photo_link",
    "notes": "notes", "event_impo": "event_import_source", "event_im_1": "event_import_id",
    "country_na": "country_name", "country_co": "country_code",
    "admin_divi": "admin_division_name", "admin_di_1": "admin_division_population",
    "gazeteer_c": "gazeteer_closest_point", "gazeteer_d": "gazeteer_distance",
    "submitted_": "submitted_date", "created_da": "created_date",
    "last_edite": "last_edited_date", "longitude": "longitude", "latitude": "latitude",
}
FIELDNAMES = list(FIELD_MAP.values())


def _read_dbf(data: bytes) -> list[dict]:
    """Same minimal DBF reader as fetch_flood_catalog.py."""
    num_records, header_size, record_size = struct.unpack("<IHH", data[4:12])
    fields = []
    pos = 32
    while data[pos:pos + 1] != b"\r":
        name = data[pos:pos + 11].split(b"\x00")[0].decode("latin1")
        length = data[pos + 16]
        fields.append((name, length))
        pos += 32

    rows = []
    offset = header_size
    for _ in range(num_records):
        record = data[offset:offset + record_size]
        field_offset = 1
        row = {}
        for name, length in fields:
            row[name] = record[field_offset:field_offset + length].decode("latin1").strip()
            field_offset += length
        rows.append(row)
        offset += record_size
    return rows


def main() -> None:
    resp = httpx.get(ARCHIVE_URL, timeout=60, follow_redirects=True)
    resp.raise_for_status()
    with zipfile.ZipFile(BytesIO(resp.content)) as zf:
        dbf_bytes = zf.read(DBF_NAME)

    rows = _read_dbf(dbf_bytes)
    print(f"Parsed {len(rows)} total landslide records from the global GLC export")

    out_rows = []
    for r in rows:
        try:
            lat, lon = float(r["latitude"]), float(r["longitude"])
        except ValueError:
            continue
        if not (BBOX["minlat"] <= lat <= BBOX["maxlat"] and BBOX["minlon"] <= lon <= BBOX["maxlon"]):
            continue
        out_rows.append({FIELD_MAP[k]: v for k, v in r.items() if k in FIELD_MAP})

    with open(OUTPUT_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(out_rows)
    print(f"Wrote {len(out_rows)} pan-India landslide events to {OUTPUT_PATH.name}")


if __name__ == "__main__":
    main()
