"""Fetch and parse the genre list from Every Noise at Once (everynoise.com).

everynoise.com is the only comprehensive, current source for Spotify's full
micro-genre taxonomy (~6,300 genres). Spotify's own API never exposed this list
and deprecated even its small genre-seed endpoint in Nov 2024.

Each genre on the front page is a positioned <div> that embeds a representative
Spotify track id and an "e.g. Artist \"Song\"" hint, e.g.:

    <div id=item1 preview_url="https://p.scdn.co/mp3-preview/..."
         class="genre scanme" ...
         onclick="playx(&quot;1V6gIisPpYqgFeWbMLI0bA&quot;, &quot;pop&quot;, this);"
         title='e.g. Demi Lovato &quot;Heart Attack&quot;'>pop<a ...>&raquo;</a></div>

So one scrape yields, per genre: the genre name, a representative track id, and
the example artist/song text. That is everything we need to seed the playlist.
"""

from __future__ import annotations

import html
import json
import pathlib
import re
import time
from dataclasses import dataclass, asdict

import requests

DEFAULT_URL = "https://everynoise.com/"
# A realistic browser UA + headers. everynoise tends to 403 requests that look
# botty — especially from datacenter IPs like CI runners. This helps, but note
# it may still block cloud IPs outright; callers should fall back to the cache.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_DIV_RE = re.compile(r"<div\s+id=item(\d+)([^>]*)>([^<]*)")
_PREVIEW_RE = re.compile(r'preview_url="([^"]*)"')
_PLAYX_RE = re.compile(r"playx\(&quot;([A-Za-z0-9]+)&quot;,\s*&quot;(.*?)&quot;")
_TITLE_RE = re.compile(r'title="([^"]*)"')


@dataclass
class Genre:
    name: str
    track_id: str | None       # everynoise's representative track id
    example: str | None        # 'e.g. Artist "Song"' hint text
    preview_url: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def fetch_html(url: str = DEFAULT_URL, timeout: int = 60, retries: int = 3) -> str:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            last = e
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last}")


def load_cached(path) -> list[Genre]:
    """Load a previously scraped genre list from data/genres.json."""
    data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    return [Genre(name=d["name"], track_id=d.get("track_id"),
                  example=d.get("example"), preview_url=d.get("preview_url"))
            for d in data]


def parse(html_text: str) -> list[Genre]:
    genres: list[Genre] = []
    seen: set[str] = set()
    for m in _DIV_RE.finditer(html_text):
        attrs = m.group(2)
        display = html.unescape(m.group(3)).strip()
        playx = _PLAYX_RE.search(attrs)
        track_id = playx.group(1) if playx else None
        name = html.unescape(playx.group(2)).strip() if playx else display
        if not name or name in seen:
            continue
        seen.add(name)
        preview = _PREVIEW_RE.search(attrs)
        title = _TITLE_RE.search(attrs)
        genres.append(Genre(
            name=name,
            track_id=track_id,
            example=html.unescape(title.group(1)).strip() if title else None,
            preview_url=preview.group(1) if preview else None,
        ))
    return genres


def fetch_genres(url: str = DEFAULT_URL) -> list[Genre]:
    genres = parse(fetch_html(url))
    if len(genres) < 500:
        raise RuntimeError(
            f"Only parsed {len(genres)} genres from {url} — the page layout may "
            "have changed. Check src/genres.py regexes against the current HTML."
        )
    return genres


if __name__ == "__main__":
    # Refresh the genre cache from everynoise (run this LOCALLY — everynoise
    # blocks datacenter IPs). Writes data/genres.json; commit + push it so the
    # weekly cloud job picks up any new genres.
    gs = fetch_genres()
    out = pathlib.Path(__file__).resolve().parent.parent / "data" / "genres.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps([g.to_dict() for g in gs], indent=2, ensure_ascii=False))
    print(f"wrote {len(gs)} genres to {out}")
