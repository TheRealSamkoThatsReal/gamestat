# gamestat

**A WinDirStat for your game library.** One command scans your installed Steam
games and builds a slick, self-contained web report that ranks them from
**most-played to least-played** and draws a **disk-usage treemap** — the
WinDirStat homage, but for games.

Zero dependencies. Pure Python standard library. Runs on **Linux, Windows, and
macOS**.

---

## Two views

- **▤ Ranked list** — every installed game, most → least played, with cover art,
  playtime, size on disk, and "last played." Sort by *Most played / Biggest /
  Recent / A–Z*, plus live search.
- **▦ Disk treemap** — each game is a rectangle **sized by disk usage** and
  **colored by playtime** (dim blue → cyan → magenta = more hours). Hover to
  highlight.

The report is a single HTML file with a neon-noir theme. Cover art loads from
Steam's CDN by appid, so there's no fragile local-cache mapping (and it falls
back to solid heat-colored tiles offline).

## Install

### Prebuilt binary (no Python needed)

Grab the latest standalone binary for your OS from the
[**Releases**](../../releases) page:

| OS | Asset |
|----|-------|
| Linux | `gamestat-linux-x86_64` |
| Windows | `gamestat-windows-x86_64.exe` |
| macOS (Apple Silicon) | `gamestat-macos-arm64` |

On Linux/macOS, make it executable: `chmod +x gamestat-*` then run it.

### From source

Requires Python 3.9+. No pip install needed.

```sh
git clone https://github.com/TheRealSamkoThatsReal/gamestat
cd gamestat
python3 gamestat.py
```

## Usage

```
gamestat              # scan → write report → open in browser
gamestat --no-open    # just write the report, print its path
gamestat --all        # include Proton / runtimes / redistributables
gamestat --json       # print the raw scan data as JSON
gamestat -o FILE      # choose the output path
```

The report is written to `~/.cache/gamestat/report.html` by default.

## How it works

Everything comes from Steam's own local files — no login, no API keys:

- `libraryfolders.vdf` → every library folder (across all your drives)
- `appmanifest_*.acf` → installed game name, `SizeOnDisk`, `LastPlayed`
- `userdata/*/config/localconfig.vdf` → `Playtime` (minutes), summed across all
  local users

Proton, Steam Linux Runtime, and redistributables are filtered out by default
(pass `--all` to include them).

Steam is located automatically: standard paths plus Flatpak/Snap on Linux, and
the registry (`HKCU\Software\Valve\Steam`) on Windows.

## Notes & limitations

- Only reads what Steam records **locally** — playtime/last-played reflect this
  machine's Steam data.
- Currently Steam-only. Non-Steam launchers (Heroic, Lutris, Epic) aren't
  scanned yet.
- Cover art needs an internet connection; offline, tiles render as solid
  heat-colored blocks.

## License

MIT — see [LICENSE](LICENSE). Cover art is © Valve/Steam, loaded from Steam's
public CDN.
