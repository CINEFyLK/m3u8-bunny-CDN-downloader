# m3u8-dl
by CINEFy

---
This is explictly for educational and authorized use only
---

Fast HLS (`.m3u8`) downloader written in pure Python. It keeps a pool of
keep-alive HTTP connections, downloads segments in parallel, writes them
straight to disk in order, and remuxes the result with a single ffmpeg
`-c copy` pass.

- **No per-segment temp files** for clear streams — segments are streamed
  into one output file at their exact byte offsets.
- **24 connections by default**, with optional multi-range splitting of very
  large segments for single-stream sources.
- **AES-128** (`KEYFORMAT=identity`) support, decrypted during remux.
- **Resumable** — a `.part.state` file records progress, so an interrupted
  download continues where it stopped.
- **Two engines**: a native HLS engine for `.m3u8` links, and
  [yt-dlp](https://github.com/yt-dlp/yt-dlp) for everything else. `--engine auto`
  picks one and falls back automatically.
- Works as a CLI tool or as a Python function.

---

## Requirements

| | |
|---|---|
| Python | 3.8+ |
| `yt_dlp` | required (pip) — only for the `ytdlp` engine |
| `ffmpeg` | optional but recommended — needed for remuxing, separate audio tracks and AES-128 |
| `aria2c` | optional — used as yt-dlp's external downloader for multi-range HTTP |

```bash
pip install yt-dlp
```

`ffmpeg` is discovered automatically: on PATH first, then under
`C:\Program Files\ffmpeg\...` and `%LOCALAPPDATA%\ffmpeg\...`.

If ffmpeg is missing the tool still works, it just renames the raw
`.ts`/`.mp4` stream instead of remuxing (and AES-128 streams fail with a clear
message).

---

## Install

```bash
git clone https://github.com/<you>/m3u8-dl.git
cd m3u8-dl
```

The tool is a single file, so cloning is optional — grab `video.py` and run it.

---

## Quick start

```bash
# interactive: run with no arguments, paste a URL when asked
python video.py

# 720p into the current folder
python video.py "https://example.com/stream/master.m3u8" -q 720

# 1080p, 32 connections, explicit output name
python video.py "https://example.com/stream/master.m3u8" -q 1080 -n 32 -o movie.mp4

# audio rendition only
python video.py "https://example.com/stream/master.m3u8" --audio-only

# see what the master playlist offers
python video.py "https://example.com/stream/master.m3u8" -l

# batch, from a file or the clipboard
python video.py -i urls.txt
python video.py -c
```

With no arguments and a TTY attached you get an interactive quality menu
(`1080p / 720p / 480p / 360p / best / audio / list`) and can paste a URL
straight in.

---

## Output naming

Without `-o`/`-O` the filename is derived from the URL path, sanitised, with
the quality appended — `720p/1080p/360p/...`:

```
https://cdn.example.com/hls/ep12/master.m3u8   ->  ep12_720p.mp4
https://cdn.example.com/hls/index.m3u8         ->  video_720p.mp4   (generic stems are ignored)
```

Container is picked from the variant codecs (VP9/AV1/WebM/Opus → `mkv`,
otherwise `mp4`); override with `--container`.

---

## Options

| Flag | Default | Description |
|---|---|---|
| `urls ...` | – | one or more `.m3u8` URLs |
| `-i`, `--input-file` | – | text file, one URL per line (`#` comments ignored) |
| `-c`, `--clipboard` | off | read the URL from the clipboard (Windows) |
| `-q`, `--quality` | ask | max height (`720`, `1080`, …) or `best` |
| `-n`, `--connections` | `24` | parallel connections per stream (max 256) |
| `--queue-depth` | `0` | in-flight tasks; `0` = connections + 4 |
| `--split-mb` | `0` | split segments larger than this into parallel byte ranges (`8`–`16` recommended) |
| `--split-conns` | `4` | connections used per split segment |
| `--direct-mb` | `24` | stream segments above this size directly to disk |
| `-o`, `--output` | – | output file **or** folder |
| `-O`, `--output-dir` | – | folder for the auto-named output file |
| `--container` | auto | force `mp4` or `mkv` |
| `--faststart` | off | move the MP4 index to the front for streaming |
| `-r`, `--referer` | `https://iframe.mediadelivery.net/` | `Referer` header (`''` disables it) |
| `-H`, `--header` | – | extra header, repeatable: `-H 'Cookie: a=b'` |
| `--engine` | `auto` | `fast` (native HLS), `ytdlp`, or `auto` |
| `--max-height` | `1080` | height cap for the yt-dlp engine |
| `--audio-only` | off | download only the audio rendition |
| `--no-audio` | off | skip the separate audio rendition |
| `--no-resume` | off | ignore an existing `.part` file / state |
| `--force-mux` | off | always remux, even when the raw stream is already usable |
| `--keep-raw` | off | keep the downloaded stream instead of remuxing it |
| `--keep-dir` | temp dir | keep temporary files in this folder |
| `-l`, `--list` | off | list variants / renditions and exit |
| `-t`, `--timeout` | `25.0` | socket timeout in seconds |
| `-v`, `--verbose` | off | verbose HTTP and yt-dlp logging |
| `--quiet` | off | no progress bar or summary |
| `--ytdlp-template` | `%(title)s [%(height)sp].%(ext)s` | yt-dlp output template |

`--help` prints the same list with defaults filled in.

---

## How it works

### Variant selection

The master playlist is parsed for `EXT-X-STREAM-INF` and `EXT-X-MEDIA`
renditions. `--quality` acts as a height cap; among the variants at or below
the cap the highest one wins, ties broken by bandwidth. With no cap the
highest variant is used. Separate audio is matched to the variant's `AUDIO`
group, preferring stereo or 5.1, highest bandwidth.

### Parallel segment pool

1. The media playlist is parsed into a task list: one task per segment, or one
   per byte-range part when splitting is enabled. `EXT-X-MAP` init segments
   become their own tasks.
2. A bounded semaphore caps in-flight tasks; worker threads pull from a queue
   and fetch over per-thread, per-host keep-alive connections (HTTP/1.1 over
   ALPN, `Accept-Encoding: identity` so byte offsets stay valid).
3. Results are published with a sequence number. A single writer consumes them
   **in playlist order** and writes to `output` at the precomputed offset —
   for playlists with `EXT-X-BYTERANGE` the file is pre-truncated to the exact
   final size and each part seeks to its own position. No ordering buffer, no
   temp files, no final concatenation step.
4. Redirects, dropped connections and transient errors are retried with
   exponential backoff; the first error stops the run and is reported.

Large segments (≥ `--direct-mb`, when offsets are known) bypass the in-memory
buffer and are streamed into the output file as they arrive.

### Splitting

For long single-file renditions `--split-mb N --split-conns K` divides any
segment larger than N MB into K byte ranges fetched concurrently, so a single
slow TCP stream is no longer the bottleneck. Only enable it when the server
honours `Range` requests.

### AES-128

Encrypted (`METHOD=AES-128`, `KEYFORMAT=identity`) streams are downloaded as
segments plus a generated `local.m3u8` manifest and the key file, then remuxed
in one ffmpeg pass using `-decryption_key` / `-decryption_iv`. If the playlist
rotates keys, only the first one is used and a warning is printed.

### Resume

State is written to `<output>.part.state` (playlist fingerprint + next task
sequence + bytes written) every few tasks and on `Ctrl+C`. Re-running the same
command skips completed tasks; a fingerprint mismatch (different playlist)
discards the state. The state file is removed on success. Interrupted runs
print where they will continue from.

### Engines

`--engine auto` uses the native engine for `.m3u8` URLs and yt-dlp otherwise,
falling back to yt-dlp if the native engine errors. The yt-dlp engine is
configured with concurrent fragment downloads, native HLS, resumed `.part`
files, 20 retries, and aria2c as an external downloader when available.

---

## Progress output

```
  [*] 720p  4.2MB/s  1183 segments  24 connections  [AES-128]
  download 742/1183 seg  62.7%  318.4MB/506.9MB  21.7MB/s  ETA 00:09
  [+] remuxed in 0:04 -> episode_720p.mp4 (506.9MB)
  [i] 512.3MB transferred in 0:28
```

The progress line is redrawn in place and suppressed when stdout is not a TTY
or `--quiet` is set.

---

## Python API

```python
from video import download_m3u8

download_m3u8("https://example.com/stream/master.m3u8", quality="720")
download_m3u8(url, quality="1080", output_name="movie.mp4", connections=32)
download_m3u8(url, referer_url="", header=["Cookie: session=abc"], timeout=60)
```

`download_m3u8(url, quality=..., output_name=..., referer_url=..., **overrides)`
accepts any option name from the CLI. Returns the output path, or `None` if the
download was stopped or failed.

---

## Troubleshooting

**`HTTP 403` on the playlist or segments** — the stream needs a `Referer` or
`Cookie`. Pass `-r <url>` and/or `-H 'Cookie: ...'`. Note that a default
`Referer` is sent; clear it with `-r ''` if the CDN rejects it.

**`the stream is AES-128 encrypted but ffmpeg was not found`** — install
ffmpeg, or point at it by putting it on `PATH`.

**`unsupported encryption KEYFORMAT=...`** — Widevine/PlayReady/SAMPLE-AES
(`KEYFORMAT` other than `identity`) needs a DRM licence server; this tool only
handles plain AES-128.

**Slow or stalling downloads** — raise `-n`, and for long single-file
renditions try `--split-mb 8 --split-conns 4`. If a CDN rate-limits
aggressively, lower `-n` to 8–12.

**Segments downloaded out of order** — the writer is strictly ordered, so
corruption means a server that ignores `Range` requests. Turn off
`--split-mb`, or force remux with `--force-mux`.

**`no usable variant in the master playlist`** — the URL is probably a media
playlist rather than a master; check with `-l`.

---

## Notes

Only download streams you have the rights to save. The default `Referer` is
set for a popular CDN and can be overridden or disabled.

## License

MIT
