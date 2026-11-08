"""
HUDdle settings: every setting both scripts use, with its default and what it does.

Don't edit this file. Put the settings you want to change in overlay_config.py
(copy overlay_config.example.py to start). Anything set there overrides the value
here, and overlay_config.py is in .gitignore, so your ESPN cookies and X token
never end up in a commit and updating HUDdle never overwrites your settings.
"""

# ======================= Both scripts =======================
TV_IP = "192.168.1.50"       # your Android TV box's IP (example)
PC_IP = None                 # this PC's LAN address as the TV sees it; None = auto-detect

# Where things sit on the TV, for every *POSITION setting (0-8 grid):
#   0 top right, 1 top left, 2 bottom right, 3 bottom left, 4 center,
#   5 top center, 6 bottom center, 7 center left, 8 center right


# ======================= Fantasy bar (fantasy_overlay.py) =======================
LEAGUE_ID = 123456           # from the URL on fantasy.espn.com: ...?leagueId=123456
YEAR = 2026
MY_TEAM = "Team Name"        # all or part of your team name (case-insensitive)

# Private leagues: paste in quotes. Public league: leave as None.
ESPN_S2 = None
SWID = None                  # keep the { } braces

FETCH_SECONDS = 30           # how often to pull fresh scores from ESPN
CYCLE_SECONDS = 10           # how long each player matchup stays up before rotating
PAGE_REFRESH_SECONDS = 5     # how often the panel picks up new scores from the script
ONLY_DURING_GAMES = True     # False = keep the panel up even when no one is playing

POSITION = 5                 # where the bar sits (5 = top center)
PANEL_WIDTH = 1240           # panel size on the TV; text scales to fit. Too narrow for the player
PANEL_HEIGHT = 96            #   wings and they drop off; the player matchup moves under the score
PANEL_OPACITY = 0.85         # player wings' background darkness (0-1)
MY_COLOR = "2f80ed"          # accent colors (hex) for your team and your opponent
OPP_COLOR = "f2994a"
WIN_SPREAD = 0.7             # win % volatility: higher = less confident estimate

WEB_PORT = 8765              # port on this PC the fantasy bar's page is served on


# ======================= College football replays (cfb_replays.py) =======================
CFB_MODE = "ranked"          # "ranked" = every game with a ranked team
                             # "selected" = only the games in CFB_GAME_IDS
CFB_GAME_IDS = []            # ESPN game ids or game page links (run --list to find them)
CFB_RANK_CUTOFF = 25         # ranked mode: 10 = only games with a top-10 team

CFB_SOURCE = "auto"          # where clips come from: "espn" (free, no account), "x" (needs
                             # X_BEARER_TOKEN), or "auto" = X if a token is set, otherwise ESPN

# ---- Clips from X only ----
X_BEARER_TOKEN = None        # X API app Bearer Token (or set the X_BEARER_TOKEN environment variable)
# X accounts to pull clips from (no @). Clips are matched to games by the team names in the post.
CFB_X_ACCOUNTS = ["ESPNCFB", "CFBONFOX", "CBSSportsCFB", "BigTenNetwork",
                  "SECNetwork", "accnetwork", "Big12Conference"]
# Team accounts: any video they post during their game belongs to that game, even with no team name
# in the text. Key = X handle, value = ESPN team abbreviation from --list, e.g. {"AlabamaFTBL": "ALA"}
CFB_TEAM_ACCOUNTS = {}
# Extra names to recognize in post text. Key = ESPN abbreviation, e.g. {"ALA": ["Bama", "Roll Tide"]}
CFB_TEAM_ALIASES = {}
CFB_GAME_COOLDOWN_SECONDS = 120  # skip more clips from the same game for this long (same play, other account)
X_COST_PER_POST = 0.005      # only used for the cost estimate printed in the console

# ---- Clips from either source ----
CFB_POLL_SECONDS = 60        # how often to check for clips while a watched game is live
CFB_AFTER_FINAL_MINUTES = 15 # keep looking for clips this long after a game ends
CFB_MAX_CLIP_SECONDS = 90    # skip longer videos (full-game recaps, press conferences)
CFB_MAX_CLIP_AGE_MINUTES = 10  # drop clips that waited in the queue longer than this
CFB_MAX_BITRATE = 3_000_000  # about 720p; lower it (e.g. 1_000_000 for SD) if clips stutter
CFB_DECODER = "auto"         # "hardware" usually fixes stutter on onn and other Amlogic boxes, where
                             # PiPup decodes popup video in software by default; "software"; or
                             # "auto" = let PiPup decide

CFB_POSITION = 2             # where the clip sits (2 = bottom right)
CFB_WIDTH = 640              # clip size on the TV
CFB_HEIGHT = 360
CFB_MUTED = True             # True keeps the game's sound; False plays the clip's audio
CFB_SHOW_CAPTION = True      # score line and clip description with the clip
CFB_CAPTION_TEXT_SIZE = 13   # caption text size on the clip (PiPup's default is 16)
CFB_PLAY_COUNT = 2           # how many times each clip plays back to back (1 = once)
CFB_LOAD_SECONDS = 3         # time allowed for a clip to start playing; raise it if the end of a
                             # clip gets cut off by its replay, lower it if there's a pause between plays

# ---- Buttons ----
CFB_ASK_FIRST = True         # show a "Watch replay" button first; the clip plays only if you press it
CFB_PROMPT_SECONDS = 15      # how long the button stays up; ignoring it (or pressing BACK) skips the clip
CFB_PROMPT_THUMBNAIL = True  # show a small still from the clip beside the text
CFB_PROMPT_TEXT_SIZE = 13    # text size on the button card (PiPup's default is 16); smaller = smaller card
CFB_PROMPT_POSITION = None   # where the button card sits; None = same place as the clip
CFB_CLOSE_BUTTON = True      # a ✕ button on the playing clip to close it (and skip its replay); while
                             # a clip with ✕ is up, the remote's OK and arrow keys go to it, not the TV app
CFB_CLOSE_BUTTON_SIZE = 10   # size of the ✕ (PiPup's default button is much bigger and widens the clip)
CFB_CALLBACK_PORT = 8766     # port on this PC the TV reports button presses to

# ---- Scoreboard: a bar at the top center that rotates through ranked games ----
CFB_SCOREBOARD = True        # False = replay clips only (or run with --no-scoreboard)
CFB_SCOREBOARD_GAMES = "auto"  # "auto": live ranked games plus ones about to start or just finished; when
                             #   there are none of those, every ranked game this week (so it's always up)
                             # "all": every ranked game this week   "live": only games in progress
CFB_SCOREBOARD_RANK_CUTOFF = 25    # 10 = only games with a top-10 team
CFB_SCOREBOARD_CYCLE_SECONDS = 12  # how long each game stays up before rotating
CFB_SCOREBOARD_FETCH_SECONDS = 30  # how often scores and stats are pulled from ESPN
CFB_SCOREBOARD_POSITION = 5  # where the scoreboard sits (5 = top center). Running the fantasy bar
                             #   too? It's also top center by default; move one, e.g. POSITION = 6
CFB_SCOREBOARD_WIDTH = 1240  # bar size on the TV; text scales to fit. Too narrow for the stat wings
CFB_SCOREBOARD_HEIGHT = 96   #   and they drop off, leaving the scoreboard itself
CFB_SCOREBOARD_OPACITY = 0.88  # stat wings' background darkness (0-1)
CFB_SCOREBOARD_PORT = 8767   # port on this PC the scoreboard page is served on
