"""
Lightweight SQLite persistence for hazard reports and SOS alerts.

Every hazard/SOS record is written to backend/logistics.db as it comes in,
and reloaded on server startup (see the lifespan handler in main.py) so
reports survive a restart. routing.py queries hazards fresh on every route
calculation (rather than caching them in memory) so an expired report stops
affecting routes immediately, without needing a restart.

Hazard reports carry two extra fields to keep a single false or stale report
from permanently blocking a corridor:
  - confirmations: starts at 1 (the reporter); a second nearby report of the
    same type (see find_nearby_hazard) bumps this instead of creating a
    duplicate row, and refreshes the report's clock.
  - ttl_hours: the report is treated as active only until created_at +
    ttl_hours (see _is_expired) - past that it's excluded from
    get_active_hazards()/get_incident_points() entirely, i.e. it "expires".
"""
from __future__ import annotations

import math
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

DB_PATH = Path(__file__).parent / "logistics.db"
DEFAULT_HAZARD_TTL_HOURS = 12.0
NEARBY_HAZARD_RADIUS_KM = 3.0
SOS_DEDUP_RADIUS_KM = 2.0
SOS_DEDUP_WINDOW_MINUTES = 5.0


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(d_lambda / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _is_expired(created_at: str, ttl_hours: float) -> bool:
    created = datetime.fromisoformat(created_at)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= created + timedelta(hours=ttl_hours)


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hazards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                severity TEXT NOT NULL DEFAULT 'MODERATE',
                description TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                confirmations INTEGER NOT NULL DEFAULT 1,
                ttl_hours REAL NOT NULL DEFAULT 12.0
            )
            """
        )
        # Migrate a DB created before confirmations/ttl_hours existed.
        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(hazards)")}
        if "confirmations" not in existing_cols:
            conn.execute("ALTER TABLE hazards ADD COLUMN confirmations INTEGER NOT NULL DEFAULT 1")
        if "ttl_hours" not in existing_cols:
            conn.execute(f"ALTER TABLE hazards ADD COLUMN ttl_hours REAL NOT NULL DEFAULT {DEFAULT_HAZARD_TTL_HOURS}")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sos_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                truck_id TEXT,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                cargo TEXT,
                reason TEXT,
                source TEXT NOT NULL,
                raw_message TEXT,
                reported_by TEXT,
                received_at TEXT NOT NULL
            )
            """
        )
        # Migrate a DB created before the WhatsApp voice SOS triage fields existed.
        existing_sos_cols = {row["name"] for row in conn.execute("PRAGMA table_info(sos_alerts)")}
        for col in ("urgency", "action_needed", "summary"):
            if col not in existing_sos_cols:
                conn.execute(f"ALTER TABLE sos_alerts ADD COLUMN {col} TEXT")
        if "status" not in existing_sos_cols:
            conn.execute("ALTER TABLE sos_alerts ADD COLUMN status TEXT NOT NULL DEFAULT 'PENDING'")
        # Migrate a DB created before the recovery (after-phase) fields existed.
        for col in ("dispatched_at", "resolved_at", "outcome_type", "outcome_note"):
            if col not in existing_sos_cols:
                conn.execute(f"ALTER TABLE sos_alerts ADD COLUMN {col} TEXT")
        # Non-emergency WhatsApp voice messages the AI triage step rejects -
        # never becomes an sos_alerts row (so it can't show up as an active
        # SOS/on the map), kept here only so the Command Center can surface a
        # brief "false alarm rejected" notification instead of it vanishing
        # with zero trace anywhere in the system.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rejected_voice_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT,
                reason TEXT,
                raw_message TEXT,
                received_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS drivers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone_number TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )
            """
        )
        # No expiry column here on purpose - the agreed design is "stays
        # logged in until explicit sign-out", not a timeout.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS driver_sessions (
                token TEXT PRIMARY KEY,
                phone_number TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )


def insert_hazard(
    type_: str,
    latitude: float,
    longitude: float,
    description: str = "",
    severity: str = "MODERATE",
    ttl_hours: float = DEFAULT_HAZARD_TTL_HOURS,
) -> dict:
    created_at = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO hazards (type, latitude, longitude, severity, description, created_at, confirmations, ttl_hours)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
            (type_, latitude, longitude, severity, description, created_at, ttl_hours),
        )
        return {
            "id": cur.lastrowid, "type": type_, "latitude": latitude, "longitude": longitude,
            "severity": severity, "description": description, "created_at": created_at,
            "confirmations": 1, "ttl_hours": ttl_hours,
        }


def find_nearby_hazard(type_: str, latitude: float, longitude: float, radius_km: float = NEARBY_HAZARD_RADIUS_KM) -> Optional[dict]:
    """Finds the nearest still-active hazard of the same type within radius_km,
    so a second driver's report confirms an existing one instead of duplicating it."""
    with _connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM hazards WHERE type = ?", (type_,))]
    nearest, nearest_km = None, None
    for row in rows:
        if _is_expired(row["created_at"], row["ttl_hours"]):
            continue
        distance_km = _haversine_km(latitude, longitude, row["latitude"], row["longitude"])
        if distance_km <= radius_km and (nearest is None or distance_km < nearest_km):
            nearest, nearest_km = row, distance_km
    return nearest


def confirm_hazard(hazard_id: int) -> dict:
    """Bumps a hazard's confirmation count and refreshes created_at, so a
    hazard that's still being reported doesn't expire out from under it."""
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute("UPDATE hazards SET confirmations = confirmations + 1, created_at = ? WHERE id = ?", (now, hazard_id))
        row = conn.execute("SELECT * FROM hazards WHERE id = ?", (hazard_id,)).fetchone()
        return dict(row)


def find_recent_duplicate_sos(
    reported_by: Optional[str],
    latitude: float,
    longitude: float,
    radius_km: float = SOS_DEDUP_RADIUS_KM,
    window_minutes: float = SOS_DEDUP_WINDOW_MINUTES,
) -> Optional[dict]:
    """Finds this same reporter's own most recent SOS if it was sent from
    nearby within the last window_minutes - so mashing the SOS button (or a
    flaky connection retrying the same request) confirms/returns the
    existing alert instead of flooding the map with duplicates. Scoped to
    reported_by so it can never suppress a second driver's genuinely
    separate distress call from the same spot."""
    if not reported_by:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    with _connect() as conn:
        rows = [
            dict(r) for r in conn.execute(
                "SELECT * FROM sos_alerts WHERE reported_by = ? ORDER BY id DESC", (reported_by,)
            )
        ]
    for row in rows:
        received = datetime.fromisoformat(row["received_at"])
        if received.tzinfo is None:
            received = received.replace(tzinfo=timezone.utc)
        if received < cutoff:
            break  # rows are newest-first, so nothing after this is in-window either
        if _haversine_km(latitude, longitude, row["latitude"], row["longitude"]) <= radius_km:
            return row
    return None


def insert_sos_alert(
    truck_id: Optional[str],
    latitude: float,
    longitude: float,
    cargo: str,
    reason: str,
    source: str,
    raw_message: Optional[str] = None,
    reported_by: Optional[str] = None,
    urgency: Optional[str] = None,
    action_needed: Optional[str] = None,
    summary: Optional[str] = None,
) -> dict:
    duplicate = find_recent_duplicate_sos(reported_by, latitude, longitude)
    if duplicate is not None:
        return duplicate

    received_at = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO sos_alerts (truck_id, latitude, longitude, cargo, reason, source, raw_message, reported_by, received_at, urgency, action_needed, summary)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (truck_id, latitude, longitude, cargo, reason, source, raw_message, reported_by, received_at, urgency, action_needed, summary),
        )
        return {
            "id": cur.lastrowid, "truck_id": truck_id, "latitude": latitude, "longitude": longitude,
            "cargo": cargo, "reason": reason, "source": source, "raw_message": raw_message,
            "reported_by": reported_by, "received_at": received_at,
            "urgency": urgency, "action_needed": action_needed, "summary": summary,
        }


def insert_rejected_voice_message(phone: Optional[str], reason: str, raw_message: Optional[str]) -> dict:
    received_at = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO rejected_voice_messages (phone, reason, raw_message, received_at) VALUES (?, ?, ?, ?)",
            (phone, reason, raw_message, received_at),
        )
        return {"id": cur.lastrowid, "phone": phone, "reason": reason, "raw_message": raw_message, "received_at": received_at}


def get_recent_rejected_voice_messages(limit: int = 20) -> List[dict]:
    """Most recent first - the Command Center only needs enough to flash a
    notification for whichever one(s) just arrived since its last poll."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM rejected_voice_messages ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]


def get_active_hazards() -> List[dict]:
    """Hazards that haven't passed their ttl_hours expiry yet - a cleared road
    stops being reported as blocked once its report expires, without needing
    a driver to explicitly retract it."""
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM hazards ORDER BY id").fetchall()
        return [dict(row) for row in rows if not _is_expired(row["created_at"], row["ttl_hours"])]


def get_incident_points() -> List[Tuple[float, float]]:
    """(lat, lon) of every still-active hazard plus every persisted SOS alert
    (SOS history doesn't expire - a past distress call stays real history) -
    the accumulated incident data behind the risk model's historical_incident_rate feature."""
    with _connect() as conn:
        hazard_rows = conn.execute("SELECT latitude, longitude, created_at, ttl_hours FROM hazards").fetchall()
        sos_rows = conn.execute("SELECT latitude, longitude FROM sos_alerts").fetchall()
    active_hazards = [(r["latitude"], r["longitude"]) for r in hazard_rows if not _is_expired(r["created_at"], r["ttl_hours"])]
    return active_hazards + [(r["latitude"], r["longitude"]) for r in sos_rows]


def get_all_sos_alerts() -> List[dict]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM sos_alerts ORDER BY id").fetchall()
        return [dict(row) for row in rows]


def set_sos_status(
    alert_id: int,
    status: str,
    outcome_type: Optional[str] = None,
    outcome_note: Optional[str] = None,
) -> Optional[dict]:
    """Direct status transition for one SOS alert (PENDING -> DISPATCHED ->
    RESOLVED) - no clustering, no aggregation, just this one row. Stamps
    dispatched_at/resolved_at the first time each transition happens, and
    records the outcome_type/outcome_note captured when resolving (the
    "after"-phase damage/outcome note) - these feed get_recovery_stats()."""
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        if status == "DISPATCHED":
            conn.execute(
                "UPDATE sos_alerts SET status = ?, dispatched_at = COALESCE(dispatched_at, ?) WHERE id = ?",
                (status, now, alert_id),
            )
        elif status == "RESOLVED":
            conn.execute(
                """UPDATE sos_alerts
                   SET status = ?, resolved_at = ?, outcome_type = ?, outcome_note = ?
                   WHERE id = ?""",
                (status, now, outcome_type, outcome_note, alert_id),
            )
        else:
            conn.execute("UPDATE sos_alerts SET status = ? WHERE id = ?", (status, alert_id))
        row = conn.execute("SELECT * FROM sos_alerts WHERE id = ?", (alert_id,)).fetchone()
        return dict(row) if row else None


def get_active_sos_count() -> int:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM sos_alerts WHERE status = 'PENDING'").fetchone()[0]


def get_active_sos_alerts() -> List[dict]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM sos_alerts WHERE status = 'PENDING' ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]


def bulk_set_sos_status(alert_ids: List[int], status: str) -> None:
    """Additive helper for heat_zones.py's group dispatch - one query, does
    not touch set_sos_status or any existing single-alert path. Also stamps
    dispatched_at (mirroring set_sos_status) so a group dispatch's response
    time still counts toward get_recovery_stats()."""
    if not alert_ids:
        return
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        placeholders = ",".join("?" * len(alert_ids))
        if status == "DISPATCHED":
            conn.execute(
                f"UPDATE sos_alerts SET status = ?, dispatched_at = COALESCE(dispatched_at, ?) WHERE id IN ({placeholders})",
                (status, now, *alert_ids),
            )
        else:
            conn.execute(f"UPDATE sos_alerts SET status = ? WHERE id IN ({placeholders})", (status, *alert_ids))


def get_resolved_sos_alerts() -> List[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM sos_alerts WHERE status = 'RESOLVED' ORDER BY resolved_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]


def get_recovery_stats() -> dict:
    """The "after"-phase recovery numbers, computed on the fly from
    sos_alerts timestamps - no separate aggregate table to fall out of sync."""
    with _connect() as conn:
        resolved_rows = [dict(r) for r in conn.execute("SELECT * FROM sos_alerts WHERE status = 'RESOLVED'")]
        dispatched_rows = [
            dict(r) for r in conn.execute("SELECT * FROM sos_alerts WHERE dispatched_at IS NOT NULL")
        ]

    def _minutes_between(start_iso: str, end_iso: str) -> float:
        start = datetime.fromisoformat(start_iso)
        end = datetime.fromisoformat(end_iso)
        return (end - start).total_seconds() / 60.0

    response_times = [
        _minutes_between(r["received_at"], r["dispatched_at"]) for r in dispatched_rows if r["received_at"]
    ]
    recovery_times = [
        _minutes_between(r["received_at"], r["resolved_at"]) for r in resolved_rows if r["received_at"]
    ]
    today = datetime.now(timezone.utc).date()
    resolved_today = sum(
        1 for r in resolved_rows if datetime.fromisoformat(r["resolved_at"]).date() == today
    )

    return {
        "resolved_count": len(resolved_rows),
        "resolved_today": resolved_today,
        "avg_response_minutes": round(sum(response_times) / len(response_times), 1) if response_times else None,
        "avg_recovery_minutes": round(sum(recovery_times) / len(recovery_times), 1) if recovery_times else None,
    }


def get_or_create_driver(phone_number: str) -> int:
    """Returns the driver id for phone_number, creating a row the first
    time this number ever completes a real Twilio Verify login."""
    with _connect() as conn:
        row = conn.execute("SELECT id FROM drivers WHERE phone_number = ?", (phone_number,)).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO drivers (phone_number, created_at) VALUES (?, ?)",
            (phone_number, datetime.now(timezone.utc).isoformat()),
        )
        return cur.lastrowid


def create_driver_session(phone_number: str) -> str:
    """Issues a new opaque session token for an already-verified phone
    number - called right after check_verification_code (driver_auth.py)
    reports success, never before."""
    token = secrets.token_urlsafe(32)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO driver_sessions (token, phone_number, created_at) VALUES (?, ?, ?)",
            (token, phone_number, datetime.now(timezone.utc).isoformat()),
        )
    return token


def get_driver_by_session(token: str) -> Optional[str]:
    """Resolves a session token to the real, Twilio-verified phone number
    behind it - this is what lets /api/sos stop trusting whatever truck_id
    the client sends and use a server-known identity instead."""
    with _connect() as conn:
        row = conn.execute("SELECT phone_number FROM driver_sessions WHERE token = ?", (token,)).fetchone()
        return row["phone_number"] if row else None


def delete_driver_session(token: str) -> None:
    """Sign-out - removes the session row so the old token can't be reused,
    rather than just having the client forget it locally."""
    with _connect() as conn:
        conn.execute("DELETE FROM driver_sessions WHERE token = ?", (token,))
