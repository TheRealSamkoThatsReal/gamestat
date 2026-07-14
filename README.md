# gamestat

**A WinDirStat for your game library.** One command scans every game launcher it
can find and builds a slick, self-contained web report that ranks your games by
**playtime** and draws a **disk-usage treemap** — the WinDirStat homage, but for
games, across all your stores at once.

Zero dependencies. Pure Python standard library. Runs on **Linux, Windows, and
macOS**.

---

## Two views

- **▤ Ranked list** — every installed game, most → least played, with cover art,
  a per-launcher badge, playtime, size on disk, and "last played." Sort by
  *Most played / Biggest / Recent / A–Z*, filter by launcher, plus live search.
- **▦ Disk treemap** — each game is a rectangle **sized by disk usage** and
  **colored by playtime** (dim blue → cyan → magenta = more hours), with a
  launcher-colored accent. Hover to highlight.

The report is a single HTML file with a neon-noir theme. Cover art comes from
each store's artwork (Steam/Epic CDN, Lutris local covers); games without art
get a clean initials placeholder.

## Supported launchers

`gamestat` measures **disk usage for every launcher** (from its manifest, or by
walking the install folder). **Playtime is only available where the launcher
records it locally** — everything else ranks by size / recency and shows "—".

| Launcher | Disk size | Playtime | How it's found |
|----------|:---------:|:--------:|----------------|
| **Steam** | ✅ | ✅ | `appmanifest_*.acf` + `localconfig.vdf` |
| **Lutris** (Linux) | ✅ | ✅ | `pga.db` (SQLite) |
| **Epic** | ✅ | — | Windows manifests · Heroic/Legendary on Linux |
| **GOG** | ✅ | — | Windows registry · Heroic on Linux |
| **Amazon Games** | ✅ | — | Heroic/Nile on Linux |
| **EA** | ✅ | — | Windows uninstall registry |
| **Ubisoft Connect** | ✅ | — | Windows registry |
| **Battle.net** | ✅ | — | Windows uninstall registry |
| **Riot** | ✅ | — | `RiotClientInstalls.json` |

Steam and Lutris paths are live-tested; the Windows-native launcher scanners use
documented install locations and fail safe (a launcher that isn't present, or a
format that doesn't parse, is simply skipped — it never crashes the report).

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
gamestat                   # scan → write report → open in browser
gamestat --no-open         # just write the report, print its path
gamestat --all             # include Proton / runtimes / redistributables
gamestat --only steam,epic # limit to specific launchers
gamestat --json            # print the raw scan data as JSON
gamestat -o FILE           # choose the output path
```

The report is written to `~/.cache/gamestat/report.html` by default.

## Uninstalling games

`gamestat uninstall "<game>"` removes a game through its launcher's real
mechanism — it isn't a blind `rm -rf`.

```
gamestat uninstall "Assetto Corsa"            # prompts, then removes
gamestat uninstall "Hogwarts" --dry-run       # preview exactly what's removed
gamestat uninstall 244210                     # by Steam appid
gamestat uninstall "Fortnite" --via-launcher  # hand off to the launcher's own uninstaller
gamestat uninstall "<game>" --yes             # skip the confirmation prompt
```

What it does per launcher:

- **Steam** — deletes the install folder, `appmanifest_<appid>.acf`, and the
  shader cache; Steam then correctly sees the game as uninstalled. The **Proton
  prefix** (which can hold local-only saves) is handled by cloud-save detection
  (below). Steam Cloud saves in `userdata/` are **never** touched.
- **Lutris** — deletes the install folder and marks the entry uninstalled in
  Lutris (playtime is kept).
- **Epic / GOG / Amazon (Heroic)** — deletes the folder and removes the entry
  from Heroic. With `--via-launcher` it runs `legendary uninstall` if available.
- **Windows launchers** — deletes the install folder, or with `--via-launcher`
  runs the registered native uninstaller (`UninstallString`).

Safety:

- **Cloud-save aware (Steam)** — gamestat detects whether a game has a Steam
  Cloud save (a ☁ shows in the report). If it does, removing the Proton prefix
  is safe (the save lives in `userdata/` + the cloud) and gamestat reclaims it.
  If it **doesn't**, gamestat **keeps the Proton prefix by default** so it can't
  wipe local-only saves — pass `--remove-prefix` to delete it anyway, or
  `--keep-compat` to always keep it.
- **Dry-run friendly** — always prints the exact paths and reclaimable size
  first; `--dry-run` deletes nothing.
- **Confirmation prompt** before anything is deleted (skip with `--yes`).
- **Refuses to run while the launcher is open** (Steam rewrites manifests if it's
  running); override with `--force`.
- **Path guards** refuse to delete your home folder, a filesystem root, or any
  suspiciously shallow path.

## How it works

Everything comes from each launcher's own local files — no logins, no API keys.
Each launcher is a small independent scanner that returns a normalized record
(`source, name, size, playtime, last_played, art`); a bad or missing launcher is
skipped, never fatal. Disk size is read from a manifest when the launcher
provides one, otherwise measured by walking the install directory (metadata
only). Games that appear under more than one launcher are de-duplicated, keeping
the richest record.

Launchers are located automatically — standard paths, Flatpak/Snap on Linux, and
the Windows registry where relevant.

## Notes & limitations

- **Playtime** is only shown for launchers that record it locally (Steam,
  Lutris). Epic, GOG, EA, Ubisoft, Battle.net, Riot, and Amazon don't expose
  local playtime, so those games rank by size / last-played and show "—".
- Playtime/last-played reflect **this machine's** local data.
- Cover art needs an internet connection for Steam/Epic titles; offline (or when
  a title has no art) it falls back to a clean initials placeholder / heat tile.
- Xbox / Microsoft Store games aren't scanned yet.

## License

MIT — see [LICENSE](LICENSE). Cover art is © the respective stores, loaded from
their public CDNs (or your local launcher cache).
