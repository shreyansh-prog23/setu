"""
Downloads the Dartmouth Flood Observatory (DFO) Global Active Archive of
Large Flood Events (mirrored on HDX, free/no-key/CC-BY) - 4,029 real
recorded flood events worldwide, 1985-2010, with location, dates, deaths/
displaced, cause, and DFO's own severity/magnitude scoring per event.

Shipped as an Esri shapefile (.dbf attribute table + .shp geometry). We only
need the attribute table (it already carries a Centroid_X/Centroid_Y per
event), so this parses the .dbf directly with the stdlib rather than adding
a GIS dependency for one field lookup - the DBF format is a simple, fixed-
width binary layout (see _read_dbf below).

Filtered to a South Asia bounding box (same one used for the earthquake
catalog) - 120 of the 4,029 events fall inside it.

Writes ../india_dfo_floods.csv, consumed by build_flood_training_data.py.
Run directly:
    cd backend && python ml/fetch_flood_catalog.py
"""
from __future__ import annotations

import csv
import struct
import zipfile
from io import BytesIO
from pathlib import Path

import httpx

OUTPUT_PATH = Path(__file__).parent.parent / "india_dfo_floods.csv"
ARCHIVE_URL = (
    "https://data.humdata.org/dataset/1fd855de-57c6-42b3-83e1-9cf989b0f70d/"
    "resource/984cc240-b2b7-4266-9f61-5715a9e10ff5/download/"
    "wlf_nhr_fl_dfomasterlist_20190418.zip"
)
DBF_NAME = "wlf_nhr_fl_dfomasterlist_20190418.dbf"

BBOX = {"minlat": 6, "maxlat": 38, "minlon": 68, "maxlon": 98}
FIELDNAMES = ["event_id", "began", "ended", "country", "latitude", "longitude", "dead", "displaced", "main_cause", "severity", "magnitude"]


def _read_dbf(data: bytes) -> list[dict]:
    """Minimal reader for the subset of the DBF format (III/dBASE) this
    archive uses - fixed-width header + field descriptors + fixed-width
    records, no memo fields. See the DBF spec: each field descriptor is 32
    bytes (name, type, length), terminated by a 0x0D byte."""
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
        field_offset = 1  # first byte is the deletion flag
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
    print(f"Parsed {len(rows)} total flood records from the DFO archive")

    out_rows = []
    for r in rows:
        try:
            lat, lon = float(r["Centroid_Y"]), float(r["Centroid_X"])
        except ValueError:
            continue
        if not (BBOX["minlat"] <= lat <= BBOX["maxlat"] and BBOX["minlon"] <= lon <= BBOX["maxlon"]):
            continue
        out_rows.append({
            "event_id": r["Register__"],
            "began": r["Began"],  # YYYYMMDD
            "ended": r["Ended"],
            "country": r["Country__c"],
            "latitude": lat,
            "longitude": lon,
            "dead": r["Dead"] or "0",
            "displaced": r["Displaced"] or "0",
            "main_cause": r["Main_cause"],
            "severity": r["Severity__"],
            "magnitude": r["Magnitude"],
        })

    with open(OUTPUT_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(out_rows)
    print(f"Wrote {len(out_rows)} South-Asia flood events (1985-2010) to {OUTPUT_PATH.name}")


if __name__ == "__main__":
    main()
