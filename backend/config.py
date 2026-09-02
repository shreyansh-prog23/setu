"""
Environment-driven configuration for the TomTom routing integration.

Reads settings from process environment variables (a `.env` file is loaded
automatically by main.py on startup via python-dotenv). See the module
docstring in routing.py / the project README for the full list of variables.
"""
from __future__ import annotations

import os
from functools import lru_cache


class Settings:
    tomtom_api_key: str
    tomtom_base_url: str
    request_timeout_seconds: float

    def __init__(self) -> None:
        api_key = os.getenv("TOMTOM_API_KEY")
        if not api_key:
            raise RuntimeError(
                "TOMTOM_API_KEY is not set. Get a free key at "
                "https://developer.tomtom.com/ and export it as an environment "
                "variable, or add TOMTOM_API_KEY=... to a .env file in the "
                "backend directory."
            )
        self.tomtom_api_key = api_key
        self.tomtom_base_url = os.getenv(
            "TOMTOM_BASE_URL", "https://api.tomtom.com/routing/1/calculateRoute"
        )
        self.request_timeout_seconds = float(os.getenv("TOMTOM_TIMEOUT_SECONDS", "10"))


@lru_cache
def get_settings() -> Settings:
    """
    Lazily construct and cache the Settings singleton.

    Deferred (rather than built at import time) so the rest of the app -
    including the unrelated SMS/SOS endpoints - still starts up cleanly even
    if TOMTOM_API_KEY hasn't been configured yet. The routing endpoint will
    raise a clear 500 error only when it's actually called without a key.
    """
    return Settings()
