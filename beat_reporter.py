"""
The Dingers and Stingers Report -- a fantasy football beat reporter, by Matthew Waters.

Runs in GitHub Actions. Pulls the league live from Sleeper, rebuilds the all-time
history and rivalries, reads lore.md for the pre-Sleeper years, has Claude write
the edition in first person as Matthew Waters, and publishes a styled newsletter
page plus an updating archive homepage into docs/ (served by Pages).

Column types (choose when you run it, or let auto decide):
  auto       preseason before games start, weekly once real games are played
  weekly     recap of this week's matchups
  preseason  historical power rankings with reasoning (records, titles, drafts)

Secrets it expects (set in the repo, never in code):
  ANTHROPIC_API_KEY       your Claude API key (sk-ant-...)
  ANTHROPIC_WORKSPACE_ID  your workspace id (wrksp_...) if your key needs one
  LEAGUE_ID               your Sleeper league id
Optional:
  BEAT_MODEL    model id, defaults to claude-sonnet-5
  BEAT_WEEK     force a specific week
  BEAT_COLUMN   auto | weekly | preseason  (defaults to auto)
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

# ---- Masthead. Rename PUBLICATION to whatever you like (your league name works). ----
PUBLICATION = "The Dingers and Stingers Report"
BYLINE = "Matthew Waters"
TAGLINE = "Fantasy football's least objective newsletter"

# Real names keyed by Sleeper username (lowercased, no @). Team names change every
# season, but usernames are stable, so this ties each year's team back to the person.
# To add or fix someone, edit one line here.
REAL_NAMES = {
    "drewww": "Drew",
    "maxhayes97": "Max",
    "mattrobertson": "Matt R",
    "aherrmann95": "Austin",
    "ericampr11": "Erica",
    "johnsagherian12": "John S",
    "timmybobbobam": "Tim",
    "fullback4fulham": "Elijah",
    "johnvaughan": "John V",
    "dallasdickinso": "Dallas",
    "koopercupp": "Matt W",
    "drbarrycorey": "Ryan",
}


def person_for(display_name):
    return REAL_NAMES.get((display_name or "").strip().lstrip("@").lower())

LEAGUE_ID = "".join((os.environ.get("LEAGUE_ID") or "").split())
MODEL = os.environ.get("BEAT_MODEL", "claude-sonnet-5")

# ----------------------------------------------------------------------------
# Sleeper read-only API (no key needed)
# ----------------------------------------------------------------------------
BASE = "https://api.sleeper.app/v1"
_session = requests.Session()
_session.headers.update({"User-Agent": "dingers-stingers/1.0"})


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
# History: walk Sleeper seasons, rebuild H2H, all-time records, champions
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
        dn = u.get("display_name", "")
        person = person_for(dn)  # real name if we know this username, else None
        team = meta.get("team_name") or dn or f"Team {r['roster_id']}"
        label = f"{team} ({person})" if person else team
        out[r["roster_id"]] = {
            "user_id": r.get("owner_id"),
            "name": person or dn or f"Team {r['roster_id']}",
            "team": label,
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
    return {"names": names, "h2h": dict(h2h), "alltime": dict(alltime),
            "champions": champs, "chain": chain}


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


def draft_history(chain):
    """Early-round picks per manager across Sleeper seasons (for draft tendencies).
    Returns {team_name: ["2024 R1 Player (RB)", ...]}. Defensive: {} on any error."""
    try:
        out = defaultdict(list)
        for lg in reversed(chain):
            season = lg.get("season")
            owners = owner_map(lg["league_id"])
            drafts = _get(f"league/{lg['league_id']}/drafts") or []
            if not drafts:
                continue
            draft_id = drafts[0].get("draft_id")
            if not draft_id:
                continue
            for p in (_get(f"draft/{draft_id}/picks") or []):
                rnd = p.get("round")
                if not rnd or rnd > 2:
                    continue
                who = owners.get(p.get("roster_id"), {}).get("team")
                if not who:
                    continue
                meta = p.get("metadata") or {}
                name = f"{meta.get('first_name','')} {meta.get('last_name','')}".strip() or "a pick"
                pos = meta.get("position", "")
                out[who].append(f"{season} R{rnd} {name} ({pos})" if pos else f"{season} R{rnd} {name}")
        return dict(out)
    except Exception as e:
        print(f"draft_history skipped: {e!r}")
        return {}


def player_position(pid):
    if pid is None:
        return ""
    if isinstance(pid, str) and pid.isalpha():
        return "DEF"
    global _players
    if _players is None:
        _players = _load_players()
    return (_players.get(str(pid)) or {}).get("position", "") or ""


def transaction_history(chain):
    """In-season habits per manager across Sleeper seasons: waiver/FA adds, trades
    they took part in, FAAB spent, and the positions they chase. Returns
    {team_name: "one line summary"}. Defensive: {} on any error."""
    try:
        agg = defaultdict(lambda: {"adds": 0, "trades": 0, "faab": 0,
                                   "pos": defaultdict(int)})
        for lg in reversed(chain):
            owners = owner_map(lg["league_id"])
            for wk in range(1, 19):
                for t in (_get(f"league/{lg['league_id']}/transactions/{wk}") or []):
                    if t.get("status") != "complete":
                        continue
                    if t.get("type") == "trade":
                        for rid in (t.get("roster_ids") or []):
                            who = owners.get(rid, {}).get("team")
                            if who:
                                agg[who]["trades"] += 1
                        continue
                    bid = ((t.get("settings") or {}).get("waiver_bid")) or 0
                    for pid, rid in (t.get("adds") or {}).items():
                        who = owners.get(rid, {}).get("team")
                        if not who:
                            continue
                        agg[who]["adds"] += 1
                        agg[who]["faab"] += bid if t.get("type") == "waiver" else 0
                        pos = player_position(pid)
                        if pos:
                            agg[who]["pos"][pos] += 1
        out = {}
        for who, d in agg.items():
            top = ", ".join(p for p, _ in sorted(d["pos"].items(),
                                                 key=lambda x: x[1], reverse=True)[:2])
            s = f"{d['adds']} waiver or FA adds, in {d['trades']} trades, {d['faab']} FAAB spent"
            out[who] = s + (f", chases {top}" if top else "")
        return out
    except Exception as e:
        print(f"transaction_history skipped: {e!r}")
        return {}


def alltime_block(hist):
    titles = defaultdict(int)
    for c in hist["champions"]:
        if c.get("champion"):
            titles[c["champion"]] += 1
    rows = sorted(hist["alltime"].values(),
                  key=lambda r: (r["w"] / (r["w"] + r["l"]) if (r["w"] + r["l"]) else 0,
                                 r["w"]), reverse=True)
    lines = []
    for r in rows:
        gp = r["w"] + r["l"]
        pct = (r["w"] / gp) if gp else 0
        t = titles.get(r["name"], 0)
        lines.append(f"- {r['name']}: {r['w']} and {r['l']} ({pct:.3f}), "
                     f"{t} Sleeper era title(s)")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Briefings (plain text handed to Claude), one per column type
# ----------------------------------------------------------------------------
def top_players(m, n=3):
    st = m.get("starters") or []
    pts = m.get("starters_points") or []
    rows = [(player_name(p), round(pts[i] if i < len(pts) else 0, 2)) for i, p in enumerate(st)]
    rows.sort(key=lambda x: x[1], reverse=True)
    return rows[:n]


def _read_lore():
    return (ROOT / "lore.md").read_text() if (ROOT / "lore.md").exists() else ""


def build_weekly(league, season, week, hist):
    owners = owner_map(LEAGUE_ID)
    rosters = {r["roster_id"]: (r.get("settings") or {}) for r in get_rosters(LEAGUE_ID)}
    ms = get_matchups(LEAGUE_ID, week)
    pairs = defaultdict(list)
    for m in ms:
        if m.get("matchup_id") is not None:
            pairs[m["matchup_id"]].append(m)

    lines = [f"LEAGUE: {league.get('name')}  SEASON: {season}  WEEK: {week}  EDITION: WEEKLY RECAP",
             "", "MATCHUPS THIS WEEK:"]
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

    lore = _read_lore()
    if lore.strip():
        lines.append("\nLEAGUE LORE (pre Sleeper history and running storylines):\n" + lore)
    return "\n".join(lines), f"Week {week}, {season}", f"week-{week}-{season}"


def build_preseason(league, season, hist):
    lines = [f"LEAGUE: {league.get('name')}  SEASON: {season}  "
             f"EDITION: PRESEASON HISTORICAL POWER RANKINGS", ""]
    lines.append("SLEEPER ERA RECORDS (2022 to now, from live data), best win rate first:")
    lines.append(alltime_block(hist))
    if hist["champions"]:
        lines.append("\nSLEEPER ERA CHAMPIONS:")
        for c in sorted(hist["champions"], key=lambda x: x["season"] or ""):
            lines.append(f"- {c['season']}: {c['champion']}")
    dh = draft_history(hist["chain"])
    if dh:
        lines.append("\nDRAFT TENDENCIES (first and second round picks by manager, Sleeper era):")
        for who, picks in dh.items():
            lines.append(f"- {who}: " + "; ".join(picks))
    th = transaction_history(hist["chain"])
    if th:
        lines.append("\nIN SEASON TRANSACTION TENDENCIES (Sleeper era):")
        for who, summary in th.items():
            lines.append(f"- {who}: {summary}")
    lore = _read_lore()
    if lore.strip():
        lines.append("\nLEAGUE LORE (authoritative pre 2022 history: all-time win rates, "
                     "champions, manager dossiers, records):\n" + lore)
    lines.append("\nNOTE ON COUNTING: the LORE all-time win rates already span every season "
                 "and are the definitive all-time record. Use the Sleeper era section above "
                 "only for recent form and to confirm recent champions. Do not add the two "
                 "together, and do not double count titles.")
    return "\n".join(lines), f"Preseason Power Rankings, {season}", f"preseason-{season}"


# ----------------------------------------------------------------------------
# Personas
# ----------------------------------------------------------------------------
COMMON = """You are Matthew Waters, founding-era manager and self-appointed beat
reporter of this fantasy football league. You are writing an edition of the
league newsletter.

VOICE
- Write in the first person singular. Use "I", never the editorial "we".
- You are a shameless homer. You are also the winningest manager in league
  history and a three time champion (2019, 2021, 2023). Cover your own team with
  suspicious generosity and openly joke about the conflict of interest, but still
  cover everyone fairly enough to be readable. The bias is the running gag.
- Grizzled local sports columnist crossed with a features writer. Funny, vivid,
  a little theatrical. Affectionate trash talk about the football only, never
  about anyone's real life. Never break character.

NAMING
- In the briefing, each team is written as Team Name (Person). Lead with the team
  name, and bring in the person's real name when it sharpens a historical callback
  or keeps things clear. Your own team is the one marked (Matt W); that is you.

HARD GRAMMAR RULE
- Never use dashes of any kind. No em dashes, no en dashes, and no hyphens.
  Rephrase to avoid them. Write records and scores with words, for example
  "9 and 5" not "9-5", and "110 to 108" not "110-108". This rule is absolute.

OUTPUT FORMAT
- Return clean semantic HTML for the article body ONLY. No <html>, <head>,
  <body>, no CSS, no inline styles, no markdown.
- Use only these tags: h2, h3, p, ul, ol, li, strong, em, blockquote.
- Ground every stat in the briefing. Invent only color, nicknames, and jokes.
  Any quote you attribute to a manager must be obviously playful."""

WEEKLY = """THIS EDITION: the weekly recap. Structure it as:
  <h2> a punchy headline for the week's lede
  two or three <p> setting the biggest story
  <h3>Around the League</h3> then, for EACH matchup, an <h3> subhead and a couple
    of <p> covering the stakes, the key performers by name, a nod to the rivalry
    or head to head history when there is juice, and a verdict or prediction.
    Skip no game.
  <h3>Power Rankings</h3> an <ol> ranking every team with a one line barb each
  <h3>The Back Page</h3> one closing bit: a playful quote, an award, or a grudge."""

PRESEASON = """THIS EDITION: the preseason historical power rankings. There are no
games yet, so this is a state of the league piece built entirely on history.
Structure it as:
  <h2> a punchy headline for the season ahead
  two or three <p> setting the stage: dynasties, droughts, who enters as the team
    to beat and who is out for redemption
  <h3>The Rankings</h3> an <ol> ranking EVERY manager from first to worst. For each
    one, write a short paragraph (inside the <li>) explaining WHY they are ranked
    there, citing specific evidence from the briefing: all-time win rate, titles,
    signature seasons, their DRAFT TENDENCIES (the kinds of players they reach for
    early), and their IN SEASON HABITS from the transaction data (how active they
    are on waivers, how freely they trade, how much FAAB they burn, and which
    positions they chase). Be concrete and use the real numbers.
  <h3>The Field</h3> a paragraph or two on sleepers, teams trending up or down,
    and a bold prediction for the season.
  <h3>The Back Page</h3> rank yourself honestly-ish, then lobby shamelessly for
    your own case anyway, and take one playful shot at your top rival.
Base the ranking mainly on the all-time win rates and championships, using recent
Sleeper era form, draft tendencies, and in season transaction habits as supporting
reasoning."""

PERSONAS = {"weekly": WEEKLY, "preseason": PRESEASON}


def call_claude(briefing, persona):
    # Remove ALL whitespace, not just the ends, so a key pasted as two lines still works.
    key = "".join((os.environ.get("ANTHROPIC_API_KEY") or "").split())
    workspace = "".join((os.environ.get("ANTHROPIC_WORKSPACE_ID") or "").split())
    if not key:
        return ("<h2>Dry run edition</h2><p>No ANTHROPIC_API_KEY was set, so this is a "
                "placeholder. The briefing was assembled successfully.</p>"
                f"<pre>{briefing[:1500]}</pre>")
    import anthropic
    kwargs = {"api_key": key, "timeout": 600.0, "max_retries": 4}
    if workspace:  # identity-linked keys must name the workspace they act in
        kwargs["default_headers"] = {"anthropic-workspace-id": workspace}
    client = anthropic.Anthropic(**kwargs)
    messages = [{"role": "user", "content": "Here is this edition's briefing:\n\n" + briefing}]
    print(f"Prompt is about {len(persona) + len(briefing)} characters")

    def _text(msg):
        return "".join(b.text for b in msg.content
                       if getattr(b, "type", None) == "text").strip()

    last_err = None
    for attempt in range(1, 4):
        try:
            try:
                # This is a writing task, so turn extended thinking OFF. Left on, the
                # model spends the whole token budget thinking and returns no text
                # (stop_reason=max_tokens, blocks=['thinking']), which is what failed.
                msg = client.messages.create(model=MODEL, max_tokens=8000, system=persona,
                                             messages=messages, thinking={"type": "disabled"})
            except anthropic.BadRequestError as be:
                # Some models will not let you disable thinking. Give a big budget so it
                # can finish thinking AND still write the column.
                print(f"[attempt {attempt}] could not disable thinking ({be!r}); using a large budget")
                msg = client.messages.create(model=MODEL, max_tokens=32000, system=persona,
                                             messages=messages)
            text = _text(msg)
            if text:
                return text
            blocks = [getattr(b, "type", "?") for b in msg.content]
            last_err = RuntimeError(f"empty response (stop_reason={msg.stop_reason}, blocks={blocks})")
            print(f"[attempt {attempt}] {last_err}")
        except Exception as e:
            last_err = e
            cause = getattr(e, "__cause__", None)
            print(f"[attempt {attempt}] Claude call failed: {e!r}"
                  + (f"  underlying cause: {cause!r}" if cause else ""))
        time.sleep(4 * attempt)
    raise SystemExit(f"Claude call failed after 3 tries. Last error: {last_err!r}")


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
ol,ul{font-size:18px;padding-left:22px}li{margin-bottom:12px}
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
            f"<title>{title} | {PUBLICATION}</title>{FONTS}<style>{STYLE}</style></head>"
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


def update_manifest(slug, title, date):
    items = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else []
    items = [it for it in items if it["slug"] != slug]  # replace re-runs of same edition
    items.append({"slug": slug, "title": title,
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


# ----------------------------------------------------------------------------
# Schedule builder (rivalry-optimized round robin)
# ----------------------------------------------------------------------------
import difflib

REG_WEEKS = int(os.environ.get("BEAT_SEASON_WEEKS", "14"))
THANKS_WEEK = int(os.environ.get("BEAT_THANKSGIVING_WEEK", "12"))

# Hard-locked rivalry matchups, by person name (must match REAL_NAMES values).
# Leave a list empty to let the optimizer choose that week instead. Each list must
# be a perfect matching: every manager appears exactly once.
FORCED_WEEK1 = [("Matt W", "Matt R"), ("John S", "John V"), ("Max", "Tim"),
                ("Erica", "Dallas"), ("Drew", "Elijah"), ("Austin", "Ryan")]
FORCED_THANKS = [("John V", "Matt W"), ("Matt R", "Max"), ("Tim", "Erica"),
                 ("Dallas", "Drew"), ("Elijah", "Austin"), ("Ryan", "John S")]

SCHEDULE_REVEAL = """THIS EDITION: the schedule reveal. A full week by week schedule
table gets printed right after your words, so do NOT list the whole schedule and do
NOT use any tables or lists. Write only:
  <h2> a punchy headline announcing the schedule
  two or three <p> hyping the season, then calling out the Week 1 rivalry games and
    the Thanksgiving rivalry games by name, with your usual bias and needling.
Keep it to those paragraphs. The table follows on its own."""


def _all_matchings(items):
    if not items:
        yield []
        return
    first, rest = items[0], items[1:]
    for i in range(len(rest)):
        for m in _all_matchings(rest[:i] + rest[i + 1:]):
            yield [(first, rest[i])] + m


def rivalry_score(ua, ub, hist, person_of):
    key = tuple(sorted([str(ua), str(ub)]))
    rec = hist["h2h"].get(tuple(sorted([ua, ub])))
    games = (rec["a"] + rec["b"]) if rec else 0
    closeness = (1 - abs(rec["a"] - rec["b"]) / games) if (rec and games) else 0
    h2h = games * (0.4 + 0.6 * closeness)
    sim = difflib.SequenceMatcher(None, (person_of.get(ua) or "").lower(),
                                  (person_of.get(ub) or "").lower()).ratio()
    return h2h + sim * 8


def rivalry_reason(ua, ub, hist, person_of):
    rec = hist["h2h"].get(tuple(sorted([ua, ub])))
    na, nb = person_of.get(ua, "?"), person_of.get(ub, "?")
    sim = difflib.SequenceMatcher(None, na.lower(), nb.lower()).ratio()
    if sim > 0.55:
        return "name twins, and this league is not big enough for both"
    if rec:
        aw, bw, games = rec["a"], rec["b"], rec["a"] + rec["b"]
        if games >= 3 and abs(aw - bw) <= 1:
            return f"a dead heat, {aw} to {bw} across {games} meetings"
        if games:
            hi, lo = max(aw, bw), min(aw, bw)
            lead = na if aw >= bw else nb
            return f"a revenge game, {lead} owns the series {hi} to {lo}"
    return "no history yet, so it is time to start some"


def top_matchings(managers, hist, person_of, k):
    scored = sorted(
        ([sum(rivalry_score(a, b, hist, person_of) for a, b in m), m]
         for m in _all_matchings(managers)),
        key=lambda x: x[0], reverse=True)
    return [m for _, m in scored[:k]]


def _arrange_for_first(matching, n):
    pos = [None] * n
    for p, (a, b) in enumerate(matching):
        pos[p] = a
        pos[n - 1 - p] = b
    return pos


def round_robin(positions):
    n = len(positions)
    fixed, rot = positions[0], positions[1:]
    rounds = []
    for _ in range(n - 1):
        arr = [fixed] + rot
        rounds.append([(arr[i], arr[n - 1 - i]) for i in range(n // 2)])
        rot = [rot[-1]] + rot[:-1]
    return rounds


def _resolve_forced(pairs, person_of):
    if not pairs:
        return None
    name_to_uid = {(nm or "").strip().lower(): uid
                   for uid, nm in person_of.items() if uid is not None}
    resolved = []
    for a, b in pairs:
        ua, ub = name_to_uid.get(a.strip().lower()), name_to_uid.get(b.strip().lower())
        if ua is None or ub is None:
            print(f"forced matchup name not found ({a} vs {b}); using the optimizer instead")
            return None
        resolved.append((ua, ub))
    real = sorted(u for u in person_of if u is not None)
    if sorted(x for pr in resolved for x in pr) != real:
        print("forced matchups do not cover every manager once; using the optimizer")
        return None
    return resolved


def build_schedule(hist, owners):
    managers = [i["user_id"] for i in owners.values() if i["user_id"]]
    person_of = {i["user_id"]: i["name"] for i in owners.values() if i["user_id"]}
    if len(managers) % 2:                       # odd count: add a bye
        managers.append(None)
        person_of[None] = "BYE"

    tops = top_matchings(managers, hist, person_of, 4)
    week1 = _resolve_forced(FORCED_WEEK1, person_of) or tops[0]
    rounds = round_robin(_arrange_for_first(week1, len(managers)))

    weeks = {1: week1}
    wk = 2
    for rnd in rounds[1:]:
        weeks[wk] = rnd; wk += 1
    rr_last = wk - 1                             # last full round robin week

    bonus = [m for m in tops if m != week1] or [week1]
    for j, w in enumerate(range(rr_last + 1, REG_WEEKS + 1)):
        weeks[w] = bonus[j % len(bonus)]

    thanks = _resolve_forced(FORCED_THANKS, person_of)
    if 1 <= THANKS_WEEK <= REG_WEEKS and THANKS_WEEK != 1:
        if thanks:
            weeks[THANKS_WEEK] = thanks
        elif THANKS_WEEK > rr_last:
            weeks[THANKS_WEEK] = tops[1] if len(tops) > 1 else week1
        else:
            best_w = max(range(2, rr_last + 1),
                         key=lambda x: sum(rivalry_score(a, b, hist, person_of) for a, b in weeks[x]))
            weeks[THANKS_WEEK], weeks[best_w] = weeks[best_w], weeks[THANKS_WEEK]

    out = []
    for w in range(1, REG_WEEKS + 1):
        rivalry = (w == 1 or w == THANKS_WEEK)
        games = [(person_of.get(a, "?"), person_of.get(b, "?"),
                  rivalry_reason(a, b, hist, person_of) if rivalry else None)
                 for a, b in weeks[w]]
        out.append({"week": w, "rivalry": rivalry, "games": games})
    return out


def _schedule_briefing(schedule, season):
    lines = [f"SEASON: {season}. Everyone plays everyone at least once across the round "
             f"robin, with rivalry weeks in Week 1 and on Thanksgiving.", "", "WEEK 1 RIVALRY GAMES:"]
    for a, b, r in next(w for w in schedule if w["week"] == 1)["games"]:
        lines.append(f"- {a} vs {b} ({r})")
    tw = next((w for w in schedule if w["rivalry"] and w["week"] != 1), None)
    if tw:
        lines.append(f"\nTHANKSGIVING (Week {tw['week']}) RIVALRY GAMES:")
        for a, b, r in tw["games"]:
            lines.append(f"- {a} vs {b} ({r})")
    return "\n".join(lines)


def _schedule_table(schedule):
    parts = []
    for wk in schedule:
        tag = " &middot; Rivalry Week" if wk["rivalry"] else ""
        parts.append(f"<h3>Week {wk['week']}{tag}</h3><ul>")
        for a, b, reason in wk["games"]:
            li = f"<li>{a} vs {b}"
            if reason:
                li += f" <em>({reason})</em>"
            parts.append(li + "</li>")
        parts.append("</ul>")
    return "".join(parts)


def publish_schedule(league, season, hist, owners):
    schedule = build_schedule(hist, owners)
    try:
        intro = call_claude(_schedule_briefing(schedule, season), COMMON + "\n\n" + SCHEDULE_REVEAL)
    except SystemExit as e:
        print(f"schedule intro skipped: {e}")
        intro = ("<h2>The Schedule Is Set</h2><p>Everyone plays everyone at least once, "
                 "with rivalry games to open the season and again on Thanksgiving. The "
                 "full slate is below.</p>")
    body = intro + _schedule_table(schedule)
    date = datetime.now(timezone.utc)
    title, slug = f"{season} Schedule", f"schedule-{season}"
    (EDITIONS / f"{slug}.html").write_text(render_edition(title, body, date))
    update_manifest(slug, title, date)
    rebuild_index()
    print(f"Published {slug}")


# ----------------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------------
def resolve_column_type(week):
    ct = (os.environ.get("BEAT_COLUMN") or "auto").strip().lower()
    if ct in ("weekly", "preseason", "schedule"):
        return ct
    ms = get_matchups(LEAGUE_ID, week)  # auto: any real scores yet?
    return "weekly" if any((m.get("points") or 0) > 0 for m in ms) else "preseason"


def main():
    if not LEAGUE_ID:
        raise SystemExit("LEAGUE_ID is not set.")
    week = int(os.environ["BEAT_WEEK"]) if os.environ.get("BEAT_WEEK") else (get_state().get("week") or 1)
    column_type = resolve_column_type(week)
    print(f"Building '{column_type}' edition for league {LEAGUE_ID}, week {week}")

    league = get_league(LEAGUE_ID)
    if not league:
        raise SystemExit(f"Could not load league {LEAGUE_ID}. Check the LEAGUE_ID secret.")
    season = league.get("season")
    hist = build_history(LEAGUE_ID)

    if column_type == "schedule":
        publish_schedule(league, season, hist, owner_map(LEAGUE_ID))
        return
    if column_type == "preseason":
        briefing, title, slug = build_preseason(league, season, hist)
    else:
        briefing, title, slug = build_weekly(league, season, week, hist)

    body = call_claude(briefing, COMMON + "\n\n" + PERSONAS[column_type])
    date = datetime.now(timezone.utc)
    (EDITIONS / f"{slug}.html").write_text(render_edition(title, body, date))
    update_manifest(slug, title, date)
    rebuild_index()
    print(f"Published {slug}")


if __name__ == "__main__":
    main()
