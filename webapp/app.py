import datetime
import os
import threading
import time

from flask import Flask, jsonify, render_template, request

from scorigami_lib import ScorigamiError, build_report, extract_player_query

app = Flask(__name__)

DEFAULT_START_YEAR = 2003
CACHE_TTL_SECONDS = 60 * 30  # 30 minutes
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

    cache_key = (player.lower(), disambig, start_year, end_year)
    cached = _cache_get(cache_key)
    if cached is not None:
        return jsonify(cached)

    try:
        report = build_report(player, start_year, end_year, disambig=disambig)
    except ScorigamiError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"Something went wrong fetching that player's data: {e}"}), 502

    _cache_set(cache_key, report)
    return jsonify(report)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
