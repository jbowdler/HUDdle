"""
HUDdle - live ESPN fantasy football scores on top of anything playing on your Android TV.
Draws the overlay with PiPup (maintained fork: https://github.com/mhoogenbosch/PiPup).

Shows a slim scoreboard bar at the top center of the TV: matchup score on top,
one player matchup below (QB vs QB, RB vs RB...) that rotates every few seconds.

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
PANEL_WIDTH = 820            # panel size on the TV; text scales to fit
PANEL_HEIGHT = 74
PANEL_OPACITY = 0.85         # background darkness (0-1)
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


def player_dict(p):
    if p is None:
        return None
    st, label = status_of(p)
    return {"name": short_name(p), "team": p.proTeam, "pts": round(p.points, 1),
            "proj": round(p.projected_points, 1), "status": st, "label": label}


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
    now = datetime.now()
    var = 0.0
    for p in starters(lineup):
        k = kickoff(p)
        if not k or now >= k + GAME_LENGTH:
            continue  # bye or final
        frac_left = 1.0 if now < k else max(0.0, 1 - (now - k) / timedelta(hours=3))
        var += (WIN_SPREAD * p.projected_points * frac_left) ** 2
    return math.sqrt(var)


def win_probability(my_proj, opp_proj, my_lineup, opp_lineup):
    sd = math.hypot(remaining_sd(my_lineup), remaining_sd(opp_lineup))
    diff = my_proj - opp_proj
    if sd < 0.01:
        return 100 if diff > 0 else 0 if diff < 0 else 50
    pct = 50 * (1 + math.erf(diff / (sd * math.sqrt(2))))
    return max(1, min(99, round(pct)))


def team_dict(team, score, proj, lineup):
    return {"name": team.team_name if team else "BYE", "score": round(score, 1),
            "proj": round(proj, 1), "left": left_to_play(lineup), "live": live_count(lineup)}


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
        "refresh": PAGE_REFRESH_SECONDS,
        "cycle": CYCLE_SECONDS,
    }


# ---------------- web page served to the TV ----------------

PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  html, body { margin: 0; background: transparent; overflow: hidden; height: 100%; }
  body { font-family: Roboto, "Segoe UI", Arial, sans-serif; color: #f2f4f7; }
  #bar { position: relative; box-sizing: border-box; width: 100%; padding: .3em 1em .42em;
         overflow: hidden; border-radius: .55em; background: rgba(10,12,18,var(--op,.85));
         border: 1px solid rgba(255,255,255,.08); }

  /* Each line: left half | center | right half. Halves are equal so the center stays centered. */
  .line { display: flex; align-items: center; white-space: nowrap; font-variant-numeric: tabular-nums; }
  .half { flex: 1 1 0; min-width: 0; display: flex; align-items: center; }
  .half.r { flex-direction: row-reverse; }
  .ctr { flex: 0 0 auto; text-align: center; margin: 0 1.1em; }
  .nm { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
  .half.r .nm { text-align: right; }

  /* Numbers: never shrink, fixed gaps */
  .num { flex: 0 0 auto; text-align: center; }
  .proj { color: #8b93a3; font-size: .78em; min-width: 2.6em; }
  .pts { font-weight: 700; min-width: 2.4em; }
  .half:not(.r) .proj { margin: 0 .9em 0 .8em; }
  .half.r .proj { margin: 0 .8em 0 .9em; }

  /* Header */
  .hdr .nm { font-size: .78em; color: #c3c9d4; }
  .cnt { flex: 0 0 auto; font-size: .56em; color: #8b93a3; margin: 0 .9em; letter-spacing: .02em; }
  .cnt b { color: #ff6b5b; font-weight: 700; }
  .hdr .sc { flex: 0 0 auto; font-size: 1.22em; font-weight: 700; }
  .hdr .half:not(.r) .sc { margin-left: .8em; }
  .hdr .half.r .sc { margin-right: .8em; }
  .wk b { font-weight: 700; letter-spacing: .04em; }
  .wk .wp { color: #2ecc71; } .wk .lp { color: #ff6b5b; }
  .wk { display: block; font-size: .48em; letter-spacing: .12em; color: #8b93a3; line-height: 1.1; }
  .pill { display: inline-block; min-width: 3em; padding: .06em .45em; border-radius: 1em;
          font-size: .64em; font-weight: 700; line-height: 1.25; }
  .up { background: rgba(46,204,113,.18); color: #2ecc71; }
  .down { background: rgba(231,76,60,.22); color: #ff6b5b; }
  .tie { background: rgba(241,196,15,.18); color: #f1c40f; }
  .win { color: #2ecc71; }

  .sep { height: 1px; background: rgba(255,255,255,.08); margin: .24em 0 .22em; }

  /* Player row */
  #row { font-size: .82em; transition: opacity .35s ease; }
  #row.fade { opacity: 0; }
  .slot { display: inline-block; min-width: 3.4em; padding: .1em .4em; border-radius: .35em;
          background: rgba(143,162,196,.14); color: #a9bad8; font-weight: 700;
          font-size: .72em; letter-spacing: .05em; }
  .t { color: #8b93a3; font-size: .8em; margin: 0 .45em; }
  .dot { display: inline-block; width: .55em; height: .55em; border-radius: 50%; vertical-align: middle; }
  .live { background: #ff4d4d; animation: pulse 1.4s infinite; }
  .final { background: #5b6372; }
  .ko { font-size: .7em; color: #8b93a3; }
  .empty { color: #5b6372; font-style: italic; }
  @keyframes pulse { 50% { opacity: .3; } }

  #prog { position: absolute; left: 0; bottom: 0; height: 2px; width: 0; background: rgba(143,162,196,.55); }
  #prog.run { width: 100%; transition: width linear; }
  #bar.lost { border-color: rgba(255,77,77,.6); }
</style></head>
<body><div id="bar"><div class="line hdr" id="hdr"></div><div class="sep"></div><div class="line" id="row"></div><div id="prog"></div></div>
<script>
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let data = null, idx = 0, lastOk = Date.now(), refresh = 5, cycle = 5;

function st(p) {
  if (p.status === "live") return '<span class="dot live"></span>';
  if (p.status === "final") return '<span class="dot final"></span>';
  return '<span class="ko">' + esc(p.label) + '</span>';
}

function counts(t) {
  const live = t.live ? '<b>' + t.live + ' live</b> · ' : '';
  return '<span class="cnt">' + live + t.left + ' left</span>';
}

function winPct(w) {
  if (w === undefined || w === null) return '';
  return ' · <b class="' + (w >= 50 ? 'wp' : 'lp') + '">' + w + '% WIN</b>';
}

function header(d) {
  const diff = d.me.score - d.opp.score;
  const pill = diff > 0 ? '<span class="pill up">+' + diff.toFixed(1) + '</span>'
             : diff < 0 ? '<span class="pill down">−' + (-diff).toFixed(1) + '</span>'
             : '<span class="pill tie">TIED</span>';
  $("#hdr").innerHTML =
    '<div class="half"><span class="nm">' + esc(d.me.name) + '</span>' + counts(d.me) +
      '<span class="sc' + (diff > 0 ? ' win' : '') + '">' + d.me.score.toFixed(1) + '</span></div>' +
    '<div class="ctr"><span class="wk">WK ' + d.week + winPct(d.win) + '</span>' + pill + '</div>' +
    '<div class="half r"><span class="nm">' + esc(d.opp.name) + '</span>' + counts(d.opp) +
      '<span class="sc' + (diff < 0 ? ' win' : '') + '">' + d.opp.score.toFixed(1) + '</span></div>';
}

// Halves are built outside-in: name, proj, pts (the right half is mirrored with row-reverse)
function half(p, other, right) {
  if (!p) return '<div class="half' + (right ? ' r' : '') + '"><span class="nm empty">Empty</span></div>';
  const win = other && p.pts > other.pts ? " win" : "";
  const name = right
    ? st(p) + '<span class="t">' + esc(p.team) + '</span>' + esc(p.name)
    : esc(p.name) + '<span class="t">' + esc(p.team) + '</span>' + st(p);
  return '<div class="half' + (right ? ' r' : '') + '">' +
         '<span class="nm">' + name + '</span>' +
         '<span class="num proj">' + p.proj.toFixed(1) + '</span>' +
         '<span class="num pts' + win + '">' + p.pts.toFixed(1) + '</span></div>';
}

function rowHtml(r) {
  return half(r.me, r.opp, false) + '<div class="ctr"><span class="slot">' + esc(r.slot) + '</span></div>' + half(r.opp, r.me, true);
}

function showRow(animate) {
  if (!data || !data.rows.length) return;
  idx = idx % data.rows.length;
  const html = rowHtml(data.rows[idx]);
  const row = $("#row");
  if (!animate) { row.innerHTML = html; fit(); return; }
  row.classList.add("fade");
  setTimeout(() => { row.innerHTML = html; fit(); row.classList.remove("fade"); }, 350);
}

function overflowing() {
  // Shrink if the bar is too tall, or if any line's fixed parts (numbers, center) don't fit
  const bar = $("#bar");
  if (bar.offsetHeight > window.innerHeight - 1) return true;
  for (const line of document.querySelectorAll(".line"))
    if (line.scrollWidth > line.clientWidth + 1) return true;
  return false;
}

function fit() {
  let fs = window.innerHeight / 3.6;
  document.body.style.fontSize = fs + "px";
  while (overflowing() && fs > 6) {
    fs -= 0.5;
    document.body.style.fontSize = fs + "px";
  }
}

function progress() {
  const p = $("#prog");
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
      data = d; refresh = d.refresh || 5; cycle = d.cycle || 5;
      header(d);
      showRow(false);
      if (first) { setTimeout(rotate, cycle * 1000); progress(); }
      lastOk = Date.now();
      $("#bar").classList.remove("lost");
    }
  } catch (e) {
    if (Date.now() - lastOk > 30000) $("#bar").classList.add("lost");
  }
  setTimeout(poll, refresh * 1000);
}

function rotate() {
  idx++;
  showRow(true);
  progress();
  setTimeout(rotate, cycle * 1000);
}

window.addEventListener("resize", fit);
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
    print(f"Watching league {LEAGUE_ID} for '{MY_TEAM}'. Ctrl+C to stop.")

    try:
        while True:
            try:
                if time.time() - connected_at > 6 * 3600:  # roll over to the new week
                    league = connect()
                    connected_at = time.time()

                week = league.current_week
                boxes = league.box_scores(week)
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
