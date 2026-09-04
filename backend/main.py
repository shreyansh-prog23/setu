"""
SETU - Pan-India Disaster Logistics Backend
SIH Disaster Management App (landslide/earthquake/flood/cyclone risk)

Combines:
  - Emergency SMS Dispatcher Gateway: accepts SOS alerts via a JSON API
    (online clients) or via SMS (Twilio webhook fallback for low-connectivity
    zones). Compressed SMS format: "SOS|LAT,LNG|CARGO|REASON"
    Example: "SOS|13.0827,80.2707|MED_CRITICAL|COASTAL_ROAD_FLOODED"
  - Live-traffic truck routing via TomTom, with multi-hazard risk scoring
    (see routing.py / multi_hazard.py / schemas.py). Requires TOMTOM_API_KEY -
    see README instructions at the bottom of this file.

Every endpoint below requires basic API authentication (see security.py):
  - Frontend-facing endpoints need an 'X-API-Key' header matching
    SETU_API_KEY (backend/.env). The Vite frontend reads its copy from
    VITE_SETU_API_KEY (root .env.local) via src/apiClient.js.
  - The two Twilio webhooks (/api/sms-webhook, /api/whatsapp-webhook) are
    deliberately NOT gated behind any key - an emergency intake channel
    shouldn't have an auth barrier between a driver in distress and getting
    help, same reasoning as why the online SOS button only needs a logged-in
    driver, not a shared secret on top of that.
  - POST /api/sos additionally requires a driver to be logged in (see
    driver_auth.py) - an 'X-Driver-Token' header from a completed
    /api/driver/login/verify call. Requires TWILIO_VERIFY_SERVICE_SID (a
    Twilio Verify Service, configured once in the Twilio console).
    DRIVER_LOGIN_BYPASS_NUMBERS (comma-separated E.164 numbers) +
    DRIVER_LOGIN_BYPASS_CODE let a specific allowlist "verify" with a fixed
    code and no real Twilio SMS - for demoing with numbers that aren't
    Verified Caller IDs on the trial Twilio account, not a security feature.
"""

import logging
import os
from contextlib import asynccontextmanager
from typing import List, Optional

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field

import database
import driver_auth
from geocoding import geocode_search, reverse_geocode
from multi_hazard import evaluate_point_with_trend
from earthquake_engine import load_earthquake_model
from flood_engine import load_flood_model
from risk_engine import load_risk_model
from routing import RoutingServiceError, calculate_route
from schemas import RouteRequest, RouteResponse
from heat_zones import compute_heat_zones
from voice_service import process_voice_sos
from security import verify_api_key, verify_driver_session

load_dotenv()  # picks up TOMTOM_API_KEY etc. from a .env file if present

logger = logging.getLogger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_risk_model()  # loads backend/ml/risk_model.joblib (landslide)
    load_earthquake_model()  # loads backend/ml/earthquake_risk_model.joblib
    load_flood_model()  # loads backend/ml/flood_risk_model.joblib
    # Cyclone risk is rule-based (see ml/cyclone_risk_rules.py) - no model to load.
    database.init_db()  # creates backend/logistics.db tables if they don't exist yet

    # Restore SOS alerts persisted from a previous run. Hazards aren't seeded
    # into memory - routing.py reads them fresh from SQLite on every route
    # calculation so an expired report drops out immediately (see database.py).
    for row in database.get_all_sos_alerts():
        _register_alert_row(row)

    yield


app = FastAPI(title="SETU Disaster Logistics Backend", lifespan=lifespan)

# ALLOWED_ORIGINS (comma-separated, optional) adds production origins - e.g.
# the deployed Vercel URL - on top of the local dev ones below, so the same
# code works unmodified in both environments.
_extra_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173", "http://localhost:5174", *_extra_origins],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class Alert(BaseModel):
    id: int
    lat: float
    lng: float
    cargo: str
    reason: str
    source: str
    raw_message: Optional[str] = None
    reported_by: Optional[str] = None
    received_at: str
    urgency: Optional[str] = None
    action_needed: Optional[str] = None
    summary: Optional[str] = None
    status: str = "PENDING"
    dispatched_at: Optional[str] = None
    resolved_at: Optional[str] = None
    outcome_type: Optional[str] = None
    outcome_note: Optional[str] = None
    people_affected: Optional[int] = None


class HazardReport(BaseModel):
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    type: str
    description: str = ""
    severity: str = Field(default="MODERATE")
    ttl_hours: float = Field(
        default=database.DEFAULT_HAZARD_TTL_HOURS, ge=1, le=168,
        description="Hours before this report auto-expires and stops affecting routing/risk scoring.",
    )


class HazardRecord(BaseModel):
    id: int
    type: str
    latitude: float
    longitude: float
    severity: str
    description: str
    created_at: str
    confirmations: int
    ttl_hours: float


class DispatchRequest(BaseModel):
    status: str = Field(default="DISPATCHED", description="Target status, e.g. 'DISPATCHED' or 'RESOLVED'.")
    outcome_type: Optional[str] = Field(default=None, description="Only meaningful when status='RESOLVED', e.g. 'CLEARED'/'EVACUATED'/'CARGO_LOST'/'OTHER'.")
    outcome_note: Optional[str] = Field(default=None, description="Short free-text outcome/damage note, only meaningful when status='RESOLVED'.")


class RecoveryStats(BaseModel):
    resolved_count: int
    resolved_today: int
    avg_response_minutes: Optional[float] = None
    avg_recovery_minutes: Optional[float] = None


class ActiveSosResponse(BaseModel):
    active_sos_count: int
    alerts: List[Alert]


class SOSReport(BaseModel):
    # No truck_id field - the reporter's identity comes from their verified
    # driver session (see verify_driver_session/create_sos below), not
    # anything the client asserts in the request body.
    # Bounded to India's bbox (matches the frontend map's INDIA_BOUNDS), not
    # just "a valid Earth coordinate" - a real distress call can't originate
    # outside the country this system covers.
    latitude: float = Field(..., ge=5.0, le=38.0)
    longitude: float = Field(..., ge=66.0, le=99.0)
    timestamp: str
    priority: str = Field(..., description="Cargo/aid priority, e.g. 'Emergency Medical Supplies'")
    status: str = Field(default="DISPATCH_TRIGGERED")
    # The frontend (DriverView.jsx dispatchSos) has always sent these three,
    # but they weren't declared here - Pydantic silently drops undeclared
    # fields, so a driver's typed description ("3 people trapped...") never
    # actually reached the database. Declared now so create_sos below can
    # actually use them.
    incident_type: Optional[str] = None
    severity: Optional[str] = None
    notes: Optional[str] = None
    people_affected: Optional[int] = None


class DriverLoginStartRequest(BaseModel):
    phone_number: str = Field(..., description="E.164 format, e.g. '+919812345678'")


class DriverLoginVerifyRequest(BaseModel):
    phone_number: str
    code: str


class DriverLoginResponse(BaseModel):
    token: str
    phone_number: str


alerts_db: List[Alert] = []


def _alert_from_row(row: dict) -> Alert:
    """Builds an Alert from a database.sos_alerts row (any insert_sos_alert,
    set_sos_status, or get_*_sos_alerts result). Direct, standalone alerts -
    no clustering, no aggregation, one row in, one Alert out."""
    return Alert(
        id=row["id"], lat=row["latitude"], lng=row["longitude"],
        cargo=row["cargo"] or "", reason=row["reason"] or "", source=row["source"],
        raw_message=row["raw_message"], reported_by=row["reported_by"], received_at=row["received_at"],
        urgency=row.get("urgency"), action_needed=row.get("action_needed"), summary=row.get("summary"),
        status=row.get("status") or "PENDING",
        dispatched_at=row.get("dispatched_at"), resolved_at=row.get("resolved_at"),
        outcome_type=row.get("outcome_type"), outcome_note=row.get("outcome_note"),
        people_affected=row.get("people_affected"),
    )


def _register_alert_row(row: dict) -> Alert:
    """_alert_from_row, plus syncing into the in-memory feed GET /api/alerts
    serves. Genuinely new alerts get appended; a row database.insert_sos_alert
    returned as an existing duplicate (see find_recent_duplicate_sos) already
    has an entry here, so it's updated in place instead - otherwise mashing
    the SOS button would dedupe in SQLite but still flood this feed with
    repeat entries all sharing the same id."""
    alert = _alert_from_row(row)
    for i, a in enumerate(alerts_db):
        if a.id == alert.id:
            alerts_db[i] = alert
            return alert
    alerts_db.append(alert)
    return alert


def _make_alert(lat: float, lng: float, cargo: str, reason: str, source: str,
                 raw_message: Optional[str] = None, reported_by: Optional[str] = None,
                 people_affected: Optional[int] = None) -> Alert:
    row = database.insert_sos_alert(
        truck_id=reported_by, latitude=lat, longitude=lng, cargo=cargo, reason=reason,
        source=source, raw_message=raw_message, reported_by=reported_by,
        people_affected=people_affected,
    )
    return _register_alert_row(row)


def _twiml(message: str) -> Response:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Message>{message}</Message></Response>"
    )
    return Response(content=xml, media_type="application/xml")


@app.post("/api/sms-webhook")
async def sms_webhook(From: str = Form(...), Body: str = Form(...)):
    parts = [p.strip() for p in Body.strip().split("|")]

    if len(parts) != 4 or parts[0].upper() != "SOS":
        return _twiml(
            "ALERT FORMAT ERROR. Use: SOS|LAT,LNG|CARGO|REASON"
        )

    _, coords, cargo, reason = parts

    try:
        lat_str, lng_str = coords.split(",")
        lat, lng = float(lat_str.strip()), float(lng_str.strip())
    except ValueError:
        return _twiml(
            "ALERT FORMAT ERROR: invalid coordinates. Use: SOS|LAT,LNG|CARGO|REASON"
        )

    alert = _make_alert(
        lat=lat,
        lng=lng,
        cargo=cargo,
        reason=reason,
        source="SMS FALLBACK ALERT",
        raw_message=Body,
        reported_by=From,
    )

    return _twiml(
        f"SOS RECEIVED (ID #{alert.id}). Location: {lat},{lng}. "
        f"Cargo: {cargo}. Help is being dispatched."
    )


@app.get("/api/alerts", response_model=List[Alert], dependencies=[Depends(verify_api_key)])
async def get_alerts():
    return alerts_db


@app.post("/api/alerts", dependencies=[Depends(verify_api_key)])
async def report_hazard(report: HazardReport):
    """Record a driver-reported road hazard so future /api/v1/routes/calculate
    calls automatically detour around it, and persist it to SQLite so it
    survives a server restart.

    A second report of the same hazard type within ~3km confirms the
    existing record (bumping its confirmation count and refreshing its
    expiry clock) instead of creating a duplicate - and every report expires
    after ttl_hours regardless, so one fake or stale report can't
    permanently block a corridor."""
    existing = database.find_nearby_hazard(report.type, report.latitude, report.longitude)
    if existing:
        record = database.confirm_hazard(existing["id"])
        is_new = False
    else:
        record = database.insert_hazard(
            report.type, report.latitude, report.longitude, report.description, report.severity, report.ttl_hours
        )
        is_new = True

    return {
        "status": "received" if is_new else "confirmed",
        "is_new": is_new,
        "type": record["type"],
        "description": record["description"],
        "severity": record["severity"],
        "latitude": record["latitude"],
        "longitude": record["longitude"],
        "confirmations": record["confirmations"],
        "ttl_hours": record["ttl_hours"],
        "active_hazards": len(database.get_active_hazards()),
    }


@app.get("/api/hazards", response_model=List[HazardRecord], dependencies=[Depends(verify_api_key)])
async def get_hazards():
    """Active (non-expired) driver-reported hazards, for the Command Center
    map/feed to poll - see POST /api/alerts above for how these are created
    and confirmed."""
    return database.get_active_hazards()


@app.get("/api/whatsapp/rejected", dependencies=[Depends(verify_api_key)])
async def get_rejected_voice_messages():
    """Recent WhatsApp voice messages the AI triage classified as not a real
    emergency (see /api/whatsapp-webhook) - never became an SOS alert, this
    is purely so the Command Center can flash a brief notification about
    them instead of them leaving zero trace anywhere."""
    return database.get_recent_rejected_voice_messages()


@app.post("/api/sos", response_model=Alert, dependencies=[Depends(verify_api_key)])
async def create_sos(report: SOSReport, driver_phone: str = Depends(verify_driver_session)):
    # reason used to always be report.status, which is the fixed literal
    # "DISPATCH_TRIGGERED" - every online SOS showed that same useless
    # string as its description on the Command Center instead of whatever
    # the driver actually typed (e.g. "3 people trapped, need rescue").
    return _make_alert(
        lat=report.latitude,
        lng=report.longitude,
        cargo=report.priority,
        reason=report.notes or "No additional details provided.",
        source="ONLINE SOS REPORT",
        reported_by=driver_phone,
        people_affected=report.people_affected,
    )


@app.post("/api/driver/login/start", dependencies=[Depends(verify_api_key)])
async def driver_login_start(body: DriverLoginStartRequest):
    """Sends a Twilio Verify SMS code to body.phone_number. Doesn't create
    or reveal whether this number has a driver account yet - that only
    happens on a successful /verify, same as any real login flow."""
    try:
        await driver_auth.send_verification_code(body.phone_number)
    except driver_auth.VerifyError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"sent": True}


@app.post("/api/driver/login/verify", response_model=DriverLoginResponse, dependencies=[Depends(verify_api_key)])
async def driver_login_verify(body: DriverLoginVerifyRequest):
    """Checks the code against Twilio Verify; on success, gets-or-creates
    the driver row for this phone number and issues a new session token -
    this is the one place a driver's phone number turns into a real,
    persistent identity in the system."""
    approved = await driver_auth.check_verification_code(body.phone_number, body.code)
    if not approved:
        raise HTTPException(status_code=401, detail="Incorrect or expired code.")
    database.get_or_create_driver(body.phone_number)
    token = database.create_driver_session(body.phone_number)
    return DriverLoginResponse(token=token, phone_number=body.phone_number)


@app.post("/api/driver/logout", dependencies=[Depends(verify_api_key)])
async def driver_logout(x_driver_token: Optional[str] = Header(None, alias="X-Driver-Token")):
    """Deletes the session row server-side, not just something the client
    forgets locally - so a stolen/old token can't keep being used after
    sign-out."""
    if x_driver_token:
        database.delete_driver_session(x_driver_token)
    return {"signed_out": True}


@app.post("/api/whatsapp-webhook")
async def whatsapp_webhook(
    From: str = Form(...),
    MediaUrl0: str = Form(...),
    AccountSid: str = Form(...),
    Latitude: Optional[str] = Form(None),
    Longitude: Optional[str] = Form(None),
):
    """Twilio WhatsApp voice-note webhook: fetches the audio, runs it through
    the Groq transcribe+triage pipeline (voice_service.py), and registers it
    as a normal SOS alert.

    Twilio's MediaUrl0 points at its own authenticated REST API
    (api.twilio.com/.../Media/...), not a public file - fetching it requires
    HTTP Basic Auth with the account SID (Twilio sends this in the webhook
    payload itself, as AccountSid) and the account's auth token (a secret,
    read from TWILIO_AUTH_TOKEN - never sent by Twilio, so it must be
    configured here)."""
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not auth_token:
        logger.error("WhatsApp webhook hit but TWILIO_AUTH_TOKEN is not set.")
        return _twiml("Voice SOS is not configured on the server yet - please call it in.")

    try:
        # Twilio's Media resource 307-redirects to a pre-signed, no-auth CDN
        # URL for the actual bytes - the Basic Auth is only for the initial
        # api.twilio.com hop, so redirects must be followed.
        async with httpx.AsyncClient(timeout=20.0, auth=(AccountSid, auth_token), follow_redirects=True) as client:
            resp = await client.get(MediaUrl0)
        resp.raise_for_status()

        lat = float(Latitude) if Latitude else None
        lon = float(Longitude) if Longitude else None
        row = await process_voice_sos(resp.content, phone=From, lat=lat, lon=lon)
    except Exception as exc:
        logger.error("WhatsApp voice SOS processing failed: %s", exc)
        return _twiml("Sorry, we couldn't process that voice message. Please try again or call the emergency line.")

    result = _finalize_voice_sos(row, From)
    if result.get("rejected"):
        return _twiml(
            f"We couldn't identify this as an emergency ({result['reason']}). "
            "If you need help, please describe what's happening and your location."
        )
    return _twiml(f"SOS received via voice message. {row['summary']}")


def _finalize_voice_sos(row: dict, phone: str) -> dict:
    """Shared by every real voice-SOS intake channel (Twilio webhook, the
    whatsapp-listener bridge below) - NOT the dev test-upload endpoint,
    which deliberately stays a minimal, side-effect-light path for manual
    testing. Logs a rejection to its own table (for the Command Center's
    "false alarm rejected" notification) or registers a real alert -
    exactly the two things a real intake channel needs and the dev upload
    endpoint intentionally skips."""
    if row.get("rejected"):
        database.insert_rejected_voice_message(phone=phone, reason=row["reason"], raw_message=row.get("raw_message"))
        logger.info("WhatsApp voice message rejected as non-emergency from %s: %s", phone, row["reason"])
        return row
    return _register_alert_row(row).model_dump()


@app.post("/api/voice-sos/upload", dependencies=[Depends(verify_api_key)])
async def voice_sos_upload(file: UploadFile = File(...), lat: Optional[float] = None, lon: Optional[float] = None):
    """Local test path for the voice SOS pipeline - upload an audio file
    directly instead of going through Twilio. No response_model (unlike the
    other Alert-returning endpoints) since a rejected non-emergency message
    returns a differently-shaped {"rejected": ...} dict instead of an Alert."""
    audio_bytes = await file.read()
    row = await process_voice_sos(audio_bytes, phone="TEST-UPLOAD", lat=lat, lon=lon, filename=file.filename or "sos.ogg")
    if row.get("rejected"):
        return row
    return _register_alert_row(row)


@app.post("/api/whatsapp-listener/voice-sos", dependencies=[Depends(verify_api_key)])
async def whatsapp_listener_voice_sos(file: UploadFile = File(...), phone: str = Form(...)):
    """Intake for the self-hosted whatsapp-listener (Baileys) bridge - an
    alternative to the Twilio webhook above for whoever is running that
    listener process. Unlike /api/voice-sos/upload (a dev-only test path
    that hardcodes a fake identity and skips rejection logging), this uses
    the real sender phone number end to end, exactly like the Twilio path:
    real reported_by/contact, real driver-history tracking, real
    false-alarm rejection notifications on the Command Center.

    Deliberately a separate endpoint rather than reusing the Twilio one -
    that one is shaped around Twilio's own webhook fields (MediaUrl0,
    AccountSid) and fetches the audio itself from Twilio's API; this one
    receives the audio file directly, already downloaded by the listener."""
    audio_bytes = await file.read()
    row = await process_voice_sos(audio_bytes, phone=phone, filename=file.filename or "sos.ogg")
    return _finalize_voice_sos(row, phone)


@app.post("/api/sos/{alert_id}/dispatch", response_model=Alert, dependencies=[Depends(verify_api_key)])
async def dispatch_sos(alert_id: int, body: DispatchRequest = DispatchRequest()):
    """Direct status transition for one SOS alert - no clustering, no
    aggregation. Defaults to 'DISPATCHED'; pass {"status": "RESOLVED"} to
    resolve it instead. Active SOS count (GET /api/sos/active) is simply
    COUNT(*) WHERE status = 'PENDING', so this is what makes that number
    decrement."""
    row = database.set_sos_status(alert_id, body.status, body.outcome_type, body.outcome_note)
    if row is None:
        raise HTTPException(status_code=404, detail=f"SOS alert {alert_id} not found")

    updated = _alert_from_row(row)
    for i, a in enumerate(alerts_db):
        if a.id == alert_id:
            alerts_db[i] = updated
            break
    return updated


@app.get("/api/sos/active", response_model=ActiveSosResponse, dependencies=[Depends(verify_api_key)])
async def get_active_sos():
    """PENDING SOS alerts only, plus the count - the number the Command
    Center header counter and the map's SOS pins are driven from. A fresh,
    direct COUNT(*)/SELECT each call - no cached aggregate to fall out of
    sync with reality."""
    return ActiveSosResponse(
        active_sos_count=database.get_active_sos_count(),
        alerts=[_alert_from_row(r) for r in database.get_active_sos_alerts()],
    )


@app.get("/api/sos/resolved", response_model=List[Alert], dependencies=[Depends(verify_api_key)])
async def get_resolved_sos():
    """The "after"-phase resolved feed - closed-out SOS alerts, most
    recently resolved first, for the Command Center's Resolved tab."""
    return [_alert_from_row(r) for r in database.get_resolved_sos_alerts()]


@app.get("/api/recovery/stats", response_model=RecoveryStats, dependencies=[Depends(verify_api_key)])
async def get_recovery_stats():
    """The "after"-phase recovery analytics panel's numbers - resolved
    counts and average response/recovery times, computed fresh from
    sos_alerts timestamps each call (see database.get_recovery_stats)."""
    return RecoveryStats(**database.get_recovery_stats())


class BulkDispatchRequest(BaseModel):
    alert_ids: List[int]


@app.get("/api/clusters/heat-zones", dependencies=[Depends(verify_api_key)])
async def get_heat_zones():
    """Isolated aggregation layer - reads existing PENDING alerts, groups
    them (5km/2hr, stdlib math only), touches no schema and no existing
    endpoint. Purely additive."""
    return compute_heat_zones(database.get_active_sos_alerts())


@app.post("/api/clusters/dispatch", dependencies=[Depends(verify_api_key)])
async def dispatch_heat_zone(body: BulkDispatchRequest):
    """Bulk status flip for a whole heat zone in one query - reuses the same
    'status' column /api/sos/{id}/dispatch already uses, so the active-SOS
    counter decrements correctly for each alert."""
    database.bulk_set_sos_status(body.alert_ids, "DISPATCHED")
    for i, a in enumerate(alerts_db):
        if a.id in body.alert_ids:
            alerts_db[i] = a.model_copy(update={"status": "DISPATCHED"})
    return {"dispatched": body.alert_ids}


@app.post("/api/v1/routes/calculate", dependencies=[Depends(verify_api_key)])
async def calculate_route_endpoint(payload: RouteRequest, geojson: bool = False):
    """
    Calculate a live-traffic truck route between origin and destination.

    Set ?geojson=true to receive a GeoJSON Feature<LineString> (ready for a
    Mapbox/Leaflet source) instead of the normalized RouteResponse schema.
    """
    try:
        result: RouteResponse = await calculate_route(payload)
    except RoutingServiceError as exc:
        logger.error("TomTom routing failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except RuntimeError as exc:
        # Misconfiguration, e.g. TOMTOM_API_KEY not set
        logger.error("Routing misconfigured: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return result.to_geojson_feature() if geojson else result


@app.get("/api/hazard-check", dependencies=[Depends(verify_api_key)])
async def hazard_check_endpoint(lat: float, lon: float):
    """
    Cheap single-point multi-hazard check for a live "you're entering a
    hazard-prone area" GPS alert (DriverView.jsx polls this while a trip is
    active) - no TomTom call, just the same live weather lookups routing.py
    already makes for one corridor point, plus a near-term forecast trend
    per weather-driven hazard (see multi_hazard.evaluate_point_with_trend).
    """
    return await evaluate_point_with_trend(lat, lon)


@app.get("/api/geocode/reverse", dependencies=[Depends(verify_api_key)])
async def geocode_reverse_endpoint(lat: float, lon: float):
    """
    Coordinates -> {"city", "state"} for labeling map markers (SOS/hazard
    pins) with a readable place name instead of raw lat/lon. Returns null
    fields if the lookup fails - callers should fall back to showing
    coordinates in that case, not block on it.
    """
    result = await reverse_geocode(lat, lon)
    return result or {"city": None, "state": None}


@app.get("/api/geocode/search", dependencies=[Depends(verify_api_key)])
async def geocode_search_endpoint(q: str):
    """
    Free-text place search for the frontend's location autocomplete
    (DriverView.jsx's origin/destination fields) - real geocoding across
    all of India via TomTom (see geocoding.py), not a fixed hub list.
    Returns [{"name", "lat", "lon"}, ...], newest/best match first.
    """
    return await geocode_search(q)
