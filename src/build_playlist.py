"""Build / refresh the playlist. This is what runs weekly.

Steps:
  1. Scrape the current genre list from everynoise.com.
  2. Resolve one track URI per genre (faithful or fresh; see resolve.py).
  3. Find the playlist by id (env) or by name, creating it if missing.
  4. Replace the tracklist and refresh the title/description.
  5. Write data/ snapshots so the repo always reflects the latest state.

Run:  python -m src.build_playlist
Env:  SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REFRESH_TOKEN
      SPOTIFY_PLAYLIST_ID (optional; pins the target playlist)
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib

from . import genres as genres_mod
from .localenv import load_dotenv
from .log import new_logger
from .resolve import resolve
from .spotify_client import RateLimitError, SpotifyClient

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def load_config() -> dict:
    cfg_path = ROOT / "config.yaml"
    text = cfg_path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text)
    except ModuleNotFoundError:
        raise SystemExit("PyYAML is required. Run: pip install -r requirements.txt")


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def build_description(template: str) -> str:
    today = dt.date.today().isoformat()
    desc = template.replace("{date}", today)
    return desc[:300]  # Spotify caps descriptions at 300 chars


def get_or_create_playlist(client: SpotifyClient, cfg: dict, state: dict, log) -> dict:
    name = cfg["playlist"]["name"]
    public = bool(cfg["playlist"].get("public", True))

    pinned = _env("SPOTIFY_PLAYLIST_ID") or state.get("playlist_id")
    if pinned:
        try:
            return client._json("GET", f"/playlists/{pinned}",
                                params={"fields": "id,name,external_urls"})
        except Exception:
            log(f"  pinned playlist {pinned} not reachable; falling back to name lookup")

    existing = client.find_my_playlist(name)
    if existing:
        return existing

    log(f"  creating new playlist: {name!r}")
    return client.create_playlist(name, public,
                                  build_description(cfg["playlist"]["description"]))


def main() -> None:
    load_dotenv()  # picks up .env locally; no-op in CI where secrets are real env vars
    log = new_logger()
    cfg = load_config()
    DATA.mkdir(exist_ok=True)
    state_path = DATA / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}

    # Env overrides let a local run crank the batch without editing config.yaml,
    # e.g. REFRESH_BATCH=2000 python -m src.build_playlist
    if _env("REFRESH_BATCH"):
        cfg["build"]["refresh_batch"] = int(_env("REFRESH_BATCH"))
    if _env("PAUSE_MS"):
        cfg["build"]["pause_ms"] = int(_env("PAUSE_MS"))

    strategy = cfg["build"].get("strategy", "faithful")
    market = cfg["build"].get("market", "US")
    max_tracks = int(cfg["build"].get("max_tracks", 10000))

    log("1/5 fetching genres from everynoise.com ...")
    cache_path = DATA / "genres.json"
    try:
        genre_list = genres_mod.fetch_genres(cfg["source"]["everynoise_url"])
        log(f"    got {len(genre_list)} genres (live)")
        cache_path.write_text(
            json.dumps([g.to_dict() for g in genre_list], indent=2, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001 - fall back to the committed cache
        if not cache_path.exists():
            raise
        genre_list = genres_mod.load_cached(cache_path)
        log(f"    live fetch failed ({e});")
        log(f"    using cached {len(genre_list)} genres from {cache_path.name} "
            "(new genres are only picked up when the live fetch succeeds — e.g. a local run)")

    client = SpotifyClient(_env("SPOTIFY_CLIENT_ID"),
                           _env("SPOTIFY_CLIENT_SECRET"),
                           _env("SPOTIFY_REFRESH_TOKEN"),
                           log=log)
    try:
        me = client.me()
        log(f"    authenticated as {me.get('display_name') or me.get('id')} ({me.get('id')})")

        tracks_cache_path = DATA / "tracks.json"
        track_cache = json.loads(tracks_cache_path.read_text()) if tracks_cache_path.exists() else {}
        log(f"2/5 resolving tracks (strategy={strategy}, cache={len(track_cache)} entries) ...")
        pairs = resolve(genre_list, strategy, client, track_cache, cfg["build"], market, max_tracks, log)
        uris = [u for _, u in pairs]
        tracks_cache_path.write_text(json.dumps(track_cache, indent=2, ensure_ascii=False))
        log(f"    {len(uris)} unique tracks (cache now {len(track_cache)} entries)")

        log("3/5 locating playlist ...")
        playlist = get_or_create_playlist(client, cfg, state, log)
        pid = playlist["id"]
        url = playlist.get("external_urls", {}).get("spotify", f"https://open.spotify.com/playlist/{pid}")

        log("4/5 updating details + syncing changed tracks ...")
        client.update_playlist_details(
            pid,
            name=cfg["playlist"]["name"],
            description=build_description(cfg["playlist"]["description"]),
            public=bool(cfg["playlist"].get("public", True)),
        )
        # Incremental: only add/remove tracks that actually changed, so unchanged
        # tracks keep their "added at" time instead of the whole playlist re-stamping.
        client.sync_playlist_items(pid, uris, log)
    except RateLimitError as e:
        # The whole app is being rate-limited (quota). Do nothing this run rather
        # than crash — the cache/timestamps are untouched, so the next run resumes
        # exactly where this one would have. Exits cleanly (green in CI).
        log(f"Spotify is rate-limiting this app right now ({e}).")
        log("Skipping this run — nothing changed. It clears on its own; the next "
            "run picks up where it left off.")
        return

    log("5/5 writing snapshot ...")
    snapshot = {
        "name": cfg["playlist"]["name"],
        "playlist_id": pid,
        "url": url,
        "strategy": strategy,
        "updated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "track_count": len(uris),
        "tracks": [{"genre": g.name, "uri": u, "example": g.example} for g, u in pairs],
    }
    (DATA / "playlist.json").write_text(json.dumps(snapshot, indent=2, ensure_ascii=False))
    state.update({"playlist_id": pid, "url": url})
    state_path.write_text(json.dumps(state, indent=2))

    log(f"Done. {len(uris)} tracks in {cfg['playlist']['name']!r}")
    log(url)


if __name__ == "__main__":
    main()
