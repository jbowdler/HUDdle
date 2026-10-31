# Copy this file to overlay_config.py and fill in your values.
# overlay_config.py is git-ignored, so your ESPN cookies never get committed.

TV_IP = "192.168.1.50"        # your Android TV box's IP
LEAGUE_ID = 1234567           # number after leagueId= in your league's URL
YEAR = 2026
MY_TEAM = "Your Team Name"    # all or part of your ESPN team name

# Private leagues only (see README for how to find these)
ESPN_S2 = None                # "AEB..."
SWID = None                   # "{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}"

# Any other setting from the top of fantasy_overlay.py can be overridden here too, e.g.:
# CYCLE_SECONDS = 10
# POSITION = 8

# ---- College football replays (cfb_replays.py) ----
# CFB_MODE = "ranked"         # "ranked" = every game with a ranked team
#                             # "selected" = only the games in CFB_GAME_IDS
# CFB_GAME_IDS = []           # ESPN game ids or game links; run  python cfb_replays.py --list
# Clips come from ESPN (free, no account) unless you add an X API token:
# X_BEARER_TOKEN = "AAAA..."  # optional: your X API app's Bearer Token; keep it secret
# CFB_TEAM_ACCOUNTS = {}      # X only, e.g. {"AlabamaFTBL": "ALA"} to also pull clips from team accounts
