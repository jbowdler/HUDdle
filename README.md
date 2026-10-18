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

## How it works

```
ESPN Fantasy API  ──(every 30 s)──>  fantasy_overlay.py on your PC  ──(scoreboard page)──>  PiPup on the TV  ──>  overlay on screen
```

- **[PiPup](https://github.com/mhoogenbosch/PiPup)** runs on the TV and can show a web page as a transparent overlay above any app.
- **`fantasy_overlay.py`** runs on a computer on the same network. It pulls scores with [espn-api](https://github.com/cwendt94/espn-api), serves a small scoreboard page, and tells PiPup to show it.

## Requirements

- An Android TV / Google TV device (tested on an onn 4K box)
- A computer on the same home network, left on during games (Windows instructions below; macOS and Linux work the same way with their own terminal)
- Python 3.10 or newer
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

### 3. Test the overlay

With something playing on the TV:

```
curl -X POST http://192.168.1.50:7979/notify -H "Content-Type: application/json" -d "{\"duration\":10,\"position\":5,\"title\":\"Test\",\"message\":\"Overlay works\"}"
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
- Press **Ctrl+C** to stop; the bar disappears immediately. If the script dies, the bar clears itself within about 90 seconds.

To run it in the background on Windows, use `pythonw fantasy_overlay.py` (stop it by ending `pythonw.exe` in Task Manager), or add it to Task Scheduler.

## Settings

Any of these can go in `overlay_config.py`.

| Setting | Default | What it does |
| --- | --- | --- |
| `TV_IP` | — | The TV's IP address |
| `LEAGUE_ID` | — | Your ESPN league ID |
| `YEAR` | 2026 | Season year |
| `MY_TEAM` | — | All or part of your team name |
| `ESPN_S2`, `SWID` | None | Login cookies for private leagues |
| `FETCH_SECONDS` | 30 | How often scores are pulled from ESPN |
| `CYCLE_SECONDS` | 10 | How long each position matchup stays on screen |
| `PAGE_REFRESH_SECONDS` | 5 | How often the bar picks up new scores |
| `ONLY_DURING_GAMES` | True | Show the bar only while your matchup has a game in progress |
| `POSITION` | 5 | 5 top center, 8 bottom center, 0 top right, 1 top left |
| `PANEL_WIDTH`, `PANEL_HEIGHT` | 820, 74 | Bar size; text scales to fit |
| `PANEL_OPACITY` | 0.85 | Background darkness, 0 to 1 |
| `WIN_SPREAD` | 0.7 | Win % volatility; higher is less confident |
| `WEB_PORT` | 8765 | Port the scoreboard page is served on |
| `PC_IP` | None | Computer's IP as seen by the TV; None auto-detects |

## About the win %

ESPN's API doesn't expose its own win probability, so HUDdle estimates one. It starts from ESPN's live projected totals and widens the uncertainty for every point still in play: a player who hasn't kicked off counts fully, one mid-game counts partly, and a finished player not at all. It's in the same ballpark as the ESPN app but won't match it exactly.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| Test curl times out | Check the TV's IP (`adb shell "ip -4 addr \| grep inet"`) and that both devices are on the same network, not a guest or isolated Wi-Fi. A VPN on the computer can also block local traffic; allow LAN traffic in its settings. |
| Curl returns PopupProps but nothing shows | Re-run the `appops set` command; reinstalling PiPup resets it. |
| `No module named 'requests'` | `python -m pip install espn-api requests` |
| `espn_s2 and swid are required` | The league is private (add both cookies) or `LEAGUE_ID` is wrong. |
| `No matchup found` | `MY_TEAM` doesn't match your ESPN team name. |
| Bar appears but is blank | Windows Firewall is blocking port 8765; allow Python on Private networks. |
| Bar border turns red | The TV lost contact with the computer; check the script is running and the computer is awake. |

## Disclaimer

HUDdle is an unofficial hobby project and is not affiliated with or endorsed by ESPN or the NFL. It uses ESPN's undocumented fantasy API through espn-api, which can change without notice.

## Credits

- [espn-api](https://github.com/cwendt94/espn-api) for the ESPN fantasy data
- [PiPup](https://github.com/mhoogenbosch/PiPup) (fork of [rogro82/PiPup](https://github.com/rogro82/PiPup)) for the TV overlay

## License

MIT
