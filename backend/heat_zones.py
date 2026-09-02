"""
Isolated, read-only aggregation on top of existing PENDING sos_alerts rows -
no schema changes, no writes, no touching the main SOS flow. Pure stdlib
math (haversine), computed fresh on each request.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import List

RADIUS_KM = 5.0
WINDOW_HOURS = 2
MIN_GROUP_SIZE = 2


def _haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dlambda = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def compute_heat_zones(alerts: List[dict]) -> List[dict]:
    now = datetime.now(timezone.utc)
    recent = [a for a in alerts if (now - datetime.fromisoformat(a["received_at"])).total_seconds() <= WINDOW_HOURS * 3600]

    used, zones = set(), []
    for a in recent:
        if a["id"] in used:
            continue
        group = [a] + [
            b for b in recent
            if b["id"] not in used and b["id"] != a["id"]
            and _haversine_km(a["latitude"], a["longitude"], b["latitude"], b["longitude"]) <= RADIUS_KM
        ]
        if len(group) < MIN_GROUP_SIZE:
            continue
        used.update(g["id"] for g in group)

        hazard_counts: dict = {}
        for g in group:
            h = g.get("cargo") or "Unknown"
            hazard_counts[h] = hazard_counts.get(h, 0) + 1
        top_hazard = max(hazard_counts, key=hazard_counts.get)

        zones.append({
            "zone_id": f"HZ-{group[0]['id']}",
            "center_lat": round(sum(g["latitude"] for g in group) / len(group), 4),
            "center_lon": round(sum(g["longitude"] for g in group) / len(group), 4),
            "total_reports": len(group),
            "alert_ids": [g["id"] for g in group],
            "top_hazard": top_hazard,
            "severity": "CRITICAL" if len(group) >= 4 else "HIGH",
            "label": f"{len(group)} pending distress calls within {RADIUS_KM:.0f}km",
        })
    return zones
