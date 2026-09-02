"""
Basic API authentication for the frontend-facing endpoints - a shared key
that stops casual/opportunistic abuse (a script hitting the open API
directly), NOT real user authentication. SETU_API_KEY ends up embedded in
the built frontend JS bundle (Vite has no way around this for a purely
client-side app), so anyone who loads the app and opens dev tools can
extract it. That's an inherent limit of this approach, not a bug to fix
later; real protection against a targeted attacker would need actual user
accounts and server-side sessions.

The two Twilio webhooks (SMS/WhatsApp SOS intake) deliberately have no
equivalent gate - see main.py's module docstring for why.
"""
import os
import secrets as secrets_module
from typing import Optional

from fastapi import Header, HTTPException

import database


def verify_api_key(x_api_key: Optional[str] = Header(None, alias="X-API-Key")) -> None:
    """Dependency for every endpoint the frontend calls directly. Reads
    SETU_API_KEY fresh on every call (not cached at module level) - main.py
    imports this module before it calls load_dotenv(), so a module-level
    os.getenv() here would permanently see an empty environment. Fails
    closed and loudly (500) if SETU_API_KEY isn't configured at all - a
    missing config should never silently look like "auth is on and
    passing", it should be impossible to miss."""
    api_key = os.getenv("SETU_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="Server misconfigured: SETU_API_KEY is not set.")
    if not x_api_key or not secrets_module.compare_digest(x_api_key, api_key):
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key header.")


def verify_driver_session(x_driver_token: Optional[str] = Header(None, alias="X-Driver-Token")) -> str:
    """Dependency for the online SOS endpoint - resolves a driver's session
    token (issued at /api/driver/login/verify) to their real, Twilio-
    verified phone number. Returns that phone number so the endpoint can use
    it as the actual reported_by/truck_id, instead of trusting whatever the
    client puts in the request body."""
    phone_number = database.get_driver_by_session(x_driver_token) if x_driver_token else None
    if not phone_number:
        raise HTTPException(status_code=401, detail="Not logged in - missing or invalid driver session.")
    return phone_number
