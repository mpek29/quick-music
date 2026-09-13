#!/usr/bin/env python3
"""quick-music - a Spotify playlist becomes tagged MP3s, then lands on your phone.

    python quickmusic.py get <spotify-playlist-url>
    python quickmusic.py push

No API key, no account, no third-party site.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}

# Alternate cuts YouTube loves to surface when you did not ask for them.
NOISE = ("live", "cover", "reaction", "lyrics", "sped up", "slowed", "8d", "nightcore")

# Illegal on FAT32/exFAT, i.e. on most phone storage.
ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

CACHE = ".search-cache.json"
_print_lock = threading.Lock()


def say(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def safe(name: str, maxlen: int = 60) -> str:
    name = ILLEGAL.sub("", name).strip().rstrip(".")
    return re.sub(r"\s+", " ", name)[:maxlen].strip() or "untitled"


# --------------------------------------------------------------------------
# 1. Spotify - read a public playlist through its embed page (no API key)
# --------------------------------------------------------------------------


def fetch_playlist(url_or_id: str) -> dict:
    m = re.search(r"playlist[/:]([A-Za-z0-9]+)", url_or_id)
    pid = m.group(1) if m else url_or_id
    req = urllib.request.Request(
        f"https://open.spotify.com/embed/playlist/{pid}", headers={"User-Agent": UA}
    )
    html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8")
    raw = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S
    )
    if not raw:
        raise SystemExit("Spotify returned an unexpected page - is the playlist public?")
    ent = json.loads(raw.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    tracks = ent.get("trackList", [])
    if not tracks:
        raise SystemExit("No tracks found - is the playlist public?")
    return {
        "name": ent.get("name") or "playlist",
        "owner": ent.get("subtitle") or "",
        "tracks": [
            {
                "n": i,
                "title": t.get("title"),
                "artist": t.get("subtitle"),
                "seconds": round(t["duration"] / 1000) if t.get("duration") else None,
            }
            for i, t in enumerate(tracks, 1)
        ],
    }


# --------------------------------------------------------------------------
# 2. YouTube - search, then score the candidates against the Spotify metadata
# --------------------------------------------------------------------------


def tokens(s: str) -> set:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return set(re.sub(r"[^a-z0-9\s]", " ", s).split())


def to_seconds(txt: str | None) -> int | None:
    if not txt:
        return None
    parts = [int(p) for p in txt.split(":") if p.isdigit()]
    if not parts:
        return None
    total = 0
    for p in parts:
        total = total * 60 + p
    return total


def search(query: str) -> list:
    url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query)
    html = (
        urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=30)
        .read()
        .decode("utf-8", "replace")
    )
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", html, re.S)
    if not m:
        return []
    try:
        sections = json.loads(m.group(1))["contents"]["twoColumnSearchResultsRenderer"][
            "primaryContents"
        ]["sectionListRenderer"]["contents"]
    except (KeyError, ValueError):
        return []
    out = []
    for sec in sections:
        for item in sec.get("itemSectionRenderer", {}).get("contents", []):
            v = item.get("videoRenderer")
            if not v:
                continue
            length = v.get("lengthText", {}).get("simpleText")
            out.append(
                {
                    "id": v["videoId"],
                    "title": "".join(r["text"] for r in v["title"]["runs"]),
                    "channel": v.get("ownerText", {}).get("runs", [{}])[0].get("text", ""),
                    "length": length,
                    "seconds": to_seconds(length),
                }
            )
    return out


def score(cand: dict, title: str, artist: str, want: int | None) -> float:
    """Higher is better. Duration is the most trustworthy signal, so it weighs most."""
    s = 0.0
    ct, cc = tokens(cand["title"]), tokens(cand["channel"])
    wt, wa = tokens(title), tokens(artist)

    if wt:
        s += 40 * len(wt & ct) / len(wt)
    if wa:
        s += 25 * len(wa & (ct | cc)) / len(wa)

    if want and cand["seconds"]:
        delta = abs(cand["seconds"] - want)
        s += 35 if delta <= 3 else 20 if delta <= 10 else 5 if delta <= 25 else -min(30, delta / 4)
    else:
        s -= 5

    if cand["channel"].lower().endswith("- topic"):  # auto-generated official channel
        s += 12
    if wa and wa & cc:
        s += 10

    low = cand["title"].lower()
    for w in NOISE:
        if w in low and w not in title.lower():
            s -= 18
    return s


def resolve(playlist: dict, cache_path: str) -> None:
    """Attach the best YouTube match and a confidence score to every track."""
    cache = {}
    if os.path.exists(cache_path):
        try:
            cache = json.load(open(cache_path, encoding="utf-8"))
        except ValueError:
            pass

    total = len(playlist["tracks"])
    for t in playlist["tracks"]:
        key = f"{t['artist']} {t['title']}"
        if key not in cache:
            first = re.split(r"[,&]", t["artist"] or "")[0].strip()
            bare = re.sub(r"[(\[].*", "", t["title"] or "").strip()
            found = []
            # YouTube sometimes returns nothing for the exact query; degrade gradually.
            for q in dict.fromkeys([key, f"{t['title']} {first}", f"{first} {bare}", t["title"]]):
                try:
                    found = search(q)[:12]
                except Exception as exc:
                    say(f"  ! search failed for {t['title']}: {exc}")
                time.sleep(0.8)
                if found:
                    break
            cache[key] = found
            json.dump(cache, open(cache_path, "w", encoding="utf-8"), ensure_ascii=False)

        ranked = sorted(cache[key], key=lambda c: score(c, t["title"], t["artist"], t["seconds"]), reverse=True)
        best = ranked[0] if ranked else None
        t["url"] = f"https://www.youtube.com/watch?v={best['id']}" if best else None
        t["confidence"] = round(score(best, t["title"], t["artist"], t["seconds"])) if best else 0
        say(f"  [{t['n']:3}/{total}] {t['confidence']:3}  {t['artist']} - {t['title']}")


# --------------------------------------------------------------------------
# 3. Download - straight to the final filename, then tag from Spotify metadata
# --------------------------------------------------------------------------


def find_ffmpeg() -> str | None:
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg", "ffmpeg.exe")
    return shutil.which("ffmpeg") or (local if os.path.exists(local) else None)


def js_runtimes() -> dict:
    """YouTube needs a JS runtime to decipher stream URLs; without one you get HTTP 403."""
    return {name: {} for name in ("deno", "node", "bun") if shutil.which(name)}


def download_one(track: dict, dest: str, total: int, quality: int, cookies: str | None) -> str | None:
    import yt_dlp

    stem = "%02d - %s - %s" % (track["i"], safe(track["artist"], 40), safe(track["title"]))
    target = os.path.join(dest, stem + ".mp3")
    if os.path.exists(target):
        say(f"  = {stem}")
        return target

    opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(dest, stem + ".%(ext)s"),
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(quality)}
        ],
        "js_runtimes": js_runtimes(),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    if ff := find_ffmpeg():
        opts["ffmpeg_location"] = ff
    if cookies:
        opts["cookiesfrombrowser"] = (cookies,)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([track["url"]])
    except Exception as exc:
        msg = str(exc).split("\n")[0]
        if "confirm your age" in msg:
            msg = "age-restricted (retry with --cookies firefox)"
        say(f"  x {stem}  ->  {msg}")
        return None

    tag(target, track, total)
    say(f"  + {stem}")
    return target


def tag(path: str, track: dict, total: int) -> None:
    """Write the Spotify names, not the YouTube video title."""
    try:
        from mutagen.easyid3 import EasyID3
        from mutagen.mp3 import MP3
    except ImportError:
        return
    try:
        audio = MP3(path, ID3=EasyID3)
        audio["title"] = track["title"]
        audio["artist"] = track["artist"]
        audio["album"] = track["album"]
        audio["albumartist"] = track["albumartist"] or track["album"]
        audio["tracknumber"] = f"{track['i']}/{total}"
        audio.save()
    except Exception:
        pass


# --------------------------------------------------------------------------
# 4. Android - push the album over ADB (same command on Windows, Linux and WSL)
# --------------------------------------------------------------------------


def find_adb() -> str:
    found = shutil.which("adb")
    if found:
        return found
    for guess in (
        os.path.expandvars(r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe"),
        os.path.expanduser("~/Android/Sdk/platform-tools/adb"),
        os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"),
    ):
        if os.path.exists(guess):
            return guess
    raise SystemExit(
        "adb not found. Install the Android platform-tools:\n"
        "  Windows  winget install Google.PlatformTools\n"
        "  Ubuntu   sudo apt install adb\n"
        "  macOS    brew install android-platform-tools"
    )


def adb_state(adb: str) -> str:
    """One of: ready, unauthorized, unplugged."""
    try:
        out = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return "unplugged"
    for line in out.splitlines()[1:]:
        line = line.strip()
        if line.endswith("\tdevice"):
            return "ready"
        if line.endswith("\tunauthorized"):
            return "unauthorized"
    return "unplugged"


def push(adb: str, folder: str) -> int:
    album = os.path.basename(folder)
    remote = f"/sdcard/Music/{album}"
    subprocess.run([adb, "shell", "mkdir", "-p", f"'{remote}'"], capture_output=True)

    have = subprocess.run(
        [adb, "shell", "ls", f"'{remote}'"], capture_output=True, text=True
    ).stdout.splitlines()
    already = {line.strip() for line in have}

    files = sorted(f for f in os.listdir(folder) if f.lower().endswith(".mp3"))
    sent = 0
    for i, f in enumerate(files, 1):
        if f in already:
            sent += 1
            say(f"  [{i}/{len(files)}] = {f}")
            continue
        r = subprocess.run(
            [adb, "push", os.path.join(folder, f), remote], capture_output=True, text=True
        )
        if r.returncode == 0:
            sent += 1
            say(f"  [{i}/{len(files)}] {f}")
        else:
            say(f"  [{i}/{len(files)}] x {f}  ->  {r.stderr.strip()[:120]}")

    # Ask Android to index the new files now, instead of waiting for a reboot.
    subprocess.run(
        [adb, "shell", "am", "broadcast", "-a",
         "android.intent.action.MEDIA_SCANNER_SCAN_FILE", "-d", f"file://{remote}"],
        capture_output=True,
    )
    return sent


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def albums(root: str) -> list:
    if not os.path.isdir(root):
        return []
    return sorted(
        (os.path.join(root, d) for d in os.listdir(root)
         if os.path.isdir(os.path.join(root, d))
         and any(f.lower().endswith(".mp3") for f in os.listdir(os.path.join(root, d)))),
        key=os.path.getmtime,
    )


def cmd_get(args: argparse.Namespace) -> int:
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        raise SystemExit("Missing dependency. Run: pip install -r requirements.txt")
    if not find_ffmpeg():
        raise SystemExit(
            "ffmpeg not found. Install it (winget install Gyan.FFmpeg) "
            "or drop ffmpeg.exe into ./ffmpeg/."
        )
    if not js_runtimes():
        say("! No JavaScript runtime (node/deno/bun) - YouTube may answer 403.")

    say("Reading the Spotify playlist...")
    pl = fetch_playlist(args.url)
    say(f"  {pl['name']} - {pl['owner']} ({len(pl['tracks'])} tracks)\n")

    dest = os.path.join(args.out, safe(pl["name"], 70))
    os.makedirs(dest, exist_ok=True)

    say("Matching each track on YouTube...")
    resolve(pl, os.path.join(dest, CACHE))

    chosen = [t for t in pl["tracks"] if t["url"] and t["confidence"] >= args.min_confidence]
    skipped = [t for t in pl["tracks"] if t not in chosen]
    for i, t in enumerate(chosen, 1):
        t.update(i=i, album=pl["name"], albumartist=pl["owner"])

    say(f"\nDownloading {len(chosen)} tracks at {args.quality} kbps into {dest}")
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        done = list(pool.map(
            lambda t: download_one(t, dest, len(chosen), args.quality, args.cookies), chosen
        ))
    got = [p for p in done if p]

    say(f"\n{len(got)}/{len(chosen)} tracks in {dest}")
    if skipped:
        say(f"\nSkipped, no confident match (confidence < {args.min_confidence}):")
        for t in skipped:
            say(f"  {t['n']:3}. {t['artist']} - {t['title']}  ({t['confidence']})")
        say("  Lower the bar with --min-confidence if you want them anyway.")
    say(f"\nNext: python {os.path.basename(__file__)} push")
    return 0 if got else 1


def cmd_push(args: argparse.Namespace) -> int:
    folder = args.folder
    if not folder:
        found = albums(args.out)
        if not found:
            raise SystemExit(f"No album folder in {args.out}. Run 'get' first.")
        folder = found[-1]
    folder = os.path.abspath(folder)
    files = [f for f in os.listdir(folder) if f.lower().endswith(".mp3")]
    if not files:
        raise SystemExit(f"No MP3 in {folder}")

    adb = find_adb()
    state = adb_state(adb)
    if state == "unauthorized":
        raise SystemExit(
            "Phone detected but not authorised.\n"
            "Unlock it, tick 'Always allow from this computer' on the USB debugging "
            "prompt, then run this again."
        )
    if state == "unplugged":
        raise SystemExit(
            "No phone detected. Plug it in over USB and turn on "
            "Settings > Developer options > USB debugging."
        )

    say(f"Sending {len(files)} tracks from {os.path.basename(folder)}")
    sent = push(adb, folder)

    say(f"\n{sent}/{len(files)} tracks in Music/{os.path.basename(folder)} on the phone.")
    if sent < len(files):
        say("Run it again to resend what is missing; files already there are skipped.")
        return 1
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="quickmusic", description=__doc__.splitlines()[0])
    p.add_argument("--out", default="music",
                   help="where album folders live, one per playlist (default: music/)")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("get", help="Spotify playlist -> tagged MP3s")
    g.add_argument("url", help="public Spotify playlist URL or id")
    g.add_argument("-q", "--quality", type=int, choices=[128, 192, 256, 320], default=256)
    g.add_argument("-j", "--jobs", type=int, default=3, help="parallel downloads (default: 3)")
    g.add_argument("--min-confidence", type=int, default=90,
                   help="drop weaker YouTube matches (default: 90)")
    g.add_argument("--cookies", metavar="BROWSER",
                   help="borrow cookies from firefox/chrome/edge for age-restricted videos")
    g.set_defaults(func=cmd_get)

    s = sub.add_parser("push", help="send an album folder to an Android phone")
    s.add_argument("folder", nargs="?", help="album folder (default: the most recent one)")
    s.set_defaults(func=cmd_push)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):  # Windows consoles default to cp1252
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
