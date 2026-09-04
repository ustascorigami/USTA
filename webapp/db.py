"""
Postgres-backed storage for this app: the "most scores claimed" leaderboard,
plus a lightweight traffic/usage log.

Needs a DATABASE_URL environment variable (a standard Postgres connection
string, e.g. from a free Neon or Supabase project) -- see README.md for how
to set one up. init_db() creates both tables the first time it's called and
is safe to call on every app startup after that (CREATE TABLE IF NOT EXISTS).
"""
import datetime
import os

import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ.get("DATABASE_URL")


class DatabaseNotConfiguredError(RuntimeError):
    pass


def _connect():
    if not DATABASE_URL:
        raise DatabaseNotConfiguredError(
            "DATABASE_URL is not set. Create a free Postgres database (Neon or "
            "Supabase both work) and set DATABASE_URL to its connection string "
            "as an environment variable -- see README.md."
        )
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def init_db():
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS leaderboard (
                player_key    TEXT PRIMARY KEY,
                player        TEXT NOT NULL,
                disambig      TEXT,
                claimed       INTEGER NOT NULL,
                universe      INTEGER NOT NULL,
                first_year    INTEGER,
                last_year     INTEGER,
                total_matches INTEGER,
                share_query   TEXT,
                submitted_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS lookup_events (
                id            SERIAL PRIMARY KEY,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
                player        TEXT,
                disambig      TEXT,
                start_year    INTEGER,
                end_year      INTEGER,
                outcome       TEXT NOT NULL,
                cache_hit     BOOLEAN NOT NULL DEFAULT FALSE,
                total_matches INTEGER,
                duration_ms   INTEGER,
                client_ip     TEXT
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_lookup_events_created_at "
            "ON lookup_events (created_at)"
        )
        conn.commit()


def _player_key(player: str, disambig: str = None) -> str:
    return player.strip().lower() + "|" + (disambig or "")


# ---------------------------------------------------------------- leaderboard

def upsert_entry(
    player: str,
    disambig: str,
    claimed: int,
    universe: int,
    first_year: int,
    last_year: int,
    total_matches: int,
    share_query: str,
) -> None:
    """
    Insert or update this player's row. A player (disambiguated by name +
    the tennisrecord.com "s" value, same as everywhere else in this app)
    can only ever have one row -- re-submitting just refreshes their
    numbers and moves their timestamp up, rather than creating a duplicate.
    """
    key = _player_key(player, disambig)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO leaderboard
                (player_key, player, disambig, claimed, universe, first_year,
                 last_year, total_matches, share_query, submitted_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (player_key) DO UPDATE SET
                player = EXCLUDED.player,
                claimed = EXCLUDED.claimed,
                universe = EXCLUDED.universe,
                first_year = EXCLUDED.first_year,
                last_year = EXCLUDED.last_year,
                total_matches = EXCLUDED.total_matches,
                share_query = EXCLUDED.share_query,
                submitted_at = now()
            """,
            (key, player, disambig, claimed, universe, first_year, last_year,
             total_matches, share_query),
        )
        conn.commit()


def get_rank(player: str, disambig: str = None):
    """1-based rank of this player by claimed count (ties broken by whoever
    submitted first), or None if they're not on the board."""
    key = _player_key(player, disambig)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT player_key FROM leaderboard ORDER BY claimed DESC, submitted_at ASC"
        )
        rows = cur.fetchall()
    for i, row in enumerate(rows, start=1):
        if row["player_key"] == key:
            return i
    return None


def get_leaderboard(limit: int = 25) -> list:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT player, disambig, claimed, universe, first_year, last_year,
                   total_matches, share_query, submitted_at
            FROM leaderboard
            ORDER BY claimed DESC, submitted_at ASC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("submitted_at"), (datetime.datetime, datetime.date)):
            d["submitted_at"] = d["submitted_at"].isoformat()
        out.append(d)
    return out


# ------------------------------------------------------------- traffic / usage

def log_lookup(
    player: str,
    disambig: str,
    start_year: int,
    end_year: int,
    outcome: str,
    cache_hit: bool,
    total_matches: int = None,
    duration_ms: int = None,
    client_ip: str = None,
) -> None:
    """
    Record one /api/scorigami call. This is fire-and-forget for the caller:
    it never raises, so a logging hiccup can't break a real lookup for the
    person using the app.
    """
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO lookup_events
                    (player, disambig, start_year, end_year, outcome, cache_hit,
                     total_matches, duration_ms, client_ip)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (player, disambig, start_year, end_year, outcome, cache_hit,
                 total_matches, duration_ms, client_ip),
            )
            conn.commit()
    except Exception:  # noqa: BLE001
        pass


def get_traffic_summary(hours: int = 24) -> dict:
    """
    A quick rollup of recent activity: total lookups, how many were cache
    hits, how many distinct players were searched, and a breakdown by
    outcome. Not wired to any page yet -- query it from a Python shell, or
    hand it to a future /api/admin/traffic route -- but it's here so that
    data's easy to get at without writing raw SQL by hand later.
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                count(*) AS total_lookups,
                count(*) FILTER (WHERE cache_hit) AS cache_hits,
                count(DISTINCT lower(player)) AS distinct_players
            FROM lookup_events
            WHERE created_at > now() - (%s * INTERVAL '1 hour')
            """,
            (hours,),
        )
        totals = dict(cur.fetchone())

        cur.execute(
            """
            SELECT outcome, count(*) AS n
            FROM lookup_events
            WHERE created_at > now() - (%s * INTERVAL '1 hour')
            GROUP BY outcome
            ORDER BY n DESC
            """,
            (hours,),
        )
        by_outcome = {row["outcome"]: row["n"] for row in cur.fetchall()}

    totals["by_outcome"] = by_outcome
    totals["hours"] = hours
    return totals
