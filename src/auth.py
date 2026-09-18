"""One-time helper: obtain a Spotify refresh token via the Authorization Code flow.

Run locally once:   python -m src.auth

It opens your browser, you approve the app, and it prints a SPOTIFY_REFRESH_TOKEN.
Put that token in your local .env and in your GitHub Actions secrets. The weekly
job then runs headless — it only needs the refresh token, never the browser.

Requires (in your environment or .env):
  SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET
  SPOTIFY_REDIRECT_URI  (default http://127.0.0.1:8888/callback)

The redirect URI must EXACTLY match one registered in your Spotify app settings.
Spotify requires https or a loopback address; use 127.0.0.1 (not "localhost").
"""

from __future__ import annotations

import base64
import os
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

from .localenv import load_dotenv

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
SCOPES = "playlist-modify-public playlist-modify-private playlist-read-private"


class _Handler(BaseHTTPRequestHandler):
    code: str | None = None
    error: str | None = None
    expected_state: str = ""

    def do_GET(self):  # noqa: N802
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        _Handler.error = params.get("error", [None])[0]
        if params.get("state", [None])[0] != _Handler.expected_state:
            _Handler.error = _Handler.error or "state_mismatch"
        _Handler.code = params.get("code", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        msg = "Authorization complete. You can close this tab and return to the terminal."
        if _Handler.error:
            msg = f"Authorization failed: {_Handler.error}. Check the terminal."
        self.wfile.write(f"<html><body><h3>{msg}</h3></body></html>".encode())

    def log_message(self, *args):  # silence default logging
        pass


def main() -> None:
    load_dotenv()
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()
    redirect_uri = os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback").strip()
    if not client_id or not client_secret:
        sys.exit("Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET (in .env or env vars) first.")

    parsed = urllib.parse.urlparse(redirect_uri)
    host, port = parsed.hostname or "127.0.0.1", parsed.port or 8888

    state = secrets.token_urlsafe(16)
    _Handler.expected_state = state
    auth_link = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
    })

    server = HTTPServer((host, port), _Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()

    print("Opening browser to authorize. If it doesn't open, paste this URL:\n")
    print(auth_link + "\n")
    webbrowser.open(auth_link)

    # Wait for the single callback request to be handled.
    while _Handler.code is None and _Handler.error is None:
        threading.Event().wait(0.2)
    server.server_close()

    if _Handler.error:
        sys.exit(f"Authorization failed: {_Handler.error}")

    resp = requests.post(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "code": _Handler.code,
        "redirect_uri": redirect_uri,
    }, headers={
        "Authorization": "Basic " + base64.b64encode(
            f"{client_id}:{client_secret}".encode()).decode(),
    }, timeout=30)

    if resp.status_code != 200:
        sys.exit(f"Token exchange failed ({resp.status_code}): {resp.text}")

    refresh_token = resp.json().get("refresh_token")
    if not refresh_token:
        sys.exit(f"No refresh_token in response: {resp.json()}")

    print("\n" + "=" * 68)
    print("SUCCESS. Save this as SPOTIFY_REFRESH_TOKEN (in .env and GitHub secrets):\n")
    print(refresh_token)
    print("=" * 68)


if __name__ == "__main__":
    main()
