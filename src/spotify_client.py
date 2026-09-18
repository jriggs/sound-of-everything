"""Minimal Spotify Web API client targeting the post-February-2026 endpoints.

Uses raw ``requests`` (no SDK) so it stays correct against the 2026 API
migration, which renamed several endpoints:

  * create playlist ......... POST  /me/playlists          (was /users/{id}/playlists)
  * list my playlists ....... GET   /me/playlists
  * replace/append items .... PUT/POST /playlists/{id}/items  (was .../tracks)
  * change details .......... PATCH /playlists/{id}          (was PUT)
  * search .................. GET   /search

Auth is the standard OAuth Authorization Code flow. This client only needs a
long-lived refresh token (obtained once via ``python -m src.auth``); it mints
short-lived access tokens on demand, so it runs headless in CI.
"""

from __future__ import annotations

import base64
import time
from typing import Any, Iterable

import requests

TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"


class SpotifyError(RuntimeError):
    pass


class SpotifyClient:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str,
                 session: requests.Session | None = None, log=None):
        if not (client_id and client_secret and refresh_token):
            raise SpotifyError(
                "Missing credentials. Set SPOTIFY_CLIENT_ID, "
                "SPOTIFY_CLIENT_SECRET and SPOTIFY_REFRESH_TOKEN "
                "(run `python -m src.auth` to get the refresh token)."
            )
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.session = session or requests.Session()
        self._log = log or (lambda *a: None)
        self._access_token: str | None = None
        self._expires_at = 0.0

    # -- auth ---------------------------------------------------------------
    def _basic_auth(self) -> str:
        raw = f"{self.client_id}:{self.client_secret}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def _refresh(self) -> None:
        resp = self.session.post(
            TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": self.refresh_token},
            headers={"Authorization": self._basic_auth()},
            timeout=30,
        )
        if resp.status_code != 200:
            raise SpotifyError(f"Token refresh failed ({resp.status_code}): {resp.text}")
        payload = resp.json()
        self._access_token = payload["access_token"]
        self._expires_at = time.time() + int(payload.get("expires_in", 3600))
        # Spotify occasionally rotates the refresh token.
        if payload.get("refresh_token"):
            self.refresh_token = payload["refresh_token"]

    def _token(self) -> str:
        if not self._access_token or time.time() >= self._expires_at - 60:
            self._refresh()
        assert self._access_token
        return self._access_token

    # -- core request with retry/backoff ------------------------------------
    def _request(self, method: str, path: str, *, params=None, json=None,
                 _retry=0) -> requests.Response:
        url = path if path.startswith("http") else f"{API_BASE}{path}"
        resp = self.session.request(
            method, url,
            headers={"Authorization": f"Bearer {self._token()}"},
            params=params, json=json, timeout=30,
        )
        # Rate limited: honour Retry-After (capped so a bad value can't stall us).
        if resp.status_code == 429 and _retry < 6:
            wait = min(int(resp.headers.get("Retry-After", "2")) + 1, 60)
            self._log(f"  rate limited by Spotify — waiting {wait}s (retry {_retry + 1}/6)")
            time.sleep(wait)
            return self._request(method, path, params=params, json=json, _retry=_retry + 1)
        # Token expired mid-flight: force one refresh and retry.
        if resp.status_code == 401 and _retry < 1:
            self._access_token = None
            return self._request(method, path, params=params, json=json, _retry=_retry + 1)
        # Transient server errors: brief backoff.
        if resp.status_code >= 500 and _retry < 4:
            time.sleep(2 ** _retry)
            return self._request(method, path, params=params, json=json, _retry=_retry + 1)
        return resp

    def _json(self, method: str, path: str, **kw) -> Any:
        resp = self._request(method, path, **kw)
        if resp.status_code >= 400:
            raise SpotifyError(f"{method} {path} -> {resp.status_code}: {resp.text[:400]}")
        return resp.json() if resp.text else {}

    # -- endpoints ----------------------------------------------------------
    def me(self) -> dict:
        return self._json("GET", "/me")

    def search(self, q: str, types: str, limit: int = 5, market: str | None = None) -> dict:
        params = {"q": q, "type": types, "limit": limit}
        if market:
            params["market"] = market
        return self._json("GET", "/search", params=params)

    def tracks(self, ids: Iterable[str]) -> list[dict]:
        ids = [i for i in ids if i]
        out: list[dict] = []
        for i in range(0, len(ids), 50):
            chunk = ids[i:i + 50]
            data = self._json("GET", "/tracks", params={"ids": ",".join(chunk)})
            out.extend(data.get("tracks", []))
        return out

    def artist_top_track_uri(self, artist_id: str, market: str = "US") -> str | None:
        data = self._json("GET", f"/artists/{artist_id}/top-tracks",
                          params={"market": market})
        items = data.get("tracks", [])
        return items[0]["uri"] if items else None

    def find_my_playlist(self, name: str) -> dict | None:
        offset = 0
        while True:
            data = self._json("GET", "/me/playlists",
                              params={"limit": 50, "offset": offset})
            for pl in data.get("items", []):
                if pl and pl.get("name") == name:
                    return pl
            if data.get("next"):
                offset += 50
            else:
                return None

    def create_playlist(self, name: str, public: bool, description: str) -> dict:
        return self._json("POST", "/me/playlists",
                          json={"name": name, "public": public,
                                "description": description})

    def update_playlist_details(self, playlist_id: str, *, name=None,
                                description=None, public=None) -> None:
        body = {k: v for k, v in
                {"name": name, "description": description, "public": public}.items()
                if v is not None}
        if not body:
            return
        # 2026: PATCH replaced PUT for "change playlist details". Fall back to
        # PUT for older app modes that have not migrated yet.
        resp = self._request("PATCH", f"/playlists/{playlist_id}", json=body)
        if resp.status_code == 405:  # method not allowed -> pre-migration app
            resp = self._request("PUT", f"/playlists/{playlist_id}", json=body)
        if resp.status_code >= 400:
            raise SpotifyError(
                f"update details -> {resp.status_code}: {resp.text[:300]}")

    def _items_call(self, method: str, playlist_id: str, uris: list[str]) -> None:
        path = f"/playlists/{playlist_id}/items"
        # Body field has historically been "uris"; retry with "items" if the
        # migrated API rejects it, so the script survives either naming.
        for field in ("uris", "items"):
            resp = self._request(method, path, json={field: uris})
            if resp.status_code < 400:
                return
            if resp.status_code == 404 and path.endswith("/items"):
                # Pre-migration app still on the old /tracks path.
                path = f"/playlists/{playlist_id}/tracks"
                resp = self._request(method, path, json={field: uris})
                if resp.status_code < 400:
                    return
            if resp.status_code != 400:
                raise SpotifyError(
                    f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
        raise SpotifyError(f"{method} {path}: rejected both 'uris' and 'items' fields")

    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> None:
        """Replace the entire tracklist. Handles >100 items via replace+append."""
        if not uris:
            self._items_call("PUT", playlist_id, [])
            return
        self._items_call("PUT", playlist_id, uris[:100])          # replace
        for i in range(100, len(uris), 100):
            self._items_call("POST", playlist_id, uris[i:i + 100])  # append
