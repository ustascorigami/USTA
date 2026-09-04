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

## Set up the database (required)

The leaderboard and the traffic log both need a Postgres database. A free
one takes a couple of minutes to set up and works the same whether you're
running locally or deployed:

1. Go to [neon.tech](https://neon.tech) (or [supabase.com](https://supabase.com) —
   either works, these instructions use Neon) and sign up for free.
2. Create a new project (any name, any region).
3. Neon shows you a **connection string** right on the dashboard — something
   like `postgresql://user:password@ep-xxxx.neon.tech/neondb?sslmode=require`.
   Copy it.
4. Set that as an environment variable named `DATABASE_URL`:
   - **Locally:** `export DATABASE_URL="postgresql://...`" before running
     `python app.py` (or put it in a `.env` file if you use one).
   - **On Render:** go to your service → **Environment** → **Add Environment
     Variable** → key `DATABASE_URL`, value the connection string you copied
     → Save. Render redeploys automatically. (`render.yaml` already declares
     this variable as one Render will prompt you for — the actual value
     never gets committed to the repo.)

The app creates its own tables the first time it starts (`db.init_db()`),
so there's no separate migration step — just set the connection string and
run it.

**Never commit or share your `DATABASE_URL`** — it contains your database
password. It's already covered by `.gitignore` if you put it in a local
`.env` file.

## Run it locally

Requires Python 3.9+.

```bash
cd webapp
python3 -m venv .venv && source .venv/bin/activate   # optional but recommended
pip install -r requirements.txt
export DATABASE_URL="postgresql://..."   # see "Set up the database" above
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
- `app.py` — a Flask app. `POST /api/scorigami` takes `{ "input": "<url or
  player name>", "start_year": 2003, "end_year": 2026 }` and returns the
  computed report as JSON; results are cached in-memory for 30 minutes per
  (player, year range) so repeat lookups are instant and polite to
  tennisrecord.com. `GET /api/leaderboard` and `POST /api/leaderboard` back
  the site-wide leaderboard (see below).
- `db.py` — Postgres storage for the leaderboard and the traffic log (see
  below). Needs `DATABASE_URL` set — see "Set up the database" above.
- `templates/index.html` — the frontend: a lookup form plus the same grid
  visualization from the original tool, now built dynamically from whatever
  the API returns, with a client-side CSV export of the full match log, and
  the leaderboard panel.

## The leaderboard

Anyone can look themselves up, then click **"Add me to the leaderboard"**
to post their claimed-score count (out of 196) to a public, site-wide
ranking. A few deliberate choices:

- **Opt-in, not automatic.** Looking someone else up never adds them to the
  leaderboard — only the person clicking the button (viewing their own
  result) does that, and it's a single upsert keyed by name (+ the
  tennisrecord.com disambiguator, if any), so re-adding yourself later just
  updates your existing entry rather than creating a duplicate.
- **Server-verified, not client-submitted.** The submit endpoint doesn't
  trust numbers sent from the browser — it re-reads the report your browser
  just computed (from the same 30-minute cache `/api/scorigami` already
  uses) and takes the claimed count from there, so the leaderboard can't be
  seeded with spoofed stats.
- **Incomplete results are rejected.** If tennisrecord.com didn't respond
  for some seasons during the lookup, the "Add me" button is disabled (with
  an explanation) rather than letting an undercounted result onto the
  board.

The leaderboard lives in Postgres now (see "Set up the database" above), so
it survives redeploys and restarts — unlike an earlier version of this that
used a local SQLite file, which Render's ephemeral disk would reset on every
deploy.

**Note:** whether looking someone up adds them automatically, versus staying
opt-in-only the way it works today, is still an open question — flag it
whenever you're ready to decide, since it changes what "traffic" on the site
means for the people who show up in it.

## Traffic / usage log

Every `/api/scorigami` call is logged to a `lookup_events` table: timestamp,
which player was searched (+ disambiguator and year range), whether it was
served from the 30-minute cache or freshly fetched, how many matches came
back, how long it took, and the outcome (`success`, `not_found`,
`source_unavailable`, or `error`). Logging is fire-and-forget — if it fails
for any reason, the lookup itself still succeeds; the log entry is just
skipped.

It also records the visitor's IP address (`client_ip`) — worth knowing
plainly, since it's the one piece of this that's about *who's* using the
site rather than *what* they searched, not just something to skim past. For
a small tool shared with a tennis league that's a pretty ordinary thing to
log (most sites keep some form of access log), but it's still real data
about real visitors, so it's worth being upfront with people about it if
that ever feels relevant.

There's no dashboard for this data yet — query it directly. Neon and
Supabase both include a free SQL editor in their dashboard; a few useful
starting queries:

```sql
-- Most recent lookups
SELECT created_at, player, disambig, outcome, cache_hit, total_matches, client_ip
FROM lookup_events ORDER BY created_at DESC LIMIT 50;

-- Most-searched players
SELECT player, count(*) FROM lookup_events GROUP BY player ORDER BY 2 DESC LIMIT 20;

-- Traffic in the last 24 hours, by outcome
SELECT outcome, count(*) FROM lookup_events
WHERE created_at > now() - interval '24 hours' GROUP BY outcome;
```

`db.get_traffic_summary(hours=24)` in `db.py` returns that same kind of
rollup as a Python dict, in case you want to wire up a simple `/api/admin/traffic`
route or a small stats panel later — it's not called from anywhere yet.

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
