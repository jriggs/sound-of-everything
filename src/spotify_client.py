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
from typing import Any

import requests

TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"


class SpotifyError(RuntimeError):
    pass


class RateLimitError(SpotifyError):
    """Raised when Spotify keeps returning 429 after our short retries — a signal
    that the app is being throttled hard, so callers can stop early instead of
    sleeping through a long penalty."""


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
        # Rate limited. A large Retry-After means a hard/long penalty (quota
        # exhausted) — don't sleep through it, surface immediately so the caller
        # can stop. A small one is a transient burst limit — retry briefly.
        if resp.status_code == 429:
            ra = resp.headers.get("Retry-After", "2").strip()
            # Retry-After is usually seconds, but the spec also allows an HTTP-date;
            # treat any non-integer as a hard throttle rather than crashing on int().
            retry_after = int(ra) if ra.isdigit() else 999
            if retry_after > 120:
                raise RateLimitError(
                    f"{method} {path}: rate limited, Retry-After={retry_after}s (hard throttle)")
            if _retry < 2:
                wait = min(retry_after + 1, 30)
                self._log(f"  rate limited by Spotify — waiting {wait}s (retry {_retry + 1}/2)")
                time.sleep(wait)
                return self._request(method, path, params=params, json=json, _retry=_retry + 1)
            raise RateLimitError(f"{method} {path}: still rate limited (429) after retries")
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
        """Replace the entire tracklist. Handles >100 items via replace+append.

        Note: this re-stamps EVERY track's "added at" time. Prefer
        sync_playlist_items for incremental updates; this is only for a full reset.
        """
        if not uris:
            self._items_call("PUT", playlist_id, [])
            return
        self._items_call("PUT", playlist_id, uris[:100])          # replace
        for i in range(100, len(uris), 100):
            self._items_call("POST", playlist_id, uris[i:i + 100])  # append

    def get_playlist_uris(self, playlist_id: str) -> list[str]:
        """Every track URI currently on the playlist, in order."""
        uris: list[str] = []
        offset = 0
        while True:
            data = self._json("GET", f"/playlists/{playlist_id}/items",
                              params={"limit": 100, "offset": offset,
                                      "fields": "items(track(uri),item(uri)),next"})
            for it in data.get("items", []):
                track = it.get("track") or it.get("item") or {}
                if track.get("uri"):
                    uris.append(track["uri"])
            if data.get("next"):
                offset += 100
            else:
                return uris

    def _items_delete(self, playlist_id: str, uris: list[str]) -> None:
        path = f"/playlists/{playlist_id}/items"
        for field in ("tracks", "items"):  # body field name changed across the migration
            body = {field: [{"uri": u} for u in uris]}
            resp = self._request("DELETE", path, json=body)
            if resp.status_code < 400:
                return
            if resp.status_code == 404 and path.endswith("/items"):
                path = f"/playlists/{playlist_id}/tracks"
                resp = self._request("DELETE", path, json=body)
                if resp.status_code < 400:
                    return
            if resp.status_code != 400:
                raise SpotifyError(f"DELETE {path} -> {resp.status_code}: {resp.text[:300]}")
        raise SpotifyError(f"DELETE {path}: rejected both 'tracks' and 'items' fields")

    def sync_playlist_items(self, playlist_id: str, desired_uris: list[str],
                            log=lambda *a: None) -> dict:
        """Incrementally update the playlist to match `desired_uris`: add only the
        new tracks and remove only the departed ones. Unchanged tracks keep their
        position and "added at" time (so only truly-changed tracks show as updated).
        New tracks are appended (Spotify has no atomic in-place item replace)."""
        current = self.get_playlist_uris(playlist_id)
        cur_set, des_set = set(current), set(desired_uris)
        to_remove = [u for u in dict.fromkeys(current) if u not in des_set]
        to_add = [u for u in desired_uris if u not in cur_set]
        log(f"    playlist has {len(current)} tracks; +{len(to_add)} new, -{len(to_remove)} removed, "
            f"{len(current) - len(to_remove)} untouched")
        for i in range(0, len(to_remove), 100):
            self._items_delete(playlist_id, to_remove[i:i + 100])
        for i in range(0, len(to_add), 100):
            self._items_call("POST", playlist_id, to_add[i:i + 100])
        return {"added": len(to_add), "removed": len(to_remove),
                "unchanged": len(current) - len(to_remove)}
