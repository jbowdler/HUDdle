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

Setup:
    python -m pip install requests
    python cfb_replays.py --list                  # this week's games and their ESPN ids
    python cfb_replays.py                         # watch the games set in overlay_config.py
    python cfb_replays.py --games 401752771 401752780   # watch just these games
    python cfb_replays.py --test                  # play a sample video to check the TV
"""

import argparse
import html
import math
import os
import re
import time
from datetime import datetime, timezone

import requests

# ======================= CONFIG =======================
TV_IP = "192.168.1.50"        # your Android TV box IP (example)

CFB_MODE = "ranked"           # "ranked" = every game with a ranked team
                              # "selected" = only the games in CFB_GAME_IDS
CFB_GAME_IDS = []             # ESPN game ids or game page links (run --list to find them)
CFB_RANK_CUTOFF = 25          # ranked mode: 10 = only games with a top-10 team

CFB_SOURCE = "auto"           # where clips come from: "espn" (free, no account), "x" (needs
                              # X_BEARER_TOKEN), or "auto" = X if a token is set, otherwise ESPN

# ---- X only (CFB_SOURCE = "x") ----
X_BEARER_TOKEN = None         # X API app Bearer Token (or set the X_BEARER_TOKEN environment variable)
# X accounts to pull clips from (no @). Clips are matched to games by the team names in the post.
CFB_X_ACCOUNTS = ["ESPNCFB", "CFBONFOX", "CBSSportsCFB", "BigTenNetwork",
                  "SECNetwork", "accnetwork", "Big12Conference"]
# Team accounts: any video they post during their game belongs to that game, even with no team name
# in the text. Key = X handle, value = ESPN team abbreviation from --list, e.g. {"AlabamaFTBL": "ALA"}
CFB_TEAM_ACCOUNTS = {}
# Extra names to recognize in post text. Key = ESPN abbreviation, e.g. {"ALA": ["Bama", "Roll Tide"]}
CFB_TEAM_ALIASES = {}

CFB_GAME_COOLDOWN_SECONDS = 120  # skip more clips from the same game for this long (same play, other account)
X_COST_PER_POST = 0.005       # only used for the cost estimate printed in the console

# ---- Both sources ----
CFB_POLL_SECONDS = 60         # how often to check for clips while a watched game is live
CFB_AFTER_FINAL_MINUTES = 15  # keep looking for clips this long after a game ends
CFB_MAX_CLIP_SECONDS = 90     # skip longer videos (full-game recaps, press conferences)
CFB_MAX_CLIP_AGE_MINUTES = 10 # drop clips that waited in the queue longer than this
CFB_MAX_BITRATE = 2_500_000   # about 720p; lower it (e.g. 1_000_000 for SD) if clips stutter

CFB_POSITION = 2              # where the clip sits: same 0-8 grid as POSITION (2 = bottom right)
CFB_WIDTH = 640               # clip size on the TV
CFB_HEIGHT = 360
CFB_MUTED = True              # True keeps the game's sound; False plays the clip's audio
CFB_SHOW_CAPTION = True       # score line and clip description with the clip
# ======================================================

# Keep your real values (especially X_BEARER_TOKEN) out of git: put any of the
# settings above in overlay_config.py next to this script (it's in .gitignore).
try:
    from overlay_config import *  # noqa: F401,F403
except ImportError:
    pass

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


def fetch_scoreboard(date=None):
    params = {"groups": FBS, "limit": 500}
    if date:
        params["dates"] = date
    r = requests.get(f"{ESPN}/scoreboard", params=params, timeout=15)
    r.raise_for_status()
    games = {}
    for ev in r.json().get("events", []):
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
    hd, sd = (source.get("HD") or {}).get("href"), source.get("href")
    urls = [hd, sd] if CFB_MAX_BITRATE >= 2_000_000 else [sd, hd]      # HD is 720p
    urls += [((links.get("mobile") or {}).get("source") or {}).get("href"),
             (hls.get("HD") or {}).get("href"), hls.get("href")]
    url = next((u for u in urls if isinstance(u, str) and u.startswith("http")
                and re.search(r"\.(mp4|m3u8)(\?|$)", u)), None)
    if not url:
        return None
    poster = v.get("thumbnail") or ((v.get("posterImages") or {}).get("default") or {}).get("href")
    return {"url": url, "poster": poster if isinstance(poster, str) else None, "seconds": seconds or None}


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
        url = (max(fits, key=rate) if fits else min(mp4, key=rate))["url"]
    else:
        hls = [v for v in variants if "mpegurl" in (v.get("content_type") or "").lower() and v.get("url")]
        if not hls:
            return None
        url = hls[0]["url"]
    return {"url": url, "poster": m.get("preview_image_url"), "seconds": seconds or None}


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


def show_clip(url, poster, seconds, title="", message=""):
    video = {"uri": url, "width": CFB_WIDTH, "height": CFB_HEIGHT, "muted": CFB_MUTED}
    if poster:
        video["poster"] = poster
    card = {"id": CARD_ID, "duration": seconds, "position": CFB_POSITION,
            "dismissScreensaver": False, "media": {"video": video}}
    if CFB_SHOW_CAPTION:
        card.update({"title": title, "message": message})
    return post_card(card)


def hide():
    try:
        requests.post(f"http://{TV_IP}:7979/cancel", params={"id": CARD_ID}, timeout=5)
    except requests.RequestException:
        pass


# ---------------- the watcher ----------------

class Replays:
    def __init__(self, x=None, selected=None):
        self.x = x                    # XClient for clips from X, or None for clips from ESPN
        self.selected = selected      # list of ESPN ids, or None for ranked mode
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
        if now < self.busy_until:
            return
        while self.queue:
            clip = self.queue.pop(0)
            if now - clip["found"] > CFB_MAX_CLIP_AGE_MINUTES * 60:
                log(f"Dropped a clip that waited too long ({clip['credit']})")
                continue
            seconds = math.ceil(clip["seconds"] or CFB_MAX_CLIP_SECONDS) + 4   # +4 s to load
            game = self.games.get(clip["game"])
            title = score_line(game) if game else ""
            text = clip["text"] if len(clip["text"]) <= 110 else clip["text"][:107] + "..."
            message = f"{text}  - {clip['credit']}" if text else clip["credit"]
            if show_clip(clip["url"], clip["poster"], seconds, title, message):
                self.busy_until = now + seconds
                label = matchup(game) if game else "clip"
                log(f"Playing: {label} from {clip['credit']}  ({len(self.queue)} more queued)")
            return


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
    print(f"Sending a test clip to the TV at {TV_IP}...")
    if show_clip(url, None, 25, "HUDdle test clip", "If this plays, video overlays work."):
        print("Sent. A video should play on the TV for about 20 seconds.")


def main():
    global VERBOSE
    ap = argparse.ArgumentParser(description="College football replay clips on your TV.")
    ap.add_argument("--list", action="store_true", help="list this week's FBS games with their ESPN ids")
    ap.add_argument("--date", help="with --list: games on this date instead (YYYYMMDD)")
    ap.add_argument("--ranked", action="store_true",
                    help="ranked mode: watch every game with a ranked team (with --list: only those games)")
    ap.add_argument("--games", nargs="+", metavar="ID",
                    help="selected mode: watch only these ESPN game ids or links")
    ap.add_argument("--source", choices=["espn", "x"], help="where clips come from (overrides CFB_SOURCE)")
    ap.add_argument("--test", nargs="?", const=SAMPLE_VIDEO, metavar="URL",
                    help="play a sample video on the TV and exit")
    ap.add_argument("--verbose", action="store_true", help="also explain why clips were skipped")
    args = ap.parse_args()
    VERBOSE = args.verbose

    if args.test:
        return test_video(args.test)
    if args.list:
        try:
            return list_games(args.date, args.ranked)
        except (requests.RequestException, ValueError) as e:
            print(f"Couldn't load games from ESPN: {e}")
            return

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

    replays = Replays(XClient(X_BEARER_TOKEN) if source == "x" else None, selected)
    clips_from = "X" if source == "x" else "ESPN"
    if selected:
        log(f"Selected mode: {games_text(len(selected))} ({', '.join(selected)}), clips from {clips_from}. Ctrl+C to stop.")
    else:
        log(f"Ranked mode: games with a top-{CFB_RANK_CUTOFF} team, clips from {clips_from}. Ctrl+C to stop.")

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
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    hide()
    if replays.x:
        n = replays.x.posts_read
        print(f"Stopped. X posts read this session: {n} (about ${n * X_COST_PER_POST:.2f}).")
    else:
        print("Stopped.")


if __name__ == "__main__":
    main()
