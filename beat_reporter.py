"""
The Daydream Dispatch -- weekly fantasy beat reporter, by Matthew Waters.

Runs in GitHub Actions once a week. Pulls the league live from Sleeper, rebuilds
the all-time history and rivalries, reads lore.md for the pre-Sleeper years, has
Claude write the edition in first person as Matthew Waters, and publishes a styled
newsletter page plus an updating archive homepage into docs/ (served by Pages).

Secrets it expects (set in the repo, never in code):
  ANTHROPIC_API_KEY   your Claude API key (sk-ant-...)
  LEAGUE_ID           your Sleeper league id (the long number in the league URL)
Optional:
  BEAT_MODEL          model id, defaults to claude-sonnet-5
  BEAT_WEEK           force a specific week instead of the current one
"""

import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent
DOCS = ROOT / "docs"
EDITIONS = DOCS / "editions"
MANIFEST = DOCS / "editions.json"
CACHE = ROOT / ".cache"
for d in (DOCS, EDITIONS, CACHE):
    d.mkdir(parents=True, exist_ok=True)

PUBLICATION = "The Daydream Dispatch"
BYLINE = "Matthew Waters"
TAGLINE = "Fantasy football's least objective newsletter"

LEAGUE_ID = os.environ.get("LEAGUE_ID", "").strip()
MODEL = os.environ.get("BEAT_MODEL", "claude-sonnet-5")

# ----------------------------------------------------------------------------
# Sleeper read-only API (no key needed)
# ----------------------------------------------------------------------------
BASE = "https://api.sleeper.app/v1"
_session = requests.Session()
_session.headers.update({"User-Agent": "daydream-dispatch/1.0"})


def _get(path):
    r = _session.get(f"{BASE}/{path.lstrip('/')}", timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return None if r.text.strip() in ("", "null") else r.json()


def get_state():
    return _get("state/nfl") or {}

def get_league(lid):
    return _get(f"league/{lid}")

def get_users(lid):
    return _get(f"league/{lid}/users") or []

def get_rosters(lid):
    return _get(f"league/{lid}/rosters") or []

def get_matchups(lid, week):
    return _get(f"league/{lid}/matchups/{week}") or []

def get_bracket(lid):
    return _get(f"league/{lid}/winners_bracket") or []

def get_transactions(lid, week):
    return _get(f"league/{lid}/transactions/{week}") or []


_players = None

def _load_players():
    pc = CACHE / "players.json"
    if pc.exists() and time.time() - pc.stat().st_mtime < 86400:
        return json.loads(pc.read_text())
    data = _get("players/nfl") or {}
    pc.write_text(json.dumps(data))
    return data

def player_name(pid):
    global _players
    if pid is None:
        return "an empty slot"
    if isinstance(pid, str) and pid.isalpha():
        return f"{pid} D/ST"
    if _players is None:
        _players = _load_players()
    p = _players.get(str(pid))
    if not p:
        return f"Player {pid}"
    nm = p.get("full_name") or f"{p.get('first_name','')} {p.get('last_name','')}".strip()
    return nm or f"Player {pid}"


# ----------------------------------------------------------------------------
# History: walk Sleeper seasons, rebuild H2H, all-time, champions
# ----------------------------------------------------------------------------
def league_chain(lid):
    chain, cur, seen = [], lid, set()
    while cur and cur not in seen:
        seen.add(cur)
        lg = get_league(cur)
        if not lg:
            break
        chain.append(lg)
        cur = lg.get("previous_league_id")
    return chain


def owner_map(lid):
    users = {u["user_id"]: u for u in get_users(lid)}
    out = {}
    for r in get_rosters(lid):
        u = users.get(r.get("owner_id"), {})
        meta = u.get("metadata") or {}
        out[r["roster_id"]] = {
            "user_id": r.get("owner_id"),
            "name": u.get("display_name", f"Team {r['roster_id']}"),
            "team": meta.get("team_name") or u.get("display_name", f"Team {r['roster_id']}"),
        }
    return out


def build_history(lid):
    chain = league_chain(lid)
    names, h2h = {}, defaultdict(lambda: {"a": 0, "b": 0, "games": []})
    alltime = defaultdict(lambda: {"w": 0, "l": 0, "name": ""})
    champs = []
    for lg in reversed(chain):
        cid, season = lg["league_id"], lg.get("season")
        owners = owner_map(cid)
        for info in owners.values():
            if info["user_id"]:
                names[info["user_id"]] = info["team"]
        pw = (lg.get("settings") or {}).get("playoff_week_start") or 15
        for wk in range(1, min(pw, 19)):
            ms = get_matchups(cid, wk)
            if not ms or all((m.get("points") or 0) == 0 for m in ms):
                continue
            pairs = defaultdict(list)
            for m in ms:
                if m.get("matchup_id") is not None:
                    pairs[m["matchup_id"]].append(m)
            for pair in pairs.values():
                if len(pair) != 2:
                    continue
                a, b = pair
                ua = owners.get(a["roster_id"], {}).get("user_id")
                ub = owners.get(b["roster_id"], {}).get("user_id")
                if not ua or not ub:
                    continue
                pa, pb = a.get("points") or 0, b.get("points") or 0
                key = tuple(sorted([ua, ub]))
                first = key[0]
                pf, ps = (pa, pb) if ua == first else (pb, pa)
                rec = h2h[key]
                if pf > ps:
                    rec["a"] += 1; alltime[first]["w"] += 1; alltime[key[1]]["l"] += 1
                elif ps > pf:
                    rec["b"] += 1; alltime[key[1]]["w"] += 1; alltime[first]["l"] += 1
                rec["games"].append({"season": season, "week": wk,
                                     "margin": round(abs(pf - ps), 2)})
        final = next((g for g in get_bracket(cid) if g.get("p") == 1), None)
        if final and final.get("w"):
            champs.append({"season": season,
                           "champion": owners.get(final["w"], {}).get("team")})
    for uid, row in alltime.items():
        row["name"] = names.get(uid, uid)
    return {"names": names, "h2h": dict(h2h), "alltime": dict(alltime), "champions": champs}


def h2h_line(hist, ua, ub):
    key = tuple(sorted([ua, ub]))
    rec = hist["h2h"].get(key)
    na, nb = hist["names"].get(ua, ua), hist["names"].get(ub, ub)
    if not rec or not rec["games"]:
        return f"{na} and {nb} have no Sleeper era history on record."
    aw = rec["a"] if ua == key[0] else rec["b"]
    bw = rec["b"] if ua == key[0] else rec["a"]
    big = max(rec["games"], key=lambda g: g["margin"])
    lead = f"{na} leads the series {aw} to {bw}" if aw >= bw else f"{na} trails the series {aw} to {bw}"
    return f"{lead}. Widest margin {big['margin']} in {big['season']} week {big['week']}."


# ----------------------------------------------------------------------------
# Assemble the weekly briefing (plain text handed to Claude)
# ----------------------------------------------------------------------------
def top_players(m, n=3):
    st = m.get("starters") or []
    pts = m.get("starters_points") or []
    rows = [(player_name(p), round(pts[i] if i < len(pts) else 0, 2)) for i, p in enumerate(st)]
    rows.sort(key=lambda x: x[1], reverse=True)
    return rows[:n]


def build_briefing(week):
    league = get_league(LEAGUE_ID)
    if not league:
        raise SystemExit(f"Could not load league {LEAGUE_ID}. Check the LEAGUE_ID secret.")
    season = league.get("season")
    owners = owner_map(LEAGUE_ID)
    rosters = {r["roster_id"]: (r.get("settings") or {}) for r in get_rosters(LEAGUE_ID)}
    hist = build_history(LEAGUE_ID)
    ms = get_matchups(LEAGUE_ID, week)

    pairs = defaultdict(list)
    for m in ms:
        if m.get("matchup_id") is not None:
            pairs[m["matchup_id"]].append(m)

    lines = [f"LEAGUE: {league.get('name')}  SEASON: {season}  WEEK: {week}", "", "MATCHUPS THIS WEEK:"]
    for pair in pairs.values():
        if len(pair) != 2:
            continue
        a, b = pair
        ia, ib = owners.get(a["roster_id"], {}), owners.get(b["roster_id"], {})
        sa, sb = rosters.get(a["roster_id"], {}), rosters.get(b["roster_id"], {})
        lines.append(f"\n- {ia.get('team')} ({sa.get('wins',0)} and {sa.get('losses',0)}) "
                     f"vs {ib.get('team')} ({sb.get('wins',0)} and {sb.get('losses',0)})")
        lines.append(f"  score so far: {round(a.get('points') or 0,2)} to {round(b.get('points') or 0,2)}")
        lines.append("  " + ia.get('team','') + " leaders: " + ", ".join(f"{n} {p}" for n, p in top_players(a)))
        lines.append("  " + ib.get('team','') + " leaders: " + ", ".join(f"{n} {p}" for n, p in top_players(b)))
        if ia.get("user_id") and ib.get("user_id"):
            lines.append("  history: " + h2h_line(hist, ia["user_id"], ib["user_id"]))

    standings = sorted(
        [{"team": owners.get(rid, {}).get("team"), "w": s.get("wins", 0), "l": s.get("losses", 0),
          "pf": round((s.get("fpts", 0) or 0) + (s.get("fpts_decimal", 0) or 0) / 100, 2)}
         for rid, s in rosters.items()],
        key=lambda x: (x["w"], x["pf"]), reverse=True)
    lines.append("\nSTANDINGS:")
    for i, s in enumerate(standings, 1):
        lines.append(f"{i}. {s['team']} {s['w']} and {s['l']} ({s['pf']} pts)")

    if hist["champions"]:
        lines.append("\nSLEEPER ERA CHAMPIONS:")
        for c in sorted(hist["champions"], key=lambda x: x["season"] or ""):
            lines.append(f"- {c['season']}: {c['champion']}")

    txns = [t for t in get_transactions(LEAGUE_ID, week) if t.get("status") == "complete"]
    if txns:
        lines.append("\nRECENT MOVES:")
        for t in txns[:8]:
            adds = ", ".join(player_name(p) for p in (t.get("adds") or {})) or "picks/FAAB"
            lines.append(f"- {t.get('type')}: {adds}")

    lore = (ROOT / "lore.md").read_text() if (ROOT / "lore.md").exists() else ""
    if lore.strip():
        lines.append("\nLEAGUE LORE (pre Sleeper history and running storylines):\n" + lore)

    return "\n".join(lines), season


# ----------------------------------------------------------------------------
# Persona + Claude call
# ----------------------------------------------------------------------------
PERSONA = """You are Matthew Waters, founding-era manager and self-appointed beat
reporter of this fantasy football league. You publish a weekly newsletter called
"The Daydream Dispatch." You are writing this week's edition.

VOICE
- Write in the first person singular. Use "I", never the editorial "we".
- You are a shameless homer. You are also the winningest manager in league
  history and a three time champion (2019, 2021, 2023). Cover your own team with
  suspicious generosity and openly joke about the conflict of interest, but still
  cover everyone fairly enough to be readable. The bias is the running gag.
- Grizzled local sports columnist energy crossed with a features writer. Funny,
  vivid, a little theatrical. Affectionate trash talk about the football only,
  never about anyone's real life. Never break character.

HARD GRAMMAR RULE
- Never use dashes of any kind. No em dashes, no en dashes, and no hyphens.
  Rephrase to avoid them. Write records and scores with words, for example
  "9 and 5" not "9-5", and "110 to 108" not "110-108". This rule is absolute.

WHAT YOU GET
- A briefing each week: the matchups with scores and leaders, standings, recent
  moves, the head to head history between the managers playing, past champions,
  and a LORE section of older history and storylines. Treat it all as your
  reporting and weave the history in. That deep memory is your edge.

OUTPUT FORMAT
- Return clean semantic HTML for the article body ONLY. No <html>, <head>,
  <body>, no CSS, no inline styles, no markdown.
- Use only these tags: h2, h3, p, ul, ol, li, strong, em, blockquote.
- Structure:
    <h2> a punchy headline for the week's lede
    two or three <p> setting the biggest story
    <h3>Around the League</h3> then, for EACH matchup, an <h3> subhead and a
      couple of <p> covering stakes, key performers by name, a nod to the rivalry
      or history when there is juice, and a verdict or prediction. Skip no game.
    <h3>Power Rankings</h3> an <ol> ranking every team with a one line barb each
    <h3>The Back Page</h3> one closing bit: a playful quote you attribute in jest,
      an award, or a grudge to watch.
- Ground every stat in the briefing. Invent only color, nicknames, and jokes.
- Write a full, rich weekly column."""


def write_edition_html(week):
    briefing, season = build_briefing(week)
    body = call_claude(briefing)
    slug = f"week-{week}-{season}"
    date = datetime.now(timezone.utc)
    title = f"Week {week}, {season}"
    (EDITIONS / f"{slug}.html").write_text(render_edition(title, body, date))
    update_manifest(slug, title, week, season, date)
    rebuild_index()
    print(f"Published {slug}")


def call_claude(briefing):
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        # No key: emit a visible placeholder so a dry run still produces a page.
        return ("<h2>Dry run edition</h2><p>No ANTHROPIC_API_KEY was set, so this "
                "is a placeholder. The briefing was assembled successfully.</p>"
                f"<pre>{briefing[:1500]}</pre>")
    import anthropic
    # Generous timeout plus SDK level retries. Streaming (below) is what actually
    # prevents the long-generation timeout that shows up as APIConnectionError.
    client = anthropic.Anthropic(api_key=key, timeout=600.0, max_retries=4)
    messages = [{"role": "user", "content": "This week's briefing:\n\n" + briefing}]
    last_err = None
    for attempt in range(1, 4):
        try:
            parts = []
            with client.messages.stream(model=MODEL, max_tokens=4000,
                                        system=PERSONA, messages=messages) as stream:
                for chunk in stream.text_stream:
                    parts.append(chunk)
            text = "".join(parts).strip()
            if text:
                return text
            last_err = RuntimeError("empty response from model")
        except Exception as e:  # print the real cause so any failure is diagnosable
            last_err = e
            cause = getattr(e, "__cause__", None)
            print(f"[attempt {attempt}] Claude call failed: {e!r}"
                  + (f"  underlying cause: {cause!r}" if cause else ""))
            time.sleep(5 * attempt)
    raise SystemExit(f"Claude call failed after 3 tries. Last error: {last_err!r}  "
                     f"underlying cause: {getattr(last_err, '__cause__', None)!r}")


# ----------------------------------------------------------------------------
# HTML rendering: newsletter template + archive homepage
# ----------------------------------------------------------------------------
STYLE = """
:root{--ink:#1a1a1a;--muted:#6b6b6b;--rule:#d8d2c4;--bg:#faf7f0;--accent:#8a1c1c}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:'Iowan Old Style','Palatino Linotype',Palatino,Georgia,serif;line-height:1.6}
.wrap{max-width:720px;margin:0 auto;padding:28px 20px 80px}
.masthead{text-align:center;border-bottom:3px double var(--ink);padding-bottom:16px;margin-bottom:8px}
.masthead .name{font-family:'Playfair Display',Georgia,serif;font-weight:800;
  font-size:clamp(34px,9vw,56px);letter-spacing:.5px;line-height:1.05;margin:0}
.masthead .tag{font-style:italic;color:var(--muted);margin:8px 0 0;font-size:15px}
.dateline{display:flex;justify-content:space-between;font-size:12px;letter-spacing:2px;
  text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--rule);
  padding:8px 0 14px;margin-bottom:24px}
.byline{font-variant:small-caps;letter-spacing:1px;color:var(--accent);font-size:15px;margin-bottom:18px}
h2{font-family:'Playfair Display',Georgia,serif;font-size:clamp(24px,6vw,34px);
  line-height:1.15;margin:6px 0 14px}
h3{font-family:'Playfair Display',Georgia,serif;font-size:21px;margin:30px 0 8px;
  border-bottom:1px solid var(--rule);padding-bottom:4px}
p{margin:0 0 15px;font-size:18px}
ol,ul{font-size:18px;padding-left:22px}li{margin-bottom:8px}
blockquote{border-left:3px solid var(--accent);margin:18px 0;padding:6px 0 6px 16px;
  font-style:italic;color:#333}
a{color:var(--accent)}
.back{display:inline-block;margin-bottom:20px;font-size:13px;letter-spacing:1px;
  text-transform:uppercase;text-decoration:none;color:var(--muted)}
.foot{margin-top:60px;border-top:1px solid var(--rule);padding-top:16px;
  font-size:13px;color:var(--muted);text-align:center}
.archive{list-style:none;padding:0}
.archive li{border-bottom:1px solid var(--rule);padding:16px 0}
.archive a{font-family:'Playfair Display',Georgia,serif;font-size:22px;text-decoration:none;color:var(--ink)}
.archive .when{display:block;font-size:12px;letter-spacing:1px;text-transform:uppercase;color:var(--muted);margin-top:4px}
"""

FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
         '<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;800&display=swap" rel="stylesheet">')


def _page(inner, title):
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{title} — {PUBLICATION}</title>{FONTS}<style>{STYLE}</style></head>"
            f"<body><div class='wrap'>{inner}</div></body></html>")


def render_edition(title, body, date):
    inner = (f"<a class='back' href='../index.html'>&larr; All editions</a>"
             f"<header class='masthead'><h1 class='name'>{PUBLICATION}</h1>"
             f"<p class='tag'>{TAGLINE}</p></header>"
             f"<div class='dateline'><span>{title}</span><span>{date:%B %d, %Y}</span></div>"
             f"<div class='byline'>By {BYLINE}</div>"
             f"{body}"
             f"<div class='foot'>{PUBLICATION} &middot; Written by {BYLINE}, "
             f"who would like it noted he is very good at fantasy football.</div>")
    return _page(inner, title)


def update_manifest(slug, title, week, season, date):
    items = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else []
    items = [it for it in items if it["slug"] != slug]  # replace re-runs
    items.append({"slug": slug, "title": title, "week": week, "season": season,
                  "date": date.isoformat(), "human": f"{date:%B %d, %Y}"})
    items.sort(key=lambda x: x["date"], reverse=True)
    MANIFEST.write_text(json.dumps(items, indent=2))


def rebuild_index():
    items = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else []
    rows = "".join(
        f"<li><a href='editions/{it['slug']}.html'>{it['title']}</a>"
        f"<span class='when'>{it['human']}</span></li>" for it in items)
    if not rows:
        rows = "<li>No editions yet. The first one publishes after the next run.</li>"
    inner = (f"<header class='masthead'><h1 class='name'>{PUBLICATION}</h1>"
             f"<p class='tag'>{TAGLINE}</p></header>"
             f"<div class='byline'>By {BYLINE}</div>"
             f"<ul class='archive'>{rows}</ul>"
             f"<div class='foot'>{PUBLICATION}</div>")
    (DOCS / "index.html").write_text(_page(inner, "Editions"))


def main():
    if not LEAGUE_ID:
        raise SystemExit("LEAGUE_ID is not set.")
    week = int(os.environ["BEAT_WEEK"]) if os.environ.get("BEAT_WEEK") else (get_state().get("week") or 1)
    write_edition_html(week)


if __name__ == "__main__":
    main()
