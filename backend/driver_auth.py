"""
Driver phone-number login via Twilio Verify - the SMS/voice OTP product,
not hand-rolled code generation/expiry. Verify Service is configured once
in the Twilio console (TWILIO_VERIFY_SERVICE_SID); these two calls are the
whole integration - send a code, check a code.

This closes the one real identity gap in SOS intake: the WhatsApp and SMS
fallback channels (see main.py's whatsapp_webhook/sms_webhook) already get
a real, Twilio-verified phone number for free via the webhook's own `From`
field - only the online app's SOS button previously let the client just
assert a truck_id with zero verification. This makes that path require the
same kind of real phone-number identity the other two channels already had.

Demo bypass: the Twilio account is on the free trial, which can only send
real codes to numbers manually verified in the Twilio console - not a real
driver base. DRIVER_LOGIN_BYPASS_NUMBERS (comma-separated E.164 numbers) +
DRIVER_LOGIN_BYPASS_CODE let a short, explicit allowlist "verify" with a
fixed code and zero Twilio calls, so a demo isn't blocked on Twilio's
compliance-profile review. This is NOT a security feature - it's scoped to
numbers someone deliberately put in an env var, same trust level as the
Verified Caller ID workaround it replaces, not a weaker one.
"""
import os

import httpx

VERIFY_BASE_URL = "https://verify.twilio.com/v2"
REQUEST_TIMEOUT_SECONDS = 10.0


class VerifyError(Exception):
    """Twilio Verify call failed or is misconfigured - callers turn this
    into a clean HTTP error rather than letting a raw Twilio failure leak
    through."""


def _bypass_numbers() -> set[str]:
    raw = os.getenv("DRIVER_LOGIN_BYPASS_NUMBERS", "")
    return {n.strip() for n in raw.split(",") if n.strip()}


def _credentials() -> tuple[str, str, str]:
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    service_sid = os.getenv("TWILIO_VERIFY_SERVICE_SID")
    if not account_sid or not auth_token or not service_sid:
        raise VerifyError("Driver login is not configured (missing TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN/TWILIO_VERIFY_SERVICE_SID).")
    return account_sid, auth_token, service_sid


async def send_verification_code(phone_number: str) -> None:
    """Starts a Verify SMS (Twilio automatically falls back to voice on its
    own if configured that way) to phone_number. Raises VerifyError on any
    failure - misconfiguration, Twilio rejecting the number, rate limiting
    (Twilio enforces its own cooldown between repeat sends to the same
    number, so this doesn't need to reimplement that).

    A bypass-listed number sends nothing at all - no Twilio call, no
    quota/rate-limit consumed - since check_verification_code below never
    needs Twilio to confirm it either."""
    if phone_number in _bypass_numbers():
        return
    account_sid, auth_token, service_sid = _credentials()
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, auth=(account_sid, auth_token)) as client:
            resp = await client.post(
                f"{VERIFY_BASE_URL}/Services/{service_sid}/Verifications",
                data={"To": phone_number, "Channel": "sms"},
            )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise VerifyError(f"Twilio rejected the verification request: {exc.response.text}") from exc
    except httpx.HTTPError as exc:
        raise VerifyError(f"Could not reach Twilio Verify: {exc}") from exc


async def check_verification_code(phone_number: str, code: str) -> bool:
    """Returns True only if Twilio reports this code as 'approved' for this
    phone number - a wrong code, an expired code, or a Twilio-side error all
    fall through to False (login just fails cleanly, no VerifyError raised
    for a plain wrong code - that's an expected case, not a real failure).

    A bypass-listed number never touches Twilio at all - approved only if
    the code matches DRIVER_LOGIN_BYPASS_CODE exactly."""
    if phone_number in _bypass_numbers():
        bypass_code = os.getenv("DRIVER_LOGIN_BYPASS_CODE")
        return bool(bypass_code) and code == bypass_code

    account_sid, auth_token, service_sid = _credentials()
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, auth=(account_sid, auth_token)) as client:
            resp = await client.post(
                f"{VERIFY_BASE_URL}/Services/{service_sid}/VerificationCheck",
                data={"To": phone_number, "Code": code},
            )
        if resp.status_code == 404:
            return False  # no pending verification for this number (expired/never started/already used)
        resp.raise_for_status()
        return resp.json().get("status") == "approved"
    except httpx.HTTPError:
        return False
