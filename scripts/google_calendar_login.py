"""
scripts/google_calendar_login.py — One-time Google Calendar connection
==========================================================================
Run this once to let Gama's calendar integration (actions/calendar_action.py)
read and manage events on your Google Calendar:

    python scripts/google_calendar_login.py

What happens:
  1. Opens your browser to Google's login/consent page.
  2. After you approve, Google redirects to a page Gama is briefly
     listening on (http://127.0.0.1:8900/callback) — nothing leaves
     your machine except the OAuth exchange itself.
  3. Gama exchanges that one-time code for a refresh token and stores
     it encrypted (Windows DPAPI, tied to this Windows user).

You will not need to run this again unless you revoke Gama's access
from your Google account settings.

Prerequisite: an OAuth "Desktop app" client at
https://console.cloud.google.com/apis/credentials with the Google
Calendar API enabled, and:
  - Redirect URI:  http://127.0.0.1:8900/callback
  - Client ID + Secret copied into config/api_keys.json as
    "google_client_id" / "google_client_secret"
    (or set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET env vars).

Author : Vineet Machchal
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actions import google_calendar_auth  # noqa: E402


def main() -> int:
    if not google_calendar_auth.is_configured():
        print("No Google Client ID/Secret configured.")
        print("1. Create an OAuth Desktop app client at")
        print("   https://console.cloud.google.com/apis/credentials")
        print("2. Enable the 'Google Calendar API' for that project.")
        print(f"3. Add this exact Redirect URI: {google_calendar_auth.REDIRECT_URI}")
        print("4. Put the Client ID + Secret in config/api_keys.json as")
        print("   \"google_client_id\" and \"google_client_secret\"")
        print("   (or set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET env vars), then re-run this script.")
        return 1

    if google_calendar_auth.has_refresh_token():
        print("Gama already has a stored Google Calendar login.")
        answer = input("Log in again anyway? [y/N] ").strip().lower()
        if answer != "y":
            return 0

    print("Opening your browser for Google Calendar login...")
    ok, message = google_calendar_auth.login_interactive()
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
