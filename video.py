import argparse
import copy
import hashlib
import http.client
import json
import os
import queue
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from collections import OrderedDict, deque
from dataclasses import dataclass, field

import yt_dlp

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
DEFAULT_REFERER = "https://iframe.mediadelivery.net/"
ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')
GENERIC_STEMS = {"index", "master", "playlist", "video", "stream", "chunklist", "hls", "out",
                 "master_playlist", "videoindex", "index_v3", "adaptive", "prod"}
VIDEO_SUFFIXES = (".mp4", ".mkv", ".ts", ".m4v", ".webm", ".mov", ".m2ts", ".flv", ".mp3", ".m4a")


def human_bytes(value):
    number = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if number < 1024 or unit == "TB":
            return f"{int(number)}B" if unit == "B" else f"{number:.1f}{unit}"
        number /= 1024


def human_time(seconds):
    seconds = int(max(0, seconds or 0))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def parse_attrs(text):
    return {key: (value[1:-1] if value.startswith('"') else value) for key, value in ATTR_RE.findall(text or "")}


def parse_rate(value):
    match = re.match(r"\s*([0-9]*\.?[0-9]+)\s*([kKmMgG]?)", str(value or ""))
    if not match:
        return 0
    scale = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[match.group(2).lower()]
    return int(float(match.group(1)) * scale)


def find_tool(name):
    found = shutil.which(name)
    if found:
        return found
    folders = {"ffmpeg": "ffmpeg", "aria2c": "aria2"}
    roots = [os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("LOCALAPPDATA", "")]
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        base = os.path.join(root, folders.get(name, name))
        if not os.path.isdir(base):
            continue
        for entry in sorted(os.listdir(base), reverse=True):
            for candidate in (os.path.join(base, entry, "bin", name + ".exe"), os.path.join(base, name + ".exe")):
                if os.path.isfile(candidate):
                    return candidate
    return None


_SSL_CONTEXT = ssl.create_default_context()
_SSL_CONTEXT.set_alpn_protocols(["http/1.1"])


class Http:
    """Thread-safe HTTP client with one keep-alive connection per thread per host."""

    def __init__(self, headers=None, timeout=25.0, retries=4, verbose=False):
        self.headers = {"User-Agent": UA, "Accept-Encoding": "identity", "Connection": "keep-alive"}
        self.headers.update(headers or {})
        self.timeout = timeout
        self.retries = retries
        self.verbose = verbose
        self.bytes_downloaded = 0
        self._local = threading.local()
        self._all = []
        self._lock = threading.Lock()
        self._sizes = {}
        self._sizes_lock = threading.Lock()
        self._counter_lock = threading.Lock()

    @staticmethod
    def _port(parts):
        return parts.port or (443 if parts.scheme == "https" else 80)

    def _connection(self, parts):
        store = getattr(self._local, "conns", None)
        if store is None:
            store = self._local.conns = {}
        key = (parts.scheme, parts.hostname, self._port(parts))
        conn = store.get(key)
        if conn is not None:
            return conn
        if parts.scheme == "https":
            conn = http.client.HTTPSConnection(parts.hostname, self._port(parts), timeout=self.timeout,
                                               context=_SSL_CONTEXT)
        else:
            conn = http.client.HTTPConnection(parts.hostname, self._port(parts), timeout=self.timeout)
        store[key] = conn
        with self._lock:
            self._all.append(conn)
        return conn

    def _drop(self, parts):
        store = getattr(self._local, "conns", None) or {}
        conn = store.pop((parts.scheme, parts.hostname, self._port(parts)), None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def _open(self, url, byte_range=None, extra=None, method="GET"):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise ValueError(f"unsupported URL scheme: {parts.scheme or 'none'}")
        headers = dict(self.headers)
        headers.update(extra or {})
        if byte_range and (byte_range[0] or byte_range[1] is not None):
            headers["Range"] = f"bytes={byte_range[0]}-{byte_range[1] if byte_range[1] is not None else ''}"
        last = None
        for attempt in range(self.retries):
            try:
                conn = self._connection(parts)
                target = parts.path or "/"
                if parts.query:
                    target += "?" + parts.query
                conn.request(method, target, headers=headers)
                response = conn.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    try:
                        response.read(4096)
                    finally:
                        response.close()
                    if not location or attempt >= self.retries - 1:
                        raise RuntimeError(f"redirect loop starting at {url}")
                    self._drop(parts)
                    url = urllib.parse.urljoin(url, location)
                    parts = urllib.parse.urlsplit(url)
                    continue
                return response
            except (http.client.HTTPException, OSError) as err:
                last = err
                self._drop(parts)
                if self.verbose:
                    print(f"    [http] retry {attempt + 1}/{self.retries}: {type(err).__name__}: {err}")
                time.sleep(min(4.0, 0.2 * (2 ** attempt)))
        raise last if last else RuntimeError(f"cannot open {url}")

    def _account(self, count):
        with self._counter_lock:
            self.bytes_downloaded += count

    def text(self, url, extra=None):
        response = self._open(url, extra=extra)
        try:
            if response.status >= 400:
                raise RuntimeError(f"HTTP {response.status} for {url}")
            body = response.read()
        finally:
            response.close()
        self._account(len(body))
        return body.decode("utf-8-sig", "replace")

    def ranged_size(self, url):
        """Exact byte size of a resource, or None when the server cannot serve ranges."""
        with self._sizes_lock:
            if url in self._sizes:
                return self._sizes[url]
        total = None
        try:
            response = self._open(url, byte_range=(0, 0))
            try:
                if response.status == 206:
                    match = re.search(r"/([0-9]+)\s*$", response.headers.get("Content-Range", ""))
                    if match:
                        total = int(match.group(1))
            finally:
                try:
                    response.read(1)
                except Exception:
                    pass
                response.close()
        except Exception:
            total = None
        with self._sizes_lock:
            self._sizes[url] = total
        return total

    def read(self, url, start=0, end=None, sink=None, buffer_size=262144):
        response = self._open(url, byte_range=(start, end))
        try:
            if response.status >= 400:
                raise RuntimeError(f"HTTP {response.status} for {url}")
            if response.status == 200 and start:
                raise RuntimeError(f"server ignored the byte range for {url}")
            received = 0
            if sink is None:
                chunks = []
                while True:
                    block = response.read(buffer_size)
                    if not block:
                        break
                    received += len(block)
                    chunks.append(block)
                payload = b"".join(chunks)
                self._account(received)
                return payload
            buffer = bytearray(buffer_size)
            view = memoryview(buffer)
            while True:
                count = response.readinto(buffer)
                if not count:
                    break
                sink(view[:count])
                received += count
            self._account(received)
            return received
        finally:
            response.close()

    def close(self):
        with self._lock:
            conns, self._all = self._all, []
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass


@dataclass
class Segment:
    url: str
    duration: float = 0.0
    start: int = 0
    length: int = 0
    key_uri: str = ""
    key_iv: str = ""
    map_url: str = ""
    map_start: int = 0
    map_length: int = 0


@dataclass
class Playlist:
    url: str = ""
    media_sequence: int = 0
    target_duration: float = 0.0
    endlist: bool = False
    key_format: str = ""
    variant: dict = field(default_factory=dict)
    variants: list = field(default_factory=list)
    audios: list = field(default_factory=list)
    segments: list = field(default_factory=list)
    audio: dict = field(default_factory=dict)
    fingerprint: str = ""


def parse_byte_range(text, previous_end=0):
    if not text:
        return previous_end, 0
    if "@" in text:
        offset, _, length = text.partition("@")
        return int(offset), int(length)
    return previous_end, int(text)


def parse_playlist(text, url):
    playlist = Playlist(url=url)
    variants, audios, segments = [], [], []
    pending_variant = None
    key_uri = key_iv = ""
    key_format = ""
    map_url, map_start, map_length = "", 0, 0
    byte_end = 0
    duration = 0.0
    media_sequence = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF:"):
            pending_variant = parse_attrs(line.split(":", 1)[1])
            continue
        if line.startswith("#EXT-X-I-FRAME-STREAM-INF:"):
            pending_variant = None
            continue
        if line.startswith("#EXT-X-MEDIA:"):
            attrs = parse_attrs(line.split(":", 1)[1])
            if (attrs.get("TYPE") or "").upper() == "AUDIO" and attrs.get("URI"):
                attrs["url"] = urllib.parse.urljoin(url, attrs["URI"])
                attrs["bandwidth"] = parse_rate(attrs.get("BANDWIDTH"))
                audios.append(attrs)
            continue
        if line.startswith("#EXTINF:"):
            try:
                duration = float(line.split(":", 1)[1].split(",")[0])
            except ValueError:
                duration = 0.0
            continue
        if line.startswith("#EXT-X-KEY:"):
            attrs = parse_attrs(line.split(":", 1)[1])
            method = (attrs.get("METHOD") or "NONE").upper()
            if method == "NONE":
                key_uri = key_iv = key_format = ""
            else:
                key_uri = urllib.parse.urljoin(url, attrs.get("URI", ""))
                key_iv = attrs.get("IV", "")
                key_format = (attrs.get("KEYFORMAT") or "identity").lower()
            continue
        if line.startswith("#EXT-X-MAP:"):
            attrs = parse_attrs(line.split(":", 1)[1])
            map_url = urllib.parse.urljoin(url, attrs.get("URI", ""))
            map_start, map_length = parse_byte_range(attrs.get("BYTERANGE", ""))
            continue
        if line.startswith("#EXT-X-BYTERANGE:"):
            start, length = parse_byte_range(line.split(":", 1)[1], byte_end)
            byte_end = start + length
            if segments:
                segments[-1].start = start
                segments[-1].length = length
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                media_sequence = int(line.split(":", 1)[1])
            except ValueError:
                media_sequence = 0
            continue
        if line.startswith("#EXT-X-TARGETDURATION:"):
            try:
                playlist.target_duration = float(line.split(":", 1)[1])
            except ValueError:
                pass
            continue
        if line == "#EXT-X-ENDLIST":
            playlist.endlist = True
            continue
        if line.startswith("#"):
            continue
        if pending_variant is not None:
            entry = dict(pending_variant)
            entry["url"] = urllib.parse.urljoin(url, line)
            resolution = str(entry.get("RESOLUTION") or "")
            match = re.match(r"\s*(\d+)\s*[xX]\s*(\d+)\s*$", resolution)
            entry["width"] = int(entry.get("WIDTH") or (match.group(1) if match else 0))
            entry["height"] = int(entry.get("HEIGHT") or (match.group(2) if match else 0))
            entry["bandwidth"] = parse_rate(entry.get("BANDWIDTH"))
            entry["avg_bandwidth"] = parse_rate(entry.get("AVERAGE-BANDWIDTH")) or entry["bandwidth"]
            variants.append(entry)
            pending_variant = None
            continue
        segments.append(Segment(url=urllib.parse.urljoin(url, line), duration=duration, start=byte_end,
                                key_uri=key_uri, key_iv=key_iv, map_url=map_url,
                                map_start=map_start, map_length=map_length))
        if segments[-1].length:
            byte_end = segments[-1].start + segments[-1].length
    playlist.variants = variants
    playlist.audios = audios
    if not variants:
        playlist.media_sequence = media_sequence
        playlist.segments = segments
        playlist.fingerprint = hashlib.sha1(
            "\n".join(segment.url for segment in segments).encode()).hexdigest()
    playlist.key_format = key_format
    return playlist


def fetch_playlist(url, http, extra=None):
    body = http.text(url, extra=extra)
    if "#EXTM3U" not in body.split("\n")[0]:
        raise RuntimeError("not an m3u8 playlist (no #EXTM3U)")
    return parse_playlist(body, url)


def select_variant(playlist, max_height):
    variants = [variant for variant in playlist.variants if variant.get("url")]
    if not variants:
        return None
    pool = [v for v in variants if not max_height or v.get("height", 0) <= max_height] or variants
    return max(pool, key=lambda v: (v.get("height", 0), v.get("bandwidth", 0)))


def select_audio(playlist, variant):
    audios = [audio for audio in playlist.audios if audio.get("url")]
    if not audios:
        return {}
    group = variant.get("AUDIO") if isinstance(variant, dict) else None
    pool = [a for a in audios if not group or a.get("GROUP-ID") == group] or audios
    return max(pool, key=lambda a: (str(a.get("CHANNELS", "")) in ("2", "6"), a.get("bandwidth", 0)))


def pick_container(codecs):
    lowered = (codecs or "").lower()
    for token in ("vp9", "vp09", "av01", "theora", "vorbis", "opus", "wmv", "msmpeg", "rv40", "vc1", "ffv1", "dvbsub"):
        if token in lowered:
            return "mkv"
    return "mp4"


def default_name(url, quality, container):
    try:
        stem = os.path.splitext(os.path.basename(urllib.parse.urlsplit(url).path))[0]
    except Exception:
        stem = ""
    stem = re.sub(r'[<>:"/\\|?*]+', "_", stem).strip(" .")
    if not stem or stem.lower() in GENERIC_STEMS:
        stem = "video"
    if quality and quality not in ("best", ""):
        stem = f"{stem}_{quality}p"
    return f"{stem}.{container}"


def unique_path(path):
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    for index in range(1, 1000):
        candidate = f"{stem} ({index}){ext}"
        if not os.path.exists(candidate):
            return candidate
    return f"{stem} ({int(time.time())}){ext}"


@dataclass
class Task:
    position: int = 0
    url: str = ""
    start: int = 0
    end: int = -1
    size: int = 0
    seq: int = -1
    offset: int = -1
    parts: int = 1
    final: bool = True
    is_map: bool = False
    name: str = ""


class Progress:
    def __init__(self, total_bytes, total_count, enabled, label):
        self.total_bytes = max(0, total_bytes or 0)
        self.total_count = max(0, total_count or 0)
        self.done_bytes = 0
        self.done_count = 0
        self.started = time.time()
        self.last_draw = 0.0
        self.visible = bool(enabled) and sys.stdout.isatty()
        self.label = label
        self.samples = [(self.started, 0)]
        self.lock = threading.Lock()

    def add(self, size, count=0, force=False):
        with self.lock:
            self.done_bytes += size or 0
            self.done_count += count
            now = time.time()
            self.samples.append((now, self.done_bytes))
            while len(self.samples) > 2 and self.samples[1][0] < now - 5:
                self.samples.pop(0)
            if not self.visible or (not force and now - self.last_draw < 0.15):
                return
            self.last_draw = now
            elapsed = max(1e-6, now - self.started)
            window = self.samples
            speed = ((window[-1][1] - window[0][1]) / (window[-1][0] - window[0][0])) if window[-1][0] > window[0][0] else 0
            fields = [f"\r{self.label}"]
            if self.total_count:
                fields.append(f"{self.done_count}/{self.total_count} seg {100.0 * self.done_count / self.total_count:5.1f}%")
            if self.total_bytes:
                fields.append(f"{human_bytes(self.done_bytes)}/{human_bytes(self.total_bytes)}")
            if speed > 0:
                fields.append(f"{human_bytes(speed)}/s")
                if self.total_count and self.done_count:
                    fields.append(f"ETA {human_time((self.total_count - self.done_count) * elapsed / self.done_count)}")
            sys.stdout.write(" ".join(fields).ljust(92))
            sys.stdout.flush()

    def close(self):
        with self.lock:
            if self.visible:
                sys.stdout.write("\r" + " " * 96 + "\r")
                sys.stdout.flush()
                self.visible = False


class HlsEngine:
    """Parallel HLS segment downloader: keep-alive connections, ordered writer, no per-segment temp files."""

    def __init__(self, http, playlist, output=None, folder=None, workers=24, queue_depth=0,
                 split_mb=0, split_conns=4, direct_mb=24, resume=True, progress=True, label="  "):
        self.http = http
        self.playlist = playlist
        self.output = output
        self.folder = folder
        self.workers = max(1, workers)
        self.depth = max(1, queue_depth or self.workers + 4)
        self.split_bytes = max(0, split_mb) << 20
        self.split_conns = max(1, split_conns)
        self.direct_bytes = max(1, direct_mb) << 20
        self.resume = bool(resume) and not folder
        self.progress = progress
        self.label = label
        self.tasks = []
        self.names = {}
        self.map_names = {}
        self.explicit = False
        self.keys = []
        self.state_path = (output + ".part.state") if output and self.resume else ""
        self.total_bytes = 0
        self.total_count = len(playlist.segments)
        self.progress_counter = None
        self.output_handle = None
        self._queue = queue.Queue()
        self._slots = threading.BoundedSemaphore(self.depth)
        self._cond = threading.Condition()
        self._results = {}
        self._next_seq = 0
        self._running = 0
        self._truncate_at = 0
        self._error = None
        self._stop = threading.Event()
        self._handles = OrderedDict()
        self._workers = []

    def _rate(self):
        variant = self.playlist.variant or {}
        return variant.get("avg_bandwidth") or variant.get("bandwidth") or 0

    def _bootstrap_rate(self):
        if self._rate() or not self.playlist.segments:
            return
        first = self.playlist.segments[0]
        size = self.http.ranged_size(first.url)
        if size and first.duration:
            self.playlist.variant = {"avg_bandwidth": int(size * 8 / first.duration)}

    def _estimate(self, segment):
        if segment.length:
            return segment.length
        rate = self._rate()
        if rate and segment.duration:
            return int(rate / 8 * segment.duration)
        return 0

    def _plan(self):
        self._bootstrap_rate()
        self.explicit = any(segment.length for segment in self.playlist.segments) or bool(self.folder)
        tasks = []
        cursor = 0
        pending_map = None
        map_index = 0
        for position, segment in enumerate(self.playlist.segments):
            if segment.map_url and segment.map_url != pending_map:
                pending_map = segment.map_url
                size = segment.map_length
                base = segment.map_start if segment.map_length else cursor
                name = f"init{map_index:06d}.mp4" if self.folder else ""
                if self.folder:
                    self.map_names[position] = name
                tasks.append(Task(position=position, url=segment.map_url, start=segment.map_start,
                                  end=segment.map_start + size - 1 if size else None, size=size,
                                  offset=base if self.explicit else -1, is_map=True, name=name))
                cursor = max(cursor, base + size)
                map_index += 1
            if segment.key_uri and segment.key_uri not in [key[0] for key in self.keys]:
                self.keys.append((segment.key_uri, segment.key_iv))
            size = self._estimate(segment)
            if size and self.split_bytes and size >= self.split_bytes:
                exact = self.http.ranged_size(segment.url)
                if exact:
                    size = exact
            parts = self._parts(size)
            name = ""
            if self.folder:
                name = f"seg{position:06d}.ts"
                self.names[position] = name
            base = segment.start if segment.length else cursor
            for part_index, (part_start, part_end) in enumerate(parts):
                task = Task(position=position, url=segment.url, start=segment.start + part_start,
                            end=segment.start + part_end if part_end is not None else None,
                            size=part_end - part_start + 1 if part_end is not None else 0,
                            offset=base + part_start if self.explicit else -1, parts=len(parts),
                            final=part_index == len(parts) - 1, name=name)
                cursor = max(cursor, task.offset + task.size) if self.explicit else cursor
                tasks.append(task)
        for seq, task in enumerate(tasks):
            task.seq = seq
        self.tasks = tasks
        if self.explicit and not self.folder:
            self.total_bytes = max((task.offset + task.size for task in tasks), default=0)
        else:
            self.total_bytes = sum(task.size for task in tasks)

    def _parts(self, size):
        if not size:
            return [(0, None)]
        if not self.split_bytes or self.split_conns <= 1 or size < self.split_bytes:
            return [(0, size - 1)]
        count = min(self.split_conns, max(1, size // self.split_bytes))
        if count <= 1:
            return [(0, size - 1)]
        chunk = size // count
        parts, cursor = [], 0
        for position in range(count):
            end = size - 1 if position == count - 1 else cursor + chunk - 1
            parts.append((cursor, end))
            cursor = end + 1
        return parts

    def _handle(self, path, mode):
        handle = self._handles.get(path)
        if handle is not None and not handle.closed:
            self._handles.move_to_end(path)
            return handle
        handle = open(path, mode, buffering=0)
        self._handles[path] = handle
        while len(self._handles) > 48:
            _, stale = self._handles.popitem(last=False)
            try:
                stale.close()
            except Exception:
                pass
        return handle

    def _close_handles(self):
        for handle in self._handles.values():
            try:
                handle.close()
            except Exception:
                pass
        self._handles.clear()

    def _publish(self, seq, payload):
        with self._cond:
            self._results[seq] = payload
            self._cond.notify_all()

    def _worker(self):
        while not self._stop.is_set():
            try:
                task = self._queue.get_nowait()
            except queue.Empty:
                time.sleep(0.002)
                continue
            try:
                self._fetch(task)
            except Exception as err:
                self._publish(task.seq, err)

    def _stream(self, task):
        if self.folder:
            handle = self._handle(os.path.join(self.folder, task.name), "ab")
        else:
            handle = self._handle(self.output, "r+b")
            handle.seek(task.offset)
        return self.http.read(task.url, task.start, task.end, sink=handle.write)

    def _fetch(self, task):
        if self.output and not self.folder and self.tasks and not os.path.isfile(self.output):
            self._prepare_output()
        streaming = (self.explicit and task.size >= self.direct_bytes
                     and (not self.folder or task.parts == 1))
        if streaming:
            self._publish(task.seq, ("len", self._stream(task)))
            return
        data = self.http.read(task.url, task.start, task.end)
        if task.size and not self.explicit and len(data) > task.size:
            data = data[:task.size]
        self._publish(task.seq, ("data", data))

    def _consume(self, timeout):
        with self._cond:
            if self._next_seq not in self._results:
                self._cond.wait(timeout)
                if self._next_seq not in self._results:
                    return False
            payload = self._results.pop(self._next_seq)
            seq = self._next_seq
            self._next_seq += 1
        if isinstance(payload, Exception):
            if self._error is None:
                self._error = payload
            self._stop.set()
            return True
        kind, value = payload
        task = self.tasks[seq]
        try:
            if self.folder:
                handle = self._handle(os.path.join(self.folder, task.name), "ab")
                handle.write(value) if kind == "data" else None
                self._handles.move_to_end(os.path.join(self.folder, task.name))
                size = value if kind == "data" else None
            else:
                if self.explicit:
                    self.output_handle.seek(task.offset)
                else:
                    self.output_handle.seek(self._running)
                    self._running += len(value) if kind == "data" else value
                if kind == "data":
                    self.output_handle.write(value)
                size = len(value) if kind == "data" else value
            if size is None:
                size = len(value)
            if not self.folder and self.explicit:
                self._truncate_at = max(self._truncate_at, task.offset + size)
            else:
                self._truncate_at = max(self._truncate_at, self._running)
        finally:
            self._slots.release()
        self.progress_counter.add(size, 1 if task.final and not task.is_map else 0)
        if self._next_seq % 8 == 0:
            self._save_state()
        return True

    def _prepare_output(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.output)) or ".", exist_ok=True)
        if not os.path.exists(self.output):
            open(self.output, "wb").close()
        self.output_handle = open(self.output, "r+b", buffering=1024 * 512)

    def _save_state(self):
        if not self.state_path:
            return
        try:
            temporary = self.state_path + ".tmp"
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump({"fingerprint": self.playlist.fingerprint, "next_seq": self._next_seq,
                           "written": self._truncate_at}, handle)
            os.replace(temporary, self.state_path)
        except Exception:
            pass

    def _load_state(self):
        if not self.state_path or not os.path.isfile(self.state_path):
            return 0
        try:
            with open(self.state_path, "r", encoding="utf-8") as handle:
                state = json.load(handle)
        except Exception:
            return 0
        if state.get("fingerprint") != self.playlist.fingerprint:
            return 0
        skip = min(int(state.get("next_seq", 0)), len(self.tasks))
        written = int(state.get("written", 0))
        if not skip:
            return 0
        if self.explicit:
            if not os.path.isfile(self.output):
                return 0
        else:
            if not os.path.isfile(self.output) or os.path.getsize(self.output) < written:
                return 0
            self._truncate_at = written
            self._running = written
        print(f"  [resume] {skip}/{len(self.tasks)} tasks already stored, continuing from {human_bytes(written)}")
        return skip

    def run(self):
        self._plan()
        if self.folder:
            os.makedirs(self.folder, exist_ok=True)
            for entry in os.listdir(self.folder):
                if entry.startswith(("seg", "init")) and entry.endswith((".ts", ".mp4")):
                    try:
                        os.remove(os.path.join(self.folder, entry))
                    except OSError:
                        pass
            start = 0
        else:
            start = self._load_state()
            self._prepare_output()
            if not start:
                self._truncate_at = 0
                self._running = 0
                if self.explicit and self.total_bytes:
                    try:
                        self.output_handle.truncate(self.total_bytes)
                    except OSError:
                        pass
            elif not self.explicit:
                self.output_handle.truncate(self._truncate_at)
        self.progress_counter = Progress(self.total_bytes, self.total_count, self.progress, self.label)
        self._next_seq = start
        pending = deque(self.tasks[start:])
        self._workers = [threading.Thread(target=self._worker, daemon=True) for _ in range(self.workers)]
        for worker in self._workers:
            worker.start()
        try:
            while True:
                moved = False
                while pending:
                    if self._slots.acquire(blocking=False):
                        self._queue.put(pending.popleft())
                        moved = True
                    else:
                        break
                if self._consume(0.1):
                    moved = True
                if self._error is not None:
                    break
                if self._next_seq >= len(self.tasks) and not pending and self._queue.empty():
                    break
                if not moved:
                    time.sleep(0.005)
        except KeyboardInterrupt:
            self._stop.set()
            self._save_state()
            raise
        finally:
            self._stop.set()
            for worker in self._workers:
                worker.join(timeout=3)
        self.progress_counter.close()
        if self._error is not None:
            self._save_state()
            raise RuntimeError(f"segment download failed: {self._error}")
        if self.folder:
            self._close_handles()
            return self.folder
        self._save_state()
        self.output_handle.flush()
        self.output_handle.truncate(self.total_bytes if self.explicit else self._truncate_at)
        self.output_handle.close()
        self.output_handle = None
        if self.state_path and os.path.isfile(self.state_path):
            os.remove(self.state_path)
        return self.output


def ffmpeg_mux(inputs, output, container, faststart=False, key=None, iv=None, threads=4):
    tool = find_tool("ffmpeg")
    if not tool:
        print("  [!] ffmpeg not found: keeping the raw stream, install ffmpeg to remux")
        return None
    command = [tool, "-hide_banner", "-loglevel", "error", "-y",
               "-protocol_whitelist", "file,crypto,data,http,https,tcp,tls"]
    if key:
        command += ["-decryption_key", key]
    if iv:
        command += ["-decryption_iv", iv]
    for item in inputs:
        command += ["-i", item]
    if len(inputs) > 1:
        command += ["-map", "0:v:0", "-map", f"{len(inputs) - 1}:a:0"]
    else:
        command += ["-map", "0:v:0", "-map", "0:a:0?"]
    if container == "mp4" and faststart:
        command += ["-movflags", "+faststart"]
    if container == "mkv":
        command += ["-max_muxing_queue_size", "4096"]
    command += ["-threads", str(threads), "-c", "copy", "-y", output]
    started = time.time()
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    _, errors = process.communicate()
    if process.returncode != 0:
        lines = (errors or b"").decode("utf-8", "replace").strip().splitlines()
        print("  [!] ffmpeg failed: " + (lines[-1] if lines else f"exit {process.returncode}"))
        return None
    print(f"  [+] remuxed in {human_time(time.time() - started)} -> {os.path.basename(output)} "
          f"({human_bytes(os.path.getsize(output))})")
    return output


def write_local_manifest(playlist, engine, key_name="", key_iv=""):
    lines = ["#EXTM3U", "#EXT-X-VERSION:6"]
    if playlist.segments:
        lines.append(f"#EXT-X-TARGETDURATION:{int(max(1, max(s.duration for s in playlist.segments)))}")
        lines.append(f"#EXT-X-MEDIA-SEQUENCE:{playlist.media_sequence}")
    if key_name:
        attrs = f'METHOD=AES-128,URI="{key_name}"'
        if key_iv:
            attrs += f",IV=0x{key_iv}"
        lines.append("#EXT-X-KEY:" + attrs)
    for position, segment in enumerate(playlist.segments):
        lines.append(f"#EXTINF:{segment.duration:.5f},")
        init = engine.map_names.get(position)
        if init and os.path.isfile(os.path.join(engine.folder, init)):
            lines.append(f'#EXT-X-MAP:URI="{init}"')
        lines.append(engine.names.get(position, f"seg{position:06d}.ts"))
    lines.append("#EXT-X-ENDLIST")
    path = os.path.join(engine.folder, "local.m3u8")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return path


def fetch_keys(http, keys, folder):
    if not keys:
        return "", "", None
    if len(keys) > 1:
        print("  [!] the playlist rotates keys; the remux can only use the first one")
    uri, iv = keys[0]
    name = "key.bin"
    material = http.read(uri, 0, 16)
    with open(os.path.join(folder, name), "wb") as handle:
        handle.write(material)
    return name, iv, material.hex()


def build_options(args):
    headers = {}
    if args.referer:
        headers["Referer"] = args.referer
    for item in args.header or []:
        key, _, value = item.partition(":")
        if value.strip():
            headers[key.strip()] = value.strip()
    return headers


def resolve_stream(http, url, args):
    extra = {"Referer": args.referer} if args.referer else None
    master = fetch_playlist(url, http, extra)
    if args.audio_only and master.audios:
        audio = select_audio(master, {})
        media = fetch_playlist(audio["url"], http, extra)
        media.variant = {"bandwidth": audio.get("bandwidth") or 0}
        media.audio = {}
        return media
    if not master.variants:
        master.variant = {}
        return master
    height_cap = 0 if args.quality in (None, "", "best") else parse_rate(args.quality)
    variant = select_variant(master, height_cap)
    if variant is None:
        raise RuntimeError("no usable variant in the master playlist")
    media = fetch_playlist(variant["url"], http, extra)
    media.variant = variant
    media.audio = {} if args.no_audio else select_audio(master, variant)
    return media


def make_engine(http, media, output, folder, args, label=""):
    return HlsEngine(http, media, output=output, folder=folder, workers=args.connections,
                     queue_depth=args.queue_depth, split_mb=args.split_mb, split_conns=args.split_conns,
                     direct_mb=args.direct_mb, resume=not args.no_resume, progress=not args.quiet,
                     label=label)


def resolve_output(url, args, container):
    name = default_name(url, args.quality, container)
    if args.output:
        if args.output.lower().endswith(VIDEO_SUFFIXES) and not os.path.isdir(args.output):
            return unique_path(args.output)
        return unique_path(os.path.join(args.output, name))
    if args.output_dir:
        return unique_path(os.path.join(args.output_dir, name))
    return unique_path(os.path.join(os.getcwd(), name))


def run_fast(url, args):
    started = time.time()
    http = Http(headers=build_options(args), timeout=args.timeout, verbose=args.verbose)
    workdir = args.keep_dir
    try:
        media = resolve_stream(http, url, args)
        if not media.segments:
            raise RuntimeError("no media segments in the playlist")
        if media.key_format not in ("", "identity"):
            raise RuntimeError(f"unsupported encryption KEYFORMAT={media.key_format}")
        encrypted = any(segment.key_uri for segment in media.segments)
        codecs = (media.variant.get("codecs") or "") + "," + (media.audio.get("CODECS") or "")
        container = args.container or pick_container(codecs)
        output = resolve_output(url, args, container)
        fmp4 = any(segment.map_url for segment in media.segments)
        raw_ext = "mp4" if fmp4 else "ts"
        print(f"  [*] {media.variant.get('height') or '?'}p  "
              f"{human_bytes((media.variant.get('bandwidth') or 0) / 8)}/s  "
              f"{len(media.segments)} segments  {args.connections} connections"
              f"{'  [AES-128]' if encrypted else ''}")
        owned = workdir is None
        workdir = workdir or tempfile.mkdtemp(prefix="hls_")
        try:
            if encrypted:
                if not find_tool("ffmpeg"):
                    raise RuntimeError("the stream is AES-128 encrypted but ffmpeg was not found")
                folder = os.path.join(workdir, "segments")
                engine = make_engine(http, media, None, folder, args, label="  download")
                engine.run()
                key_name, key_iv, key_hex = fetch_keys(http, engine.keys, folder)
                manifest = write_local_manifest(media, engine, key_name, key_iv)
                if args.keep_raw:
                    print(f"  [i] encrypted segments kept in {folder}")
                    return manifest
                ffmpeg_mux([manifest], output, container, args.faststart,
                           key=key_hex, iv=("0x" + key_iv) if key_iv else None)
                return output
            raw = os.path.join(workdir, f"stream.{raw_ext}")
            make_engine(http, media, raw, None, args, label="  download").run()
            audio_input = None
            if media.audio.get("url"):
                audio_playlist = fetch_playlist(media.audio["url"], http,
                                                {"Referer": args.referer} if args.referer else None)
                audio_playlist.variant = {"avg_bandwidth": media.audio.get("bandwidth") or 0}
                audio_input = os.path.join(workdir, "audio.ts")
                make_engine(http, audio_playlist, audio_input, None, args, label="  audio   ").run()
            needs_mux = bool(audio_input) or args.force_mux or raw_ext != container
            if needs_mux and ffmpeg_mux([raw] + ([audio_input] if audio_input else []),
                                        output, container, args.faststart):
                return output
            if os.path.exists(output):
                try:
                    os.remove(output)
                except OSError:
                    pass
            shutil.move(raw, output)
            return output
        finally:
            if owned and os.path.isdir(workdir):
                shutil.rmtree(workdir, ignore_errors=True)
            elif workdir:
                print(f"  [i] temporary files kept in {workdir}")
    finally:
        http.close()
        if not args.quiet:
            print(f"  [i] {human_bytes(http.bytes_downloaded)} transferred in {human_time(time.time() - started)}")


def run_ytdlp(url, args):
    ffmpeg = find_tool("ffmpeg")
    aria2c = find_tool("aria2c")
    options = {
        "format": f"bestvideo[height<={args.max_height}]+bestaudio/best[height<={args.max_height}]/best",
        "outtmpl": args.output if (args.output and args.output.lower().endswith(VIDEO_SUFFIXES))
        else args.ytdlp_template,
        "http_headers": {"User-Agent": UA, **({"Referer": args.referer} if args.referer else {})},
        "concurrent_fragment_downloads": args.connections,
        "hls_prefer_native": True,
        "continuedl": not args.no_resume,
        "retries": 20,
        "fragment_retries": 20,
        "file_access_retries": 5,
        "socket_timeout": args.timeout,
        "merge_output_format": args.container or "mp4",
        "buffersize": 1024 * 256,
        "verbose": args.verbose,
        "downloader_args": {"ffmpeg_i": ["-allowed_extensions", "ALL"], "ffmpeg": ["-allowed_extensions", "ALL"]},
        "postprocessor_args": {"ffmpeg": ["-allowed_extensions", "ALL"]},
    }
    if args.split_mb:
        options["http_chunk_size"] = args.split_mb << 20
    if ffmpeg:
        options["ffmpeg_location"] = os.path.dirname(ffmpeg)
    if aria2c:
        options["external_downloader"] = {"http": aria2c, "https": aria2c}
        options["external_downloader_args"] = {"aria2c": ["-x8", "-s8", "-k1M", "--file-allocation=none",
                                                          "--max-connection-per-server=8", "--summary-interval=0",
                                                          "--console-log-level=warn"]}
    print(f"  [*] yt-dlp engine  {args.connections} concurrent fragments"
          f"{'  + aria2c multi-range' if aria2c else ''}")
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([url])
    return True


def looks_like_hls(url):
    return ".m3u8" in url.lower()


def list_variants(url, args):
    http = Http(headers=build_options(args), timeout=args.timeout)
    try:
        playlist = fetch_playlist(url, http, {"Referer": args.referer} if args.referer else None)
        if not playlist.variants:
            print(f"  media playlist: {len(playlist.segments)} segments, "
                  f"target {playlist.target_duration:.0f}s, endlist={playlist.endlist}")
            return
        print(f"  {'height':>7}  {'bitrate':>11}  {'fps':>5}  codecs")
        for variant in sorted(playlist.variants, key=lambda v: -v.get("height", 0)):
            fps = re.search(r"([0-9.]+)$", str(variant.get("FRAME-RATE", "")))
            print(f"  {variant.get('height', 0):>7}  {human_bytes(variant.get('bandwidth', 0) / 8) + '/s':>11}"
                  f"  {(fps.group(1) if fps else ''):>5}  {variant.get('codecs', '')}")
        for audio in playlist.audios:
            print(f"  audio {audio.get('NAME', '')} {audio.get('LANGUAGE', '')} "
                  f"{human_bytes(audio.get('bandwidth', 0) / 8)}/s {audio.get('CODECS', '')}")
    finally:
        http.close()


def build_parser():
    parser = argparse.ArgumentParser(
        prog="video.py",
        description="Fast m3u8/HLS downloader: parallel keep-alive segment pool, AES-128 support, quick remux.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("urls", nargs="*", help="m3u8 url(s)")
    parser.add_argument("-i", "--input-file", help="text file with one url per line")
    parser.add_argument("-c", "--clipboard", action="store_true", help="read the url from the clipboard")
    parser.add_argument("-q", "--quality", help="max height (e.g. 720) or 'best'")
    parser.add_argument("--max-height", type=int, default=1080, help="height cap for the yt-dlp engine")
    parser.add_argument("-n", "--connections", type=int, default=24, help="parallel connections per stream")
    parser.add_argument("--queue-depth", type=int, default=0, help="0 = connections + 4 (memory/speed trade-off)")
    parser.add_argument("-o", "--output", help="output file or folder")
    parser.add_argument("-O", "--output-dir", help="folder for the output file")
    parser.add_argument("--container", choices=["mp4", "mkv"], help="force the output container")
    parser.add_argument("-r", "--referer", default=DEFAULT_REFERER, help="Referer header ('' disables it)")
    parser.add_argument("-H", "--header", action="append", help="extra header, e.g. -H 'Cookie: a=b'")
    parser.add_argument("--engine", choices=["auto", "fast", "ytdlp"], default="auto")
    parser.add_argument("--split-mb", type=int, default=0,
                        help="split segments bigger than this into parallel byte ranges (8-16 recommended)")
    parser.add_argument("--split-conns", type=int, default=4, help="connections per split segment")
    parser.add_argument("--direct-mb", type=int, default=24, help="stream segments above this size straight to disk")
    parser.add_argument("--audio-only", action="store_true", help="download the audio rendition only")
    parser.add_argument("--no-audio", action="store_true", help="skip the external audio rendition")
    parser.add_argument("--no-resume", action="store_true", help="ignore an existing .part file")
    parser.add_argument("--force-mux", action="store_true", help="always remux with ffmpeg")
    parser.add_argument("--faststart", action="store_true", help="move the mp4 index to the front")
    parser.add_argument("--keep-dir", help="keep temporary files in this folder")
    parser.add_argument("--keep-raw", action="store_true", help="keep the raw stream instead of remuxing")
    parser.add_argument("-l", "--list", action="store_true", help="list the available qualities and exit")
    parser.add_argument("-t", "--timeout", type=float, default=25.0)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--ytdlp-template", default="%(title)s [%(height)sp].%(ext)s")
    return parser


def ask_quality(args, interactive):
    if args.quality or args.quiet or not interactive:
        return args.quality or "best"
    print("\nSelect Video Quality:")
    print("1) 1080p (Highest ~2.5GB)")
    print("2) 720p  (Medium ~1.0GB)")
    print("3) 480p  (Low ~500MB)")
    print("4) 360p  (Lowest ~250MB)")
    print("b) best   a) audio only   l) list all variants")
    choice = input("Enter option (default 2): ").strip().lower()
    if choice in ("b", "l"):
        return "best"
    if choice == "a":
        return "audio"
    return {"1": "1080", "2": "720", "3": "480", "4": "360"}.get(choice, "720")


def gather_urls(args, parser):
    urls = [url.strip() for url in args.urls if url.strip()]
    if args.input_file and os.path.isfile(args.input_file):
        with open(args.input_file, "r", encoding="utf-8") as handle:
            urls += [line.strip() for line in handle if line.strip() and not line.startswith("#")]
    if args.clipboard:
        try:
            import tkinter
            root = tkinter.Tk()
            root.withdraw()
            urls.append(root.clipboard_get().strip())
            root.destroy()
        except Exception:
            print("  [!] could not read the clipboard")
    if not urls and sys.stdin.isatty():
        pasted = input("Paste your .m3u8 URL here: ").strip()
        if pasted:
            urls.append(pasted)
    if not urls:
        parser.print_help()
        sys.exit(1)
    return urls


def run_one(url, args, stop):
    started = time.time()
    print(f"\n[>] {url}")
    mode = args.engine
    use_fast = mode == "fast" or (mode == "auto" and looks_like_hls(url))
    if args.quality == "audio":
        args.audio_only = True
        args.quality = "best"
    result = None
    if use_fast:
        try:
            result = run_fast(url, args)
        except KeyboardInterrupt:
            stop.set()
            print("\n  [!] stopped: the .part file is kept so the next run can resume")
            return None
        except Exception as err:
            if mode == "fast":
                print(f"  [!] fast engine failed: {err}")
                return None
            print(f"  [!] fast engine failed ({err}); falling back to yt-dlp")
            result = None
    if result is None and (mode == "ytdlp" or (mode == "auto" and not use_fast)):
        try:
            run_ytdlp(url, args)
        except KeyboardInterrupt:
            stop.set()
            print("\n  [!] stopped")
            return None
    if not args.quiet:
        print(f"[+] finished in {human_time(time.time() - started)}")
    return result


def download_m3u8(m3u8_url, quality="720", output_name=None, referer_url=DEFAULT_REFERER, **overrides):
    args = build_parser().parse_args([])
    args.quality = quality
    args.referer = referer_url
    args.output = output_name
    for key, value in overrides.items():
        if hasattr(args, key):
            setattr(args, key, value)
    return run_one(m3u8_url, args, threading.Event())


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.connections = max(1, min(256, args.connections))
    urls = gather_urls(args, parser)
    interactive = not args.urls and sys.stdin.isatty()
    quality = ask_quality(args, interactive)
    args.quality = quality
    args.list = args.list or quality == "list"
    if args.list:
        args.quality = "best"
    stop = threading.Event()
    failures = 0
    for url in urls:
        current = copy.copy(args)
        if len(urls) > 1 and args.output and not args.output.lower().endswith(VIDEO_SUFFIXES):
            current.output = args.output
        try:
            if current.list:
                list_variants(url, current)
                continue
            run_one(url, current, stop)
        except KeyboardInterrupt:
            stop.set()
            print("\n[!] stopped")
            break
        except Exception as err:
            failures += 1
            print(f"[x] {type(err).__name__}: {err}")
        if stop.is_set():
            break
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
