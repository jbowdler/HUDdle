# HUDdle 🏈

**A heads-up display for fantasy football: live ESPN scores, win %, and player matchups overlaid on any Android TV app.**

HUDdle puts a slim scoreboard bar at the top of your TV while you watch the game, in any app: YouTube TV, a streaming app, an IPTV player, whatever. It shows your matchup score, win probability and how many players each side has left, then cycles through every position matchup (QB vs QB, RB vs RB, FLEX vs FLEX…) so you can sweat your lineup without picking up your phone.

<!-- Add a photo of the bar on your TV here -->
<!-- ![HUDdle on a TV](docs/screenshot.jpg) -->

## Features

- **Matchup header:** both team scores, margin, estimated win %, players live and players left
- **Rotating player matchups:** each starter vs their opponent's starter, with points, projections and game status (live, final, kickoff time)
- **Works over any app**, because the bar is a system overlay, not part of a video player
- **No flicker:** the bar is drawn once and updates its numbers in place
- **Only shows during games** (optional) and removes itself when you stop the script
- **Public and private ESPN leagues**
- **College football replays (optional):** highlight clips pop up in a corner of the TV while your games are live, for every ranked game or just the games you pick. Free clips from ESPN, or from X if you have X API access. See [College football replays](#college-football-replays).

## How it works

```
ESPN Fantasy API  ──(every 30 s)──>  fantasy_overlay.py on your PC  ──(scoreboard page)──>  PiPup on the TV  ──>  overlay on screen
```

- **[PiPup](https://github.com/mhoogenbosch/PiPup)** runs on the TV and can show a web page as a transparent overlay above any app.
- **`fantasy_overlay.py`** runs on a computer on the same network. It pulls scores with [espn-api](https://github.com/cwendt94/espn-api), serves a small scoreboard page, and tells PiPup to show it.

## Requirements

- An Android TV / Google TV device (tested on an onn 4K box)
- A computer on the same home network, left on during games (Windows instructions below; macOS and Linux work the same way with their own terminal)
- Python 3.10 or newer (espn-api requires it)
- [ADB](https://developer.android.com/tools/releases/platform-tools), used once to install PiPup

## Install

### 1. Connect to the TV with ADB

1. Install ADB. On Windows: `winget install -e --id Google.PlatformTools`, then open a new terminal.
2. On the TV, turn on Developer options: **Settings → System → About**, then click **Android TV OS build** 7 times.
3. Turn on **Settings → System → Developer options → Wireless debugging**.
4. Choose **Pair device with pairing code** and pair using the IP:port and code the TV shows:
   ```
   adb pair 192.168.1.50:37241 482915
   ```
5. Connect using the IP:port on the main Wireless debugging screen:
   ```
   adb connect 192.168.1.50:41093
   adb devices
   ```

The addresses and codes above are examples. Reserve the TV's IP in your router's DHCP settings so it never changes.

### 2. Install PiPup on the TV

Download the latest APK from the [PiPup fork's releases](https://github.com/mhoogenbosch/PiPup/releases) (the original rogro82 version lacks features HUDdle needs), then:

```
adb install PiPup.apk
adb shell appops set nl.rogro82.pipup SYSTEM_ALERT_WINDOW allow
```

The second command grants the overlay permission, which Google TV has no settings screen for. Open PiPup on the TV once, then restart the TV.

Prefer a script? The PiPup fork ships `install.ps1` (Windows) and `install.sh` (macOS/Linux) on its releases page. They download the APK, install it and grant the permission in one step, for example `.\install.ps1 192.168.1.50`.

Reinstalling PiPup resets the overlay permission, so re-run the `appops` command after any manual reinstall.

### 3. Test the overlay

With something playing on the TV:

```
curl -X POST http://192.168.1.50:7979/notify -H "Content-Type: application/json" -d "{\"duration\":10,\"position\":5,\"title\":\"Test\",\"message\":\"Overlay works\"}"
```

That works in Command Prompt. In Windows PowerShell, `curl` means something else, so use this instead:

```powershell
Invoke-RestMethod -Method Post -Uri http://192.168.1.50:7979/notify -ContentType "application/json" -Body '{"duration":10,"position":5,"title":"Test","message":"Overlay works"}'
```

A card should appear at the top center of the screen.

### 4. Install Python and the libraries

```
winget install -e --id Python.Python.3.12
python -m pip install espn-api requests
```

If `python` opens the Microsoft Store, turn off **python.exe** and **python3.exe** under **Settings → Apps → Advanced app settings → App execution aliases**.

### 5. Configure

Copy `overlay_config.example.py` to `overlay_config.py` and fill it in:

```python
TV_IP = "192.168.1.50"
LEAGUE_ID = 1234567           # number after leagueId= in your league's URL
YEAR = 2026
MY_TEAM = "Your Team Name"    # all or part of your ESPN team name
ESPN_S2 = None                # private leagues only
SWID = None                   # private leagues only
```

`overlay_config.py` is in `.gitignore`, so your ESPN cookies never end up in a commit.

**Private leagues** need two login cookies. Log in at fantasy.espn.com in Chrome or Edge, press **F12 → Application → Cookies → https://fantasy.espn.com**, and copy the values of `espn_s2` and `SWID` (keep the curly braces on SWID). Treat them like a password.

### 6. Run

```
python fantasy_overlay.py
```

- On first run, allow Python through Windows Firewall on **Private networks**. The TV loads the scoreboard from your computer, so this is required.
- The console prints a preview link like `http://192.168.1.23:8765/`. Open it in a browser to see the scoreboard.
- The bar appears whenever a starter in your matchup has a game in progress. Set `ONLY_DURING_GAMES = False` to test anytime.
- The script reconnects to ESPN every 6 hours, so it rolls over to the new week on its own if you leave it running.
- Press **Ctrl+C** to stop; the bar disappears immediately. If the script dies, the bar clears itself within about 90 seconds.

On macOS or Linux, run `python3 fantasy_overlay.py` instead.

To run it in the background on Windows, use `pythonw fantasy_overlay.py` (stop it by ending `pythonw.exe` in Task Manager), or add it to Task Scheduler.

## Settings

Any of these can go in `overlay_config.py`.

| Setting | Default | What it does |
| --- | --- | --- |
| `TV_IP` | example value | The TV's IP address |
| `LEAGUE_ID` | example value | Your ESPN league ID |
| `YEAR` | 2026 | Season year |
| `MY_TEAM` | example value | All or part of your team name (not case-sensitive) |
| `ESPN_S2`, `SWID` | None | Login cookies for private leagues |
| `FETCH_SECONDS` | 30 | How often scores are pulled from ESPN |
| `CYCLE_SECONDS` | 10 | How long each position matchup stays on screen |
| `PAGE_REFRESH_SECONDS` | 5 | How often the bar picks up new scores |
| `ONLY_DURING_GAMES` | True | Show the bar only while your matchup has a game in progress |
| `POSITION` | 5 | Where the bar sits: 0 top right, 1 top left, 2 bottom right, 3 bottom left, 4 center, 5 top center, 6 bottom center, 7 center left, 8 center right |
| `PANEL_WIDTH`, `PANEL_HEIGHT` | 820, 74 | Bar size; text scales to fit |
| `PANEL_OPACITY` | 0.85 | Background darkness, 0 to 1 |
| `WIN_SPREAD` | 0.7 | Win % volatility; higher is less confident |
| `WEB_PORT` | 8765 | Port the scoreboard page is served on |
| `PC_IP` | None | Computer's IP as seen by the TV; None auto-detects |

## About the win %

ESPN's API doesn't expose its own win probability, so HUDdle estimates one. It starts from ESPN's live projected totals and widens the uncertainty for every point still in play: a player who hasn't kicked off counts fully, one mid-game counts partly, and a finished player not at all. It's in the same ballpark as the ESPN app but won't match it exactly.

## College football replays

`cfb_replays.py` plays college football highlight clips in a small video popup while your games are live. It's separate from the fantasy bar: run either one, or both at once (they use different popups).

```
ESPN scoreboard ──> which watched games are live ──> new highlight clips (ESPN, or X) ──> PiPup plays the clip
```

- **Two modes.** `ranked` watches every game with a ranked team (the ranking ESPN shows). `selected` watches only the games you pick, by ESPN game id.
- **Clips come from ESPN by default:** the highlight videos ESPN attaches to each game. It's free and needs no account. ESPN doesn't post clips for every game or every big play, and they can show up a few minutes behind.
- **Clips from X are optional.** If you have X API access, the script can pull video posts from network, conference and team accounts instead, which is usually faster and covers more games. See [Clips from X](#clips-from-x-optional).
- **Only live games are checked**, from kickoff until 15 minutes after the final.
- **The TV streams each clip straight from ESPN or X**, at about 720p by default. Nothing is downloaded to your computer.
- **Clips queue up** and play one at a time.

### 1. Configure

Add these to `overlay_config.py`. `TV_IP` is shared with the fantasy bar.

```python
CFB_MODE = "ranked"            # or "selected"
CFB_GAME_IDS = []              # selected mode only (see step 3)
```

Only `requests` is needed: `python -m pip install requests`.

### 2. Test video on the TV

```
python cfb_replays.py --test
```

A 15-second sample video should play in the bottom-right corner. If it doesn't, see [Troubleshooting](#troubleshooting).

### 3. Pick games (selected mode)

List this week's FBS games with their ESPN ids. Games with a ranked team are marked with `*`:

```
python cfb_replays.py --list
python cfb_replays.py --list --date 20261107   # a specific day
```

```
   ESPN id  When             Matchup
 401752771  Sat 3:30 PM     *#3 Texas @ #9 Georgia   (ABC)
 401752780  Sat 7:00 PM      Michigan State @ Michigan   (NBC)
```

Put the ids you want in `CFB_GAME_IDS` and set `CFB_MODE = "selected"`. You can also paste the link of an ESPN game page (it has `gameId/401752771` in it):

```python
CFB_MODE = "selected"
CFB_GAME_IDS = [401752771, "https://www.espn.com/college-football/game/_/gameId/401752780"]
```

Or skip the config and pass them for one run: `python cfb_replays.py --games 401752771 401752780`. Games that aren't on the FBS scoreboard (FCS, for example) work too if you give their id.

### 4. Run

```
python cfb_replays.py               # uses CFB_MODE from overlay_config.py
python cfb_replays.py --ranked      # ranked mode for this run
python cfb_replays.py --verbose     # also explain why clips were skipped
```

The console shows which watched games are live, each clip it finds, and what's playing. Press **Ctrl+C** to stop and the popup disappears.

When the script starts watching a game, clips ESPN already posted (previews, earlier plays) are skipped; only clips posted after that are played. Start it before kickoff to catch everything.

### Clips from X (optional)

X usually has clips sooner and for more games, but needs X API access, which is paid per use.

1. Sign up at [developer.x.com](https://developer.x.com), create a project and an app, and add credit to the account.
2. In the app's **Keys and tokens**, generate the **Bearer Token** and copy it. Treat it like a password.
3. Add it to `overlay_config.py`:

   ```python
   X_BEARER_TOKEN = "AAAA..."
   ```

With a token set, the script uses X automatically (`CFB_SOURCE = "auto"`). Set `CFB_SOURCE = "espn"` to keep using ESPN anyway, or pass `--source espn` / `--source x` for one run.

**Accounts.** By default clips come from national network and conference accounts (`@ESPNCFB`, `@CFBONFOX`, `@CBSSportsCFB`, `@BigTenNetwork`, `@SECNetwork`, `@accnetwork`, `@Big12Conference`). Each clip is matched to a game by the team names in the post. If two accounts post the same play, only the first is shown.

**Team accounts.** National accounts name the teams in almost every highlight post. Team accounts often don't ("LET'S GOOOO 🐶"), so map them to their ESPN team abbreviation (shown by `--list`) and every video they post during that team's game counts:

```python
CFB_TEAM_ACCOUNTS = {"GeorgiaFootball": "UGA", "AlabamaFTBL": "ALA"}
```

If a nickname isn't being recognized, add it as an alias: `CFB_TEAM_ALIASES = {"ALA": ["Bama", "Roll Tide"]}`.

**Cost.** Each post the X API returns costs about $0.005, which was X's pay-per-use rate when this was written; check your developer console for current pricing. To keep the bill small, the script only asks for video posts from your accounts, only new ones since the last check, and only while a watched game is live. Startup also looks up each account once, at about $0.01 each. With the default seven accounts, a full Saturday should cost roughly $1 to $2 (an estimate, not a measurement). When you stop the script, it prints how many X posts were read and roughly what they cost.

### Replay settings

Any of these can go in `overlay_config.py`.

| Setting | Default | What it does |
| --- | --- | --- |
| `CFB_MODE` | `"ranked"` | `"ranked"`: every game with a ranked team. `"selected"`: only `CFB_GAME_IDS` |
| `CFB_GAME_IDS` | `[]` | ESPN game ids or game links for selected mode |
| `CFB_RANK_CUTOFF` | 25 | Ranked mode: 10 watches only games with a top-10 team |
| `CFB_SOURCE` | `"auto"` | `"espn"`, `"x"`, or `"auto"` (X if `X_BEARER_TOKEN` is set, otherwise ESPN) |
| `CFB_POLL_SECONDS` | 60 | How often to check for new clips |
| `CFB_AFTER_FINAL_MINUTES` | 15 | Keep looking for clips this long after a game ends |
| `CFB_MAX_CLIP_SECONDS` | 90 | Skip longer videos (full-game recaps, press conferences) |
| `CFB_MAX_CLIP_AGE_MINUTES` | 10 | Drop clips that waited in the queue longer than this |
| `CFB_MAX_BITRATE` | 2500000 | About 720p; lower it to `1000000` for SD if clips stutter |
| `CFB_POSITION` | 2 | Where the clip sits (same grid as `POSITION`; 2 is bottom right) |
| `CFB_WIDTH`, `CFB_HEIGHT` | 640, 360 | Clip size |
| `CFB_MUTED` | True | `False` plays the clip's sound over the game |
| `CFB_SHOW_CAPTION` | True | Show the score line and clip description with the clip |
| `X_BEARER_TOKEN` | None | X only: your X API Bearer Token (or set the `X_BEARER_TOKEN` environment variable) |
| `CFB_X_ACCOUNTS` | 7 national accounts | X only: handles to pull clips from (no @) |
| `CFB_TEAM_ACCOUNTS` | `{}` | X only: team handles mapped to ESPN abbreviations |
| `CFB_TEAM_ALIASES` | `{}` | X only: extra names to recognize for a team |
| `CFB_GAME_COOLDOWN_SECONDS` | 120 | X only: after a clip from a game, skip that game's clips for this long (the same play from another account) |

## Troubleshooting

| Problem | Fix |
| --- | --- |
| Test curl times out | Check the TV's IP (`adb shell "ip -4 addr \| grep inet"`) and that both devices are on the same network, not a guest or isolated Wi-Fi. A VPN on the computer can also block local traffic; allow LAN traffic in its settings. |
| Curl returns PopupProps but nothing shows | Re-run the `appops set` command; reinstalling PiPup resets it. |
| `No module named 'requests'` | `python -m pip install espn-api requests` |
| `espn_s2 and swid are required` | The league is private (add both cookies) or `LEAGUE_ID` is wrong. |
| `cannot be accessed with the provided credentials` | Your cookies are wrong or have expired. Log in to ESPN again and copy fresh `espn_s2` and `SWID` values. |
| `League ... does not exist` | Check `LEAGUE_ID` and `YEAR`. |
| `No matchup found` | `MY_TEAM` doesn't match your ESPN team name. |
| Bar appears but is blank | Windows Firewall is blocking port 8765; allow Python on Private networks. If the computer has a VPN or virtual network adapters, the auto-detected address may be wrong: open the preview link from a phone on the same Wi-Fi, and if it doesn't load, set `PC_IP` to the computer's real LAN address. |
| Console says `No games in progress for your matchup - waiting` | Working as intended: `ONLY_DURING_GAMES` is on and none of your matchup's starters is playing right now. Set it to `False` to test. |
| No **Wireless debugging** option on the TV | Older Android TV builds don't have it. Turn on **USB debugging** in Developer options instead, then try `adb connect 192.168.1.50:5555` and accept the prompt on the TV. This works on many boxes but hasn't been tested with HUDdle. |
| Bar border turns red | The TV lost contact with the computer; check the script is running and the computer is awake. |
| `--test` shows nothing or a black box | Update to the latest release of the PiPup fork; video playback needs a recent version. Check the TV can reach the internet. |
| Replay clips stutter | Lower `CFB_MAX_BITRATE` to `1000000` (SD). Some boxes decode the popup in software. |
| `X rejected the request (401)` | `X_BEARER_TOKEN` is wrong. Copy the Bearer Token from your app's **Keys and tokens** again. |
| `X API error 402` or a credits message | Your X developer account is out of credit. |
| `X account not found` | A handle in `CFB_X_ACCOUNTS` or `CFB_TEAM_ACCOUNTS` is misspelled or the account no longer exists. |
| `Can't load ESPN game ...` | That id isn't an ESPN college football game. Check it with `--list`. |
| No clips for a game (ESPN) | ESPN doesn't post clips for every game, and clips longer than `CFB_MAX_CLIP_SECONDS` are skipped. Run with `--verbose` to see what was skipped. Clips from X cover more games. |
| No clips for a game (X) | Run with `--verbose` to see why posts were skipped. Add the teams' accounts to `CFB_TEAM_ACCOUNTS` or their nicknames to `CFB_TEAM_ALIASES`. |
| `Clips from X need X_BEARER_TOKEN` | `CFB_SOURCE` is `"x"` but no token is set. Add the token, or set `CFB_SOURCE = "espn"`. |

## Privacy and security

- Your ESPN cookies stay on your computer. They are only sent to ESPN and are never included in the scoreboard page.
- The scoreboard page on port 8765 has no login, so anyone on your home network can open it and see your matchup.
- PiPup's port 7979 on the TV also has no login, so anyone on your network can put a popup on your TV. Keep both on a trusted home network, not a public or shared one.
- Your X Bearer Token stays in `overlay_config.py` and is only sent to X. Anyone who has it can spend your X credit.
- Replay clips stream from ESPN's or X's servers directly to the TV.

## Disclaimer

HUDdle is an unofficial hobby project and is not affiliated with or endorsed by ESPN, the NFL, the NCAA or X. It uses ESPN's undocumented fantasy, scoreboard and game APIs, which can change without notice. Replay clips belong to ESPN or the accounts that post them; HUDdle only plays them on your own TV and doesn't save or re-share them.

## Credits

- [espn-api](https://github.com/cwendt94/espn-api) for the ESPN fantasy data
- [PiPup](https://github.com/mhoogenbosch/PiPup) (fork of [rogro82/PiPup](https://github.com/rogro82/PiPup)) for the TV overlay
- ESPN and the [X API](https://developer.x.com) for the replay clips

## License

MIT
