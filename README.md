# Tennis Scorigami (web app)

A small web app version of the tennis "scorigami" tool: paste a
[tennisrecord.com](https://www.tennisrecord.com) match-history URL, or just
type a player's name, and it fetches every season on record, then shows
which of the 196 standard best-of-three tennis scores that player has (and
hasn't) posted.

This works for **any** USTA adult player listed on tennisrecord.com, not
just one hardcoded name — the original single-player version of this tool
lives in the parent folder.

## Why this needs a server (and can't just be a webpage)

The lookup has to fetch pages from tennisrecord.com on the fly, and browsers
block a plain webpage's JavaScript from fetching arbitrary third-party
sites. So this ships as a tiny Flask backend (which does the fetching) plus
a frontend page it serves. It needs to run somewhere with outbound internet
access — either your own computer, or a small hosting service.

## Run it locally

Requires Python 3.9+.

```bash
cd webapp
python3 -m venv .venv && source .venv/bin/activate   # optional but recommended
pip install -r requirements.txt
python app.py
```

Then open **http://localhost:5000** in your browser.

## Deploy it for free so anyone can use it

Any host that runs a Python web app works. Two easy free options:

### Option A: Render

1. Push this `webapp/` folder to a GitHub repo (or the whole
   `tennis-scorigami` folder — Render just needs `webapp/` as the root).
2. On [render.com](https://render.com), choose **New > Web Service**,
   connect the repo, and set the **root directory** to `webapp` if you
   pushed the parent folder.
3. Render will detect `render.yaml` automatically and use it, or manually
   set:
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn app:app --workers 2 --threads 4 --timeout 90`
4. Deploy. You'll get a public URL like `https://tennis-scorigami.onrender.com`.

The free tier spins down after inactivity, so the first request after a
while takes ~30-60 seconds to wake up — normal for free hosting.

### Option B: Railway

1. Push the `webapp/` folder to GitHub.
2. On [railway.app](https://railway.app), **New Project > Deploy from GitHub
   repo**, pick the repo (set root directory to `webapp` if needed).
3. Railway auto-detects the `Procfile` and Python app. No extra config
   needed. Deploy, then generate a public domain from the service settings.

### Option C: any other host

This is a completely standard Flask app (`app.py`, `requirements.txt`,
`Procfile`). Fly.io, PythonAnywhere, a VPS with `gunicorn` + `nginx`, or a
Docker container all work the same way — install `requirements.txt` and run
`gunicorn app:app`.

## How it works

- `scorigami_lib.py` — fetches and parses tennisrecord.com match-history
  pages (one request per year, done in parallel), then builds the four
  "never had" grids (straight-set wins/losses, match-tiebreak wins/losses).
  Includes the fix for a real tennisrecord.com display quirk: **straight-set
  LOSS matches show their set scores in winner-first order**, not the
  player's own perspective (e.g. a match lost 2-6, 3-6 displays as "6-2,
  6-3" on the site) — the W/L column is the only reliable signal, so those
  two set scores get reversed before anything else uses them. Straight-set
  wins and all 3-set (match-tiebreak) matches are already shown correctly.
- `app.py` — a Flask app with one API route, `POST /api/scorigami`, that
  takes `{ "input": "<url or player name>", "start_year": 2003, "end_year":
  2026 }` and returns the computed report as JSON. Results are cached
  in-memory for 30 minutes per (player, year range) so repeat lookups are
  instant and polite to tennisrecord.com.
- `templates/index.html` — the frontend: a lookup form plus the same grid
  visualization from the original tool, now built dynamically from whatever
  the API returns, with a client-side CSV export of the full match log.

## Notes / limitations

- By default it checks years 2003 through the current year for every
  lookup (fetched in parallel, so it's usually a few seconds). Use the
  "Advanced" year range fields to narrow or widen that.
- Player names must match tennisrecord.com's spelling/capitalization
  exactly — there's no fuzzy search, since tennisrecord.com itself doesn't
  offer one via this URL pattern.
- Non-standard match formats (single-set round-robin pool play, time-capped
  incomplete sets) can't fit the 196-score grid, so they're listed
  separately in the "matches outside this grid" section rather than being
  dropped silently.
- The in-memory cache resets whenever the server restarts (e.g. on a free
  host that spins down when idle) — that's expected and fine.
