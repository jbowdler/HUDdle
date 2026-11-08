"""
HUDdle college football replays - highlight clips played over whatever is on your Android TV.
Draws the popup with PiPup (maintained fork: https://github.com/mhoogenbosch/PiPup).

Watches one of two sets of games:
  - "ranked":   every game with a ranked team (ESPN's displayed ranking)
  - "selected": only the games you pick, by ESPN game id

How it works:
  - ESPN's scoreboard says which of those games are live right now.
  - While any are live, the script looks for new highlight clips from one of two sources:
      ESPN (default, free, no account): the highlight videos ESPN attaches to each game.
      X (optional, needs X API access): new video posts from a list of X accounts,
        matched to a game by the team names in the post or the team account that posted it.
  - New clips play one at a time in a PiPup popup on the TV, which streams them
    straight from ESPN or X; nothing is downloaded to this PC.
  - A scoreboard bar sits at the top center of the TV the whole time, rotating through
    ranked games: score, quarter and clock, network, down and distance, and each team's
    stats (pass and rush yards, turnovers, top performers...) on its side of the bar.

Setup:
    python -m pip install requests
    python cfb_replays.py --list                  # this week's games and their ESPN ids
    python cfb_replays.py                         # watch the games set in overlay_config.py
    python cfb_replays.py --games 401752771 401752780   # watch just these games
    python cfb_replays.py --test                  # play a sample video to check the TV
    python cfb_replays.py --test-scoreboard       # show the scoreboard with made-up games
    python cfb_replays.py --scoreboard-only       # just the scoreboard, no replay clips
"""

import argparse
import html
import json
import math
import os
import queue
import re
import secrets
import socket
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

# ======================= SETTINGS =======================
# Every setting, its default and what it does is in default_config.py. Change them in your own
# overlay_config.py next to this script (copy overlay_config.example.py to start); it overrides
# the defaults and is in .gitignore, so your logins stay out of git and updates never touch it.
from default_config import *  # noqa: F401,F403
try:
    from overlay_config import *  # noqa: F401,F403
except ImportError:
    pass
# ========================================================

X_BEARER_TOKEN = X_BEARER_TOKEN or os.environ.get("X_BEARER_TOKEN")

ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/college-football"
X_API = "https://api.x.com/2"
CARD_ID = "cfb-replay"
FBS = 80                      # ESPN group id for all FBS games
TOP_25 = 25                   # ranks shown next to team names
MAX_QUERY = 512               # X recent search query length limit
SAMPLE_VIDEO = "https://storage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4"
VERBOSE = False


def games_text(n):
    return f"{n} game" + ("" if n == 1 else "s")


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def debug(msg):
    if VERBOSE:
        log("  " + msg)


def parse_time(s):
    # ESPN: 2026-10-31T16:00Z   X: 2026-10-31T19:42:11.000Z
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def iso_utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_game_id(value):
    """Accepts 401752771, "401752771" or an ESPN game page link."""
    s = str(value).strip()
    m = re.search(r"gameId[/=](\d+)", s) or re.fullmatch(r"(\d+)", s)
    return m.group(1) if m else None


def parse_game_ids(values):
    ids = []
    for v in values:
        for part in re.split(r"[\s,]+", str(v)):
            if not part:
                continue
            gid = parse_game_id(part)
            if gid:
                if gid not in ids:
                    ids.append(gid)
            else:
                print(f"Ignoring '{part}': not an ESPN game id or game link")
    return ids


# ---------------- ESPN games ----------------

def team_from(c):
    t = c.get("team") or {}
    rank = (c.get("curatedRank") or {}).get("current") or c.get("rank") or 99
    score = c.get("score", "")
    if isinstance(score, dict):
        score = score.get("displayValue", "")
    try:
        rank = int(rank)
    except (TypeError, ValueError):
        rank = 99
    return {
        "id": str(t.get("id", "")),
        "abbr": t.get("abbreviation") or "",
        "short": t.get("shortDisplayName") or t.get("location") or t.get("displayName") or "",
        "rank": rank,
        "score": str(score),
        "raw": t,
    }


def game_from(event_id, comp, status, date=None):
    sides = {c.get("homeAway"): team_from(c) for c in comp.get("competitors", [])}
    st = (status or {}).get("type", {})
    nets = [n for b in comp.get("broadcasts", []) for n in b.get("names", [])]
    return {
        "id": str(event_id),
        "away": sides.get("away"),
        "home": sides.get("home"),
        "state": st.get("state", "pre"),          # pre / in / post
        "detail": st.get("shortDetail") or st.get("detail") or "",
        "date": date or comp.get("date") or "",
        "tv": ", ".join(nets),
    }


def fetch_events(date=None):
    """ESPN's raw scoreboard events: this week's FBS games, or one day's with date=YYYYMMDD."""
    params = {"groups": FBS, "limit": 500}
    if date:
        params["dates"] = date
    r = requests.get(f"{ESPN}/scoreboard", params=params, timeout=15)
    r.raise_for_status()
    return r.json().get("events", [])


def fetch_scoreboard(date=None):
    games = {}
    for ev in fetch_events(date):
        comp = (ev.get("competitions") or [{}])[0]
        g = game_from(ev.get("id"), comp, ev.get("status") or comp.get("status"), ev.get("date"))
        if g["away"] and g["home"]:
            games[g["id"]] = g
    return games


def fetch_summary(game_id):
    r = requests.get(f"{ESPN}/summary", params={"event": game_id}, timeout=15)
    r.raise_for_status()
    return r.json()


def fetch_game(game_id):
    """One game by id, for picks that aren't on the FBS scoreboard (e.g. FCS)."""
    comp = ((fetch_summary(game_id).get("header") or {}).get("competitions") or [{}])[0]
    g = game_from(game_id, comp, comp.get("status"))
    if not (g["away"] and g["home"]):
        raise ValueError("no teams in ESPN's response")
    return g


def espn_clip(v):
    """The playable clip in one of ESPN's game videos, or None if it's too long or has no MP4/HLS link."""
    try:
        seconds = float(v.get("duration") or 0)
    except (TypeError, ValueError):
        seconds = 0
    if seconds > CFB_MAX_CLIP_SECONDS:
        return None
    links = v.get("links") or {}
    source = links.get("source") or {}
    hls = source.get("HLS") or {}
    found = []
    hrefs(source, found)
    hrefs(links.get("mobile"), found)
    mp4 = [u for u in dict.fromkeys(found) if re.search(r"\.mp4(\?|$)", u)]
    # ESPN file names carry their bitrate (..._360p30_1464k.mp4), so pick the best one that fits
    rated = [(kbps_in(u), u) for u in mp4 if kbps_in(u)]
    if rated:
        fits = [p for p in rated if p[0] * 1000 <= CFB_MAX_BITRATE]
        url = (max(fits) if fits else min(rated))[1]
    else:
        hd, sd = (source.get("HD") or {}).get("href"), source.get("href")
        urls = [hd, sd] if CFB_MAX_BITRATE >= 2_000_000 else [sd, hd]      # HD is 720p
        urls += mp4 + [(hls.get("HD") or {}).get("href"), hls.get("href")]
        url = next((u for u in urls if isinstance(u, str) and u.startswith("http")
                    and re.search(r"\.(mp4|m3u8)(\?|$)", u)), None)
    if not url:
        return None
    poster = v.get("thumbnail") or ((v.get("posterImages") or {}).get("default") or {}).get("href")
    return {"url": url, "poster": poster if isinstance(poster, str) else None, "seconds": seconds or None,
            "quality": quality(url)}


def hrefs(node, out):
    """Every http link under a part of ESPN's video "links" tree."""
    if isinstance(node, dict):
        for k, val in node.items():
            if k == "href" and isinstance(val, str) and val.startswith("http"):
                out.append(val)
            else:
                hrefs(val, out)


def kbps_in(url):
    m = re.search(r"_(\d{3,5})k\.mp4(\?|$)", url)
    return int(m.group(1)) if m else None


def quality(url, bit_rate=None):
    """A short label like "640x360, 832 kbps" from what the URL (or X) says about the file."""
    m = re.search(r"/(\d{3,4}x\d{3,4})/", url) or re.search(r"_(\d{3,4}p)\d*_", url)
    kbps = round(bit_rate / 1000) if bit_rate else kbps_in(url)
    parts = [m.group(1) if m else "", f"{kbps} kbps" if kbps else ""]
    return ", ".join(p for p in parts if p) or ("HLS" if ".m3u8" in url else "unknown size")


def is_ranked(g, cutoff=CFB_RANK_CUTOFF):
    return any(1 <= t["rank"] <= cutoff for t in (g["away"], g["home"]))


def team_label(t, full=False):
    name = t["short"] if full else t["abbr"]
    return f"#{t['rank']} {name}" if 1 <= t["rank"] <= TOP_25 else name


def matchup(g, full=False):
    return f"{team_label(g['away'], full)} @ {team_label(g['home'], full)}"


def score_line(g):
    a, h = g["away"], g["home"]
    return f"{team_label(a)} {a['score']}  @  {team_label(h)} {h['score']}   {g['detail']}"


def score_short(g):
    a, h = g["away"], g["home"]
    return f"{team_label(a)} {a['score']} @ {team_label(h)} {h['score']} · {g['detail']}"


def shorten(text, limit):
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def kickoff_label(g):
    try:
        k = parse_time(g["date"]).astimezone()
    except (ValueError, AttributeError):
        return ""
    return f"{k:%a} {k.hour % 12 or 12}:{k:%M} {'AM' if k.hour < 12 else 'PM'}"


# ---------------- matching a post to a game ----------------

_RX = {}


def term_regex(key):
    # "i:" keys match any case, "c:" keys (abbreviations) only in capitals; whole words only
    if key not in _RX:
        flags = re.IGNORECASE if key.startswith("i:") else 0
        _RX[key] = re.compile(r"(?<!\w)" + re.escape(key[2:]) + r"(?!\w)", flags)
    return _RX[key]


def aliases_for(t):
    out = []
    for k, names in CFB_TEAM_ALIASES.items():
        if str(k).upper() in (t["abbr"].upper(), t["id"]):
            out += [names] if isinstance(names, str) else list(names)
    return out


def team_terms(t):
    raw = t["raw"]
    names = {raw.get(k) for k in ("location", "displayName", "shortDisplayName", "name", "nickname")}
    squashed = re.sub(r"[^A-Za-z0-9]", "", raw.get("location") or "")   # #OhioState
    if len(squashed) >= 6:
        names.add(squashed)
    names.update(aliases_for(t))
    keys = {"i:" + n.strip().lower() for n in names if n and len(n.strip()) >= 3}
    if len(t["abbr"]) >= 3:
        keys.add("c:" + t["abbr"])
    return keys


def index_terms(games):
    """Each team keeps only the names no other team on the scoreboard shares
    ("Tigers" is dropped, "LSU" and "Clemson" stay)."""
    owners = {}
    for g in games.values():
        for t in (g["away"], g["home"]):
            t["terms"] = team_terms(t)
            for k in t["terms"]:
                owners.setdefault(k, set()).add(t["id"])
    for g in games.values():
        for t in (g["away"], g["home"]):
            t["unique"] = [k for k in t["terms"] if len(owners[k]) == 1]


def team_score(text, t):
    # Longest name found wins, so "Washington State" beats "Washington"
    return max((len(k) - 2 for k in t.get("unique", []) if term_regex(k).search(text)), default=0)


def match_game(text, games):
    scored = [(max(team_score(text, g["away"]), team_score(text, g["home"])), g) for g in games.values()]
    best = max((s for s, _ in scored), default=0)
    if best < 3:
        return None
    top = [g for s, g in scored if s == best]
    if len(top) > 1:
        top = [g for g in top if g["state"] == "in"]
    return top[0] if len(top) == 1 else None


def clean_text(text):
    text = html.unescape(text or "")
    text = re.sub(r"https?://\S+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def pick_video(m):
    """The clip URL to give PiPup, or None if this media isn't a usable video."""
    if not m or m.get("type") != "video":
        return None
    seconds = (m.get("duration_ms") or 0) / 1000
    if seconds > CFB_MAX_CLIP_SECONDS:
        return None
    variants = m.get("variants") or []
    mp4 = [v for v in variants if v.get("content_type") == "video/mp4" and v.get("url")]
    if mp4:
        rate = lambda v: v.get("bit_rate") or 0  # noqa: E731
        fits = [v for v in mp4 if rate(v) <= CFB_MAX_BITRATE]
        best = max(fits, key=rate) if fits else min(mp4, key=rate)
        url, label = best["url"], quality(best["url"], best.get("bit_rate"))
    else:
        hls = [v for v in variants if "mpegurl" in (v.get("content_type") or "").lower() and v.get("url")]
        if not hls:
            return None
        url, label = hls[0]["url"], "HLS"
    return {"url": url, "poster": m.get("preview_image_url"), "seconds": seconds or None, "quality": label}


# ---------------- X API ----------------

class XError(Exception):
    pass


class XAuthError(XError):
    pass


class XClient:
    def __init__(self, token):
        self.http = requests.Session()
        self.http.headers["Authorization"] = f"Bearer {token}"
        self.posts_read = 0
        self.wait_until = 0

    def get(self, path, params):
        r = self.http.get(X_API + path, params=params, timeout=20)
        if r.status_code == 429:
            self.wait_until = int(r.headers.get("x-rate-limit-reset") or time.time() + 60)
            raise XError(f"X rate limit hit; pausing until {time.strftime('%H:%M:%S', time.localtime(self.wait_until))}")
        if r.status_code in (401, 403):
            raise XAuthError(f"X rejected the request ({r.status_code}): {r.text[:300]}")
        if r.status_code >= 400:
            raise XError(f"X API error {r.status_code}: {r.text[:300]}")
        return r.json()

    def lookup(self, handles):
        """{user id: handle} for the handles that exist (one-time cost at startup)."""
        found = {}
        for i in range(0, len(handles), 100):
            data = self.get("/users/by", {"usernames": ",".join(handles[i:i + 100])})
            for u in data.get("data", []):
                found[u["id"]] = u["username"]
            for e in data.get("errors", []):
                print(f"X account not found or unavailable: @{e.get('value')}")
        return found

    def search(self, query, since_id=None, start_time=None):
        params = {"query": query, "max_results": 100,
                  "expansions": "attachments.media_keys",
                  "media.fields": "type,duration_ms,preview_image_url,variants",
                  "tweet.fields": "created_at,author_id,attachments"}
        if since_id:
            params["since_id"] = since_id
        elif start_time:
            params["start_time"] = start_time
        posts, media, newest = [], {}, None
        for _ in range(5):
            data = self.get("/tweets/search/recent", params)
            meta = data.get("meta", {})
            newest = newest or meta.get("newest_id")
            posts += data.get("data", [])
            for m in (data.get("includes") or {}).get("media", []):
                media[m["media_key"]] = m
            if not meta.get("next_token"):
                break
            params["next_token"] = meta["next_token"]
        self.posts_read += len(posts)
        return posts, media, newest


def build_queries(handles):
    """Recent search queries covering every handle, each under X's length limit."""
    def q(hs):
        return "(" + " OR ".join(f"from:{h}" for h in hs) + ") has:videos -is:retweet -is:reply"
    queries, cur = [], []
    for h in handles:
        if cur and len(q(cur + [h])) > MAX_QUERY:
            queries.append(q(cur))
            cur = []
        cur.append(h)
    if cur:
        queries.append(q(cur))
    return queries


# ---------------- PiPup ----------------

def post_card(card):
    try:
        requests.post(f"http://{TV_IP}:7979/notify", json=card, timeout=5)
        return True
    except requests.RequestException as e:
        log(f"Could not reach TV at {TV_IP}: {e}")
        return False


def show_clip(url, poster, seconds, title="", message="", close_callback=None):
    video = {"uri": url, "width": CFB_WIDTH, "height": CFB_HEIGHT, "muted": CFB_MUTED}
    if poster:
        video["poster"] = poster
    decoder = str(CFB_DECODER).lower()
    if decoder in ("hardware", "software"):
        video["softwareDecoder"] = decoder == "software"
    card = {"id": CARD_ID, "duration": seconds, "position": CFB_POSITION, "padding": 8,
            "dismissScreensaver": False, "media": {"video": video}}
    if CFB_SHOW_CAPTION:
        size = int(CFB_CAPTION_TEXT_SIZE)
        card.update({"title": title, "message": message, "titleSize": size, "messageSize": max(8, size - 2)})
    if close_callback:
        # PiPup closes the popup itself when ✕ is pressed and tells us, so the replay can be skipped.
        # Without buttonSize PiPup draws its big classic button, which widens the caption row and
        # with it the whole popup and the video.
        card.update({"buttons": [{"id": "close", "label": "✕"}], "buttonsInline": True,
                     "buttonSize": int(CFB_CLOSE_BUTTON_SIZE), "buttonCornerRadius": 4,
                     "callback": close_callback})
    return post_card(card)


def clip_on_screen():
    """Whether our popup is still showing (False after ✕ or BACK), or None if the TV doesn't say."""
    try:
        popups = requests.get(f"http://{TV_IP}:7979/state", timeout=3).json().get("popups")
    except (requests.RequestException, ValueError, AttributeError):
        return None
    if not isinstance(popups, list):
        return None
    return any(p == CARD_ID or (isinstance(p, dict) and p.get("id") == CARD_ID) for p in popups)


def hide():
    try:
        requests.post(f"http://{TV_IP}:7979/cancel", params={"id": CARD_ID}, timeout=5)
    except requests.RequestException:
        pass


def show_prompt(cur, seconds, callback):
    """The "Watch replay" button. While it's up, the remote's OK and arrow keys go to it, not the TV app;
    BACK closes it."""
    # Kept small on purpose: one row with a thumbnail icon, two short lines of text and the buttons
    # at the end of the row. PiPup sizes the card to its content.
    size = int(CFB_PROMPT_TEXT_SIZE)
    position = CFB_PROMPT_POSITION if CFB_PROMPT_POSITION is not None else CFB_POSITION
    card = {"id": CARD_ID, "duration": seconds, "position": position, "dismissScreensaver": False,
            "title": cur.get("short_title") or cur["title"] or "Replay available",
            "message": cur["message"], "titleSize": size, "messageSize": max(8, size - 2),
            "padding": 10, "cornerRadius": 8, "showProgress": True, "callback": callback,
            "buttonsInline": True, "buttonSize": max(8, size - 1),
            "buttons": [{"id": "watch", "label": "Watch replay"}, {"id": "skip", "label": "✕"}]}
    if CFB_PROMPT_THUMBNAIL and cur.get("poster"):
        card.update({"icon": cur["poster"], "iconPosition": "left", "iconWidth": 80})
    return post_card(card)


def detect_pc_ip():
    # The address this PC uses to reach the TV (no traffic is actually sent)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((TV_IP, 7979))
        return s.getsockname()[0]
    finally:
        s.close()


class ButtonServer:
    """Receives PiPup's button presses: POST /pipup/<token> with {"button": "watch", ...}."""

    def __init__(self, port, pc_ip=None):
        self.presses = queue.Queue()
        self.wake = threading.Event()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                try:
                    body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                    data = json.loads(body or b"{}")
                except ValueError:
                    data = {}
                m = re.fullmatch(r"/pipup/([\w-]+)", self.path)
                if m and isinstance(data, dict):
                    server.presses.put((m.group(1), str(data.get("button", ""))))
                    server.wake.set()
                self.send_response(204 if m else 404)
                self.end_headers()

            def log_message(self, *args):
                pass

        pc_ip = pc_ip or detect_pc_ip()
        self.http = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        self.base = f"http://{pc_ip}:{self.http.server_address[1]}/pipup/"
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def new_callback(self):
        """A fresh single-use callback URL and its token."""
        token = secrets.token_urlsafe(12)
        return self.base + token, token

    def pressed(self, token):
        """The button pressed on the prompt with this token, if any (presses for older prompts are dropped)."""
        found = None
        while True:
            try:
                t, button = self.presses.get_nowait()
            except queue.Empty:
                return found
            if t == token:
                found = button

    def close(self):
        self.http.shutdown()


# ---------------- the watcher ----------------

class Replays:
    def __init__(self, x=None, selected=None, buttons=None, ask_first=True, close_button=True):
        self.x = x                    # XClient for clips from X, or None for clips from ESPN
        self.selected = selected      # list of ESPN ids, or None for ranked mode
        self.buttons = buttons        # ButtonServer for button presses, or None for no buttons
        self.ask_first = bool(buttons) and ask_first          # "Watch replay" card before each clip
        self.close_button = bool(buttons) and close_button    # ✕ on the playing clip
        self.prompt = None            # the clip being offered with a "Watch replay" button
        self.espn_seen = {}           # game id -> ESPN video ids already seen
        self.games = {}
        self.ended_at = {}
        self.warned = set()
        self.queries = None
        self.handles = {}             # X user id -> handle
        self.team_accounts = {}       # X user id -> ESPN team abbreviation / id
        self.since = {}
        self.last_x_poll = 0
        self.queue = []
        self.busy_until = 0
        self.current = None           # the clip on screen, while it still has plays left
        self.seen_media = set()
        self.last_clip = {}           # game id -> time of the last clip we kept
        self.last_status = None

    # --- ESPN ---
    def refresh_games(self, now):
        try:
            games = fetch_scoreboard()
        except (requests.RequestException, ValueError) as e:
            log(f"ESPN error: {e}")
            return
        for gid in self.selected or []:
            if gid in games:
                continue
            try:
                games[gid] = fetch_game(gid)
            except (requests.RequestException, ValueError) as e:
                if gid not in self.warned:
                    log(f"Can't load ESPN game {gid}: {e}")
                    self.warned.add(gid)
        for gid, g in games.items():
            prev = self.games.get(gid)
            if g["state"] == "post" and prev and prev["state"] == "in":
                self.ended_at[gid] = now
        index_terms(games)
        self.games = games
        self.print_status(now)

    def watched(self):
        if self.selected is not None:
            return [gid for gid in self.selected if gid in self.games]
        return [gid for gid, g in self.games.items() if is_ranked(g)]

    def active(self, now):
        grace = CFB_AFTER_FINAL_MINUTES * 60
        return {gid for gid in self.watched()
                if self.games[gid]["state"] == "in"
                or now - self.ended_at.get(gid, -math.inf) < grace}

    def print_status(self, now):
        live = sorted(self.active(now))
        status = (len(self.watched()), tuple(live))
        if status == self.last_status:
            return
        self.last_status = status
        if live:
            names = [matchup(self.games[g]) + (" (final)" if self.games[g]["state"] == "post" else "") for g in live]
            log(f"Watching {games_text(status[0])}; live now: " + ", ".join(names))
        else:
            log(f"Watching {games_text(status[0])}; none live - waiting")

    def poll(self, now):
        if self.x:
            self.poll_x(now)
        else:
            self.poll_espn(now)

    def enqueue(self, clip, game, text, credit, now):
        self.queue.append({**clip, "game": game["id"], "text": text, "credit": credit, "found": now})
        log(f"Clip found: {matchup(game)} - {credit}: {text[:70]}")

    # --- clips from ESPN ---
    def poll_espn(self, now):
        for gid in sorted(self.active(now)):
            game = self.games[gid]
            try:
                videos = fetch_summary(gid).get("videos") or []
            except (requests.RequestException, ValueError) as e:
                log(f"ESPN error loading clips for {matchup(game)}: {e}")
                continue
            first_look = gid not in self.espn_seen
            seen = self.espn_seen.setdefault(gid, set())
            new = []
            for v in videos:
                vid = str(v.get("id") or v.get("headline") or "")
                if vid and vid not in seen:
                    seen.add(vid)
                    new.append(v)
            if first_look:
                # Clips posted before we started watching this game (previews, earlier plays) aren't replayed
                if new:
                    debug(f"{matchup(game)}: {len(new)} earlier ESPN clips skipped")
                continue
            dates = [v.get("originalPublishDate") or v.get("published") or v.get("lastModified") for v in new]
            new = ([v for _, v in sorted(zip(dates, new), key=lambda p: p[0])] if all(dates)
                   else list(reversed(new)))           # ESPN lists newest first
            for v in new:
                text = clean_text(v.get("headline") or v.get("description"))
                clip = espn_clip(v)
                if clip:
                    self.enqueue(clip, game, text, "ESPN", now)
                else:
                    debug(f"skip ESPN clip (too long or no playable link): {text[:60]}")

    # --- clips from X ---
    def setup_x(self):
        handles = list(dict.fromkeys(list(CFB_X_ACCOUNTS) + list(CFB_TEAM_ACCOUNTS)))
        self.handles = self.x.lookup(handles)
        by_handle = {h.lower(): uid for uid, h in self.handles.items()}
        self.team_accounts = {by_handle[h.lower()]: str(team)
                              for h, team in CFB_TEAM_ACCOUNTS.items() if h.lower() in by_handle}
        self.queries = build_queries(list(self.handles.values()))
        if not self.queries:
            raise XAuthError("none of the X accounts in CFB_X_ACCOUNTS / CFB_TEAM_ACCOUNTS were found")
        log(f"Checking {len(self.handles)} X accounts for clips")

    def poll_x(self, now):
        active = self.active(now)
        if not active or now < self.x.wait_until:
            return
        if self.queries is None:
            self.setup_x()
        start = None
        if now - self.last_x_poll > 3 * CFB_POLL_SECONDS:
            # Fresh start (or back after a quiet spell): only look at the last poll interval,
            # so we don't pay for hours of posts nobody will watch
            self.since.clear()
            start = iso_utc(now - CFB_POLL_SECONDS)
        self.last_x_poll = now
        posts, media = [], {}
        for q in self.queries:
            p, m, newest = self.x.search(q, since_id=self.since.get(q), start_time=start)
            posts += p
            media.update(m)
            if newest:
                self.since[q] = newest
        for post in sorted(posts, key=lambda p: p.get("created_at", "")):
            self.consider(post, media, active, now)

    def game_for_team(self, team):
        team = team.upper()
        mine = [g for g in self.games.values()
                if team in (g["away"]["abbr"].upper(), g["home"]["abbr"].upper(), g["away"]["id"], g["home"]["id"])]
        live = [g for g in mine if g["state"] == "in"]
        return (live or mine or [None])[0]

    def consider(self, post, media, active, now):
        clip = key = None
        for key in (post.get("attachments") or {}).get("media_keys", []):
            clip = pick_video(media.get(key))
            if clip:
                break
        text = clean_text(post.get("text"))
        handle = self.handles.get(post.get("author_id"), "?")
        if not clip:
            debug(f"skip @{handle}: no usable video (too long or not a video) - {text[:60]}")
            return
        if key in self.seen_media:
            return
        self.seen_media.add(key)

        game = None
        team = self.team_accounts.get(post.get("author_id"))
        if team:
            game = self.game_for_team(team)
        if game is None:
            game = match_game(text, self.games)
        if game is None:
            debug(f"skip @{handle}: couldn't tell which game - {text[:60]}")
            return
        if game["id"] not in active:
            debug(f"skip @{handle}: {matchup(game)} isn't a watched live game")
            return
        created = parse_time(post["created_at"]).timestamp() if post.get("created_at") else now
        last = self.last_clip.get(game["id"])
        if last is not None and created - last < CFB_GAME_COOLDOWN_SECONDS:
            debug(f"skip @{handle}: another clip from {matchup(game)} just played (likely the same play)")
            return
        self.last_clip[game["id"]] = created
        self.enqueue(clip, game, text, f"@{handle}", now)

    # --- TV ---
    def play_next(self, now):
        cur = self.current
        if cur and cur.get("token") and self.buttons.pressed(cur["token"]) == "close":
            self.closed(cur, now)
            cur = None
        if cur and now >= cur["next_at"]:
            if cur["play"] < cur["plays"]:
                # Closed with BACK? PiPup doesn't report that, but its popup list does
                if clip_on_screen() is False:
                    self.closed(cur, now)
                else:
                    self.replay(cur, now)
                    return
            self.current = None
        if self.prompt:
            offer = self.prompt
            button = self.buttons.pressed(offer["token"])
            if button == "watch":
                self.prompt = None
                self.start(offer, now)
                return
            if button or now >= offer["expires"]:
                # Skip pressed, or ignored / closed with BACK (PiPup doesn't report those)
                self.prompt = None
                log(f"Skipped: {offer['label']}" if button else f"Not watched: {offer['label']}")
            else:
                return
        if now < self.busy_until:
            return
        while self.queue:
            clip = self.queue.pop(0)
            if now - clip["found"] > CFB_MAX_CLIP_AGE_MINUTES * 60:
                log(f"Dropped a clip that waited too long ({clip['credit']})")
                continue
            game = self.games.get(clip["game"])
            text = clip["text"] if len(clip["text"]) <= 110 else clip["text"][:107] + "..."
            cur = {**clip, "title": score_line(game) if game else "",
                   "short_title": score_short(game) if game else "",
                   "message": f"{text}  - {clip['credit']}" if text else clip["credit"],
                   "label": matchup(game) if game else "clip"}
            if self.ask_first:
                self.ask(cur, now)
            else:
                self.start(cur, now)
            return

    def closed(self, cur, now):
        # Closed on the TV: skip any replays and let the next clip come up now
        self.current = None
        self.busy_until = now
        log(f"Closed: {cur['label']}")

    def ask(self, cur, now):
        url, token = self.buttons.new_callback()
        waiting = f"  (+{len(self.queue)} waiting)" if self.queue else ""
        offer = dict(cur, message=shorten(cur["text"] or cur["credit"], 60) + waiting)
        if show_prompt(offer, CFB_PROMPT_SECONDS, url):
            self.prompt = {**cur, "token": token, "expires": now + CFB_PROMPT_SECONDS + 1}
            log(f"Replay ready: {cur['label']} - {cur['text'][:60]}  (waiting for the button)")

    def start(self, cur, now):
        # One play = load time + the clip; the popup itself stays up a second longer so a
        # replay can take over before it closes
        play_time = math.ceil(cur["seconds"] or CFB_MAX_CLIP_SECONDS) + CFB_LOAD_SECONDS
        plays = max(1, int(CFB_PLAY_COUNT))
        cur = {**cur, "play": 1, "plays": plays, "play_time": play_time, "next_at": now + play_time,
               "close_url": None, "token": None}
        if self.close_button:
            cur["close_url"], cur["token"] = self.buttons.new_callback()
        if self.show(cur):
            self.current = cur
            self.busy_until = now + play_time * plays + 1
            log(f"Playing: {cur['label']} from {cur['credit']} [{cur.get('quality', '?')}]  "
                f"({len(self.queue)} more queued)")

    def sleep(self, seconds):
        # Wakes early when a button is pressed, so the clip starts right away
        if self.buttons:
            self.buttons.wake.wait(seconds)
            self.buttons.wake.clear()
        else:
            time.sleep(seconds)

    def show(self, cur):
        # A short caption, so the caption row (and the ✕ at its end) stays narrower than the video
        title = cur.get("short_title") or cur["title"]
        if cur["plays"] > 1:
            title = f"{title}  ({cur['play']}/{cur['plays']})".strip()
        message = f"{shorten(cur['text'], 60)} - {cur['credit']}" if cur.get("text") else cur["credit"]
        # Keep the popup up a few seconds past the replay time, so a late loop tick still finds it
        # on screen (and doesn't mistake it for closed); the replay replaces it anyway
        linger = 6 if cur["play"] < cur["plays"] else 1
        return show_clip(cur["url"], cur["poster"], cur["play_time"] + linger, title, message,
                         cur.get("close_url"))

    def replay(self, cur, now):
        # PiPup keeps a video playing when the same popup is sent again, so close it and
        # open it fresh to start the clip from the top
        cur["play"] += 1
        cur["next_at"] = now + cur["play_time"]
        hide()
        if self.show(cur):
            debug(f"Replaying clip ({cur['play']}/{cur['plays']})")


# ---------------- scoreboard (top center, ranked games) ----------------

SCOREBOARD_ID = "cfb-scoreboard"
PERIODS = {1: "1ST", 2: "2ND", 3: "3RD", 4: "4TH", 5: "OT"}
LEADERS = (("passingYards", "PASS"), ("rushingYards", "RUSH"), ("receivingYards", "REC"))
EXTRA_STATS = (("completionAttempts", "COMP"), ("thirdDownEff", "3RD DN"), ("firstDowns", "1ST DN"),
               ("possessionTime", "POSS"), ("totalPenaltiesYards", "PEN"))


def period_name(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    return PERIODS.get(n) or (f"{n - 4}OT" if n > 5 else "")


def clock_time(k):
    return f"{k.hour % 12 or 12}:{k:%M} {'AM' if k.hour < 12 else 'PM'}"


def game_clock(status, date):
    """[small label, big label] for the middle of the scoreboard, e.g. ["2ND", "5:32"] or ["FINAL", ""]."""
    st = status.get("type") or {}
    state, name = st.get("state", "pre"), st.get("name", "")
    short = (st.get("shortDetail") or st.get("detail") or "").strip()
    if state == "pre":
        try:
            k = parse_time(date).astimezone()
        except (ValueError, AttributeError):
            return [short.upper(), ""]
        return [f"{k:%a}".upper(), "TBD" if "TBD" in short.upper() else clock_time(k)]
    if state == "post":
        return [short.upper() or "FINAL", ""]
    period = period_name(status.get("period"))
    if name == "STATUS_HALFTIME":
        return ["HALF", ""]
    if name == "STATUS_END_PERIOD":
        return ["END", period]
    if "DELAY" in name or "delay" in short.lower():
        return ["DELAY", period]
    return [period, status.get("displayClock") or ""]


def board_team(c, side, sit, state):
    base = team_from(c)
    t = base["raw"]
    recs = {}
    for r in c.get("records") or c.get("record") or []:
        if isinstance(r, dict) and r.get("summary"):
            recs.setdefault((r.get("type") or r.get("name") or "").lower(), r["summary"])
    lines = []
    for ls in c.get("linescores") or []:
        try:
            lines.append(str(int(float(ls.get("value", ls.get("displayValue"))))))
        except (TypeError, ValueError, AttributeError):
            pass
    timeouts = sit.get(f"{side}Timeouts")
    logo = t.get("logo") or ((t.get("logos") or [{}])[0] or {}).get("href") or ""
    return {
        "id": base["id"], "abbr": base["abbr"] or base["short"][:4].upper(), "name": base["short"],
        "rank": base["rank"], "score": base["score"] or "0",
        "record": recs.get("total") or recs.get("overall") or next(iter(recs.values()), ""),
        "conf": recs.get("vsconf") or "",
        "venue_rec": recs.get("home") if side == "home" else (recs.get("road") or recs.get("away") or ""),
        "color": t.get("color") or "", "alt": t.get("alternateColor") or "", "logo": logo,
        "timeouts": timeouts if isinstance(timeouts, int) and state == "in" else None,
        "poss": state == "in" and bool(base["id"]) and str(sit.get("possession") or "") == base["id"],
        "winner": bool(c.get("winner")), "lines": lines,
    }


def board_game(ev):
    """One scoreboard event with everything the scoreboard shows that ESPN's scoreboard has."""
    comp = (ev.get("competitions") or [{}])[0]
    status = ev.get("status") or comp.get("status") or {}
    st = status.get("type") or {}
    state = st.get("state", "pre")
    sit = comp.get("situation") or {} if state == "in" else {}
    sides = {c.get("homeAway"): c for c in comp.get("competitors", [])}
    if "away" not in sides or "home" not in sides:
        return None
    nets = [n for b in comp.get("broadcasts") or [] for n in b.get("names") or [] if n]
    nets = nets or [(b.get("media") or {}).get("shortName") for b in comp.get("geoBroadcasts") or []]
    nets = [n for n in nets if n]
    odds = (comp.get("odds") or [{}])[0] or {}
    notes = [n.get("headline") for n in comp.get("notes") or [] if n.get("headline")]
    date = ev.get("date") or comp.get("date") or ""
    return {
        "id": str(ev.get("id")), "state": state, "date": date, "status_name": st.get("name", ""),
        "detail": st.get("shortDetail") or st.get("detail") or "",
        "away": board_team(sides["away"], "away", sit, state),
        "home": board_team(sides["home"], "home", sit, state),
        "clock": game_clock(status, date), "network": nets[0] if nets else "",
        "situation": sit.get("downDistanceText") or "", "redzone": bool(sit.get("isRedZone")),
        "last_play": (sit.get("lastPlay") or {}).get("text") or "",
        "line": odds.get("details") or "", "ou": odds.get("overUnder"),
        "venue": (comp.get("venue") or {}).get("fullName") or "", "note": notes[0] if notes else "",
    }


def kick_ts(g):
    try:
        return parse_time(g["date"]).timestamp()
    except (ValueError, AttributeError):
        return 0


def pick_board_games(games, mode, now):
    """The ranked games to rotate through, live ones first."""
    ranked = [g for g in games if is_ranked(g, CFB_SCOREBOARD_RANK_CUTOFF)]
    live = sorted((g for g in ranked if g["state"] == "in"), key=kick_ts)
    pre = sorted((g for g in ranked if g["state"] == "pre"), key=kick_ts)
    post = sorted((g for g in ranked if g["state"] == "post"), key=kick_ts, reverse=True)
    if mode == "live":
        return live
    if mode == "auto":
        soon = [g for g in pre if kick_ts(g) - now < 90 * 60]           # kicks off within 90 minutes
        recent = [g for g in post if now - kick_ts(g) < 4.5 * 3600]     # finished in about the last hour
        if live or soon or recent:
            return live + soon + recent
    return live + pre + post


def summary_details(s, g):
    """Team stats, top performers, win probability and the line from a game's ESPN summary."""
    ids = {g["away"]["id"]: "away", g["home"]["id"]: "home"}
    out = {"stats": {"away": {}, "home": {}}, "leaders": {"away": {}, "home": {}},
           "winp": None, "line": "", "ou": None}
    for t in (s.get("boxscore") or {}).get("teams") or []:
        side = ids.get(str((t.get("team") or {}).get("id", ""))) or t.get("homeAway")
        if side in out["stats"]:
            for stat in t.get("statistics") or []:
                if stat.get("name") and stat.get("displayValue") not in (None, ""):
                    out["stats"][side][stat["name"]] = str(stat["displayValue"])
    for t in s.get("leaders") or []:
        side = ids.get(str((t.get("team") or {}).get("id", "")))
        if not side:
            continue
        for cat in t.get("leaders") or []:
            top = (cat.get("leaders") or [None])[0]
            if top:
                who = (top.get("athlete") or {}).get("shortName") or (top.get("athlete") or {}).get("displayName") or ""
                out["leaders"][side][cat.get("name")] = f"{who}  {top.get('displayValue') or ''}".strip()
    try:
        out["winp"] = float((s.get("winprobability") or [])[-1]["homeWinPercentage"]) * 100
    except (IndexError, KeyError, TypeError, ValueError):
        try:   # before kickoff: ESPN's FPI projection
            out["winp"] = float(((s.get("predictor") or {}).get("homeTeam") or {}).get("gameProjection"))
        except (TypeError, ValueError):
            pass
    pick = (s.get("pickcenter") or [{}])[0] or {}
    out["line"], out["ou"] = pick.get("details") or "", pick.get("overUnder")
    return out


def board_view(g, d=None):
    """What the scoreboard page draws for one game."""
    d = d or {}
    a, h, state = g["away"], g["home"], g["state"]
    stats = d.get("stats") or {"away": {}, "home": {}}
    leaders = d.get("leaders") or {"away": {}, "home": {}}
    winp = d.get("winp")

    def team(t):
        out = {k: t[k] for k in ("abbr", "name", "score", "record", "color", "alt", "logo",
                                 "timeouts", "poss", "winner")}
        out["rank"] = t["rank"] if 1 <= t["rank"] <= TOP_25 else None
        return out

    def both(label, key_a, key_h=None, kind=None, **extra):
        cell = {"label": label, "away": key_a, "home": key_h, **extra}
        if kind:
            cell["kind"] = kind
        return cell

    ticker = [{"label": label, "away": leaders["away"].get(key, ""), "home": leaders["home"].get(key, "")}
              for key, label in LEADERS if leaders["away"].get(key) or leaders["home"].get(key)]
    view = {"id": g["id"], "state": state, "clock": g["clock"], "network": g["network"],
            "away": team(a), "home": team(h), "redzone": False, "poss": "", "situation": "", "winp": None}
    line, ou = g["line"] or d.get("line") or "", g["ou"] if g["ou"] is not None else d.get("ou")

    if state == "pre":
        pct = (lambda p: f"{round(p)}%") if winp is not None else (lambda p: "")
        cells = [both("REC", a["record"], h["record"]), both("CONF", a["conf"], h["conf"]),
                 both("AWAY", a["venue_rec"], h["venue_rec"], home_label="HOME"),
                 both("FPI", pct(100 - (winp or 0)), pct(winp or 0))]
        view["cells"] = [c for c in cells if c["away"] or c["home"]]
        view["ticker"] = ticker            # before kickoff ESPN's leaders are season totals
        view["situation"] = " · ".join(x for x in (g["note"], line and f"Line {line}",
                                                   ou is not None and ou != "" and f"O/U {ou}", g["venue"]) if x)
        view["winp"] = winp
        view["winp_label"] = "FPI"
    else:
        sa, sh = stats["away"], stats["home"]
        view["cells"] = [both("PASS", sa.get("netPassingYards"), sh.get("netPassingYards"), "num"),
                         both("RUSH", sa.get("rushingYards"), sh.get("rushingYards"), "num"),
                         both("YDS", sa.get("totalYards"), sh.get("totalYards"), "num"),
                         both("TO", sa.get("turnovers"), sh.get("turnovers"), "to")]
        ticker += [{"label": label, "away": sa.get(key, ""), "home": sh.get(key, "")}
                   for key, label in EXTRA_STATS if sa.get(key) or sh.get(key)]
        if a["lines"] or h["lines"]:
            ticker.append({"label": "BY QTR", "away": "  ".join(a["lines"]), "home": "  ".join(h["lines"])})
        view["ticker"] = ticker
        if state == "in":
            if g["status_name"] == "STATUS_HALFTIME":
                view["situation"] = "HALFTIME" + (f" · {g['venue']}" if g["venue"] else "")
            elif g["situation"]:
                view["situation"] = g["situation"]
                view["poss"] = a["abbr"] if a["poss"] else h["abbr"] if h["poss"] else ""
                view["redzone"] = g["redzone"]
            else:
                view["situation"] = g["last_play"] or g["detail"]
            view["winp"] = winp
            view["winp_label"] = "WIN"
        else:
            view["situation"] = " · ".join(x for x in (g["note"], g["venue"]) if x) or g["detail"]
    if view["winp"] is not None:
        view["winp"] = max(0.0, min(100.0, round(view["winp"], 1)))
    return view


def show_scoreboard(url):
    card = {"id": SCOREBOARD_ID, "duration": max(90, int(CFB_SCOREBOARD_FETCH_SECONDS) * 3),
            "position": CFB_SCOREBOARD_POSITION, "padding": 0, "backgroundColor": "#00000000",
            "dismissScreensaver": False,
            "media": {"web": {"uri": url, "width": CFB_SCOREBOARD_WIDTH, "height": CFB_SCOREBOARD_HEIGHT,
                              "transparent": True}}}
    return post_card(card)


def hide_scoreboard():
    try:
        requests.post(f"http://{TV_IP}:7979/cancel", params={"id": SCOREBOARD_ID}, timeout=5)
    except requests.RequestException:
        pass


SCOREBOARD_PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { --op: .88; }
  html, body { margin: 0; height: 100%; background: transparent; overflow: hidden; }
  body { font-family: Roboto, "Segoe UI", Arial, sans-serif; color: #f2f4f7; font-size: 24px;
         font-variant-numeric: tabular-nums; }
  #sb { position: relative; height: 100%; width: 100%; }
  #stage { display: flex; align-items: center; justify-content: center; height: 100%; transition: opacity .4s ease; }
  #stage.fade { opacity: 0; }
  @keyframes pulse { 50% { opacity: .25; } }

  /* ---- center: the scoreboard ---- */
  #pod { position: relative; flex: 0 0 auto; width: 22.5em; height: 3.62em; box-sizing: border-box;
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
        background: var(--c); display: flex; align-items: center; justify-content: center; }
  .lg span { font-size: .7em; font-weight: 800; }
  .lg img { position: absolute; left: 0; top: 0; width: 100%; height: 100%; object-fit: contain;
            filter: drop-shadow(0 0 .06em rgba(0,0,0,.9)); }
  .lg.ok { background: none; } .lg.ok span { visibility: hidden; }
  .nmcol { flex: 1 1 auto; min-width: 0; margin: 0 .3em 0 .38em; line-height: 1.05; }
  .team.h .nmcol { margin: 0 .38em 0 .3em; }
  .team.h .nmcol { text-align: right; }
  .ab { font-size: .98em; font-weight: 800; letter-spacing: .02em; white-space: nowrap; overflow: hidden;
        text-overflow: ellipsis; }
  .rk { font-style: normal; font-size: .56em; font-weight: 700; color: #b4bccb; margin-right: .22em;
        vertical-align: .32em; }
  .rec { font-size: .47em; color: #9aa3b2; letter-spacing: .05em; white-space: nowrap; margin-top: .2em; }
  .tos { display: inline-block; margin: 0 .45em; vertical-align: .12em; }
  .tos i { display: inline-block; width: .8em; height: .26em; margin-right: .2em; border-radius: .1em;
           background: rgba(255,255,255,.18); }
  .tos i.on { background: #f5c542; }
  .pb { position: absolute; right: -.28em; bottom: -.1em; width: .82em; height: .5em; display: flex;
        filter: drop-shadow(0 0 .08em rgba(0,0,0,.9)); }
  .team.h .pb { right: auto; left: -.28em; }
  .pb svg { width: 100%; height: 100%; }
  .sc { flex: 0 0 auto; min-width: 1.3em; text-align: center; font-size: 1.55em; font-weight: 800;
        letter-spacing: -.02em; }
  .sc:empty { display: none; }
  .team.dim .sc, .team.dim .ab { color: #7a8292; }
  .team.dim .lg { opacity: .5; }

  .mid { flex: 0 0 4.3em; display: flex; flex-direction: column; align-items: center; justify-content: center;
         background: rgba(0,0,0,.38); border-left: 1px solid rgba(255,255,255,.07);
         border-right: 1px solid rgba(255,255,255,.07); }
  .clk { display: flex; align-items: center; white-space: nowrap; }
  .clk b { font-size: .6em; font-weight: 800; letter-spacing: .07em; color: #f5c542; margin-right: .45em; }
  .clk span { font-size: .9em; font-weight: 700; }
  .clk.solo b { font-size: .78em; color: #f2f4f7; margin: 0; letter-spacing: .05em; }
  .clk.pre b { color: #aeb6c4; } .clk.pre span { font-size: .7em; }
  .ld { display: inline-block; width: .3em; height: .3em; border-radius: 50%; background: #ff4d4d;
        margin-right: .3em; animation: pulse 1.4s infinite; }
  .net { margin-top: .22em; font-size: .42em; font-weight: 800; letter-spacing: .1em; padding: .12em .6em;
         border-radius: .3em; background: #2a3140; color: #fff; border: 1px solid rgba(255,255,255,.28);
         white-space: nowrap; max-width: 9em; overflow: hidden; text-overflow: ellipsis; }

  .strip { flex: 0 0 1em; display: flex; align-items: center; padding: 0 .55em; min-width: 0;
           background: rgba(255,255,255,.05); border-top: 1px solid rgba(255,255,255,.07); }
  .strip > span { font-size: .5em; }
  .strip.rz { background: linear-gradient(90deg, rgba(231,76,60,.55), rgba(231,76,60,.18)); }
  .cnt { flex: 0 0 auto; color: #6f7888; letter-spacing: .08em; }
  .sm { flex: 1 1 auto; min-width: 0; margin: 0 .8em; text-align: center; white-space: nowrap; overflow: hidden;
        text-overflow: ellipsis; color: #d7dce5; letter-spacing: .03em; }
  .pt { font-weight: 800; color: #fff; margin-right: .55em; }
  .pt svg { width: 1.25em; height: .72em; margin-right: .3em; vertical-align: -.06em; }
  .rzp { display: inline-block; margin-left: .6em; padding: 0 .5em; border-radius: .6em; background: #e74c3c;
         color: #fff; font-weight: 800; letter-spacing: .1em; animation: pulse 1.6s infinite; }
  .wpt { flex: 0 0 auto; color: #8f98a8; letter-spacing: .06em; } .wpt b { color: #fff; }
  .wpb { flex: 0 0 .13em; display: flex; background: rgba(255,255,255,.08); }
  .wpb i { display: block; height: 100%; transition: width .8s ease; }

  /* ---- the wings: each team's stats ---- */
  .wing { flex: 1 1 0; min-width: 0; height: 2.92em; box-sizing: border-box; position: relative;
          display: flex; flex-direction: column; justify-content: center; overflow: hidden; }
  .wing::before { content: ""; position: absolute; left: 0; right: 0; top: 0; height: .09em; background: var(--c); }
  .wing.l { margin-right: .18em; padding: 0 .55em 0 1.35em;
            background: linear-gradient(90deg, var(--cs) 0, rgba(10,12,18,var(--op)) 58%);
            clip-path: polygon(.95em 0, 100% 0, 100% 100%, 0 100%); }
  .wing.r { margin-left: .18em; padding: 0 1.35em 0 .55em;
            background: linear-gradient(270deg, var(--cs) 0, rgba(10,12,18,var(--op)) 58%);
            clip-path: polygon(0 0, calc(100% - .95em) 0, 100% 100%, 0 100%); }
  .cells { display: flex; }
  .wing.r .cells { flex-direction: row-reverse; }
  .cell { flex: 1 1 0; min-width: 0; padding: 0 .22em; display: flex; flex-direction: column; align-items: center; }
  .cell .lb { font-size: .4em; font-weight: 700; letter-spacing: .14em; color: #9aa3b2; white-space: nowrap; }
  .cell .v { font-size: .92em; font-weight: 800; line-height: 1.12; white-space: nowrap; }
  .cell.bad .v { color: #ff6b5b; }
  .cell.txt .v { font-size: .74em; line-height: 1.38; }
  .bar { display: flex; width: 76%; height: .12em; margin-top: .1em; border-radius: .1em; overflow: hidden;
         background: rgba(255,255,255,.12); }
  .bar i { display: block; height: 100%; background: var(--c); }
  .wing.r .bar { justify-content: flex-end; }
  .tick { margin-top: .32em; font-size: .46em; color: #cfd5df; white-space: nowrap; overflow: hidden;
          text-overflow: ellipsis; letter-spacing: .02em; transition: opacity .3s ease; }
  .tick.fade { opacity: 0; }
  .tick b { display: inline-block; color: #fff; font-weight: 800; letter-spacing: .1em; padding: 0 .45em;
            margin-right: .6em; border-radius: .25em; background: var(--cs); }
  .wing.r .tick { text-align: right; } .wing.r .tick b { margin: 0 0 0 .6em; }
  body.nowings .wing, body.nogames .wing { display: none; }
  #pod.empty { justify-content: center; align-items: center; color: #8f98a8; }
  #pod.empty span { font-size: .6em; letter-spacing: .06em; }
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
const BALL = '<svg viewBox="0 0 26 15"><ellipse cx="13" cy="7.5" rx="12" ry="6.6" fill="#b8692c" ' +
  'stroke="#7a3f12" stroke-width="1"/><path d="M8 7.5h10M10.5 5.6v3.8M13 5.6v3.8M15.5 5.6v3.8" ' +
  'stroke="#fff" stroke-width="1.1" stroke-linecap="round"/></svg>';
const NETS = [["ESPN", "#c8102e"], ["ABC", "#111"], ["SEC", "#0b3d91"], ["ACC", "#013ca6"],
  ["FOX", "#0a3a85"], ["FS1", "#0a3a85"], ["BTN", "#0c5aa6"], ["BIG TEN", "#0c5aa6"], ["CBS", "#1d4fa0"],
  ["NBC", "#4b2a86"], ["PEACOCK", "#111"], ["CW", "#2e7d32"], ["TNT", "#111"], ["TRUTV", "#111"]];
const TICK_MS = 4000;
let games = [], idx = 0, curId = null, lastKey = "", lastOk = Date.now();
let refresh = 5, cycle = 12, started = false, rotT = null, tickT = null;
const tickPos = {};

function rgb(hex) {
  hex = String(hex || "").replace("#", "");
  if (!/^[0-9a-f]{6}$/i.test(hex)) return null;
  return [0, 2, 4].map(i => parseInt(hex.slice(i, i + 2), 16));
}
function lum(c) {
  const f = v => { v /= 255; return v <= .03928 ? v / 12.92 : Math.pow((v + .055) / 1.055, 2.4); };
  return .2126 * f(c[0]) + .7152 * f(c[1]) + .0722 * f(c[2]);
}
// A team's color, swapped for its alternate (or a neutral) when it would vanish on the dark bar
function teamColor(t) {
  let c = rgb(t.color);
  const a = rgb(t.alt);
  if ((!c || lum(c) < .03) && a && lum(a) >= .03) c = a;
  return c && lum(c) >= .03 ? c : [143, 162, 196];
}
const rgba = (c, a) => "rgba(" + c[0] + "," + c[1] + "," + c[2] + "," + a + ")";
const num = v => { const n = parseFloat(String(v == null ? "" : v).replace(/,/g, "")); return isNaN(n) ? null : n; };
const blank = v => v == null || v === "";

function netStyle(n) {
  const u = String(n).toUpperCase();
  const hit = NETS.find(p => u.indexOf(p[0]) === 0);
  return hit ? "background:" + hit[1] : "";
}

function teamHtml(g, side, c) {
  const t = g[side], o = g[side === "away" ? "home" : "away"];
  const logo = /^https?:\/\//.test(t.logo || "")
    ? '<img src="' + esc(t.logo) + '" alt="" onload="this.parentNode.classList.add(\'ok\')" onerror="this.remove()">' : "";
  const rank = t.rank ? '<i class="rk">' + t.rank + '</i>' : "";
  let tos = "";
  if (g.state === "in" && t.timeouts != null) {
    tos = '<span class="tos">';
    for (let i = 0; i < 3; i++) tos += '<i class="' + (i < t.timeouts ? "on" : "") + '"></i>';
    tos += '</span>';
  }
  const rec = side === "home" ? tos + esc(t.record) : esc(t.record) + tos;
  const dim = g.state === "post" && o.winner && !t.winner ? " dim" : "";
  return '<div class="team ' + (side === "away" ? "a" : "h") + dim + '" style="--c:' + rgba(c, 1) +
    ';--cs:' + rgba(c, .38) + '"><span class="stripe"></span>' +
    '<span class="lg"><span>' + esc(String(t.abbr).slice(0, 1)) + '</span>' + logo +
    (t.poss ? '<i class="pb">' + BALL + '</i>' : "") + '</span>' +
    '<div class="nmcol"><div class="ab">' + rank + esc(t.abbr) + '</div><div class="rec">' + rec + '</div></div>' +
    '<span class="sc">' + (g.state === "pre" ? "" : esc(t.score)) + '</span></div>';
}

function midHtml(g) {
  const big = g.clock[1], live = g.state === "in" ? '<i class="ld"></i>' : "";
  return '<div class="mid"><div class="clk ' + g.state + (big ? "" : " solo") + '">' + live +
    '<b>' + esc(g.clock[0]) + '</b>' + (big ? '<span>' + esc(big) + '</span>' : "") + '</div>' +
    (g.network ? '<span class="net" style="' + netStyle(g.network) + '">' + esc(g.network) + '</span>' : "") + '</div>';
}

function stripHtml(g, n, total) {
  let mid = "";
  if (g.poss) mid += '<b class="pt">' + BALL + esc(g.poss) + '</b>';
  mid += esc(g.situation);
  if (g.redzone) mid += '<span class="rzp">RED ZONE</span>';
  let wp = "";
  if (g.winp != null) {
    const home = g.winp >= 50, pct = Math.round(home ? g.winp : 100 - g.winp);
    wp = esc(g.winp_label || "WIN") + ' <b>' + esc(g[home ? "home" : "away"].abbr) + ' ' + pct + '%</b>';
  }
  return '<div class="strip' + (g.redzone ? " rz" : "") + '"><span class="cnt">' +
    (total > 1 ? n + "/" + total : "") + '</span><span class="sm">' + mid + '</span><span class="wpt">' + wp + '</span></div>';
}

function wpBar(g, ca, ch) {
  if (g.winp == null) return '<div class="wpb"></div>';
  return '<div class="wpb"><i style="width:' + (100 - g.winp) + '%;background:' + rgba(ca, .95) + '"></i>' +
    '<i style="width:' + g.winp + '%;background:' + rgba(ch, .95) + '"></i></div>';
}

function cellsHtml(g, side) {
  const other = side === "away" ? "home" : "away";
  return (g.cells || []).map(cell => {
    const v = cell[side], o = cell[other];
    let cls = "", bar = "";
    if (cell.kind === "num") {
      const a = num(v), b = num(o);
      const w = a != null && b != null && a + b > 0 ? Math.round(100 * Math.max(a, 0) / (Math.max(a, 0) + Math.max(b, 0))) : 0;
      bar = '<span class="bar"><i style="width:' + w + '%"></i></span>';
    } else if (cell.kind === "to") {
      if (num(v) > 0) cls = " bad";
      bar = '<span class="bar" style="visibility:hidden"></span>';
    } else cls = " txt";
    return '<div class="cell' + cls + '"><span class="lb">' + esc(cell[side + "_label"] || cell.label) +
      '</span><span class="v">' + esc(blank(v) ? "–" : v) + '</span>' + bar + '</div>';
  }).join("");
}

function wingSet(el, g, side, c) {
  el.style.setProperty("--c", rgba(c, 1));
  el.style.setProperty("--cs", rgba(c, .45));
  el.innerHTML = '<div class="cells">' + cellsHtml(g, side) + '</div><div class="tick" id="tk-' + side + '"></div>';
}

function tickHtml(item, side) {
  if (!item) return "";
  const v = blank(item[side]) ? "–" : esc(item[side]), b = '<b>' + esc(item.label) + '</b>';
  return side === "away" ? b + v : v + b;
}

function showTick(animate) {
  const g = games[idx];
  if (!g) return;
  const items = g.ticker || [];
  const item = items.length ? items[(tickPos[g.id] || 0) % items.length] : null;
  const els = [$("tk-away"), $("tk-home")].filter(Boolean);
  const put = () => { els.forEach(el => { el.innerHTML = tickHtml(item, el.id.slice(3)); el.classList.remove("fade"); }); };
  if (!animate) return put();
  els.forEach(el => el.classList.add("fade"));
  setTimeout(put, 300);
}

function render(animate) {
  const pc = $("pc"), pod = $("pod");
  document.body.classList.toggle("nogames", !games.length);
  if (!games.length) {
    pod.className = "empty";
    pc.innerHTML = '<span>No ranked games right now</span>';
    lastKey = "";
    return;
  }
  pod.className = "";
  idx = idx % games.length;
  const g = games[idx];
  const key = JSON.stringify(g) + "|" + idx + "|" + games.length;
  if (!animate && key === lastKey) return;
  const draw = () => {
    curId = g.id;
    lastKey = key;
    const ca = teamColor(g.away), ch = teamColor(g.home);
    pc.innerHTML = '<div class="main">' + teamHtml(g, "away", ca) + midHtml(g) + teamHtml(g, "home", ch) + '</div>' +
      stripHtml(g, idx + 1, games.length) + wpBar(g, ca, ch);
    wingSet($("wl"), g, "away", ca);
    wingSet($("wr"), g, "home", ch);
    fit();
    showTick(false);
    if (animate && started) { clearTimeout(tickT); tickT = setTimeout(ticker, TICK_MS); }  // a full first slice
  };
  if (!animate) return draw();
  const st = $("stage");
  st.classList.add("fade");
  setTimeout(() => { draw(); st.classList.remove("fade"); }, 400);
}

function overflowing() {
  if ($("stage").scrollHeight > window.innerHeight + 1) return true;
  for (const el of document.querySelectorAll(".main, .clk, .ab"))
    if (el.offsetParent && el.scrollWidth > el.clientWidth + 1) return true;
  // Stat values are centered in their cells, so their overflow doesn't show in scrollWidth
  const gap = parseFloat(document.body.style.fontSize) * .4;
  for (const v of document.querySelectorAll(".cell .v"))
    if (v.offsetParent && v.offsetWidth > v.parentNode.clientWidth - gap) return true;
  return false;
}

function fitAt(wings) {
  document.body.classList.toggle("nowings", !wings);
  let fs = window.innerHeight / 3.75;
  document.body.style.fontSize = fs + "px";
  while (overflowing() && fs > 6) { fs -= .5; document.body.style.fontSize = fs + "px"; }
  return fs;
}

// Full size with the stat wings if they fit; if they'd squeeze the text much, drop them instead
function fit() {
  const full = window.innerHeight / 3.75, withWings = fitAt(true);
  if (withWings >= full * .85) return;
  if (fitAt(false) <= withWings + .5) fitAt(true);   // the wings weren't what was squeezing it
}

function progress() {
  const p = $("prog");
  p.classList.remove("run"); p.style.transitionDuration = "0s"; void p.offsetWidth;
  if (games.length < 2) return;
  p.style.transitionDuration = cycle + "s"; p.classList.add("run");
}

function rotate() {
  if (games.length > 1) { idx = (idx + 1) % games.length; render(true); }
  progress();
  rotT = setTimeout(rotate, cycle * 1000);
}

function ticker() {
  const g = games[idx];
  if (g && (g.ticker || []).length > 1) { tickPos[g.id] = (tickPos[g.id] || 0) + 1; showTick(true); }
  tickT = setTimeout(ticker, TICK_MS);
}

async function poll() {
  try {
    const r = await fetch("data.json?t=" + Date.now(), {cache: "no-store"});
    const d = await r.json();
    if (d.ready) {
      document.documentElement.style.setProperty("--op", d.opacity);
      refresh = d.refresh || 5; cycle = d.cycle || 12;
      games = d.games || [];
      const j = games.findIndex(g => g.id === curId);
      if (j >= 0) idx = j;
      render(false);
      if (!started) { started = true; progress(); rotT = setTimeout(rotate, cycle * 1000); tickT = setTimeout(ticker, TICK_MS); }
    }
    lastOk = Date.now();
    $("sb").classList.remove("lost");
  } catch (e) {
    if (Date.now() - lastOk > 45000) $("sb").classList.add("lost");
  }
  setTimeout(poll, refresh * 1000);
}

window.addEventListener("resize", fit);
poll();
</script></body></html>
"""


class Scoreboard:
    """Serves the scoreboard page to the TV and keeps its data fresh from ESPN in the background."""

    def __init__(self, pc_ip=None, port=None):
        self.state = {"ready": False}
        self.lock = threading.Lock()
        self.cache = {}               # game id -> {"at", "state", "data"} from ESPN's game summary
        self.stopping = threading.Event()
        self.showing = False
        self.last_note = None
        self.error_logged = False
        board = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/data.json"):
                    with board.lock:
                        body, ctype = json.dumps(board.state).encode(), "application/json"
                elif self.path in ("/", "/index.html") or self.path.startswith("/?"):
                    body, ctype = SCOREBOARD_PAGE.encode(), "text/html; charset=utf-8"
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
                pass

        self.http = ThreadingHTTPServer(("0.0.0.0", CFB_SCOREBOARD_PORT if port is None else port), Handler)
        self.url = f"http://{pc_ip or detect_pc_ip()}:{self.http.server_address[1]}/"
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def set_games(self, views):
        with self.lock:
            self.state = {"ready": True, "games": views, "cycle": CFB_SCOREBOARD_CYCLE_SECONDS,
                          "refresh": 5, "opacity": CFB_SCOREBOARD_OPACITY}

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while not self.stopping.is_set():
            try:
                self.update(time.time())
            except Exception as e:  # never let a surprise in ESPN's data take the scoreboard down
                log(f"Scoreboard error: {e}")
            self.stopping.wait(CFB_SCOREBOARD_FETCH_SECONDS)

    def update(self, now):
        try:
            events = fetch_events()
        except (requests.RequestException, ValueError) as e:
            if not self.error_logged:
                log(f"Scoreboard: ESPN error ({e}); keeping the last scores up")
                self.error_logged = True
            self.keep_alive(self.showing)
            return
        self.error_logged = False
        games = [g for g in (board_game(ev) for ev in events) if g]
        mode = str(CFB_SCOREBOARD_GAMES).lower()
        picked = pick_board_games(games, mode, now)
        self.set_games([board_view(g, self.details(g, now)) for g in picked])
        self.keep_alive(bool(picked))
        live = sum(1 for g in picked if g["state"] == "in")
        note = (f"Scoreboard: {games_text(len(picked))} with a top-{CFB_SCOREBOARD_RANK_CUTOFF} team"
                f" ({live} live)" if picked else
                "Scoreboard: no ranked games live - hidden" if mode == "live" else
                "Scoreboard: no ranked games on ESPN's scoreboard - hidden")
        if note != self.last_note:
            log(note)
            self.last_note = note

    def details(self, g, now):
        """Stats from the game's ESPN summary: every fetch while live, once after the final,
        every 15 minutes before kickoff (for the line and FPI)."""
        cached = self.cache.get(g["id"])
        if cached and cached["state"] == g["state"]:
            age = now - cached["at"]
            if (g["state"] == "post" or (g["state"] == "in" and age < CFB_SCOREBOARD_FETCH_SECONDS - 2)
                    or (g["state"] == "pre" and age < 900)):
                return cached["data"]
        try:
            data = summary_details(fetch_summary(g["id"]), g)
        except (requests.RequestException, ValueError) as e:
            debug(f"Scoreboard: couldn't load stats for {matchup(g)}: {e}")
            return cached["data"] if cached else None
        self.cache[g["id"]] = {"at": now, "state": g["state"], "data": data}
        return data

    def keep_alive(self, show):
        # Re-sending the same card id and page keeps it up without redrawing it
        if show:
            self.showing = show_scoreboard(self.url) or self.showing
        elif self.showing:
            hide_scoreboard()
            self.showing = False

    def stop(self):
        self.stopping.set()
        hide_scoreboard()
        self.http.shutdown()


def start_scoreboard():
    try:
        board = Scoreboard(PC_IP)
    except OSError as e:
        log(f"Couldn't serve the scoreboard on port {CFB_SCOREBOARD_PORT} ({e}); running without it.")
        return None
    log(f"Scoreboard page: {board.url}  (open it in a browser on this PC to preview)")
    board.start()
    return board


def sample_games():
    """Made-up games for --test-scoreboard: one live in the red zone, one final, one before kickoff."""
    logo = "https://a.espncdn.com/i/teamlogos/ncaa/500/{}.png".format

    def team(abbr, name, rank, score, record, color, alt, tid, **kw):
        return {"abbr": abbr, "name": name, "rank": rank, "score": score, "record": record, "color": color,
                "alt": alt, "logo": logo(tid), "timeouts": None, "poss": False, "winner": False, **kw}

    def stat(label, a, h, kind="num"):
        return {"label": label, "away": a, "home": h, "kind": kind}

    live = {"id": "sample-live", "state": "in", "clock": ["3RD", "4:12"], "network": "ABC",
            "away": team("TEX", "Texas", 5, "24", "8-1", "bf5700", "ffffff", 251, timeouts=2, poss=True),
            "home": team("UGA", "Georgia", 9, "20", "7-2", "ba0c2f", "000000", 61, timeouts=3),
            "redzone": True, "poss": "TEX", "situation": "2nd & 6 at UGA 14", "winp": 44.6, "winp_label": "WIN",
            "cells": [stat("PASS", "212", "187"), stat("RUSH", "131", "96"), stat("YDS", "343", "283"),
                      stat("TO", "0", "2", "to")],
            "ticker": [{"label": "PASS", "away": "A. Manning  17/24, 212 YDS, 2 TD",
                        "home": "G. Beck  15/27, 187 YDS, 1 TD, 1 INT"},
                       {"label": "RUSH", "away": "Q. Wisner  18 CAR, 97 YDS", "home": "N. Frazier  12 CAR, 61 YDS"},
                       {"label": "3RD DN", "away": "6-11", "home": "3-10"},
                       {"label": "BY QTR", "away": "7  10  7", "home": "3  14  3"}]}
    final = {"id": "sample-final", "state": "post", "clock": ["FINAL/OT", ""], "network": "NBC",
             "away": team("USC", "USC", 14, "27", "6-3", "9e2237", "ffc72c", 30),
             "home": team("ORE", "Oregon", 3, "34", "9-0", "154733", "fee11a", 2483, winner=True),
             "redzone": False, "poss": "", "situation": "Autzen Stadium", "winp": None,
             "cells": [stat("PASS", "301", "244"), stat("RUSH", "88", "203"), stat("YDS", "389", "447"),
                       stat("TO", "1", "0", "to")],
             "ticker": [{"label": "PASS", "away": "M. Moss  24/38, 301 YDS, 2 TD",
                         "home": "D. Moore  19/26, 244 YDS, 3 TD"},
                        {"label": "BY QTR", "away": "7  7  3  10  0", "home": "3  14  7  3  7"}]}
    pre = {"id": "sample-pre", "state": "pre", "clock": ["SAT", "7:30 PM"], "network": "ESPN",
           "away": team("TENN", "Tennessee", 20, "0", "6-3", "ff8200", "ffffff", 2633),
           "home": team("ALA", "Alabama", 1, "0", "9-0", "9e1b32", "ffffff", 333),
           "redzone": False, "poss": "", "situation": "Line ALA -10.5 · O/U 52.5 · Bryant-Denny Stadium",
           "winp": 78.4, "winp_label": "FPI",
           "cells": [{"label": "REC", "away": "6-3", "home": "9-0"}, {"label": "CONF", "away": "3-3", "home": "6-0"},
                     {"label": "AWAY", "home_label": "HOME", "away": "2-2", "home": "5-0"},
                     {"label": "FPI", "away": "22%", "home": "78%"}],
           "ticker": [{"label": "PASS", "away": "J. Aguero  2,104 YDS, 15 TD", "home": "T. Simpson  2,456 YDS, 21 TD"}]}
    return [live, final, pre]


def test_scoreboard(seconds=90):
    try:
        board = Scoreboard(PC_IP)
    except OSError as e:
        print(f"Couldn't serve the scoreboard on port {CFB_SCOREBOARD_PORT}: {e}")
        return
    board.set_games(sample_games())
    print(f"Showing a sample scoreboard (made-up games) on the TV at {TV_IP} for {seconds} seconds.")
    print(f"Preview it in a browser: {board.url}   Ctrl+C to stop early.")
    if not show_scoreboard(board.url):
        board.http.shutdown()
        return
    try:
        time.sleep(seconds)
    except KeyboardInterrupt:
        pass
    board.stop()
    print("Done - scoreboard removed.")


# ---------------- commands ----------------

def list_games(date=None, ranked_only=False):
    games = sorted(fetch_scoreboard(date).values(), key=lambda g: (g["date"], g["id"]))
    if ranked_only:
        games = [g for g in games if is_ranked(g)]
    if not games:
        print("No games found.")
        return
    print(f"{'ESPN id':>10}  {'When':<15}  Matchup")
    for g in games:
        when = {"in": g["detail"], "post": "Final"}.get(g["state"]) or kickoff_label(g)
        star = "*" if is_ranked(g) else " "
        tv = f"   ({g['tv']})" if g["tv"] else ""
        print(f"{g['id']:>10}  {when:<15} {star}{matchup(g, full=True)}{tv}")
    print(f"\n* = includes a top-{CFB_RANK_CUTOFF} team (watched in ranked mode)")
    print("To watch specific games, put their ids in CFB_GAME_IDS in overlay_config.py")
    print("and set CFB_MODE = \"selected\", or run:  python cfb_replays.py --games ID ID ...")


def test_video(url):
    size = "720p sample" if url == SAMPLE_VIDEO else quality(url)
    print(f"Sending a test clip ({size}, decoder: {CFB_DECODER}) to the TV at {TV_IP}...")
    if not show_clip(url, None, 25, "HUDdle test clip", "If this plays, video overlays work."):
        return
    print("Sent. A video should play on the TV for about 20 seconds.")
    if url == SAMPLE_VIDEO:
        print("This sample is 720p whatever CFB_MAX_BITRATE says; it's for checking that video plays")
        print("and comparing CFB_DECODER settings. Real clips print their size when they play.")
    try:
        time.sleep(12)
        last = requests.get(f"http://{TV_IP}:7979/state", timeout=5).json().get("lastPopup") or {}
    except (requests.RequestException, ValueError, KeyboardInterrupt):
        return
    if last.get("firstFrameMs") is not None:
        print(f"TV reports the first frame took {last['firstFrameMs']} ms.")
    if last.get("mediaError"):
        print(f"TV reports a playback error: {last['mediaError']}")


def test_button():
    try:
        buttons = ButtonServer(CFB_CALLBACK_PORT, PC_IP)
    except OSError as e:
        print(f"Couldn't listen for button presses on port {CFB_CALLBACK_PORT}: {e}")
        return
    print(f"Showing a \"Watch replay\" button on the TV at {TV_IP}. The TV reports presses to {buttons.base}...")
    url, token = buttons.new_callback()
    offer = {"title": "HUDdle test", "message": "Press Watch replay to play a sample", "poster": None}
    if not show_prompt(offer, CFB_PROMPT_SECONDS, url):
        return
    print(f"Press Watch replay (or ✕) on the TV within {CFB_PROMPT_SECONDS} seconds.")

    def wait_for(token, seconds):
        deadline = time.time() + seconds
        while time.time() < deadline:
            buttons.wake.wait(1)
            buttons.wake.clear()
            button = buttons.pressed(token)
            if button:
                return button
        return None

    try:
        button = wait_for(token, CFB_PROMPT_SECONDS + 5)
        if button == "watch":
            print("Got Watch replay. Playing the sample clip.")
            close_url, close_token = buttons.new_callback() if CFB_CLOSE_BUTTON else (None, None)
            show_clip(SAMPLE_VIDEO, None, 25, "HUDdle test clip", "The button works.", close_url)
            if close_url:
                print("Press ✕ on the clip to close it.")
                if wait_for(close_token, 25) == "close":
                    print("Got ✕. The clip closed.")
                else:
                    print("No ✕ press arrived (the clip ran to the end).")
        elif button:
            print(f"Got {button}. The button works.")
        else:
            print("No button press arrived. If you pressed one, this PC's firewall is probably blocking")
            print(f"port {CFB_CALLBACK_PORT} (allow Python on Private networks), or PC_IP needs setting.")
    except KeyboardInterrupt:
        pass
    buttons.close()


def scoreboard_only():
    board = start_scoreboard()
    if not board:
        return
    log(f"Scoreboard only (no replay clips): games with a top-{CFB_SCOREBOARD_RANK_CUTOFF} team. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    board.stop()
    print("Stopped - scoreboard removed.")


def main():
    global VERBOSE, CFB_DECODER
    ap = argparse.ArgumentParser(description="College football replay clips on your TV.")
    ap.add_argument("--list", action="store_true", help="list this week's FBS games with their ESPN ids")
    ap.add_argument("--date", help="with --list: games on this date instead (YYYYMMDD)")
    ap.add_argument("--ranked", action="store_true",
                    help="ranked mode: watch every game with a ranked team (with --list: only those games)")
    ap.add_argument("--games", nargs="+", metavar="ID",
                    help="selected mode: watch only these ESPN game ids or links")
    ap.add_argument("--source", choices=["espn", "x"], help="where clips come from (overrides CFB_SOURCE)")
    ap.add_argument("--decoder", choices=["auto", "hardware", "software"],
                    help="how the TV decodes clips (overrides CFB_DECODER)")
    ap.add_argument("--test", nargs="?", const=SAMPLE_VIDEO, metavar="URL",
                    help="play a sample video on the TV and exit")
    ap.add_argument("--test-button", action="store_true",
                    help="show a Watch replay button on the TV and check the press reaches this PC")
    ap.add_argument("--test-scoreboard", action="store_true",
                    help="show the scoreboard on the TV with made-up games for 90 seconds and exit")
    ap.add_argument("--scoreboard-only", action="store_true", help="show the scoreboard without replay clips")
    ap.add_argument("--no-scoreboard", action="store_true", help="replay clips without the scoreboard")
    ap.add_argument("--no-ask", action="store_true", help="play clips without asking first (overrides CFB_ASK_FIRST)")
    ap.add_argument("--verbose", action="store_true", help="also explain why clips were skipped")
    args = ap.parse_args()
    VERBOSE = args.verbose
    CFB_DECODER = args.decoder or CFB_DECODER

    if args.test:
        return test_video(args.test)
    if args.test_button:
        return test_button()
    if args.test_scoreboard:
        return test_scoreboard()
    if args.list:
        try:
            return list_games(args.date, args.ranked)
        except (requests.RequestException, ValueError) as e:
            print(f"Couldn't load games from ESPN: {e}")
            return

    if args.scoreboard_only:
        return scoreboard_only()

    mode = "selected" if args.games else "ranked" if args.ranked else str(CFB_MODE).lower()
    selected = None
    if mode == "selected":
        selected = parse_game_ids(args.games or CFB_GAME_IDS)
        if not selected:
            print("Selected mode needs games: add ESPN game ids to CFB_GAME_IDS in overlay_config.py,")
            print("or pass them with --games. Run  python cfb_replays.py --list  to find them.")
            return
    elif mode != "ranked":
        print(f"CFB_MODE must be \"ranked\" or \"selected\", not \"{CFB_MODE}\".")
        return
    source = args.source or str(CFB_SOURCE).lower()
    if source == "auto":
        source = "x" if X_BEARER_TOKEN else "espn"
    if source not in ("espn", "x"):
        print(f"CFB_SOURCE must be \"espn\", \"x\" or \"auto\", not \"{CFB_SOURCE}\".")
        return
    if source == "x" and not X_BEARER_TOKEN:
        print("Clips from X need X_BEARER_TOKEN in overlay_config.py (see the README's college football")
        print("section), or use clips from ESPN instead: set CFB_SOURCE = \"espn\".")
        return

    buttons = None
    ask_first = CFB_ASK_FIRST and not args.no_ask
    if ask_first or CFB_CLOSE_BUTTON:
        try:
            buttons = ButtonServer(CFB_CALLBACK_PORT, PC_IP)
        except OSError as e:
            log(f"Couldn't listen for button presses on port {CFB_CALLBACK_PORT} ({e}); "
                "clips will play without asking and without a ✕ button.")
    replays = Replays(XClient(X_BEARER_TOKEN) if source == "x" else None, selected, buttons,
                      ask_first, CFB_CLOSE_BUTTON)
    if replays.ask_first:
        log(f"Each clip waits for its Watch replay button (button presses come back to {buttons.base}...)")
    if replays.close_button:
        log("Playing clips have a ✕ button to close them early.")
    clips_from = "X" if source == "x" else "ESPN"
    if selected:
        log(f"Selected mode: {games_text(len(selected))} ({', '.join(selected)}), clips from {clips_from}. Ctrl+C to stop.")
    else:
        log(f"Ranked mode: games with a top-{CFB_RANK_CUTOFF} team, clips from {clips_from}. Ctrl+C to stop.")

    scoreboard = start_scoreboard() if CFB_SCOREBOARD and not args.no_scoreboard else None

    next_poll = 0
    try:
        while True:
            now = time.time()
            if now >= next_poll:
                next_poll = now + CFB_POLL_SECONDS
                replays.refresh_games(now)
                try:
                    replays.poll(now)
                except XAuthError as e:
                    log(f"{e}\nCheck X_BEARER_TOKEN and your X developer account's credits.")
                    break
                except (XError, requests.RequestException) as e:
                    log(str(e))
            replays.play_next(now)
            replays.sleep(1)
    except KeyboardInterrupt:
        pass
    hide()
    if scoreboard:
        scoreboard.stop()
    if buttons:
        buttons.close()
    if replays.x:
        n = replays.x.posts_read
        print(f"Stopped. X posts read this session: {n} (about ${n * X_COST_PER_POST:.2f}).")
    else:
        print("Stopped.")


if __name__ == "__main__":
    main()
