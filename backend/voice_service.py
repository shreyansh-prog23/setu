"""
WhatsApp Voice SOS ingestion: transcribes an inbound voice note with Groq
Whisper, extracts a structured triage summary with Llama 3.3, and persists
it as a normal SOS alert (source="whatsapp_voice") so it flows through the
existing /api/alerts feed and Command Center map like any other SOS.
"""
from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from typing import Optional, Tuple

from groq import AsyncGroq

import database
from geocoding import geocode_one

logger = logging.getLogger("voice_service")

# Used only if a caller supplies no lat/lon AND the real geocoding lookup
# below fails/finds nothing (e.g. speech-to-text mangled the place name
# beyond recognition, or the network call failed) - India's geographic
# centroid, not a guessed specific place, so a failed lookup doesn't
# silently pretend precision it doesn't have.
DEFAULT_FALLBACK_COORDS = (22.9734, 78.6569)

INCIDENT_TYPES = [
    "Landslide / Mudslide", "Earthquake", "Flood / Flash Flood", "Cyclone / Storm",
    "Fire", "Medical Emergency", "Road Accident", "Structural Collapse", "Other",
]

TRIAGE_PROMPT = (
    "You are a disaster-response triage assistant for India. Read "
    "the transcript of an inbound WhatsApp voice message and extract a JSON "
    "object with exactly these keys: is_emergency (boolean - true only if this "
    "genuinely describes a disaster, accident, medical emergency, or someone in "
    "danger needing help; false for casual chat, greetings, test messages, "
    "wrong numbers, unrelated questions, or anything not describing a real "
    "emergency), incident_type (pick the SINGLE closest match from exactly this "
    f"list: {', '.join(INCIDENT_TYPES)} - use 'Other' rather than guessing a "
    "specific hazard the transcript doesn't actually support (e.g. don't say "
    "'Fire' unless burning/smoke is actually mentioned - a flood, collapse, or "
    "accident described in a caller's own words is a different, easily-confused "
    "hazard and must not be relabeled as something more dramatic-sounding); "
    "empty string if not an emergency. urgency ('CRITICAL'|'HIGH'|'MODERATE', "
    "empty string if not an emergency), spoken_location (place name mentioned "
    "in the call, normalized to the modern official city name a map geocoder "
    "will actually recognize - e.g. Banaras/Kashi -> Varanasi, Bombay -> "
    "Mumbai, Calcutta -> Kolkata, Madras -> Chennai, Poona -> Pune, Baroda -> "
    "Vadodara, Trivandrum -> Thiruvananthapuram, Mysore -> Mysuru, Cawnpore -> "
    "Kanpur, Gurgaon -> Gurugram - a common colloquial/historical Indian name "
    "is not a transcription error but still needs this normalization, since a "
    "geocoder given the old/colloquial name can silently resolve to a wrong "
    "or generic location instead of the real city; empty string if none "
    "mentioned), action_needed (short string - what "
    "responders should do first, empty string if not an emergency), summary "
    "(one sentence - if not an emergency, briefly say what the message "
    "actually was instead; if it is, describe what the caller actually said "
    "happened, not the incident_type category - don't say 'reports a fire' "
    "unless the caller actually described fire). The transcript comes from "
    "speech-to-text and may mishear place names - normalize any phonetically "
    "garbled Indian place name to its standard spelling before returning "
    "spoken_location. Respond with JSON only."
)


@lru_cache
def _client() -> AsyncGroq:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is not set - required for WhatsApp voice SOS transcription.")
    return AsyncGroq(api_key=api_key)


async def _geocode(spoken_location: str, lat: Optional[float], lon: Optional[float]) -> Tuple[float, float]:
    if lat is not None and lon is not None:
        return lat, lon
    if spoken_location:
        resolved = await geocode_one(spoken_location)
        if resolved is not None:
            return resolved
    return DEFAULT_FALLBACK_COORDS


async def process_voice_sos(
    audio_bytes: bytes,
    phone: str = "",
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    filename: str = "sos.ogg",  # Twilio WhatsApp voice notes are ogg/opus; the upload endpoint passes the real filename
) -> dict:
    """Transcribes + triages a voice SOS clip and persists it to SQLite,
    returning the same row shape as database.insert_sos_alert."""
    client = _client()

    transcript = await client.audio.transcriptions.create(file=(filename, audio_bytes), model="whisper-large-v3")

    completion = await client.chat.completions.create(
        model="openai/gpt-oss-120b",  # llama-3.3-70b-versatile is no longer served on this Groq account - see note below
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": TRIAGE_PROMPT},
            {"role": "user", "content": transcript.text},
        ],
    )
    triage = json.loads(completion.choices[0].message.content)

    # Fails open: a missing/malformed is_emergency key defaults to True
    # (treated as a real emergency) rather than False - silently dropping a
    # genuine SOS because of an LLM parsing quirk is a far worse failure
    # mode here than occasionally letting a borderline/ambiguous message
    # through to a human operator.
    if not triage.get("is_emergency", True):
        return {
            "rejected": True,
            "reason": triage.get("summary") or "The message did not appear to describe an emergency.",
            "raw_message": transcript.text,
        }

    resolved_lat, resolved_lon = await _geocode(triage.get("spoken_location", ""), lat, lon)

    return database.insert_sos_alert(
        truck_id=phone,
        latitude=resolved_lat,
        longitude=resolved_lon,
        cargo=triage.get("incident_type", "Unknown"),
        reason=triage.get("action_needed", ""),
        source="whatsapp_voice",
        raw_message=transcript.text,
        reported_by=phone,
        urgency=triage.get("urgency", "MODERATE"),
        action_needed=triage.get("action_needed", ""),
        summary=triage.get("summary", ""),
    )
