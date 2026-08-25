"""
Handles Groww API authentication.

Groww offers two auth flows:
  1. API Key + Secret flow (resets daily, requires re-approval each day)
  2. TOTP flow (no expiry, uses pyotp to generate a rotating code)

This uses the API Key + Secret flow, since that's what's configured via
GROWW_API_KEY / GROWW_API_SECRET in .env.
"""
import os
import logging
from dotenv import load_dotenv

from growwapi import GrowwAPI

load_dotenv()
logger = logging.getLogger("groww_agent.auth")


class GrowwAuthError(Exception):
    pass


def get_access_token() -> str:
    api_key = os.getenv("GROWW_API_KEY")
    api_secret = os.getenv("GROWW_API_SECRET")

    if not api_key or not api_secret:
        raise GrowwAuthError(
            "GROWW_API_KEY / GROWW_API_SECRET missing. "
            "Copy .env.example to .env and fill in your credentials."
        )

    try:
        access_token = GrowwAPI.get_access_token(api_key=api_key, secret=api_secret)
    except Exception as e:
        logger.error("Groww auth failed: %s", e)
        raise GrowwAuthError(f"Failed to obtain access token: {e}") from e

    if not access_token:
        raise GrowwAuthError("Groww returned an empty access token.")

    return access_token


def get_client() -> GrowwAPI:
    token = get_access_token()
    logger.info("Groww session established.")
    return GrowwAPI(token)
