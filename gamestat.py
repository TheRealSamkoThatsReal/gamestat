#!/usr/bin/env python3
"""gamestat — a WinDirStat for your game library.

Scans installed games across every launcher it can find — Steam, Epic, GOG,
Amazon, EA, Ubisoft, Battle.net, Riot, plus Heroic / Lutris on Linux — and
builds a neon-noir web report that ranks them by playtime and draws a
disk-usage treemap (the WinDirStat homage). Zero dependencies — pure stdlib.
Cross-platform: Linux, Windows, macOS.

Disk usage is measured for every launcher (by reading the launcher's manifest
or, failing that, walking the install folder). Playtime is only shown for
launchers that record it locally (Steam and Lutris); others rank by size /
recency and show "—" for playtime.

Usage:
    gamestat                # scan, write report, open in browser
    gamestat --no-open      # just write the report, print its path
    gamestat --all          # include Proton / runtimes / redistributables
    gamestat --only steam,epic   # limit to specific launchers
    gamestat -o FILE.html   # choose the output path
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Steam discovery (Linux / Windows / macOS)
# ---------------------------------------------------------------------------

def _windows_steam_roots() -> list[Path]:
    roots: list[Path] = []
    try:
        import winreg  # noqa: PLC0415  (Windows-only stdlib module)
        keys = [
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
        ]
        for hive, key, val in keys:
            try:
                with winreg.OpenKey(hive, key) as k:
                    p, _ = winreg.QueryValueEx(k, val)
                    roots.append(Path(p))
            except OSError:
                pass
    except ImportError:
        pass
    roots += [Path(r"C:/Program Files (x86)/Steam"), Path(r"C:/Program Files/Steam")]
    return roots


def steam_root_candidates() -> list[Path]:
    home = Path.home()
    if sys.platform.startswith("win"):
        return _windows_steam_roots()
    if sys.platform == "darwin":
        return [home / "Library/Application Support/Steam"]
    return [  # Linux (incl. Flatpak & Snap)
        home / ".steam/steam",
        home / ".local/share/Steam",
        home / ".steam/root",
        home / ".var/app/com.valvesoftware.Steam/data/Steam",
        home / "snap/steam/common/.local/share/Steam",
    ]


STEAM_ROOTS = steam_root_candidates()

# Things that live in steamapps but are not games.
TOOL_APPIDS = {
    228980,   # Steamworks Common Redistributables
    1070560,  # Steam Linux Runtime 1.0 (scout)
    1391110,  # Steam Linux Runtime - Soldier
    1628350,  # Steam Linux Runtime 3.0 (sniper)
    1580130,  # Proton BattlEye Runtime
    1826330,  # Proton EasyAntiCheat Runtime
    2180100,  # Proton Hotfix
    1493710,  # Proton Experimental
}
TOOL_NAME_RE = re.compile(
    r"^(Proton\b|Steam Linux Runtime|Steamworks Common|Steam Runtime|"
    r"SteamVR|.*Redistributabl)", re.I)


def find_steam_root() -> Path | None:
    for r in STEAM_ROOTS:
        if (r / "steamapps").is_dir():
            return r.resolve()
    return None


def parse_kv_flat(text: str) -> dict[str, str]:
    """First-occurrence of every "key" "value" scalar pair (good enough for
    appmanifest top-level fields, which are all uniquely named)."""
    out: dict[str, str] = {}
    for k, v in re.findall(r'"([^"]+)"\s+"([^"]*)"', text):
        out.setdefault(k, v)
    return out


def library_paths(root: Path) -> list[Path]:
    """All library folders (the main one plus any on other drives)."""
    paths = [root]
    vdf = root / "steamapps/libraryfolders.vdf"
    if vdf.exists():
        for m in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(errors="replace")):
            p = Path(m.replace("\\\\", "/"))
            if p.is_dir() and p not in paths:
                paths.append(p)
    return paths


def scan_manifests(libs: list[Path]) -> dict[int, dict]:
    games: dict[int, dict] = {}
    for lib in libs:
        appsdir = lib / "steamapps"
        for mf in appsdir.glob("appmanifest_*.acf"):
            kv = parse_kv_flat(mf.read_text(errors="replace"))
            try:
                appid = int(kv.get("appid", "0"))
            except ValueError:
                continue
            size = int(kv.get("SizeOnDisk", "0") or 0)
            if not appid or size <= 0:
                continue
            games[appid] = {
                "appid": appid,
                "name": kv.get("name", f"App {appid}"),
                "size": size,
                "last_played": int(kv.get("LastPlayed", "0") or 0),
                "installdir": str(appsdir / "common" / kv.get("installdir", "")),
            }
    return games


# ---------------------------------------------------------------------------
# Playtime — walk localconfig.vdf apps blocks for every user
# ---------------------------------------------------------------------------

def parse_playtime(root: Path) -> dict[int, dict]:
    """appid -> {'playtime': minutes, 'last_played': unix}. Summed across all
    local users."""
    out: dict[int, dict] = {}
    userdata = root / "userdata"
    if not userdata.is_dir():
        return out
    for user in userdata.iterdir():
        cfg = user / "config/localconfig.vdf"
        if not cfg.exists():
            continue
        _merge_playtime(cfg.read_text(errors="replace"), out)
    return out


def _merge_playtime(text: str, out: dict[int, dict]) -> None:
    # Isolate the Steam "apps" block, then tokenize it into a shallow tree.
    i = text.find('"apps"')
    if i == -1:
        return
    # Advance to the opening brace of the apps block.
    b = text.find("{", i)
    if b == -1:
        return
    toks = re.findall(r'"((?:[^"\\]|\\.)*)"|(\{)|(\})', text[b:])
    depth = 0
    cur_app: int | None = None
    pending_key: str | None = None
    for s, ob, cb in toks:
        if ob:
            depth += 1
            continue
        if cb:
            depth -= 1
            if depth <= 0:  # left the apps block entirely
                break
            if depth == 1:  # closed an individual app block
                cur_app = None
            pending_key = None
            continue
        # a quoted string token
        if depth == 1 and pending_key is None:
            # This string is an appid key; its value is the following { block.
            try:
                pending_key = s  # remember; app id confirmed when brace opens
                cur_app = int(s)
            except ValueError:
                cur_app = None
            pending_key = None
            continue
        if depth == 2 and cur_app is not None:
            if pending_key is None:
                pending_key = s
            else:
                if pending_key == "Playtime":
                    rec = out.setdefault(cur_app, {"playtime": 0, "last_played": 0})
                    rec["playtime"] += int(s or 0)
                elif pending_key == "LastPlayed":
                    rec = out.setdefault(cur_app, {"playtime": 0, "last_played": 0})
                    rec["last_played"] = max(rec["last_played"], int(s or 0))
                pending_key = None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def rec(source: str, name: str, size, playtime_min=0, has_playtime=False,
        last_played=0, *, art="", appid=0, tool=False) -> dict:
    """Normalized game record shared by every provider."""
    return {
        "source": source,
        "name": name.strip() or "Unknown",
        "size": int(size or 0),
        "playtime": int(playtime_min or 0),          # minutes
        "has_playtime": bool(has_playtime),
        "last_played": int(last_played or 0),         # unix seconds
        "art": art or "",                             # explicit cover URL
        "appid": int(appid or 0),                     # Steam appid → CDN art
        "tool": bool(tool),
    }


def dir_size(path: Path) -> int:
    """Total bytes of a directory tree (metadata only, errors ignored)."""
    total = 0
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    if e.is_symlink():
                        continue
                    if e.is_file():
                        total += e.stat().st_size
                    elif e.is_dir():
                        total += dir_size(Path(e.path))
                except OSError:
                    pass
    except OSError:
        pass
    return total


_SIZE_UNITS = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


def parse_size(s) -> int:
    """'71.21 GiB' / '512 MB' → bytes. Ints pass through."""
    if isinstance(s, (int, float)):
        return int(s)
    m = re.match(r"([\d.]+)\s*([KMGT]?)i?B", str(s).strip(), re.I)
    return int(float(m.group(1)) * _SIZE_UNITS[m.group(2).upper()]) if m else 0


def iso_unix(s) -> int:
    if not s:
        return 0
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(errors="replace"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Providers — each returns list[rec]. Any provider may raise; collect() guards.
# ---------------------------------------------------------------------------

def scan_steam(include_tools: bool) -> list[dict]:
    root = find_steam_root()
    if not root:
        return []
    games = scan_manifests(library_paths(root))
    playtimes = parse_playtime(root)
    rows = []
    for appid, g in games.items():
        is_tool = appid in TOOL_APPIDS or bool(TOOL_NAME_RE.match(g["name"]))
        if is_tool and not include_tools:
            continue
        pt = playtimes.get(appid, {})
        rows.append(rec(
            "Steam", g["name"], g["size"],
            pt.get("playtime", 0), True,
            max(g["last_played"], pt.get("last_played", 0)),
            appid=appid, tool=is_tool,
        ))
    return rows


def scan_lutris() -> list[dict]:
    import sqlite3
    dbs = [
        Path.home() / ".local/share/lutris/pga.db",
        Path.home() / ".var/app/net.lutris.Lutris/data/lutris/pga.db",
    ]
    coverdirs = [
        Path.home() / ".local/share/lutris/coverart",
        Path.home() / ".cache/lutris/coverart",
        Path.home() / ".var/app/net.lutris.Lutris/data/lutris/coverart",
    ]
    rows = []
    for db in dbs:
        if not db.exists():
            continue
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            q = ("SELECT name, directory, playtime, lastplayed, installed, "
                 "slug, runner FROM games")
            for name, directory, playtime, lastplayed, installed, slug, runner in con.execute(q):
                if not installed:
                    continue
                if runner == "steam":          # avoid double-counting Steam
                    continue
                d = Path(directory) if directory else None
                size = dir_size(d) if d and d.is_dir() else 0
                art = ""
                for cd in coverdirs:
                    p = cd / f"{slug}.jpg"
                    if p.exists():
                        art = p.resolve().as_uri()
                        break
                rows.append(rec(
                    "Lutris", name or slug or "Unknown", size,
                    int(round((playtime or 0) * 60)), True, lastplayed or 0, art=art,
                ))
        finally:
            con.close()
    return rows


def _heroic_base() -> Path | None:
    for b in (Path.home() / ".config/heroic",
              Path.home() / ".var/app/com.heroicgameslauncher.hgl/config/heroic"):
        if b.is_dir():
            return b
    return None


def scan_heroic() -> list[dict]:
    base = _heroic_base()
    if not base:
        return []
    ts = _load_json(base / "store/timestamp.json") or {}
    rows = []
    runners = [("legendary", "Epic"), ("gog", "GOG"), ("nile", "Amazon")]
    for runner, label in runners:
        lib = _load_json(base / f"store_cache/{runner}_library.json")
        if isinstance(lib, dict):
            lib = lib.get("library", list(lib.values()))
        if not isinstance(lib, list):
            continue
        exact = _legendary_sizes(base) if runner == "legendary" else {}
        for g in lib:
            if not isinstance(g, dict) or not g.get("is_installed"):
                continue
            app = g.get("app_name", "")
            inst = g.get("install", {}) or {}
            size = exact.get(app) or parse_size(inst.get("install_size", 0))
            if not size and inst.get("install_path"):
                size = dir_size(Path(inst["install_path"]))
            art = g.get("art_square") or g.get("art_cover") or ""
            last = iso_unix((ts.get(app) or {}).get("firstPlayed"))
            rows.append(rec(label, g.get("title", app), size, 0, False, last, art=art))
    return rows


def _legendary_sizes(base: Path) -> dict:
    d = _load_json(base / "legendaryConfig/legendary/installed.json") or {}
    return {k: int(v.get("install_size", 0)) for k, v in d.items()
            if isinstance(v, dict)}


# ---- Windows-native launchers (registry / config driven) ------------------

def _winreg_subkeys(hive, path):
    """Yield (subkey_name, opened_key) for every subkey. Windows only."""
    import winreg
    try:
        root = winreg.OpenKey(hive, path)
    except OSError:
        return
    try:
        i = 0
        while True:
            try:
                name = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            try:
                yield name, winreg.OpenKey(root, name)
            except OSError:
                pass
    finally:
        winreg.CloseKey(root)


def _winreg_val(key, name):
    import winreg
    try:
        return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def scan_epic_windows() -> list[dict]:
    md = Path(r"C:/ProgramData/Epic/EpicGamesLauncher/Data/Manifests")
    if not md.is_dir():
        return []
    rows = []
    for item in md.glob("*.item"):
        d = _load_json(item)
        if not isinstance(d, dict) or d.get("bIsIncompleteInstall"):
            continue
        loc = d.get("InstallLocation", "")
        size = int(d.get("InstallSize", 0)) or (dir_size(Path(loc)) if loc else 0)
        rows.append(rec("Epic", d.get("DisplayName", d.get("AppName", "?")),
                        size, 0, False, 0))
    return rows


def scan_gog_windows() -> list[dict]:
    import winreg
    rows = []
    for _id, key in _winreg_subkeys(winreg.HKEY_LOCAL_MACHINE,
                                    r"SOFTWARE\WOW6432Node\GOG.com\Games"):
        try:
            name = _winreg_val(key, "gameName") or "?"
            path = _winreg_val(key, "path")
            size = dir_size(Path(path)) if path else 0
            rows.append(rec("GOG", name, size, 0, False, 0))
        finally:
            winreg.CloseKey(key)
    return rows


def scan_ubisoft_windows() -> list[dict]:
    import winreg
    rows = []
    for _id, key in _winreg_subkeys(winreg.HKEY_LOCAL_MACHINE,
                                    r"SOFTWARE\WOW6432Node\Ubisoft\Launcher\Installs"):
        try:
            path = _winreg_val(key, "InstallDir")
            if not path:
                continue
            p = Path(path)
            rows.append(rec("Ubisoft", p.name or "Ubisoft game",
                            dir_size(p), 0, False, 0))
        finally:
            winreg.CloseKey(key)
    return rows


def _scan_uninstall_by_publisher(labels: dict) -> list[dict]:
    """Walk the Windows Uninstall registry and pick entries whose Publisher
    matches a known launcher. labels maps a lowercase publisher-substring →
    display source name."""
    import winreg
    rows = []
    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, path in roots:
        for _id, key in _winreg_subkeys(hive, path):
            try:
                pub = (_winreg_val(key, "Publisher") or "").lower()
                source = next((lbl for sub, lbl in labels.items() if sub in pub), None)
                if not source:
                    continue
                name = _winreg_val(key, "DisplayName")
                loc = _winreg_val(key, "InstallLocation")
                if not name or not loc or not Path(loc).is_dir():
                    continue
                rows.append(rec(source, name, dir_size(Path(loc)), 0, False, 0))
            finally:
                winreg.CloseKey(key)
    return rows


def scan_battlenet_windows() -> list[dict]:
    return _scan_uninstall_by_publisher({"blizzard": "Battle.net"})


def scan_ea_windows() -> list[dict]:
    return _scan_uninstall_by_publisher({"electronic arts": "EA"})


def scan_riot_windows() -> list[dict]:
    cfg = _load_json(Path(r"C:/ProgramData/Riot Games/RiotClientInstalls.json"))
    if not isinstance(cfg, dict):
        return []
    seen, rows = set(), []
    for key in ("associated_client", "patchlines"):
        for raw in (cfg.get(key) or {}):
            p = Path(raw)
            # associated_client maps game dir → client path; the key is the dir
            game_dir = p if p.is_dir() else p.parent
            if not game_dir.is_dir() or str(game_dir) in seen:
                continue
            seen.add(str(game_dir))
            rows.append(rec("Riot", game_dir.name, dir_size(game_dir), 0, False, 0))
    return rows


# Registry of provider callables. Windows-only ones no-op elsewhere because
# `import winreg` raises ImportError (caught in collect()).
PROVIDERS = {
    "steam": None,   # handled specially (needs include_tools)
    "lutris": scan_lutris,
    "heroic": scan_heroic,
    "epic": scan_epic_windows,
    "gog": scan_gog_windows,
    "ubisoft": scan_ubisoft_windows,
    "battlenet": scan_battlenet_windows,
    "ea": scan_ea_windows,
    "riot": scan_riot_windows,
}


# ---------------------------------------------------------------------------
# Assemble
# ---------------------------------------------------------------------------

def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def collect(include_tools: bool, only: set[str] | None = None) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    errors: list[str] = []

    def run(key, fn):
        if only and key not in only:
            return
        try:
            rows.extend(fn())
        except ImportError:
            pass  # e.g. winreg off-Windows
        except Exception as e:  # noqa: BLE001 — one bad launcher shouldn't kill the report
            errors.append(f"{key}: {e}")

    run("steam", lambda: scan_steam(include_tools))
    for key, fn in PROVIDERS.items():
        if fn is not None:
            run(key, fn)

    # Dedupe games that show up under more than one launcher (e.g. a Steam
    # title also imported into Lutris). Keep the record with the richest data.
    best: dict[str, dict] = {}
    for r in rows:
        k = _norm_name(r["name"])
        cur = best.get(k)
        score = (r["has_playtime"], r["playtime"], r["size"])
        if not cur or score > (cur["has_playtime"], cur["playtime"], cur["size"]):
            best[k] = r
    deduped = list(best.values())

    deduped.sort(key=lambda r: (r["has_playtime"], r["playtime"], r["size"]),
                 reverse=True)

    sources = sorted({r["source"] for r in deduped})
    meta = {
        "generated": int(time.time()),
        "total_size": sum(r["size"] for r in deduped),
        "total_playtime": sum(r["playtime"] for r in deduped if r["has_playtime"]),
        "count": len(deduped),
        "sources": sources,
        "errors": errors,
    }
    return deduped, meta


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------

def render(rows: list[dict], meta: dict) -> str:
    payload = json.dumps({"games": rows, "meta": meta})
    return HTML_TEMPLATE.replace("/*__DATA__*/", payload)


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>gamestat</title>
<style>
  :root{
    --bg:#0a0e14; --bg2:#0d1420; --panel:#111a28; --line:#1c2b40;
    --txt:#cfe3ff; --dim:#6d86a8; --cyan:#00f0ff; --mag:#ff3bd4;
    --heat0:#12324a; --shadow:0 0 0 1px var(--line),0 8px 30px rgba(0,0,0,.5);
  }
  *{box-sizing:border-box}
  html,body{margin:0;height:100%}
  body{
    background:radial-gradient(1200px 600px at 70% -10%,#122236 0%,var(--bg) 55%);
    color:var(--txt);font:14px/1.5 "JetBrainsMono Nerd Font","JetBrains Mono",ui-monospace,monospace;
    -webkit-font-smoothing:antialiased;
  }
  a{color:inherit}
  header{
    padding:22px 26px 16px;border-bottom:1px solid var(--line);
    display:flex;flex-wrap:wrap;align-items:flex-end;gap:26px;
    background:linear-gradient(180deg,rgba(0,240,255,.04),transparent);
  }
  h1{margin:0;font-size:26px;font-weight:800;letter-spacing:.5px}
  h1 .g{color:var(--cyan);text-shadow:0 0 18px rgba(0,240,255,.55)}
  h1 .s{color:var(--mag);text-shadow:0 0 18px rgba(255,59,212,.5)}
  .sub{color:var(--dim);font-size:12px;margin-top:3px}
  .stats{display:flex;gap:26px;margin-left:auto}
  .stat .n{font-size:22px;font-weight:800;color:#eaf6ff}
  .stat .l{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:1px}
  .bar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;padding:14px 26px;border-bottom:1px solid var(--line)}
  .seg{display:flex;border:1px solid var(--line);border-radius:9px;overflow:hidden}
  .seg button{background:transparent;color:var(--dim);border:0;padding:7px 14px;font:inherit;font-size:12px;cursor:pointer}
  .seg button.on{background:linear-gradient(180deg,rgba(0,240,255,.18),rgba(0,240,255,.06));color:var(--cyan)}
  .seg button+button{border-left:1px solid var(--line)}
  input[type=search]{
    background:var(--panel);border:1px solid var(--line);border-radius:9px;color:var(--txt);
    padding:8px 12px;font:inherit;font-size:13px;min-width:210px;outline:none;
  }
  input[type=search]:focus{border-color:var(--cyan);box-shadow:0 0 0 2px rgba(0,240,255,.15)}
  label.chk{display:flex;align-items:center;gap:7px;color:var(--dim);font-size:12px;cursor:pointer;user-select:none}
  main{padding:20px 26px 60px}

  /* ---- ranked list ---- */
  #list{display:grid;gap:9px}
  .row{
    display:grid;grid-template-columns:34px 90px 1fr 128px 110px 92px;gap:14px;align-items:center;
    background:linear-gradient(90deg,var(--panel),var(--bg2));border:1px solid var(--line);
    border-radius:12px;padding:9px 14px 9px 10px;box-shadow:var(--shadow);position:relative;overflow:hidden;
  }
  .row::before{content:"";position:absolute;inset:0;width:var(--fill,0%);
    background:linear-gradient(90deg,rgba(0,240,255,.10),rgba(255,59,212,.05));pointer-events:none}
  .row>*{position:relative}
  .rank{font-weight:800;color:var(--dim);text-align:center}
  .row.top .rank{color:var(--cyan)}
  .cap{position:relative;width:90px;height:42px;border-radius:6px;overflow:hidden;
    box-shadow:inset 0 0 0 1px var(--line)}
  .cap .ini{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
    font-weight:800;font-size:15px;color:rgba(255,255,255,.85);letter-spacing:1px}
  .cap img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
  .nm{font-weight:600;color:#eaf6ff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .nm small{display:block;color:var(--dim);font-size:11px;font-weight:400}
  .pt{font-weight:700}
  .pt .u{color:var(--dim);font-weight:400;font-size:11px}
  .col{text-align:right;font-variant-numeric:tabular-nums}
  .muted{color:var(--dim)}
  .pill{display:inline-block;font-size:10px;color:var(--mag);border:1px solid rgba(255,59,212,.4);
    border-radius:20px;padding:1px 7px;margin-left:6px;vertical-align:middle}
  .badge{display:inline-block;font-size:10px;font-weight:700;padding:1px 8px;border-radius:20px;
    margin-left:8px;vertical-align:middle;border:1px solid}
  .chips{display:flex;gap:6px;flex-wrap:wrap}
  .chip{background:transparent;border:1px solid var(--line);color:var(--dim);border-radius:20px;
    padding:5px 11px;font:inherit;font-size:11px;cursor:pointer;transition:all .12s}
  .chip.on{color:var(--c);border-color:color-mix(in srgb,var(--c) 55%,transparent);
    background:color-mix(in srgb,var(--c) 13%,transparent);box-shadow:0 0 12px -5px var(--c)}

  /* ---- treemap ---- */
  #treemap{position:relative;width:100%;height:74vh;border:1px solid var(--line);border-radius:14px;
    overflow:hidden;background:var(--bg2);box-shadow:var(--shadow)}
  .tile{position:absolute;overflow:hidden;border:1px solid rgba(0,0,0,.55);cursor:default;
    transition:filter .12s, transform .12s;background-size:cover;background-position:center}
  .tile:hover{filter:brightness(1.25) saturate(1.2);z-index:5;transform:scale(1.008)}
  .tile .t{position:absolute;inset:0;padding:8px 9px;display:flex;flex-direction:column;justify-content:flex-end;
    background:linear-gradient(0deg,rgba(4,8,14,.82),rgba(4,8,14,.12) 55%,transparent)}
  .tile .tn{font-size:12px;font-weight:700;color:#fff;text-shadow:0 1px 3px #000;line-height:1.2;
    display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
  .tile .ts{font-size:11px;color:#bfe9ff;text-shadow:0 1px 2px #000;margin-top:2px}
  .tile .tbadge{position:absolute;top:6px;left:8px;font-size:10px;font-weight:800;
    letter-spacing:.3px;text-shadow:0 1px 3px #000}
  .legend{display:flex;align-items:center;gap:8px;color:var(--dim);font-size:11px;margin:12px 2px 0}
  .grad{height:9px;width:180px;border-radius:5px;background:linear-gradient(90deg,var(--heat0),var(--cyan) 55%,var(--mag))}
  .hide{display:none!important}
  .empty{color:var(--dim);text-align:center;padding:60px}
  footer{color:var(--dim);font-size:11px;padding:0 26px 30px}
</style>
</head>
<body>
<header>
  <div>
    <h1><span class="g">game</span><span class="s">stat</span></h1>
    <div class="sub" id="sub">·</div>
  </div>
  <div class="stats">
    <div class="stat"><div class="n" id="s-count">·</div><div class="l">Games</div></div>
    <div class="stat"><div class="n" id="s-launch">·</div><div class="l">Launchers</div></div>
    <div class="stat"><div class="n" id="s-size">·</div><div class="l">On Disk</div></div>
    <div class="stat"><div class="n" id="s-time">·</div><div class="l">Played</div></div>
  </div>
</header>

<div class="bar">
  <div class="seg" id="view">
    <button data-v="list" class="on">▤ Ranked list</button>
    <button data-v="tree">▦ Disk treemap</button>
  </div>
  <div class="seg" id="sort">
    <button data-s="playtime" class="on">Most played</button>
    <button data-s="size">Biggest</button>
    <button data-s="last_played">Recent</button>
    <button data-s="name">A–Z</button>
  </div>
  <input type="search" id="q" placeholder="filter games…" autocomplete="off">
  <label class="chk"><input type="checkbox" id="tools"> show tools/runtimes</label>
</div>
<div class="bar" id="srcbar">
  <span class="l" style="color:var(--dim);font-size:11px;letter-spacing:1px">LAUNCHERS</span>
  <div class="chips" id="sources"></div>
</div>

<main>
  <div id="list"></div>
  <div id="tree" class="hide">
    <div id="treemap"></div>
    <div class="legend">less played <span class="grad"></span> more played · tile size = disk usage</div>
  </div>
</main>
<footer id="foot"></footer>

<script>
const DATA = /*__DATA__*/;
const $ = s => document.querySelector(s);
const state = {view:"list", sort:"playtime", q:"", tools:false, sources:new Set(DATA.meta.sources)};

const SRC = {Steam:'#66c0f4', Epic:'#d580ff', GOG:'#a259ff', Amazon:'#ff9900',
  Lutris:'#ff6b1a', 'Battle.net':'#00a8ff', Riot:'#ff4655', EA:'#ff5c5c', Ubisoft:'#3a9bff'};
const srcColor = s => SRC[s] || '#7aa2c8';

const fmtSize = b => { const u=["B","KB","MB","GB","TB"]; let i=0; while(b>=1024&&i<4){b/=1024;i++;} return b.toFixed(b<10&&i>0?1:0)+" "+u[i]; };
const fmtTime = m => { if(!m) return "—"; const h=m/60; if(h<1) return m+"m"; if(h<100) return h.toFixed(1)+"h"; return Math.round(h)+"h"; };
const fmtDate = t => { if(!t) return "never"; const d=(Date.now()/1000-t)/86400; if(d<1) return "today"; if(d<2) return "yesterday"; if(d<30) return Math.round(d)+"d ago"; if(d<365) return Math.round(d/30)+"mo ago"; return Math.round(d/365)+"y ago"; };
const steamCdn = id => `https://cdn.cloudflare.steamstatic.com/steam/apps/${id}/header.jpg`;
const artUrl = g => g.art ? g.art : (g.appid ? steamCdn(g.appid) : "");
const initials = n => (n.replace(/[^A-Za-z0-9 ]/g,'').split(/\s+/).filter(Boolean).slice(0,2).map(w=>w[0]).join('')||'?').toUpperCase();
const capHtml = g => {
  const url=artUrl(g), c=srcColor(g.source);
  const img = url ? `<img src="${url}" onerror="this.remove()">` : '';
  return `<div class="cap" style="background:linear-gradient(135deg,${c}33,#0b1420)"><span class="ini">${esc(initials(g.name))}</span>${img}</div>`;
};
const badge = s => `<span class="badge" style="color:${srcColor(s)};border-color:${srcColor(s)}66;background:${srcColor(s)}18">${esc(s)}</span>`;
const heat = f => { // 0..1 -> heat0 -> cyan -> mag
  const mix=(a,b,t)=>a.map((v,i)=>Math.round(v+(b[i]-v)*t));
  const c0=[18,50,74],c1=[0,240,255],c2=[255,59,212];
  const rgb = f<.5?mix(c0,c1,f/.5):mix(c1,c2,(f-.5)/.5);
  return `rgb(${rgb.join(",")})`;
};
// metric used for the current sort (playtime treats "unknown" as -1 so it sinks)
const metricOf = x => state.sort==="name" ? (x.has_playtime?x.playtime:0)
  : state.sort==="playtime" ? (x.has_playtime?x.playtime:-1) : (x[state.sort]||0);

function visible(){
  let g = DATA.games.slice();
  if(!state.tools) g = g.filter(x=>!x.tool);
  g = g.filter(x=>state.sources.has(x.source));
  if(state.q){ const q=state.q.toLowerCase(); g = g.filter(x=>x.name.toLowerCase().includes(q)); }
  const s = state.sort;
  if(s==="name") g.sort((a,b)=>a.name.localeCompare(b.name));
  else if(s==="playtime") g.sort((a,b)=> (b.has_playtime-a.has_playtime) || (b.playtime-a.playtime) || (b.size-a.size));
  else g.sort((a,b)=> (b[s]-a[s]) || (b.has_playtime-a.has_playtime) || (b.playtime-a.playtime));
  return g;
}

function renderStats(){
  const g = state.tools?DATA.games:DATA.games.filter(x=>!x.tool);
  $("#s-count").textContent = g.length;
  $("#s-launch").textContent = DATA.meta.sources.length;
  $("#s-size").textContent = fmtSize(g.reduce((s,x)=>s+x.size,0));
  $("#s-time").textContent = fmtTime(g.filter(x=>x.has_playtime).reduce((s,x)=>s+x.playtime,0));
  const d=new Date(DATA.meta.generated*1000);
  $("#sub").textContent = DATA.meta.sources.join('  ·  ') || 'no launchers found';
  $("#foot").textContent = `generated ${d.toLocaleString()} · playtime shown where the launcher records it (Steam, Lutris) · cover art © respective stores`;
}

function renderList(){
  const g = visible();
  const max = Math.max(1, ...g.map(x=>Math.max(0,metricOf(x))));
  const el = $("#list");
  if(!g.length){ el.innerHTML='<div class="empty">no games match.</div>'; return; }
  el.innerHTML = g.map((x,i)=>{
    const fill = Math.round(100*Math.max(0,metricOf(x))/max);
    const pt = x.has_playtime
      ? `${fmtTime(x.playtime)} <span class="u">played</span>`
      : `<span class="muted">—</span>`;
    return `<div class="row ${i<3?'top':''}" style="--fill:${fill}%">
      <div class="rank">${i+1}</div>
      ${capHtml(x)}
      <div class="nm">${esc(x.name)}${badge(x.source)}${x.tool?'<span class="pill">tool</span>':''}
        <small>${x.appid?'appid '+x.appid:'installed game'}</small></div>
      <div class="col pt">${pt}</div>
      <div class="col">${fmtSize(x.size)}</div>
      <div class="col muted">${fmtDate(x.last_played)}</div>
    </div>`;
  }).join("");
}

// squarified treemap
function squarify(items, x, y, w, h){
  const out=[]; const total=items.reduce((s,i)=>s+i.value,0)||1;
  let area=items.map(i=>({...i, a:i.value/total*w*h}));
  const worst=(row,len)=>{ const s=row.reduce((a,b)=>a+b.a,0); const mx=Math.max(...row.map(r=>r.a)),mn=Math.min(...row.map(r=>r.a));
    return Math.max(len*len*mx/(s*s), s*s/(len*len*mn)); };
  let rx=x,ry=y,rw=w,rh=h;
  while(area.length){
    const horiz = rw>=rh; const len = horiz?rh:rw;
    let row=[];
    while(area.length){
      const test=[...row, area[0]];
      if(row.length && worst(test,len)>worst(row,len)) break;
      row.push(area.shift());
    }
    const rowsum=row.reduce((a,b)=>a+b.a,0); const thick=rowsum/len;
    let off = horiz?ry:rx;
    for(const it of row){
      const cell = it.a/thick;
      if(horiz) out.push({...it, x:rx, y:off, w:thick, h:cell});
      else      out.push({...it, x:off, y:ry, w:cell, h:thick});
      off += cell;
    }
    if(horiz){ rx+=thick; rw-=thick; } else { ry+=thick; rh-=thick; }
  }
  return out;
}

function renderTree(){
  const box=$("#treemap"); const W=box.clientWidth, H=box.clientHeight;
  let g=visible().filter(x=>x.size>0);
  if(!g.length){ box.innerHTML='<div class="empty">no games match.</div>'; return; }
  const items=g.map(x=>({value:x.size, g:x}));
  const maxPt=Math.max(1,...g.map(x=>x.playtime));
  const tiles=squarify(items,0,0,W,H);
  box.innerHTML=tiles.map(t=>{
    const x=t.g; const f=x.has_playtime?Math.sqrt(x.playtime/maxPt):0;
    const big = t.w>78 && t.h>46;
    const url = artUrl(x), c = srcColor(x.source);
    const ptTxt = x.has_playtime?fmtTime(x.playtime):'playtime n/a';
    return `<div class="tile" title="${esc(x.name)} · ${esc(x.source)} — ${fmtSize(x.size)}, ${ptTxt}"
      style="left:${t.x}px;top:${t.y}px;width:${t.w}px;height:${t.h}px;
      background-color:${heat(f)};box-shadow:inset 0 3px 0 ${c};
      background-image:${big&&url?`url('${url}')`:'none'}">
      <div class="t">${big?`<div class="tn">${esc(x.name)}</div><div class="ts">${fmtSize(x.size)} · ${ptTxt}</div>`:''}</div>
      ${big?`<span class="tbadge" style="color:${c}">${esc(x.source)}</span>`:''}
    </div>`;
  }).join("");
}

function esc(s){ return s.replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

function render(){
  renderStats();
  $("#list").classList.toggle("hide", state.view!=="tree"?false:true);
  $("#tree").classList.toggle("hide", state.view==="tree"?false:true);
  if(state.view==="tree") renderTree(); else renderList();
}

function buildSources(){
  const el=$("#sources");
  el.innerHTML = DATA.meta.sources.map(s=>
    `<button class="chip on" data-src="${esc(s)}" style="--c:${srcColor(s)}">${esc(s)}</button>`).join("");
  el.onclick=e=>{ const b=e.target.closest("button"); if(!b) return;
    b.classList.toggle("on");
    state.sources = new Set([...el.querySelectorAll("button.on")].map(x=>x.dataset.src));
    render();
  };
  if(DATA.meta.sources.length<2) $("#srcbar").style.display="none";
}

$("#view").onclick=e=>{const b=e.target.closest("button"); if(!b)return;
  state.view=b.dataset.v; [...e.currentTarget.children].forEach(c=>c.classList.toggle("on",c===b)); render();};
$("#sort").onclick=e=>{const b=e.target.closest("button"); if(!b)return;
  state.sort=b.dataset.s; [...e.currentTarget.children].forEach(c=>c.classList.toggle("on",c===b)); render();};
$("#q").oninput=e=>{state.q=e.target.value.trim(); render();};
$("#tools").onchange=e=>{state.tools=e.target.checked; render();};
addEventListener("resize",()=>{ if(state.view==="tree") renderTree(); });
buildSources();
render();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="WinDirStat for your game library — all launchers.")
    ap.add_argument("-o", "--output", default=str(Path.home() / ".cache/gamestat/report.html"))
    ap.add_argument("--all", action="store_true", help="include Proton/runtimes/redistributables")
    ap.add_argument("--only", default="", metavar="LAUNCHERS",
                    help="comma-separated subset, e.g. steam,epic,lutris "
                         f"(available: {','.join(['steam', *PROVIDERS])})")
    ap.add_argument("--no-open", action="store_true", help="don't open a browser")
    ap.add_argument("--json", action="store_true", help="print raw data as JSON and exit")
    args = ap.parse_args()

    only = {s.strip().lower() for s in args.only.split(",") if s.strip()} or None
    rows, meta = collect(include_tools=args.all, only=only)

    if args.json:
        print(json.dumps({"games": rows, "meta": meta}, indent=2))
        return

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(rows, meta), encoding="utf-8")

    gb = meta["total_size"] / 1024**3
    hrs = meta["total_playtime"] / 60
    src = ", ".join(meta["sources"]) or "no launchers found"
    print(f"gamestat · {meta['count']} games · {gb:.1f} GB on disk · {hrs:.0f} h played")
    print(f"launchers: {src}")
    for e in meta["errors"]:
        print(f"  ! {e}", file=sys.stderr)
    print(f"report → {out}")
    if not args.no_open:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
