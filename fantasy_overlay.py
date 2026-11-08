"""
HUDdle - live ESPN fantasy football scores on top of anything playing on your Android TV.
Draws the overlay with PiPup (maintained fork: https://github.com/mhoogenbosch/PiPup).

Shows a scoreboard bar at the top center of the TV: the matchup score, margin and
win % in the middle, with one player matchup (QB vs QB, RB vs RB...) on either side
of it that rotates every few seconds.

How it works:
  - This script runs a small web server on your PC (port WEB_PORT).
  - PiPup on the TV shows that page once, as a transparent panel.
  - The page refreshes its own numbers every few seconds (no flicker),
    and the script pulls fresh scores from ESPN every FETCH_SECONDS.

Setup:
    python -m pip install espn-api requests
    python fantasy_overlay.py
First run: if Windows Firewall asks, click Allow (Private networks).
"""

import json
import math
import socket
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import zip_longest

import requests
from espn_api.football import League
from espn_api.football.constant import PRO_TEAM_MAP

# ======================= CONFIG =======================
TV_IP = "192.168.1.50"       # your Android TV box IP (example)
LEAGUE_ID = 123456           # from the URL on fantasy.espn.com: ...?leagueId=123456
YEAR = 2026
MY_TEAM = "Team Name"        # all or part of your team name (case-insensitive)

# Private leagues: paste in quotes. Public league: leave as None.
ESPN_S2 = None
SWID = None                  # keep the { } braces

FETCH_SECONDS = 30           # how often to pull fresh scores from ESPN
CYCLE_SECONDS = 10           # how long each player matchup stays up before rotating
PAGE_REFRESH_SECONDS = 5     # how often the panel picks up new scores from this script
ONLY_DURING_GAMES = True     # False = keep the panel up even when no one is playing

POSITION = 5                 # 5 = top center (0-8 grid: 0 top right, 1 top left, 2 bottom right,
                             #   3 bottom left, 4 center, 5 top center, ...)
PANEL_WIDTH = 1240           # panel size on the TV; text scales to fit. Too narrow for the player
PANEL_HEIGHT = 96            #   wings and they drop off; the player matchup moves under the score
PANEL_OPACITY = 0.85         # player wings' background darkness (0-1)
MY_COLOR = "2f80ed"          # accent colors (hex) for your team and your opponent
OPP_COLOR = "f2994a"
WIN_SPREAD = 0.7             # win % volatility: higher = less confident estimate

WEB_PORT = 8765
PC_IP = None                 # None = auto-detect this PC's LAN address
# ======================================================

# Keep your real values (especially ESPN_S2 / SWID) out of git:
# put any of the settings above in a file named overlay_config.py next to
# this script. It overrides the defaults and is listed in .gitignore.
try:
    from overlay_config import *  # noqa: F401,F403
except ImportError:
    pass

CARD_ID = "fantasy"
BENCH = {"BE", "IR"}
GAME_LENGTH = timedelta(hours=3, minutes=30)
SLOT_ORDER = ["QB", "RB", "WR", "TE", "RB/WR/TE", "WR/TE", "RB/WR", "OP",
              "D/ST", "K", "DL", "LB", "DB", "DP", "HC", "P"]
SLOT_LABEL = {"RB/WR/TE": "FLEX", "WR/TE": "W/T", "RB/WR": "W/R", "OP": "SFLX"}
SAFETY_SECONDS = FETCH_SECONDS * 3   # panel clears itself if the script stops

NFL_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
PRO_TEAM_IDS = {abbr: str(tid) for tid, abbr in PRO_TEAM_MAP.items() if tid}
GAME_LEFT = {}               # NFL team id or abbreviation -> fraction of its game still to play (0-1)

STATE = {"ready": False}
STATE_LOCK = threading.Lock()


# ---------------- ESPN data ----------------

def connect():
    if ESPN_S2 and SWID:
        return League(league_id=LEAGUE_ID, year=YEAR, espn_s2=ESPN_S2, swid=SWID)
    return League(league_id=LEAGUE_ID, year=YEAR)


def starters(lineup):
    return [p for p in (lineup or []) if p.slot_position not in BENCH]


def kickoff(p):
    return getattr(p, "game_date", None)  # None = bye week


def left_to_play(lineup):
    now = datetime.now()
    return sum(1 for p in starters(lineup) if kickoff(p) and kickoff(p) > now)


def in_progress(lineup):
    now = datetime.now()
    return any(kickoff(p) and kickoff(p) <= now < kickoff(p) + GAME_LENGTH
               for p in starters(lineup))


def find_my_box(boxes):
    want = MY_TEAM.lower()
    for b in boxes:
        if b.home_team and want in b.home_team.team_name.lower():
            return b, "home"
        if b.away_team and want in b.away_team.team_name.lower():
            return b, "away"
    return None, None


def side(box, which):
    if which == "home":
        return box.home_team, box.home_score, box.home_projected, box.home_lineup
    return box.away_team, box.away_score, box.away_projected, box.away_lineup


def short_name(p):
    parts = p.name.split()
    if "D/ST" in p.name or len(parts) < 2:
        return p.name
    return f"{parts[0][0]}. {' '.join(parts[1:])}"


def status_of(p):
    k = kickoff(p)
    if not k:
        return "bye", "BYE"
    now = datetime.now()
    if now < k:
        t = f"{k.hour % 12 or 12}:{k:%M}"
        return "upcoming", t if k.date() == now.date() else f"{k:%a} {t}"
    if now < k + GAME_LENGTH:
        return "live", "LIVE"
    return "final", "FINAL"


def fetch_game_left():
    """{NFL team id and abbreviation: fraction of its game still to play} from ESPN's NFL
    scoreboard: 1 before kickoff, 0 once final, and in between from the quarter and clock."""
    r = requests.get(NFL_SCOREBOARD, timeout=10)
    r.raise_for_status()
    left = {}
    for ev in r.json().get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        st = ev.get("status") or comp.get("status") or {}
        state = (st.get("type") or {}).get("state")
        if state == "pre":
            frac = 1.0
        elif state == "post":
            frac = 0.0
        else:
            try:
                period, clock = int(st.get("period") or 1), float(st.get("clock") or 0)  # clock = seconds left
            except (TypeError, ValueError):
                continue
            frac = min(1.0, max(0.0, ((4 - period) * 900 + clock) / 3600)) if period <= 4 else 0.0
        for c in comp.get("competitors", []):
            t = c.get("team") or {}
            for key in (str(t.get("id") or ""), str(t.get("abbreviation") or "").upper()):
                if key:
                    left[key] = frac
    return left


def game_left(p):
    """Fraction of a player's game still to play: from the NFL scoreboard's quarter and clock,
    or estimated from the kickoff time if the scoreboard doesn't have his game."""
    k = kickoff(p)
    if not k:
        return 0.0  # bye
    for key in (PRO_TEAM_IDS.get(p.proTeam), str(p.proTeam).upper()):
        if key in GAME_LEFT:
            return GAME_LEFT[key]
    now = datetime.now()
    if now < k:
        return 1.0
    if now >= k + GAME_LENGTH:
        return 0.0
    return max(0.0, 1 - (now - k) / timedelta(hours=3))


def live_projection(p):
    """ESPN only gives each player a pregame projection. Like ESPN's own live projections,
    this keeps what he has scored and adds his pregame projection for the part of his game left."""
    return p.points + p.projected_points * game_left(p)


def player_dict(p):
    if p is None:
        return None
    st, label = status_of(p)
    return {"name": short_name(p), "team": p.proTeam, "pts": round(p.points, 1),
            "proj": round(live_projection(p), 1), "proj0": round(p.projected_points, 1),
            "status": st, "label": label}


def lineup_rows(my_lineup, opp_lineup):
    def by_slot(lineup):
        groups = {}
        for p in starters(lineup):
            groups.setdefault(p.slot_position, []).append(p)
        return groups

    mine, theirs = by_slot(my_lineup), by_slot(opp_lineup)
    slots = sorted(set(mine) | set(theirs),
                   key=lambda s: SLOT_ORDER.index(s) if s in SLOT_ORDER else len(SLOT_ORDER))
    rows = []
    for slot in slots:
        for a, b in zip_longest(mine.get(slot, []), theirs.get(slot, [])):
            rows.append({"slot": SLOT_LABEL.get(slot, slot),
                         "me": player_dict(a), "opp": player_dict(b)})
    return rows


def live_count(lineup):
    now = datetime.now()
    return sum(1 for p in starters(lineup)
               if kickoff(p) and kickoff(p) <= now < kickoff(p) + GAME_LENGTH)


def remaining_sd(lineup):
    """Rough uncertainty in a team's remaining points.
    Each player still to play adds spread proportional to the points they still
    have left to score (full projection if not started, partial if mid-game)."""
    var = 0.0
    for p in starters(lineup):
        var += (WIN_SPREAD * p.projected_points * game_left(p)) ** 2   # 0 for byes and finals
    return math.sqrt(var)


def win_probability(my_proj, opp_proj, my_lineup, opp_lineup):
    sd = math.hypot(remaining_sd(my_lineup), remaining_sd(opp_lineup))
    diff = my_proj - opp_proj
    if sd < 0.01:
        return 100 if diff > 0 else 0 if diff < 0 else 50
    pct = 50 * (1 + math.erf(diff / (sd * math.sqrt(2))))
    return max(1, min(99, round(pct)))


def record(team):
    try:
        w, l, t = team.wins, team.losses, team.ties
    except AttributeError:
        return ""
    return f"{w}-{l}" + (f"-{t}" if t else "")


def team_dict(team, score, proj, lineup):
    name = team.team_name if team else "BYE"
    logo = getattr(team, "logo_url", "") or ""
    return {"name": name, "abbr": getattr(team, "team_abbrev", "") or name[:4].upper(),
            "record": record(team) if team else "", "logo": logo if logo.startswith("http") else "",
            "score": round(score, 1), "proj": round(proj, 1),
            "left": left_to_play(lineup), "live": live_count(lineup)}


def build_state(week, boxes, box, mine):
    theirs = "away" if mine == "home" else "home"
    m_team, m_score, m_proj, m_lineup = side(box, mine)
    o_team, o_score, o_proj, o_lineup = side(box, theirs)
    return {
        "ready": True,
        "week": week,
        "updated": datetime.now().strftime("%I:%M:%S %p").lstrip("0"),
        "me": team_dict(m_team, m_score, m_proj, m_lineup),
        "opp": team_dict(o_team, o_score, o_proj, o_lineup),
        "rows": lineup_rows(m_lineup, o_lineup),
        "win": win_probability(m_proj, o_proj, m_lineup, o_lineup),
        "opacity": PANEL_OPACITY,
        "colors": {"me": MY_COLOR, "opp": OPP_COLOR},
        "refresh": PAGE_REFRESH_SECONDS,
        "cycle": CYCLE_SECONDS,
    }


# ---------------- web page served to the TV ----------------

PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { --op: .85; }
  html, body { margin: 0; height: 100%; background: transparent; overflow: hidden; }
  body { font-family: Roboto, "Segoe UI", Arial, sans-serif; color: #f2f4f7; font-size: 24px;
         font-variant-numeric: tabular-nums; }
  #sb { position: relative; height: 100%; width: 100%; }
  #stage { display: flex; align-items: center; justify-content: center; height: 100%; }
  @keyframes pulse { 50% { opacity: .25; } }

  /* ---- center: the matchup ---- */
  #pod { position: relative; flex: 0 0 auto; width: 24.5em; height: 3.62em; box-sizing: border-box;
         display: flex; flex-direction: column; overflow: hidden; border-radius: .42em;
         background: linear-gradient(180deg, rgba(26,30,41,.97), rgba(8,10,15,.97));
         border: 1px solid rgba(255,255,255,.13); box-shadow: 0 .08em .45em rgba(0,0,0,.5); }
  #sb.lost #pod { border-color: rgba(255,77,77,.75); }
  #pc { flex: 1 1 auto; display: flex; flex-direction: column; min-height: 0; }
  #prog { position: absolute; left: 0; top: 0; height: .07em; width: 0; z-index: 2;
          background: rgba(245,197,66,.85); }
  #prog.run { width: 100%; transition: width linear; }
  .main { flex: 1 1 auto; display: flex; align-items: stretch; min-height: 0; }

  .team { flex: 1 1 0; min-width: 0; display: flex; align-items: center; position: relative;
          padding: 0 .4em 0 .55em; background: linear-gradient(90deg, var(--cs), transparent 72%); }
  .team.h { flex-direction: row-reverse; padding: 0 .55em 0 .4em;
            background: linear-gradient(270deg, var(--cs), transparent 72%); }
  .stripe { position: absolute; top: 0; bottom: 0; left: 0; width: .2em; background: var(--c); }
  .team.h .stripe { left: auto; right: 0; }
  .lg { position: relative; flex: 0 0 auto; width: 1.45em; height: 1.45em; border-radius: 50%;
        background: var(--c); display: flex; align-items: center; justify-content: center; overflow: hidden; }
  .lg span { font-size: .7em; font-weight: 800; }
  .lg img { position: absolute; left: 0; top: 0; width: 100%; height: 100%; object-fit: cover; }
  .lg.ok span { visibility: hidden; }
  .nmcol { flex: 1 1 auto; min-width: 0; margin: 0 .3em 0 .38em; line-height: 1.05; }
  .team.h .nmcol { margin: 0 .38em 0 .3em; text-align: right; }
  .ab { font-size: .98em; font-weight: 800; letter-spacing: .02em; white-space: nowrap; overflow: hidden;
        text-overflow: ellipsis; }
  .rec { font-size: .47em; color: #9aa3b2; letter-spacing: .05em; white-space: nowrap; margin-top: .2em; }
  .rec b, .cnt b { color: #ff6b5b; font-weight: 700; }
  .r1 + .r2, body.nowings .cnt { display: none; }
  body.nowings .r1 + .r2 { display: inline; } body.nowings .r1 { display: none; }
  .ld { display: inline-block; width: .55em; height: .55em; border-radius: 50%; background: #ff4d4d;
        margin: 0 .25em 0 .1em; vertical-align: .05em; animation: pulse 1.4s infinite; }
  .sc { flex: 0 0 auto; min-width: 2.2em; text-align: center; font-size: 1.4em; font-weight: 800;
        letter-spacing: -.02em; }
  .team.lead .sc { color: #fff; } .team.trail .sc { color: #aeb6c4; }

  .mid { flex: 0 0 4.3em; display: flex; flex-direction: column; align-items: center; justify-content: center;
         background: rgba(0,0,0,.38); border-left: 1px solid rgba(255,255,255,.07);
         border-right: 1px solid rgba(255,255,255,.07); }
  .clk { display: flex; align-items: center; white-space: nowrap; }
  .clk b { font-size: .52em; font-weight: 800; letter-spacing: .08em; color: #f5c542; margin-right: .45em; }
  .clk span { font-size: .78em; font-weight: 800; }
  .up { color: #2ecc71; } .down { color: #ff6b5b; } .tie { color: #f1c40f; }
  .badge { margin-top: .22em; font-size: .42em; font-weight: 800; letter-spacing: .1em; padding: .12em .6em;
           border-radius: .3em; color: #fff; white-space: nowrap; border: 1px solid rgba(255,255,255,.28);
           background: #2a3140; }
  .badge.up { background: #1e8e4f; } .badge.down { background: #b83a2e; }

  .strip { flex: 0 0 1em; display: flex; align-items: center; padding: 0 .55em; min-width: 0;
           background: rgba(255,255,255,.05); border-top: 1px solid rgba(255,255,255,.07); }
  .strip > span { font-size: .5em; }
  .pj { flex: 0 0 auto; color: #8f98a8; letter-spacing: .06em; } .pj b { color: #fff; }
  .sm { flex: 1 1 auto; min-width: 0; margin: 0 .8em; text-align: center; white-space: nowrap; overflow: hidden;
        text-overflow: ellipsis; color: #d7dce5; letter-spacing: .03em; transition: opacity .35s ease; }
  .sm .short { display: none; }
  body.nowings .sm .long { display: none; } body.nowings .sm .short { display: inline; }
  .sm b { color: #fff; } .sm .w { color: #2ecc71; }
  .chip { display: inline-block; padding: 0 .45em; margin: 0 .5em; border-radius: .3em; font-weight: 800;
          letter-spacing: .08em; background: rgba(143,162,196,.22); color: #c9d6ee; }
  .wpb { flex: 0 0 .13em; display: flex; background: rgba(255,255,255,.08); }
  .wpb i { display: block; height: 100%; transition: width .8s ease; }

  /* ---- the wings: this slot's two players ---- */
  .wing { flex: 1 1 0; min-width: 0; height: 2.92em; box-sizing: border-box; position: relative;
          display: flex; flex-direction: column; justify-content: center; overflow: hidden;
          transition: opacity .35s ease; }
  .wing.fade, .sm.fade { opacity: 0; }
  .wing::before { content: ""; position: absolute; left: 0; right: 0; top: 0; height: .09em; background: var(--c); }
  .wing.l { margin-right: .18em; padding: 0 .6em 0 1.35em;
            background: linear-gradient(90deg, var(--cs) 0, rgba(10,12,18,var(--op)) 58%);
            clip-path: polygon(.95em 0, 100% 0, 100% 100%, 0 100%); }
  .wing.r { margin-left: .18em; padding: 0 1.35em 0 .6em;
            background: linear-gradient(270deg, var(--cs) 0, rgba(10,12,18,var(--op)) 58%);
            clip-path: polygon(0 0, calc(100% - .95em) 0, 100% 100%, 0 100%); }
  .row { display: flex; align-items: center; min-width: 0; white-space: nowrap; }
  .wing.r .row { flex-direction: row-reverse; }
  .slot { flex: 0 0 auto; min-width: 2.9em; box-sizing: border-box; text-align: center; padding: .08em .35em;
          border-radius: .3em; font-size: .5em; font-weight: 800; letter-spacing: .1em; color: #fff;
          background: var(--cs); border: 1px solid var(--c); }
  .pn { flex: 1 1 auto; min-width: 0; margin: 0 .45em; font-size: .78em; font-weight: 700; overflow: hidden;
        text-overflow: ellipsis; }
  .wing.r .pn { text-align: right; }
  .st .t { color: #d7dce5; margin-right: .7em; }
  .pp { flex: 0 0 auto; min-width: 2em; text-align: center; font-size: 1.05em; font-weight: 800; }
  .pp.w { color: #2ecc71; }
  .row2 { margin-top: .28em; }
  .st { flex: 0 0 auto; min-width: 7em; font-size: .44em; font-weight: 700; letter-spacing: .1em; color: #9aa3b2; }
  .wing.r .st { text-align: right; }
  .st.live { color: #ff6b5b; } .st .ld { width: .8em; height: .8em; }
  .st.final { color: #7a8292; }
  .bar { flex: 1 1 auto; height: .14em; margin: 0 .55em; border-radius: .1em; overflow: hidden;
         background: rgba(255,255,255,.12); display: flex; }
  .wing.r .bar { justify-content: flex-end; }
  .bar i { display: block; height: 100%; background: var(--c); }
  .bar.over i { background: #f5c542; }
  .pjn { flex: 0 0 auto; min-width: 4.8em; font-size: .44em; color: #9aa3b2; letter-spacing: .06em; text-align: center; }
  .empty { color: #6f7888; font-style: italic; }
  body.nowings .wing { display: none; }
</style></head>
<body><div id="sb"><div id="stage">
  <div class="wing l" id="wl"></div>
  <div id="pod"><div id="prog"></div><div id="pc"></div></div>
  <div class="wing r" id="wr"></div>
</div></div>
<script>
const $ = s => document.getElementById(s);
const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g,
  c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
let data = null, idx = 0, lastOk = Date.now(), refresh = 5, cycle = 10, lastHdr = "", lastRow = "";

function rgb(hex) {
  hex = String(hex || "").replace("#", "");
  return /^[0-9a-f]{6}$/i.test(hex) ? [0, 2, 4].map(i => parseInt(hex.slice(i, i + 2), 16)) : [143, 162, 196];
}
const rgba = (c, a) => "rgba(" + c[0] + "," + c[1] + "," + c[2] + "," + a + ")";
const colorVars = c => "--c:" + rgba(c, 1) + ";--cs:" + rgba(c, .4);
const f1 = n => Number(n).toFixed(1);

function teamHtml(t, side, c, lead) {
  const logo = /^https?:\/\//.test(t.logo || "")
    ? '<img src="' + esc(t.logo) + '" alt="" onload="this.parentNode.classList.add(\'ok\')" onerror="this.remove()">' : "";
  const rec = (t.record ? '<span class="r1">' + esc(t.record) + '</span>' : "") + '<span class="r2">' + counts(t) + '</span>';
  return '<div class="team ' + (side === "me" ? "a" : "h") + (lead > 0 ? " lead" : lead < 0 ? " trail" : "") +
    '" style="' + colorVars(c) + '"><span class="stripe"></span>' +
    '<span class="lg"><span>' + esc(String(t.abbr || t.name).slice(0, 1)) + '</span>' + logo + '</span>' +
    '<div class="nmcol"><div class="ab">' + esc(t.abbr || t.name) + '</div><div class="rec">' + rec + '</div></div>' +
    '<span class="sc">' + f1(t.score) + '</span></div>';
}

function counts(t) {
  return (t.live ? '<b><i class="ld"></i>' + t.live + ' live</b> · ' : "") + t.left + " left";
}

function header(d) {
  const diff = d.me.score - d.opp.score, cm = rgb(d.colors.me), co = rgb(d.colors.opp);
  const mg = diff > 0 ? '<span class="up">+' + f1(diff) + '</span>'
           : diff < 0 ? '<span class="down">−' + f1(-diff) + '</span>' : '<span class="tie">TIED</span>';
  const win = d.win == null ? "" :
    '<span class="badge ' + (d.win > 50 ? "up" : d.win < 50 ? "down" : "") + '">' + d.win + '% WIN</span>';
  const wp = d.win == null ? '<div class="wpb"></div>' :
    '<div class="wpb"><i style="width:' + d.win + '%;background:' + rgba(cm, .95) + '"></i>' +
    '<i style="width:' + (100 - d.win) + '%;background:' + rgba(co, .95) + '"></i></div>';
  const html = '<div class="main">' + teamHtml(d.me, "me", cm, diff) +
    '<div class="mid"><div class="clk"><b>WK ' + esc(d.week) + '</b>' + mg + '</div>' + win + '</div>' +
    teamHtml(d.opp, "opp", co, -diff) + '</div>' +
    '<div class="strip"><span class="pj">PROJ <b>' + f1(d.me.proj) + '</b><span class="cnt"> · ' + counts(d.me) + '</span></span>' +
    '<span class="sm" id="sm"></span><span class="pj"><span class="cnt">' + counts(d.opp) + ' · </span>PROJ <b>' +
    f1(d.opp.proj) + '</b></span></div>' + wp;
  if (html === lastHdr) return;
  lastHdr = html;
  $("pc").innerHTML = html;
  $("wl").setAttribute("style", colorVars(cm));
  $("wr").setAttribute("style", colorVars(co));
  lastRow = "";
}

function status(p) {
  const t = p.team ? '<span class="t">' + esc(p.team) + '</span>' : "";
  if (p.status === "live") return '<span class="st live">' + t + '<i class="ld"></i>LIVE</span>';
  if (p.status === "final") return '<span class="st final">' + t + 'FINAL</span>';
  return '<span class="st">' + t + esc(p.label) + '</span>';
}

function wing(p, other, slot) {
  const chip = '<span class="slot">' + esc(slot) + '</span>';
  if (!p) return '<div class="row">' + chip + '<span class="pn empty">Empty</span></div>';
  const fill = p.proj > 0 ? Math.min(100, Math.round(100 * Math.max(p.pts, 0) / p.proj)) : (p.pts > 0 ? 100 : 0);
  const won = other && p.pts > other.pts ? " w" : "";
  const name = String(p.name).replace(/ D\/ST$/, "");   // the chip already says D/ST
  return '<div class="row">' + chip + '<span class="pn">' + esc(name) + '</span>' +
    '<span class="pp' + won + '">' + f1(p.pts) + '</span></div>' +
    '<div class="row row2">' + status(p) + '<span class="bar' + (p.proj0 > 0 && p.pts > p.proj0 ? " over" : "") +
    '"><i style="width:' + fill + '%"></i></span><span class="pjn">PROJ ' + f1(p.proj) + '</span></div>';
}

// The slot's matchup in one line, for the strip when the bar is too narrow for the wings
function oneLine(r, n, total) {
  const side = (p, o) => p ? esc(p.name) + ' <b class="' + (o && p.pts > o.pts ? "w" : "") + '">' + f1(p.pts) + '</b>'
                           : '<i class="empty">Empty</i>';
  return '<span class="long">' + esc(r.slot) + ' · ' + n + '/' + total + '</span><span class="short">' +
    side(r.me, r.opp) + '<span class="chip">' + esc(r.slot) + '</span>' + side(r.opp, r.me) + '</span>';
}

function showRow(animate) {
  if (!data || !data.rows.length) return;
  idx = idx % data.rows.length;
  const r = data.rows[idx];
  const key = JSON.stringify(r) + idx + "|" + data.rows.length;
  if (!animate && key === lastRow) return;
  const els = [$("wl"), $("wr"), $("sm")];
  const put = () => {
    lastRow = key;
    $("wl").innerHTML = wing(r.me, r.opp, r.slot);
    $("wr").innerHTML = wing(r.opp, r.me, r.slot);
    $("sm").innerHTML = oneLine(r, idx + 1, data.rows.length);
    fit();
    els.forEach(el => el.classList.remove("fade"));
  };
  if (!animate) return put();
  els.forEach(el => el.classList.add("fade"));
  setTimeout(put, 350);
}

function overflowing() {
  if ($("stage").scrollHeight > window.innerHeight + 1) return true;
  for (const el of document.querySelectorAll(".main, .clk, .ab, .sc"))
    if (el.offsetParent && el.scrollWidth > el.clientWidth + 1) return true;
  for (const el of document.querySelectorAll(".wing .pn"))
    if (el.offsetParent && el.clientWidth < parseFloat(document.body.style.fontSize) * 4.4) return true;
  return false;
}

function fitAt(wings) {
  document.body.classList.toggle("nowings", !wings);
  let fs = window.innerHeight / 3.75;
  document.body.style.fontSize = fs + "px";
  while (overflowing() && fs > 6) { fs -= .5; document.body.style.fontSize = fs + "px"; }
  return fs;
}

// Full size with the player wings if they fit; if they'd squeeze the text much, drop them
// and show the player matchup in the strip instead
function fit() {
  const full = window.innerHeight / 3.75, withWings = fitAt(true);
  if (withWings >= full * .85) return;
  if (fitAt(false) <= withWings + .5) fitAt(true);
}

function progress() {
  const p = $("prog");
  p.classList.remove("run"); p.style.transitionDuration = "0s"; void p.offsetWidth;
  p.style.transitionDuration = cycle + "s"; p.classList.add("run");
}

async function poll() {
  try {
    const r = await fetch("data.json?t=" + Date.now(), {cache: "no-store"});
    const d = await r.json();
    if (d.ready) {
      document.documentElement.style.setProperty("--op", d.opacity);
      const first = !data;
      data = d; refresh = d.refresh || 5; cycle = d.cycle || 10;
      header(d);
      showRow(false);
      if (first) { setTimeout(rotate, cycle * 1000); progress(); }
      lastOk = Date.now();
      $("sb").classList.remove("lost");
    }
  } catch (e) {
    if (Date.now() - lastOk > 30000) $("sb").classList.add("lost");
  }
  setTimeout(poll, refresh * 1000);
}

function rotate() {
  idx++;
  showRow(true);
  progress();
  setTimeout(rotate, cycle * 1000);
}

window.addEventListener("resize", () => { lastRow = ""; showRow(false); });
poll();
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/data.json"):
            with STATE_LOCK:
                body = json.dumps(STATE).encode()
            ctype = "application/json"
        elif self.path in ("/", "/index.html") or self.path.startswith("/?"):
            body = PAGE.encode()
            ctype = "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # keep the console quiet


def start_web_server():
    server = ThreadingHTTPServer(("0.0.0.0", WEB_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def detect_pc_ip():
    # The address this PC uses to reach the TV (no traffic is actually sent)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((TV_IP, 7979))
        return s.getsockname()[0]
    finally:
        s.close()


# ---------------- PiPup ----------------

def show(url):
    card = {
        "id": CARD_ID,
        "duration": SAFETY_SECONDS,
        "position": POSITION,
        "padding": 0,
        "backgroundColor": "#00000000",
        "dismissScreensaver": False,
        "media": {"web": {"uri": url, "width": PANEL_WIDTH, "height": PANEL_HEIGHT,
                          "transparent": True}},
    }
    try:
        requests.post(f"http://{TV_IP}:7979/notify", json=card, timeout=5)
        return True
    except requests.RequestException as e:
        print(f"Could not reach TV at {TV_IP}: {e}")
        return False


def hide():
    try:
        requests.post(f"http://{TV_IP}:7979/cancel", params={"id": CARD_ID}, timeout=5)
    except requests.RequestException:
        pass


# ---------------- main loop ----------------

def main():
    global STATE
    start_web_server()
    pc_ip = PC_IP or detect_pc_ip()
    url = f"http://{pc_ip}:{WEB_PORT}/"
    print(f"Panel page: {url}  (open it in a browser on the PC to preview)")

    league = connect()
    connected_at = time.time()
    showing = False
    last_log = None
    clock_warned = False
    print(f"Watching league {LEAGUE_ID} for '{MY_TEAM}'. Ctrl+C to stop.")

    try:
        while True:
            try:
                if time.time() - connected_at > 6 * 3600:  # roll over to the new week
                    league = connect()
                    connected_at = time.time()

                week = league.current_week
                boxes = league.box_scores(week)
                try:
                    left = fetch_game_left()
                    clock_warned = False
                except (requests.RequestException, ValueError) as e:
                    left = {}
                    if not clock_warned:
                        print(f"Couldn't load the NFL scoreboard ({e}); estimating game clocks from kickoff times")
                        clock_warned = True
                GAME_LEFT.clear()
                GAME_LEFT.update(left)
                box, mine = find_my_box(boxes)

                if not box:
                    print(f"No matchup found for '{MY_TEAM}' in week {week} - check MY_TEAM.")
                else:
                    new_state = build_state(week, boxes, box, mine)
                    with STATE_LOCK:
                        STATE = new_state

                    live = in_progress(box.home_lineup) or in_progress(box.away_lineup)
                    if live or not ONLY_DURING_GAMES:
                        showing = show(url) or showing   # same id + same page = keeps it alive, no redraw
                        log = f"Week {week}: {new_state['me']['score']} - {new_state['opp']['score']}"
                    else:
                        if showing:
                            hide()
                            showing = False
                        log = "No games in progress for your matchup - waiting"
                    if log != last_log:
                        print(time.strftime("%H:%M:%S"), log)
                        last_log = log

            except Exception as e:
                print(f"Error: {e}")

            time.sleep(FETCH_SECONDS)
    except KeyboardInterrupt:
        hide()
        print("Stopped - panel removed.")


if __name__ == "__main__":
    main()
