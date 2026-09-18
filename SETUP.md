# Setup

How to run your own copy of *The Sound of Everything (Updated Weekly)* and switch
on the weekly auto-refresh. About 15 minutes.

> The commands below use `python` and `pip`. If your system doesn't find them,
> use `python3` and `pip3`. Optionally create a virtual environment first
> (`python -m venv .venv`) and activate it — the activation command differs by
> operating system (see the Python docs). Everything else is the same.

## Prerequisites

- Python 3.10 or newer
- A Spotify account with **Premium** — the 2026 Spotify API rules require the
  app owner to have Premium.
- A GitHub account — for the weekly cloud automation.

## 1. Create a Spotify app  (~3 min)

1. Open the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)
   and click **Create app**.
2. Give it any name and description.
3. **Redirect URI:** add exactly `http://127.0.0.1:8888/callback`
   (use `127.0.0.1`, not `localhost` — Spotify rejects `localhost`).
4. Under "Which API/SDKs are you planning to use?", check **Web API**.
5. Save, then open the app's **Settings** and copy the **Client ID** and
   **Client secret**.

## 2. Install and configure

From the project folder:

```
pip install -r requirements.txt
```

Then make a copy of `.env.example`, name the copy `.env`, open it in any text
editor, and paste in your **Client ID** and **Client secret**. Leave the other
values as they are for now.

## 3. Connect your Spotify account  (one time)

```
python -m src.auth
```

This opens your browser to approve the app. Approve it, close the tab, and the
terminal prints a **refresh token**. Copy it into `.env` as
`SPOTIFY_REFRESH_TOKEN`.

## 4. First build

```
python -m src.build_playlist
```

This creates the playlist on your profile and fills it, then prints the playlist
link and saves its id to `data/state.json`. Copy that id into
`SPOTIFY_PLAYLIST_ID` in `.env` so future runs target it directly.

> New playlists are public by default. To help people find yours, keep it public
> in the Spotify app; the keyword-rich name and description (already set) are what
> surface it in search.

## 5. Switch on the weekly auto-refresh (GitHub Actions)

1. Create a new, **empty** repository on GitHub (no README or license).
2. Push this project to it:

```
git add -A
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/<your-username>/<your-repo>.git
git push -u origin main
```

3. In the repo, go to **Settings → Secrets and variables → Actions → New
   repository secret** and add four secrets (the same values from your `.env`):
   - `SPOTIFY_CLIENT_ID`
   - `SPOTIFY_CLIENT_SECRET`
   - `SPOTIFY_REFRESH_TOKEN`
   - `SPOTIFY_PLAYLIST_ID`
4. The job then runs automatically every **Monday 08:00 UTC**. Run it manually
   anytime from the **Actions** tab → *Weekly playlist refresh* → **Run workflow**.

Change the schedule by editing the `cron` line in
`.github/workflows/weekly.yml`.

## Adding new genres later

The weekly cloud job can't reach everynoise.com (the site blocks cloud servers),
so it uses the saved genre list. To pull in newly added genres, refresh that list
from your own computer and push it:

```
python -m src.genres
git add data/genres.json
git commit -m "Refresh genre list"
git push
```

New genres get a song on the next build. Tip: before a local run, do a `git pull`
first — the cloud job commits its updates back to the repo.

## Configuration

All the knobs live in `config.yaml`:

- **name / description / public** — the playlist's details.
- **strategy** — `fresh` (songs rotate over time) or `faithful` (everynoise's
  fixed pick per genre; never changes).
- **refresh_batch** — how many genres to re-search each run, **most stale first**.
  Default **0 = try them all**; the run gets as far as Spotify's rate limit
  allows, the circuit breaker stops it, and whatever wasn't reached stays most-
  stale and is picked up first next run. A genre only advances in the queue when
  search returns a real track. Set a positive number to cap attempts per run.
- **search_pool** — how many search results to consider per genre; the first one
  not already in the playlist is chosen, so no two genres share a song and a
  refresh always picks a *different* track than before.
- **pause_ms**, **seed_from_everynoise**, **market**, **max_tracks** — see the
  comments in the file.

**Live progress:** every run prints timestamped progress (genres searched, rate,
ETA) and says when Spotify is rate-limiting. If throttling is heavy, the run
refreshes what it can, keeps the rest from cache, and finishes rather than hanging.

**Big local catch-up:** to rotate a lot of genres at once, run locally with an
override instead of raising the weekly default:

```
REFRESH_BATCH=2000 python -m src.build_playlist
```

Spotify's rate limit is **per app** (your Client ID), shared by local and cloud
runs, so spacing big runs out helps. `PAUSE_MS` similarly overrides the pause
between searches.

## How the "fresh" refresh works

Songs are chosen with Spotify **search** — the only track-discovery feature still
available to development-mode apps under the 2026 API rules (bulk track lookups,
artist top-tracks, and the popularity field are all blocked).

Choices are cached in `data/tracks.json`:

- A newly seen genre is seeded instantly with everynoise's pick.
- Each run re-searches genres **most-stale-first**, and for each it picks the top
  search result **not already in the playlist** — so no duplicates, and every
  refresh yields a genuinely different song. A genre only advances in the queue
  when it actually gets a new track.
- By default (`refresh_batch: 0`) a run tries every genre; when Spotify throttles,
  the circuit breaker stops it and the rest are picked up first next run. Over
  successive runs the whole list keeps cycling to fresh songs.

The cache survives between cloud runs because the weekly job commits it back to
the repository.

## Running on a schedule locally instead (optional)

If you'd rather not use GitHub Actions, schedule `python -m src.build_playlist`
with your operating system's task scheduler (for example cron on macOS/Linux, or
Task Scheduler on Windows). It only runs while your computer is on — and because
your home connection can reach everynoise.com, local runs also refresh the genre
list.

## Troubleshooting

- **"Only parsed N genres"** — everynoise.com changed its page layout; the parser
  in `src/genres.py` needs updating.
- **403 / insufficient scope** — re-run `python -m src.auth`.
- **401 on token refresh** — wrong Client ID/secret, or the refresh token was
  revoked (re-run `python -m src.auth`).
- **Premium required** — the 2026 Spotify rules require the app owner to have
  Premium.

## Project layout

```
config.yaml                 playlist name, strategy, options
requirements.txt
.env.example                credentials template (copy to .env)
src/
  auth.py                   one-time: connect your Spotify account
  spotify_client.py         Spotify Web API client
  genres.py                 read the genre list from everynoise.com
  resolve.py                choose a track for each genre
  build_playlist.py         main script (weekly entry point)
data/
  genres.json               saved genre list
  tracks.json               fresh-mode cache: genre -> track
  playlist.json             snapshot of the current tracklist
  state.json                remembers the playlist id
.github/workflows/weekly.yml  weekly schedule
```
