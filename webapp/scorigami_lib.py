"""
Core scraping / parsing / grid-building logic for the Tennis Scorigami
web app. Works for ANY tennisrecord.com adult player, not just one
hardcoded name.

Data source: tennisrecord.com match-history pages, one per year:
    https://www.tennisrecord.com/adult/matchhistory.aspx?year=YYYY&playername=NAME&mt=0&lt=0&yr=1

Design notes (carried over from the original single-player tool):
  - Singles and doubles matches are pooled together.
  - Tournament and league matches are both included.
  - A score "pattern" for scorigami purposes is the literal set-score
    sequence plus the W/L outcome. A USTA 10-point match tiebreak always
    shows as a bare "1-0" third set on this site, so it naturally reads
    as different from a real, time-capped third set like "3-1".
  - IMPORTANT BUG WORKAROUND: tennisrecord.com displays 2-set (straight-
    set) LOSS matches with each set's digits in winner-first order --
    i.e. NOT the player's own perspective. A match the player actually
    lost 2-6, 3-6 is shown on the site as "6-2, 6-3". The W/L column is
    the only reliable signal, so for any 2-set match where wl == "L",
    each set string is reversed (digits swapped around the dash) before
    it's used for anything. Straight-set WINS and all 3-set (breaker-
    decided) matches of either outcome are already shown in the
    player's own perspective and need no correction.
"""
import datetime
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.tennisrecord.com/adult/matchhistory.aspx"
NON_SCORE_RESULTS = {"def", "default", "ret", "w/o", "wo"}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

# The 7 standard tennis set scores, in the player's own perspective.
WON = ["6-0", "6-1", "6-2", "6-3", "6-4", "7-5", "7-6"]
LOST = ["0-6", "1-6", "2-6", "3-6", "4-6", "5-7", "6-7"]
WON_SET = set(WON)
LOST_SET = set(LOST)


class ScorigamiError(Exception):
    pass


def extract_player_query(user_input: str) -> dict:
    """
    Accept either a full tennisrecord.com URL or a bare player name.
    Returns {"player": str, "disambig": str | None}.

    When multiple players share a name, tennisrecord.com disambiguates them
    with an "s" query param (e.g. ...&playername=Ryan%20White&s=2...) once
    you've picked the right one on their site. If a pasted URL carries that
    param, we carry it through on every year's request so the app pulls the
    same player's history the user actually landed on, instead of guessing
    from the bare name.
    """
    s = (user_input or "").strip()
    if not s:
        raise ScorigamiError("Please enter a tennisrecord.com URL or a player name.")
    if s.lower().startswith("http://") or s.lower().startswith("https://"):
        parsed = urllib.parse.urlparse(s)
        if "tennisrecord.com" not in parsed.netloc.lower():
            raise ScorigamiError("That doesn't look like a tennisrecord.com URL.")
        qs = urllib.parse.parse_qs(parsed.query)
        names = qs.get("playername")
        if not names or not names[0].strip():
            raise ScorigamiError(
                "Couldn't find a playername in that URL. Paste the full "
                "tennisrecord.com match-history link, or just type the player's name."
            )
        disambig_vals = qs.get("s")
        disambig = disambig_vals[0].strip() if disambig_vals and disambig_vals[0].strip() else None
        return {"player": names[0].strip(), "disambig": disambig}
    return {"player": s, "disambig": None}


def extract_player_name(user_input: str) -> str:
    """Back-compat wrapper around extract_player_query: just the name."""
    return extract_player_query(user_input)["player"]


def fetch_year_html(player: str, year: int, disambig: str = None, timeout: float = 15.0) -> str:
    params = {"year": year, "playername": player, "mt": 0, "lt": 0, "yr": 1}
    if disambig:
        params["s"] = disambig
    url = BASE_URL + "?" + urllib.parse.urlencode(params)
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def _cell_names(cell) -> list:
    names = []
    for a in cell.find_all("a"):
        name = a.get_text(strip=True)
        if name:
            names.append(name)
    if not names:
        text = cell.get_text(strip=True)
        if text:
            names.append(text)
    return names


def parse_year_matches(html: str, year: int) -> list:
    """Parse the wide 10-column match-history table for one year."""
    soup = BeautifulSoup(html, "html.parser")

    matches = []
    for table in soup.find_all("table"):
        header_row = table.find("tr")
        if header_row is None:
            continue
        header_text = header_row.get_text(" ", strip=True)
        if "Match Date" not in header_text or "Opponent" not in header_text:
            continue

        for row in table.find_all("tr")[1:]:
            cells = row.find_all("td")
            if len(cells) < 8:
                continue

            date_txt = cells[0].get_text(strip=True)
            league_txt = cells[1].get_text(" / ", strip=True)
            team_txt = cells[2].get_text(" / ", strip=True)
            court_txt = cells[3].get_text(strip=True)
            partner_names = _cell_names(cells[4])
            opponent_names = _cell_names(cells[5])
            wl_txt = cells[6].get_text(strip=True)
            result_txt = cells[7].get_text("|", strip=True)

            if not date_txt:
                continue
            try:
                date = datetime.datetime.strptime(date_txt, "%m/%d/%Y").date()
            except ValueError:
                continue

            is_tournament = league_txt.strip().lower().startswith("tournament")
            is_doubles = len(partner_names) > 0
            sets = [s for s in result_txt.split("|") if s]

            matches.append(
                {
                    "year": year,
                    "date": date,
                    "league": league_txt,
                    "event": team_txt,
                    "court": court_txt,
                    "is_tournament": is_tournament,
                    "is_doubles": is_doubles,
                    "partner": ", ".join(partner_names),
                    "opponents": ", ".join(opponent_names),
                    "wl": wl_txt,
                    "sets": sets,
                    "result_raw": result_txt,
                }
            )
        break  # wide table found; ignore other tables on the page

    return matches


def collect_all_years(
    player: str, start_year: int, end_year: int, disambig: str = None, max_workers: int = 10
):
    """Fetch + parse every year in [start_year, end_year] concurrently."""
    matches = []
    fetched_years = {}
    errors = {}

    def _one(year):
        html = fetch_year_html(player, year, disambig=disambig)
        return year, parse_year_matches(html, year)

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(_one, y): y for y in range(start_year, end_year + 1)}
        for fut in as_completed(futs):
            y = futs[fut]
            try:
                _, year_matches = fut.result()
                fetched_years[y] = len(year_matches)
                matches.extend(year_matches)
            except Exception as e:  # noqa: BLE001
                errors[y] = str(e)

    return matches, fetched_years, errors


def reverse_set(s: str) -> str:
    a, b = s.split("-")
    return f"{b}-{a}"


def normalize_perspective(matches: list) -> None:
    """
    Correct the winner-first display bug in place: for any 2-set match
    tennisrecord.com marks as a LOSS, each individual set string is shown
    with the winner's (opponent's) games first rather than the player's
    own. Reverse those two set strings so every downstream consumer
    (scorigami patterns, the grids, the CSV export) sees the player's own
    perspective consistently. The original literal site text is kept in
    "result_raw" untouched. Straight-set WINS and all 3-set (match-
    tiebreak) matches of either outcome are already player-perspective and
    are left alone -- this is scoped exactly to the confirmed bug.
    """
    for m in matches:
        if len(m["sets"]) == 2 and m["wl"] == "L":
            m["sets"] = [reverse_set(s) for s in m["sets"]]


def is_opponent_default(match: dict) -> bool:
    """
    True when this "match" is actually an opponent no-show default rather
    than a played match. tennisrecord.com logs these with a fake 6-0, 6-0
    score and literally lists the opponent (or partner) as "Default" --
    there's no real score to track, so these are excluded the same way a
    walkover/retirement is.
    """
    opp = (match.get("opponents") or "").strip().lower()
    partner = (match.get("partner") or "").strip().lower()
    return opp == "default" or partner == "default"


def score_pattern(match: dict):
    sets = match["sets"]
    if not sets:
        return None
    if is_opponent_default(match):
        return None
    joined = " ".join(sets).strip().lower()
    if joined in NON_SCORE_RESULTS or any(tok in NON_SCORE_RESULTS for tok in joined.split()):
        return None
    return (tuple(sets), match["wl"])


def format_pattern(pattern) -> str:
    if pattern is None:
        return ""
    sets, wl = pattern
    return f"{', '.join(sets)} ({wl})"


def compute_scorigami(matches: list) -> list:
    """Sort chronologically and flag each match's first-ever score pattern."""
    matches_sorted = sorted(matches, key=lambda m: (m["date"], m["year"]))
    seen = set()
    for m in matches_sorted:
        pattern = score_pattern(m)
        m["pattern"] = pattern
        m["pattern_display"] = format_pattern(pattern)
        if pattern is None:
            m["is_scorigami"] = False
            if is_opponent_default(m):
                m["scorigami_note"] = "no comparable score (opponent no-show default)"
            else:
                m["scorigami_note"] = "no comparable score (walkover/default/retirement)"
        else:
            m["is_scorigami"] = pattern not in seen
            m["scorigami_note"] = ""
            seen.add(pattern)
    return matches_sorted


def build_grids(matches: list) -> dict:
    """
    Build the four "never had" grids (straight-set wins/losses,
    match-tiebreak wins/losses) plus the list of matches that don't fit
    a clean best-of-three grid. Expects normalize_perspective() to have
    already been run on `matches`.
    """
    gridA1, gridA2, gridB1, gridB2 = {}, {}, {}, {}
    excluded = []

    matches_sorted = sorted(matches, key=lambda m: (m["date"], m["year"]))

    for m in matches_sorted:
        sets = m["sets"]
        wl = m["wl"]
        date = m["date"]
        opponents = m["opponents"] or m["partner"] or ""

        joined = " ".join(sets).strip().lower()
        if not sets or is_opponent_default(m) or joined in NON_SCORE_RESULTS or any(
            t in NON_SCORE_RESULTS for t in joined.split()
        ):
            continue  # no comparable score at all (includes opponent no-show defaults)

        if len(sets) == 1:
            excluded.append((date, sets, wl, "single-set-roundrobin"))
            continue

        if len(sets) == 2:
            # sets is already normalized to the player's own perspective by
            # normalize_perspective() before build_grids() is called.
            s1, s2 = sets
            if s1 in WON_SET and s2 in WON_SET and wl == "W":
                gridA1.setdefault(f"{s1}|{s2}", [date.isoformat(), opponents])
            elif s1 in LOST_SET and s2 in LOST_SET and wl == "L":
                gridA2.setdefault(f"{s1}|{s2}", [date.isoformat(), opponents])
            else:
                excluded.append((date, sets, wl, "nonstandard-2set"))
            continue

        if len(sets) == 3:
            third = sets[2]
            if third != "1-0":
                excluded.append((date, sets, wl, "nonstandard-split"))
                continue
            a, b = sets[0], sets[1]
            if a in WON_SET and b in LOST_SET:
                won_set, lost_set = a, b
            elif b in WON_SET and a in LOST_SET:
                won_set, lost_set = b, a
            else:
                excluded.append((date, sets, wl, "nonstandard-split"))
                continue
            key = f"{won_set}|{lost_set}"
            if wl == "W":
                gridB1.setdefault(key, [date.isoformat(), opponents])
            elif wl == "L":
                gridB2.setdefault(key, [date.isoformat(), opponents])
            else:
                excluded.append((date, sets, wl, "nonstandard-split"))
            continue

        excluded.append((date, sets, wl, "nonstandard-split"))

    excluded_sorted = sorted(excluded, key=lambda r: r[0])
    excluded_json = [[d.isoformat(), s, w, k] for d, s, w, k in excluded_sorted]

    return {
        "WON": WON,
        "LOST": LOST,
        "gridA1": gridA1,
        "gridA2": gridA2,
        "gridB1": gridB1,
        "gridB2": gridB2,
        "excluded": excluded_json,
    }


def compute_top_scores(matches: list, n: int = 5) -> list:
    """
    The N most frequently posted score patterns (set line + W/L), most
    common first. Ties break by earliest first occurrence so the order is
    stable across requests.
    """
    counts = {}
    first_seen = {}
    for m in matches:
        pattern = m.get("pattern")
        if pattern is None:
            continue
        counts[pattern] = counts.get(pattern, 0) + 1
        if pattern not in first_seen or m["date"] < first_seen[pattern]:
            first_seen[pattern] = m["date"]

    ranked = sorted(
        counts.items(),
        key=lambda kv: (-kv[1], first_seen[kv[0]]),
    )[:n]

    return [
        {
            "pattern_display": format_pattern(pattern),
            "sets": list(pattern[0]),
            "wl": pattern[1],
            "count": count,
            "first_date": first_seen[pattern].isoformat(),
        }
        for pattern, count in ranked
    ]


def compute_top_partners(matches: list, n: int = 5) -> list:
    """
    The N doubles partners this player has teamed up with most often, most
    common first. Counts every doubles match on record (win, loss, or
    default) since this is about who they played alongside, not the score
    -- ties break by earliest first occurrence so the order is stable.
    """
    counts = {}
    wins = {}
    first_seen = {}
    last_seen = {}
    for m in matches:
        if not m.get("is_doubles"):
            continue
        partner = (m.get("partner") or "").strip()
        if not partner:
            continue
        counts[partner] = counts.get(partner, 0) + 1
        if m.get("wl") == "W":
            wins[partner] = wins.get(partner, 0) + 1
        if partner not in first_seen or m["date"] < first_seen[partner]:
            first_seen[partner] = m["date"]
        if partner not in last_seen or m["date"] > last_seen[partner]:
            last_seen[partner] = m["date"]

    ranked = sorted(
        counts.items(),
        key=lambda kv: (-kv[1], first_seen[kv[0]]),
    )[:n]

    return [
        {
            "partner": partner,
            "count": count,
            "wins": wins.get(partner, 0),
            "losses": count - wins.get(partner, 0),
            "first_date": first_seen[partner].isoformat(),
            "last_date": last_seen[partner].isoformat(),
        }
        for partner, count in ranked
    ]


def build_report(player: str, start_year: int, end_year: int, disambig: str = None) -> dict:
    matches, fetched_years, errors = collect_all_years(
        player, start_year, end_year, disambig=disambig
    )

    if not matches:
        hint = (
            " This name matches more than one player on tennisrecord.com and the "
            "disambiguated link didn't return anything either -- try re-pasting the "
            "exact URL you land on after picking the right player on their site."
            if disambig
            else " If tennisrecord.com shows more than one player with this name, "
            "paste the full match-history URL for the specific one you mean instead "
            "of just typing the name."
        )
        raise ScorigamiError(
            f'No matches found for "{player}" between {start_year} and {end_year}. '
            "Double check the player name matches tennisrecord.com exactly "
            "(first and last name, correct spelling/capitalization)." + hint
        )

    normalize_perspective(matches)
    matches = compute_scorigami(matches)
    grids = build_grids(matches)

    scored = [m for m in matches if m["pattern"] is not None]
    scorigami = [m for m in matches if m["is_scorigami"]]
    top_scores = compute_top_scores(matches, n=5)
    top_partners = compute_top_partners(matches, n=5)

    years_with_data = sorted(y for y, n in fetched_years.items() if n > 0)
    first_year = years_with_data[0] if years_with_data else start_year
    last_year = years_with_data[-1] if years_with_data else end_year

    claimed = (
        len(grids["gridA1"]) + len(grids["gridA2"]) + len(grids["gridB1"]) + len(grids["gridB2"])
    )
    universe = 49 * 4

    match_log = [
        {
            "date": m["date"].isoformat(),
            "is_scorigami": m["is_scorigami"],
            "pattern_display": m["pattern_display"],
            "wl": m["wl"],
            "is_tournament": m["is_tournament"],
            "is_doubles": m["is_doubles"],
            "league": m["league"],
            "event": m["event"],
            "court": m["court"],
            "partner": m["partner"],
            "opponents": m["opponents"],
            "result_raw": m["result_raw"],
            "scorigami_note": m["scorigami_note"],
        }
        for m in matches
    ]

    return {
        "player": player,
        "disambig": disambig,
        "queried_start_year": start_year,
        "queried_end_year": end_year,
        "first_year": first_year,
        "last_year": last_year,
        "total_matches": len(matches),
        "scored_matches": len(scored),
        "scorigami_count": len(scorigami),
        "fetch_errors": errors,
        "grids": grids,
        "top_scores": top_scores,
        "top_partners": top_partners,
        "summary": {
            "claimed": claimed,
            "universe": universe,
            "never": universe - claimed,
            "gridA1_count": len(grids["gridA1"]),
            "gridA2_count": len(grids["gridA2"]),
            "gridB1_count": len(grids["gridB1"]),
            "gridB2_count": len(grids["gridB2"]),
            "excluded_count": len(grids["excluded"]),
        },
        "match_log": match_log,
    }
