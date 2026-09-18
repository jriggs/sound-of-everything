"""Turn the genre list into an ordered list of Spotify track URIs.

Two strategies (set ``build.strategy`` in config.yaml):

  faithful  Use everynoise's representative track id for each genre. Closest to
            the original "The Sound of Everything". No per-genre API calls.

  fresh     Pick each genre's track via Spotify SEARCH (the only track-discovery
            endpoint still open to Development-Mode apps in 2026 — /tracks batch,
            /artists/top-tracks and the `popularity` field are all blocked).

`fresh` is CACHED (data/tracks.json) so runs stay cheap:
  * A brand-new genre is seeded instantly from everynoise's pick (source
    "everynoise"), then gets a real search on a later run.
  * Each run re-searches only the ``refresh_batch`` genres whose cached pick is
    oldest, so songs rotate over time without re-searching all ~6k every week.
"""

from __future__ import annotations

import datetime as dt
import time

from .genres import Genre
from .spotify_client import RateLimitError, SpotifyClient

EPOCH = "1970-01-01T00:00:00+00:00"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _dedupe_keep_order(pairs: list[tuple[Genre, str]]) -> list[tuple[Genre, str]]:
    seen: set[str] = set()
    out: list[tuple[Genre, str]] = []
    for g, u in pairs:
        if u and u not in seen:
            seen.add(u)
            out.append((g, u))
    return out


def search_track_uri(client: SpotifyClient, genre: str, market: str) -> str | None:
    try:
        data = client.search(genre, "track", limit=1, market=market)
        items = data.get("tracks", {}).get("items", [])
        return items[0]["uri"] if items else None
    except RateLimitError:
        raise  # let the caller's circuit breaker handle sustained throttling
    except Exception:  # noqa: BLE001 - a single failed search shouldn't kill the run
        return None


def resolve_faithful(genres: list[Genre]) -> list[tuple[Genre, str]]:
    return [(g, f"spotify:track:{g.track_id}") for g in genres if g.track_id]


def resolve_fresh(genres: list[Genre], client: SpotifyClient, cache: dict,
                  cfg: dict, market: str, log=print) -> list[tuple[Genre, str]]:
    refresh_batch = int(cfg.get("refresh_batch", 0))  # 0 = try every genre this run
    seed = bool(cfg.get("seed_from_everynoise", True))
    pause = max(float(cfg.get("pause_ms", 100)), 0) / 1000.0  # smooth request rate

    rl_stop = int(cfg.get("rate_limit_stop", 3))  # consecutive throttles -> stop searching

    def _seed_entry(g: Genre) -> dict:
        return {"uri": f"spotify:track:{g.track_id}", "source": "everynoise", "updated_utc": EPOCH}

    # 1. New genres: seed instantly from everynoise (rotated to search later), or
    #    resolve immediately via search if seeding is disabled.
    new = [g for g in genres if g.name not in cache]
    if new:
        log(f"  {len(new)} new genre(s)")
    for g in new:
        if seed and g.track_id:
            cache[g.name] = _seed_entry(g)
            continue
        if not g.track_id:
            continue
        try:  # seed disabled: search now, but degrade to everynoise if throttled/empty
            uri = search_track_uri(client, g.name, market)
        except RateLimitError:
            uri = None
        if uri:
            cache[g.name] = {"uri": uri, "source": "search", "updated_utc": _now()}
        else:  # seed at EPOCH so it stays most-stale and gets searched next run
            cache[g.name] = _seed_entry(g)
        time.sleep(pause)

    # 2. Rotating refresh: re-search the oldest-cached genres, with a circuit
    #    breaker so a hard throttle ends the run quickly instead of spinning.
    present = [g for g in genres if g.name in cache]
    present.sort(key=lambda g: cache[g.name].get("updated_utc", ""))
    # refresh_batch <= 0 means "try them all", most-stale first; the circuit
    # breaker stops the run when Spotify throttles, and whatever wasn't reached
    # stays most-stale and is picked up first next run.
    to_refresh = present if refresh_batch <= 0 else present[:refresh_batch]
    total = len(to_refresh)
    scope = "all" if refresh_batch <= 0 else f"batch={refresh_batch}"
    log(f"  refreshing up to {total} genres via search ({scope}, pause={int(pause * 1000)}ms)")
    t0 = time.time()
    done = rl_streak = 0
    for i, g in enumerate(to_refresh, 1):
        try:
            uri = search_track_uri(client, g.name, market)
        except RateLimitError:
            rl_streak += 1
            if rl_streak >= rl_stop:
                log(f"    Spotify is throttling hard — stopping after {done} updated "
                    f"(of {total} planned). Un-updated genres stay 'most stale' for next run.")
                break
            continue  # skipped: leave timestamp so it stays most-stale, retried next run
        rl_streak = 0
        # Only advance the freshness clock when search actually returns a track. If
        # it returns nothing, keep the existing pick AND its old timestamp so this
        # genre stays near the front of the queue and is retried next run.
        if uri:
            cache[g.name] = {"uri": uri, "source": "search", "updated_utc": _now()}
            done += 1
        time.sleep(pause)
        if i % 50 == 0 or i == total:
            el = time.time() - t0
            rate = i / el if el else 0
            eta = (total - i) / rate if rate else 0
            log(f"    {i}/{total} tried, {done} updated — {rate:.1f}/s, eta {eta:4.0f}s")
    log(f"  updated {done} of {total} tried this run (most-stale first)")

    # 3. Build ordered pairs from the cache, following the current genre order.
    n_search = sum(1 for v in cache.values() if v.get("source") == "search")
    n_seed = sum(1 for v in cache.values() if v.get("source") == "everynoise")
    log(f"  cache: {n_search} search-picked, {n_seed} still awaiting rotation")
    return [(g, cache[g.name]["uri"]) for g in genres
            if cache.get(g.name, {}).get("uri")]


def resolve(genres: list[Genre], strategy: str, client: SpotifyClient | None,
            cache: dict, cfg: dict, market: str = "US", max_tracks: int = 10000,
            log=print) -> list[tuple[Genre, str]]:
    if strategy == "fresh":
        if client is None:
            raise ValueError("fresh strategy requires an authenticated client")
        pairs = resolve_fresh(genres, client, cache, cfg, market, log)
    else:
        pairs = resolve_faithful(genres)
    return _dedupe_keep_order(pairs)[:max_tracks]
