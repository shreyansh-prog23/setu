"""
Downloads NOAA's IBTrACS (International Best Track Archive for Climate
Stewardship) v04r01 for the North Indian Ocean basin - the basin covering
the Bay of Bengal and Arabian Sea storms that hit India. Free, no key,
direct CSV, already pre-filtered by basin on NOAA's own server.

Each row is one 3-hourly track-point observation of an actual historical
storm (not one row per storm) - lat/lon/wind/pressure/distance-to-land at
that instant. DIST2LAND ships pre-computed by IBTrACS itself (km to the
nearest coastline), so no separate geocoding is needed for that feature.

Multiple forecasting agencies report wind/pressure for the same point with
very different completeness in this basin - USA_WIND (16,362 non-null) is
by far the most complete here, so it's used first, falling back to
NEWDELHI_WIND (IMD) then WMO_WIND. Restricted to 1980-present (the
satellite era IBTrACS itself recommends over earlier, less reliable years)
and rows with an actual wind reading.

Writes ../india_ibtracs_cyclones.csv, consumed by build_cyclone_training_data.py.
Run directly:
    cd backend && python ml/fetch_cyclone_catalog.py
"""
from __future__ import annotations

import csv
from pathlib import Path

import httpx

OUTPUT_PATH = Path(__file__).parent.parent / "india_ibtracs_cyclones.csv"
SOURCE_URL = (
    "https://www.ncei.noaa.gov/data/international-best-track-archive-for-climate-stewardship-"
    "ibtracs/v04r01/access/csv/ibtracs.NI.list.v04r01.csv"
)
MIN_SEASON = 1980

WIND_COLUMNS = ["USA_WIND", "NEWDELHI_WIND", "WMO_WIND"]
PRES_COLUMNS = ["USA_PRES", "NEWDELHI_PRES", "WMO_PRES"]
FIELDNAMES = ["sid", "name", "time", "latitude", "longitude", "wind_kt", "pressure_hpa", "dist2land_km"]


def _first_present(row: dict, columns: list[str]) -> str:
    for col in columns:
        val = row[col].strip()
        if val:
            return val
    return ""


def main() -> None:
    resp = httpx.get(SOURCE_URL, timeout=60)
    resp.raise_for_status()
    lines = resp.text.splitlines()
    reader = csv.DictReader(lines)
    rows = list(reader)[1:]  # first data row is a units row, not real data
    print(f"Downloaded {len(rows)} raw IBTrACS North Indian Ocean track points")

    out_rows = []
    for r in rows:
        try:
            season = int(r["SEASON"])
        except ValueError:
            continue
        if season < MIN_SEASON:
            continue
        wind = _first_present(r, WIND_COLUMNS)
        if not wind:
            continue
        try:
            lat, lon = float(r["LAT"]), float(r["LON"])
        except ValueError:
            continue
        out_rows.append({
            "sid": r["SID"],
            "name": r["NAME"],
            "time": r["ISO_TIME"],
            "latitude": lat,
            "longitude": lon,
            "wind_kt": wind,
            "pressure_hpa": _first_present(r, PRES_COLUMNS),
            "dist2land_km": r["DIST2LAND"],
        })

    with open(OUTPUT_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(out_rows)
    print(f"Wrote {len(out_rows)} track points ({MIN_SEASON}-present, with a wind reading) to {OUTPUT_PATH.name}")


if __name__ == "__main__":
    main()
