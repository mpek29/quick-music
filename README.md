<div align="center">
  <h1>quick-music</h1>
  <p><strong>A Spotify playlist, as tagged MP3s, on your phone. Two commands.</strong></p>
  <p>
    quick-music reads a public Spotify playlist, finds each track on YouTube,
    downloads the audio, writes proper ID3 tags from the Spotify metadata, and
    pushes the finished album to an Android phone. No API key, no account, no
    ad-riddled conversion site.
  </p>
</div>

<p align="center">
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/github/license/mpek29/quick-music?style=flat-square&color=6b7280"></a>
  <a href="#requirements"><img alt="Python" src="https://img.shields.io/badge/python-3.9%2B-3776AB?style=flat-square&logo=python&logoColor=white"></a>
  <a href="#requirements"><img alt="Platforms" src="https://img.shields.io/badge/windows%20%C2%B7%20linux%20%C2%B7%20wsl%20%C2%B7%20macos-supported-10a37f?style=flat-square"></a>
  <a href="https://github.com/yt-dlp/yt-dlp"><img alt="Powered by yt-dlp" src="https://img.shields.io/badge/powered%20by-yt--dlp-ff0000?style=flat-square&logo=youtube&logoColor=white"></a>
</p>

<p align="center">
  <a href="#quick-start"><strong>Quick start</strong></a>
  ·
  <a href="#requirements">Requirements</a>
  ·
  <a href="#options">Options</a>
  ·
  <a href="#how-it-works">How it works</a>
  ·
  <a href="#troubleshooting">Troubleshooting</a>
  ·
  <a href="https://github.com/mpek29/quick-music/issues">Issues</a>
</p>

## What it does and does not

| Does | Does not |
|------|----------|
| Reads any **public** Spotify playlist | Ask for a Spotify account, login or API key |
| Matches every track on YouTube and scores the match | Blindly grab the first search result |
| Downloads MP3 at up to 320 kbps | Re-encode twice or lose quality on tagging |
| Writes title, artist, album and track number | Leave you with `videoplayback.mp3` and "Unknown Artist" |
| Sends the album to Android over ADB | Need a proprietary sync tool such as HiSuite or iTunes |

## Quick start

The same two commands work in **PowerShell**, **Ubuntu**, **WSL** and **macOS**.

```bash
pip install -r requirements.txt

python quickmusic.py get "https://open.spotify.com/playlist/37i9dQZF1DX4o1oenSJRJd"
python quickmusic.py push
```

Both phases show a counter and a bar per track: the YouTube match with its
confidence score, then the download with size, speed and ETA. `--jobs` rows
advance at a time, finished ones drop off the list to make room, and anything
that went wrong stays on screen in red.

A playlist longer than 100 tracks is cut at 100 — that is all Spotify's embed
page ever hands back, and `get` says so when it happens.

`get` leaves you with a ready album folder:

```
music/
└── All Out 2000s/
    ├── 01 - Britney Spears - Oops!...I Did It Again.mp3
    ├── 02 - Madonna - Hung Up.mp3
    └── ...
```

Every playlist gets its own folder under `music/`, which is git-ignored as a
whole — so your library never shows up in `git status`, whatever the playlists
are called.

`push` sends the most recent album folder to `/sdcard/Music/` on the phone, then
asks Android to rescan, so it shows up in your music app right away. It uses the
same live view, and tracks already on the phone are skipped rather than resent —
so an interrupted transfer is resumed by simply running it again.

## Requirements

- **Python 3.9+**
- **ffmpeg** — yt-dlp needs it to produce MP3
- **A JavaScript runtime** (Node, Deno or Bun) — YouTube ciphers its stream URLs
  and yt-dlp runs JS to decipher them. Without one, every download fails with
  `HTTP Error 403`.
- **adb** — only for `push`

| | Windows | Ubuntu / WSL | macOS |
|---|---|---|---|
| ffmpeg | `winget install Gyan.FFmpeg` | `sudo apt install ffmpeg` | `brew install ffmpeg` |
| Node | `winget install OpenJS.NodeJS` | `sudo apt install nodejs` | `brew install node` |
| adb | `winget install Google.PlatformTools` | `sudo apt install adb` | `brew install android-platform-tools` |

If you would rather not install ffmpeg system-wide, drop `ffmpeg.exe` into an
`ffmpeg/` folder next to `quickmusic.py` and it will be picked up from there.

Before the first `push`, enable **Settings → Developer options → USB debugging**
on the phone, plug it in, and accept the prompt asking to trust this computer.

> On **WSL**, USB devices are not visible to Linux by default. Either run
> `python quickmusic.py push` from Windows, or attach the phone to WSL with
> [usbipd-win](https://github.com/dorssel/usbipd-win) first.

## Options

```bash
python quickmusic.py get <playlist-url> [-q 320] [-j 4] [--min-confidence 80] [--cookies firefox]
python quickmusic.py push [folder] [-j 4]
```

| Flag | Default | What it changes |
|------|---------|-----------------|
| `-q`, `--quality` | `256` | MP3 bitrate: 128, 192, 256 or 320 kbps |
| `-j`, `--jobs` | `3` | Searches, downloads and phone transfers running in parallel |
| `--min-confidence` | `90` | How sure the YouTube match must be to be kept |
| `--cookies BROWSER` | off | Borrow browser cookies to reach age-restricted videos |
| `--out DIR` | `music` | Where album folders are created and looked for |

## How it works

### Reading the playlist

Spotify's `/embed/playlist/<id>` page ships the full tracklist as JSON inside a
`__NEXT_DATA__` script tag. quick-music parses that. This is why no API key and
no account are needed — and why the playlist has to be public.

Each track yields a title, an artist and a **duration**, which turns out to be
the single most useful thing for the next step.

### Matching tracks on YouTube

Searching `"<artist> <title>"` on YouTube returns remixes, sped-up edits, hour-long
loops and reaction videos alongside the real track. quick-music scores every
candidate and keeps the best:

| Signal | Weight |
|--------|--------|
| Title words matched | up to +40 |
| Artist words matched, in the video title or channel name | up to +25 |
| Duration within 3 s of Spotify's | +35, tapering to a penalty past 25 s |
| Auto-generated `- Topic` channel (official upload) | +12 |
| Artist name in the channel name | +10 |
| Unrequested "live", "cover", "nightcore", "sped up"… | −18 each |

Duration carries the most weight because it is objective: a cover or a remix
almost never lands within three seconds of the original. Anything scoring below
`--min-confidence` is reported and skipped rather than silently downloaded wrong.

Results are cached in `.search-cache.json` inside the album folder, so re-running
`get` costs no extra searches.

### Downloading and tagging

yt-dlp pulls the best audio stream and ffmpeg converts it to MP3 **once**, straight
to its final filename. Tags are then written in place with mutagen — no second
re-encode, no quality loss.

The tags come from **Spotify**, not from the YouTube video title, so you get
`Madonna — Hung Up` instead of `Madonna - Hung Up (Official Video) [HD] 4K`.
Filenames are stripped of characters that are illegal on FAT32/exFAT, which is
what most phone storage still uses.

### Sending it to the phone

`push` uses `adb push`, which works identically on Windows, Linux and macOS and
needs no vendor software. Files already on the phone are skipped, so an
interrupted transfer is resumed simply by running the command again. A
`MEDIA_SCANNER_SCAN_FILE` broadcast at the end tells Android to index the album
immediately instead of waiting for the next reboot.

## Project structure

```
quick-music/
├── quickmusic.py      # the whole thing: get + push
├── requirements.txt   # yt-dlp, mutagen, rich
├── README.md
└── music/             # git-ignored, one folder per playlist
```

One file, around 600 lines, no framework. Everything it does is meant to be
readable in a single sitting.

## Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| `HTTP Error 403` on every track | No JavaScript runtime. Install Node, Deno or Bun. |
| `ffmpeg not found` | Install ffmpeg, or drop `ffmpeg.exe` into `ffmpeg/`. |
| `age-restricted` on some tracks | Re-run with `--cookies firefox` (or `chrome`, `edge`) while logged into YouTube in that browser. |
| Tracks skipped for low confidence | The YouTube match looked wrong. Inspect the list it prints, then lower `--min-confidence` if you disagree. |
| `Phone detected but not authorised` | Unlock the phone and accept the USB debugging prompt. |
| Album not showing in the music app | Reboot the phone to force a full media rescan. |
| yt-dlp suddenly breaks | YouTube changed something. `pip install -U yt-dlp` fixes it nine times out of ten. |

## Legal

quick-music is a personal-use tool. Downloading copyrighted material you do not
own a licence for may be illegal where you live, and it is against YouTube's
terms of service. You are responsible for what you download.

## Contributing

This is a small personal project shared as-is. Contributions are welcome!

- 🍴 **Fork it** — make your own version
- 🔧 **Pull requests** — improvements and fixes are appreciated
- 📋 **Copy it** — use the code however you want
- ✨ **Enhance it** — build something better

## License

Released under the MIT License. See [LICENSE](LICENSE).
