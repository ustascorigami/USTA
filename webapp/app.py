import datetime
import os
import threading
import time
import urllib.parse

from flask import Flask, jsonify, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix

import db
from scorigami_lib import ScorigamiError, SourceUnavailableError, build_report, extract_player_query

app = Flask(__name__)
# Render (and most hosts) put the app behind a reverse proxy, so the real
# visitor IP arrives in X-Forwarded-For rather than as the raw socket peer.
# ProxyFix makes request.remote_addr reflect that real IP for the traffic log.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)

db.init_db()

DEFAULT_START_YEAR = 2003
CACHE_TTL_SECONDS = 60 * 30  # 30 minutes
LEADERBOARD_LIMIT_DEFAULT = 25
LEADERBOARD_LIMIT_MAX = 100
_cache = {}
_cache_lock = threading.Lock()


def _cache_get(key):
    with _cache_lock:
        entry = _cache.get(key)
        if not entry:
            return None
        value, expires_at = entry
        if time.time() > expires_at:
            del _cache[key]
            return None
        return value


def _cache_set(key, value):
    with _cache_lock:
        _cache[key] = (value, time.time() + CACHE_TTL_SECONDS)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/scorigami", methods=["POST"])
def api_scorigami():
    body = request.get_json(silent=True) or {}
    user_input = body.get("input", "")
    start_year = body.get("start_year")
    end_year = body.get("end_year")

    current_year = datetime.date.today().year
    try:
        start_year = int(start_year) if start_year else DEFAULT_START_YEAR
        end_year = int(end_year) if end_year else current_year
    except (TypeError, ValueError):
        return jsonify({"error": "start_year and end_year must be numbers."}), 400

    if start_year > end_year:
        return jsonify({"error": "start_year must be before end_year."}), 400
    if end_year - start_year > 60:
        return jsonify({"error": "That's more than 60 years of history in one request — narrow the range."}), 400

    try:
        query = extract_player_query(user_input)
    except ScorigamiError as e:
        return jsonify({"error": str(e)}), 400

    player = query["player"]
    disambig = query["disambig"]
    client_ip = request.remote_addr

    cache_key = (player.lower(), disambig, start_year, end_year)
    cached = _cache_get(cache_key)
    if cached is not None:
        db.log_lookup(
            player, disambig, start_year, end_year,
            outcome="success", cache_hit=True,
            total_matches=cached.get("total_matches"), duration_ms=0,
            client_ip=client_ip,
        )
        return jsonify(cached)

    started_at = time.monotonic()
    try:
        report = build_report(player, start_year, end_year, disambig=disambig)
    except SourceUnavailableError as e:
        db.log_lookup(
            player, disambig, start_year, end_year,
            outcome="source_unavailable", cache_hit=False,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            client_ip=client_ip,
        )
        # Don't cache this -- it's a transient source problem, not a real
        # answer, so the next request should try tennisrecord.com again
        # rather than replaying a stale failure for 30 minutes.
        return jsonify({"error": str(e)}), 503
    except ScorigamiError as e:
        db.log_lookup(
            player, disambig, start_year, end_year,
            outcome="not_found", cache_hit=False,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            client_ip=client_ip,
        )
        return jsonify({"error": str(e)}), 404
    except Exception as e:  # noqa: BLE001
        db.log_lookup(
            player, disambig, start_year, end_year,
            outcome="error", cache_hit=False,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            client_ip=client_ip,
        )
        return jsonify({"error": f"Something went wrong fetching that player's data: {e}"}), 502

    db.log_lookup(
        player, disambig, start_year, end_year,
        outcome="success", cache_hit=False,
        total_matches=report.get("total_matches"),
        duration_ms=int((time.monotonic() - started_at) * 1000),
        client_ip=client_ip,
    )
    _cache_set(cache_key, report)
    return jsonify(report)


def _canonical_input(player: str, disambig: str) -> str:
    """
    The string this app's own search box would accept to reliably reach
    this exact player again -- their disambiguated tennisrecord.com URL if
    they needed one to be found in the first place, otherwise just their
    name.
    """
    if not disambig:
        return player
    params = {
        "year": datetime.date.today().year,
        "playername": player,
        "s": disambig,
        "mt": 0,
        "lt": 0,
        "yr": 0,
    }
    return "https://www.tennisrecord.com/adult/matchhistory.aspx?" + urllib.parse.urlencode(params)


@app.route("/api/leaderboard", methods=["GET"])
def api_leaderboard_get():
    limit = request.args.get("limit", default=LEADERBOARD_LIMIT_DEFAULT, type=int) or LEADERBOARD_LIMIT_DEFAULT
    limit = max(1, min(limit, LEADERBOARD_LIMIT_MAX))
    return jsonify({"entries": db.get_leaderboard(limit=limit)})


@app.route("/api/leaderboard", methods=["POST"])
def api_leaderboard_post():
    body = request.get_json(silent=True) or {}
    player = (body.get("player") or "").strip()
    disambig = body.get("disambig") or None
    start_year = body.get("start_year")
    end_year = body.get("end_year")

    if not player:
        return jsonify({"error": "Missing player."}), 400
    try:
        start_year = int(start_year)
        end_year = int(end_year)
    except (TypeError, ValueError):
        return jsonify({"error": "Missing or invalid start_year/end_year."}), 400

    # Require a report we already computed for this exact lookup (rather
    # than trusting whatever numbers the client sends) so the leaderboard
    # can't be seeded with spoofed or stale stats.
    cache_key = (player.lower(), disambig, start_year, end_year)
    report = _cache_get(cache_key)
    if report is None:
        return jsonify(
            {
                "error": "Couldn't find a recent result for that search to add. "
                "Run the lookup again above, then try adding yourself to the "
                "leaderboard right after it finishes."
            }
        ), 409

    if report.get("fetch_errors"):
        return jsonify(
            {
                "error": "This result is missing some seasons because tennisrecord.com "
                "didn't respond for all of them, so it wouldn't be a fair number for "
                "the leaderboard. Try the search again once the site is responding "
                "fully, then add yourself."
            }
        ), 409

    s = report["summary"]
    db.upsert_entry(
        player=report["player"],
        disambig=report.get("disambig"),
        claimed=s["claimed"],
        universe=s["universe"],
        first_year=report["first_year"],
        last_year=report["last_year"],
        total_matches=report["total_matches"],
        share_query=_canonical_input(report["player"], report.get("disambig")),
    )
    rank = db.get_rank(report["player"], report.get("disambig"))
    return jsonify({"rank": rank, "entries": db.get_leaderboard(limit=LEADERBOARD_LIMIT_DEFAULT)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
