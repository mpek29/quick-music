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
EMBED_CAP = 100  # the embed page hands back at most this many tracks, whatever the playlist
_print_lock = threading.Lock()


def say(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def safe(name: str, maxlen: int = 60) -> str:
    name = ILLEGAL.sub("", name).strip().rstrip(".")
    return re.sub(r"\s+", " ", name)[:maxlen].strip() or "untitled"


# --------------------------------------------------------------------------
# The live view - the whole tracklist on screen, one bar per track
# --------------------------------------------------------------------------

LABEL = 44  # fixed, so every bar starts in the same column


def plain(text: str) -> str:
    return text.replace("[", "(").replace("]", ")")  # rich reads [...] as markup


def fit(text: str, width: int = LABEL) -> str:
    text = plain(text)
    return text[: width - 1] + "…" if len(text) > width else text.ljust(width)


class View:
    """A counter on top, then one row per track underneath.

    A finished row is dropped from the list, so the window keeps showing the
    tracks still working rather than a wall of completed ones. Rows that need
    attention - a weak match, a failed download - stay put in red.
    """

    def __init__(self, title: str, count: int, downloading: bool):
        from rich.console import Group
        from rich.live import Live
        from rich.progress import (
            BarColumn,
            DownloadColumn,
            MofNCompleteColumn,
            Progress,
            SpinnerColumn,
            TextColumn,
            TimeElapsedColumn,
            TimeRemainingColumn,
            TransferSpeedColumn,
        )

        tail = (
            [DownloadColumn(), TransferSpeedColumn(), TimeRemainingColumn()]
            if downloading
            else [TextColumn("{task.fields[note]}")]
        )
        self.rows = Progress(SpinnerColumn(), TextColumn("{task.description}"), BarColumn(), *tail)
        self.head = Progress(
            TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(), TimeElapsedColumn()
        )
        self.counter = self.head.add_task(title, total=count)
        # Crop rather than fight the scrollback if the survivors still overflow.
        self.live = Live(Group(self.head, self.rows), refresh_per_second=10,
                         vertical_overflow="ellipsis")

    def __enter__(self) -> "View":
        self.live.__enter__()
        return self

    def __exit__(self, *exc) -> None:
        self.live.__exit__(*exc)

    def add(self, label: str, **fields):
        return self.rows.add_task(label, start=False, total=None, **fields)

    def start(self, task) -> None:
        self.rows.start_task(task)

    def update(self, task, **kw) -> None:
        self.rows.update(task, **kw)

    def done(self, task, ok: bool = True, **kw) -> None:
        """Retire a row: count it and drop it, or leave it on screen if it went wrong."""
        self.rows.update(task, **kw)
        if ok:
            self.head.advance(self.counter)
            self.rows.update(task, visible=False)


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


def candidates(track: dict, note) -> list:
    """Search YouTube for one track, degrading the query until something comes back."""
    first = re.split(r"[,&]", track["artist"] or "")[0].strip()
    bare = re.sub(r"[(\[].*", "", track["title"] or "").strip()
    queries = list(dict.fromkeys(
        [f"{track['artist']} {track['title']}", f"{track['title']} {first}",
         f"{first} {bare}", track["title"]]
    ))
    for i, q in enumerate(queries):
        note(f"searching {i + 1}/{len(queries)}")
        try:
            if found := search(q)[:12]:
                return found
        except Exception as exc:
            note(str(exc).split("\n")[0][:30])
        if i + 1 < len(queries):  # back off only before trying another wording
            time.sleep(0.8)
    return []


def resolve(playlist: dict, cache_path: str, jobs: int, floor: int) -> None:
    """Attach the best YouTube match and a confidence score to every track."""
    cache = {}
    if os.path.exists(cache_path):
        try:
            cache = json.load(open(cache_path, encoding="utf-8"))
        except ValueError:
            pass

    tracks = playlist["tracks"]
    lock = threading.Lock()

    def match(t: dict, task) -> None:
        key = f"{t['artist']} {t['title']}"
        view.start(task)
        if key not in cache:
            found = candidates(t, lambda msg: view.update(task, note=plain(msg)))
            with lock:
                cache[key] = found
                # Written after every track so an interrupted run keeps its searches.
                json.dump(cache, open(cache_path, "w", encoding="utf-8"), ensure_ascii=False)
        else:
            view.update(task, note="cached")

        ranked = sorted(cache[key], key=lambda c: score(c, t["title"], t["artist"], t["seconds"]), reverse=True)
        best = ranked[0] if ranked else None
        t["url"] = f"https://www.youtube.com/watch?v={best['id']}" if best else None
        t["confidence"] = round(score(best, t["title"], t["artist"], t["seconds"])) if best else 0
        # A match below the bar keeps its row; completed == total is what stops its spinner.
        weak = t["confidence"] < floor
        view.done(
            task,
            ok=not weak,
            total=1,
            completed=1,
            note=f"[red]{t['confidence']} too low[/red]" if weak else f"[green]{t['confidence']}[/green]",
        )

    with View(f"Matching {len(tracks)} tracks on YouTube", len(tracks), downloading=False) as view:
        tasks = [view.add(fit(f"{t['artist']} - {t['title']}"), note="queued") for t in tracks]
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
            list(pool.map(lambda p: match(*p), zip(tracks, tasks)))


# --------------------------------------------------------------------------
# 3. Download - straight to the final filename, then tag from Spotify metadata
# --------------------------------------------------------------------------


def find_ffmpeg() -> str | None:
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg", "ffmpeg.exe")
    return shutil.which("ffmpeg") or (local if os.path.exists(local) else None)


def js_runtimes() -> dict:
    """YouTube needs a JS runtime to decipher stream URLs; without one you get HTTP 403."""
    return {name: {} for name in ("deno", "node", "bun") if shutil.which(name)}


def download_one(track: dict, dest: str, total: int, quality: int, cookies: str | None,
                 view, task) -> str | None:
    import yt_dlp

    label = fit(f"{track['artist']} - {track['title']}")
    stem = "%02d - %s - %s" % (track["i"], safe(track["artist"], 40), safe(track["title"]))
    target = os.path.join(dest, stem + ".mp3")
    if os.path.exists(target):
        view.done(task)
        return target

    def hook(d: dict) -> None:
        if d["status"] == "downloading":
            size = d.get("total_bytes") or d.get("total_bytes_estimate")
            if size:  # a stream with no announced length keeps its bar pulsing
                view.update(task, total=size, completed=d.get("downloaded_bytes") or 0)
        elif d["status"] == "finished":
            view.update(task, completed=d.get("total_bytes"),
                        description=f"[cyan]{label}[/cyan]")

    view.start(task)
    opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(dest, stem + ".%(ext)s"),
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(quality)}
        ],
        "js_runtimes": js_runtimes(),
        "progress_hooks": [hook],
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,  # yt-dlp's own bar would fight ours for the same lines
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
            msg = "age-restricted, retry with --cookies firefox"
        # Keep the track name on the row - the reason goes in the summary below the list.
        # The row stays on screen; total == completed stops its spinner, zero keeps
        # the byte count honest, and the reason goes in the summary below the list.
        track["error"] = msg
        view.done(task, ok=False, description=f"[red]{label}[/red]", total=0, completed=0)
        return None

    tag(target, track, total)
    view.done(task)
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


def scan(adb: str, remote_file: str) -> None:
    """Ask Android to index one file, instead of waiting for it to notice by itself.

    It has to be one file at a time: the same broadcast aimed at the folder is
    accepted and then silently ignored. The path is percent-encoded, which also
    keeps spaces and quotes in track names out of the device shell's way.
    """
    uri = "file://" + urllib.parse.quote(remote_file)
    subprocess.run(
        [adb, "shell", "am", "broadcast", "-a",
         "android.intent.action.MEDIA_SCANNER_SCAN_FILE", "-d", uri],
        capture_output=True,
    )


def push(adb: str, folder: str, jobs: int) -> tuple:
    album = os.path.basename(folder)
    remote = f"/sdcard/Music/{album}"
    subprocess.run([adb, "shell", "mkdir", "-p", f"'{remote}'"], capture_output=True)

    have = subprocess.run(
        [adb, "shell", "ls", f"'{remote}'"], capture_output=True, text=True
    ).stdout.splitlines()
    already = {line.strip() for line in have}

    files = sorted(f for f in os.listdir(folder) if f.lower().endswith(".mp3"))
    failed = []
    lock = threading.Lock()

    def send(name: str, task) -> bool:
        if name in already:
            # Still worth a scan: the file can be on the phone yet missing from the
            # music app, which is what re-running push is for.
            view.start(task)
            view.update(task, note="indexing")
            scan(adb, f"{remote}/{name}")
            view.done(task, total=1, completed=1, note="already there")
            return True
        view.start(task)
        view.update(task, note="sending")
        # adb only prints its own percentages to a terminal, so the row pulses
        # instead: the counter above is what tracks the album going across.
        r = subprocess.run(
            [adb, "push", os.path.join(folder, name), remote], capture_output=True, text=True
        )
        if r.returncode == 0:
            view.update(task, note="indexing")
            scan(adb, f"{remote}/{name}")
            view.done(task, total=1, completed=1, note="sent")
            return True
        why = ((r.stderr or r.stdout).strip().splitlines() or ["adb push failed"])[-1]
        with lock:
            failed.append((name, why))
        view.done(task, ok=False, total=1, completed=1, note=f"[red]{plain(why)[:30]}[/red]")
        return False

    with View(f"Sending {len(files)} tracks to Music/{album}", len(files), downloading=False) as view:
        tasks = [view.add(fit(f), note="queued") for f in files]
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
            sent = sum(pool.map(lambda p: send(*p), zip(files, tasks)))

    return sent, failed


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
        import rich  # noqa: F401
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
    say(f"  {pl['name']} - {pl['owner']} ({len(pl['tracks'])} tracks)")
    if len(pl["tracks"]) == EMBED_CAP:
        say(f"  ! The embed page never returns more than {EMBED_CAP} tracks, so a longer "
            "playlist is cut here.")
    say("")

    dest = os.path.join(args.out, safe(pl["name"], 70))
    os.makedirs(dest, exist_ok=True)

    resolve(pl, os.path.join(dest, CACHE), args.jobs, args.min_confidence)

    chosen = [t for t in pl["tracks"] if t["url"] and t["confidence"] >= args.min_confidence]
    skipped = [t for t in pl["tracks"] if t not in chosen]
    for i, t in enumerate(chosen, 1):
        t.update(i=i, album=pl["name"], albumartist=pl["owner"])

    say("")
    with View(f"Downloading at {args.quality} kbps into {dest}", len(chosen),
              downloading=True) as view:
        tasks = [view.add(fit(f"{t['artist']} - {t['title']}")) for t in chosen]
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            done = list(pool.map(
                lambda p: download_one(p[0], dest, len(chosen), args.quality, args.cookies,
                                       view, p[1]),
                zip(chosen, tasks),
            ))
    got = [p for p in done if p]

    say(f"\n{len(got)}/{len(chosen)} tracks in {dest}")
    if failed := [t for t in chosen if t.get("error")]:
        say("\nFailed to download:")
        for t in failed:
            say(f"  {t['n']:3}. {t['artist']} - {t['title']}  ->  {t['error']}")
    if skipped:
        say(f"\nSkipped, no confident match (confidence < {args.min_confidence}):")
        for t in skipped:
            say(f"  {t['n']:3}. {t['artist']} - {t['title']}  ({t['confidence']})")
        say("  Lower the bar with --min-confidence if you want them anyway.")
    say(f"\nNext: python {os.path.basename(__file__)} push")
    return 0 if got else 1


def cmd_push(args: argparse.Namespace) -> int:
    try:
        import rich  # noqa: F401
    except ImportError:
        raise SystemExit("Missing dependency. Run: pip install -r requirements.txt")
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

    sent, failed = push(adb, folder, args.jobs)

    say(f"\n{sent}/{len(files)} tracks in Music/{os.path.basename(folder)} on the phone.")
    if failed:
        say("\nFailed to send:")
        for name, why in failed:
            say(f"  {name}  ->  {why[:100]}")
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
    g.add_argument("-j", "--jobs", type=int, default=3,
                   help="parallel searches and downloads (default: 3)")
    g.add_argument("--min-confidence", type=int, default=90,
                   help="drop weaker YouTube matches (default: 90)")
    g.add_argument("--cookies", metavar="BROWSER",
                   help="borrow cookies from firefox/chrome/edge for age-restricted videos")
    g.set_defaults(func=cmd_get)

    s = sub.add_parser("push", help="send an album folder to an Android phone")
    s.add_argument("folder", nargs="?", help="album folder (default: the most recent one)")
    s.add_argument("-j", "--jobs", type=int, default=3,
                   help="parallel transfers (default: 3)")
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
