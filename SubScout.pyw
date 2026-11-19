#!/usr/bin/env python3
"""
SubScout  v1.0
--------------
https://github.com/ilukezippo/SubScout
Finds and downloads English / Arabic subtitles for a movie folder or a TV-series folder.

Subtitle sources (choose one, or "Auto" to try them in order until one has it):
  * YTS Subs        movies          no key
  * SubDL (API)     movies + series free API key  (subdl.com -> account -> API)
  * SubSource       movies + series free API key  (subsource.net/dashboard/api) — Subscene's successor, big Arabic library
  * OpenSubtitles   movies + series free API key  (opensubtitles.com/consumers), login optional
  * Gestdown        series          no key        (Addic7ed mirror, mostly English)
  * SubDL (website) series          no key        (scraper, needs cloudscraper)

Requirements:  pip install requests beautifulsoup4 cloudscraper
Optional:      pip install keyring      (stores the OpenSubtitles password in Windows Credential Manager)
"""
from __future__ import annotations

import base64
import codecs
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
import traceback
import unicodedata
import urllib.parse
import webbrowser
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from dataclasses import dataclass, field
from io import BytesIO
from zipfile import BadZipFile, ZipFile

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

import requests
from bs4 import BeautifulSoup

try:
    import cloudscraper
except ImportError:
    cloudscraper = None
try:
    import keyring
except ImportError:
    keyring = None

# ============================== Config ==============================
APP_NAME = "SubScout"
APP_TAGLINE = "Movie & series subtitles · English & Arabic · multiple sources"
APP_ID = "ilukezippo.SubScout"
APP_VERSION = "1.0"
DONATE_URL = "https://buymeacoffee.com/ilukezippo"
KEYRING_SERVICE = "SubScout"
OLD_KEYRING_SERVICE = "YIFYSubtitleDownloader"

VIDEO_EXTS = frozenset({".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".m4v", ".ts", ".webm"})
LANGUAGES = {"english": "en", "arabic": "ar"}                 # site name -> filename suffix
LANG3 = {"en": "eng", "ar": "ara"}
LANG_ALIASES = {"en": ("en", "eng", "english"), "ar": ("ar", "ara", "arabic")}  # existing-file detection
MAP_FILENAME = ".yify_imdb_map.json"
NOT_FOUND = "NOT_FOUND"
LEAF = ("movie", "episode")

REQUEST_TIMEOUT = 15
DOWNLOAD_TIMEOUT = 45
RETRIES = 2
BACKOFF_SECS = 0.8
RETRY_CODES = frozenset({403, 429, 437, 500, 502, 503, 504})
RATE_LIMIT_RETRIES = 4

IMDB_WORKERS = 8
MOVIE_WORKERS = 4
SERIES_WORKERS = 3
SCRAPER_DELAY = 0.4
PASS1_MAX_WAIT = 4.0     # first pass: skip a rate-limited source instead of waiting longer than this
PASS2_MAX_WAIT = 180.0   # second pass: wait for the cooldown (sources reset every minute or so)
MAX_SCAN_DEPTH = 4

CHECK_ON, CHECK_OFF = "☑", "☐"
ICON_CLOSED, ICON_OPEN = "📁", "📂"
ST_IDLE, ST_READY, ST_NO_ID = "Idle", "Ready", "No IMDb ID"

ORDINALS = ("first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth",
            "tenth", "eleventh", "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth",
            "seventeenth", "eighteenth", "nineteenth", "twentieth")

COLUMNS = ("selected", "filename", "parsed_title", "year", "imdb_id", "subs", "status")
HEADINGS = {"filename": "File", "parsed_title": "Title", "year": "Year", "imdb_id": "IMDb",
            "subs": "Subtitles", "status": "Status"}

MOVIE_KINDS = frozenset({"movie", "tvMovie", "video"})
SERIES_KINDS = frozenset({"tvSeries", "tvMiniSeries"})
_YTS_RELEASE_RX = re.compile(r"(?:\byify\b|\byts\b)", re.IGNORECASE)
_SKIP_FILE_RX = re.compile(r"(?:^|[ ._\-\[(])(sample|trailer)(?:[ ._\-\])]|$)", re.IGNORECASE)

# ---------------- theme (all text is Segoe UI Bold) ----------------
FONT = "Segoe UI"


def F(size: int = 10) -> tuple:
    return (FONT, size, "bold")


C = {
    "bg": "#0f1115", "surface": "#161a21", "card": "#1c2029", "card_hi": "#262b36", "border": "#2a303c",
    "fg": "#e7eaf0", "muted": "#8a93a6", "faint": "#5c6475",
    "accent": "#7c5cff", "accent_hi": "#9b85ff", "accent_dim": "#2f2766", "accent_press": "#6847f0",
    "on_accent": "#ffffff", "on_accent_dim": "#8a82c4",
    "ok": "#4ade80", "bad": "#f87171", "warn": "#fbbf24", "stripe": "#13161c", "select": "#2b3240",
}


def _state_dir() -> str:
    base = os.getenv("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "SubScout")
    old = os.path.join(base, "YIFYSubtitleDownloader")  # settings from before the rename
    if not os.path.isdir(d) and os.path.isdir(old):
        try:
            shutil.copytree(old, d)
        except OSError:
            pass
    os.makedirs(d, exist_ok=True)
    return d


APP_STATE_FILE = os.path.join(_state_dir(), "settings.json")
CRASH_LOG = os.path.join(_state_dir(), "crash_report.txt")

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
})


# App icon (violet speech-bubble screen + magnifier), embedded so the script stays a single file.
ICON_64_PNG = (
    "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"
)
ICON_32_PNG = (
    "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"
)


# Windows sounds (C:\Windows\Media) — finished / error
SOUND_DONE = "Windows Notify Calendar.wav"
SOUND_ERROR = "Windows Notify Email.wav"


def play_sound(filename: str) -> None:
    """Play a Windows system sound without blocking; silently does nothing on other systems."""
    if sys.platform != "win32":
        return
    try:
        import winsound
        path = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Media", filename)
        if os.path.exists(path):
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        else:
            winsound.MessageBeep()
    except (ImportError, RuntimeError):
        pass


# ============================== Small helpers ==============================
def natural_key(value) -> list:
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", str(value))]


def is_video(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in VIDEO_EXTS and not _SKIP_FILE_RX.search(os.path.splitext(name)[0])


def srt_target(video_path: str, lang_code: str) -> str:
    return f"{os.path.splitext(video_path)[0]}.{lang_code}.srt"


def existing_subs(video_path: str, dir_names_lower: set | None = None) -> set:
    """Language codes that already have a subtitle next to the video (.en.srt, .eng.srt, .english.srt …)."""
    folder, name = os.path.split(video_path)
    stem = os.path.splitext(name)[0].lower()
    if dir_names_lower is None:
        try:
            dir_names_lower = {n.lower() for n in os.listdir(folder)}
        except OSError:
            return set()
    return {code for code, aliases in LANG_ALIASES.items()
            if any(f"{stem}.{a}.srt" in dir_names_lower for a in aliases)}


def read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def write_json(path: str, data) -> None:
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        try:
            os.remove(tmp)
        except OSError:
            pass


def norm_title(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9\s]", " ", _ascii(text or "").lower()).split())


def _ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


# ============================== Network ==============================
def is_unreachable(e: Exception) -> bool:
    """DNS failure / host blocked — retrying won't help."""
    text = str(e)
    return isinstance(e, requests.ConnectionError) and any(
        k in text for k in ("NameResolution", "getaddrinfo", "Name or service not known", "nodename nor servname"))


def _retry_after(r) -> float:
    """Seconds to wait from Retry-After / X-RateLimit-Reset (delta or epoch), capped at 60 s."""
    for h in ("Retry-After", "X-RateLimit-Reset"):
        try:
            v = float(r.headers.get(h, ""))
        except (TypeError, ValueError):
            continue
        if v > 1e9:  # epoch timestamp
            v -= time.time()
        if v > 0:
            return min(60.0, v + 0.5)
    return 0.0


def http_get(url: str, *, session=None, headers=None, params=None, timeout=REQUEST_TIMEOUT, retry_429=True):
    """GET with retries on blocks/5xx/connection errors. 429 waits for Retry-After.
    Other 4xx (e.g. 404) and DNS failures fail immediately."""
    s = session or SESSION
    last_err: Exception | None = None
    attempts = RETRIES + 1
    attempt = 0
    while attempt < attempts:
        wait = BACKOFF_SECS * (attempt + 1)
        try:
            r = s.get(url, params=params, headers=headers, timeout=timeout, allow_redirects=True)
            if r.status_code == 429:
                if not retry_429:
                    raise requests.HTTPError(f"HTTP 429 for {url}", response=r)
                attempts = RATE_LIMIT_RETRIES + 1
                wait = _retry_after(r) or 2.0 * (2 ** attempt)  # 2, 4, 8, 16 s
            if r.status_code in RETRY_CODES:
                raise requests.HTTPError(f"HTTP {r.status_code} for {url}", response=r)
            r.raise_for_status()
            return r
        except requests.HTTPError as e:
            last_err = e
            code = e.response.status_code if e.response is not None else None
            if code is not None and (code not in RETRY_CODES or (code == 429 and not retry_429)):
                raise
        except requests.RequestException as e:
            last_err = e
            if is_unreachable(e):
                raise
        attempt += 1
        if attempt < attempts:
            time.sleep(wait)
    raise last_err  # type: ignore[misc]


_SECRET_RX = re.compile(r"((?:api_key|apikey|token|key)=)[^&\s'\"]+", re.IGNORECASE)


def short_error(e: Exception) -> str:
    """Readable one-line error with API keys masked."""
    if is_unreachable(e):
        host = urllib.parse.urlparse(getattr(getattr(e, "request", None), "url", "") or "").hostname
        return f"can't reach {host or 'the site'} (DNS failed — blocked by your ISP or offline)"
    if isinstance(e, requests.Timeout):
        return "timed out"
    if isinstance(e, requests.ConnectionError):
        return "connection failed"
    code = http_status(e)
    text = f"HTTP {code}" if code else str(e)
    if code == 429:
        text += " (rate limited)"
    return _SECRET_RX.sub(r"\1***", text)[:200]


def http_status(e: Exception) -> int | None:
    resp = getattr(e, "response", None)
    return resp.status_code if resp is not None else None


# ============================== Filename parsing ==============================
_YEAR_RX = re.compile(r"\b(19\d\d|20\d\d)\b")
_TAG_RX = re.compile(
    r"\b(2160p|1080p|720p|480p|4k|uhd|bluray|blu ray|brrip|bdrip|webrip|web dl|webdl|hdrip|dvdrip|hdtv|"
    r"remux|hevc|10bit|h264|x264|h265|x265|aac|ac3|dts|mp3|yify|yts)\b.*$", re.IGNORECASE)


def clean_filename_to_title(filename: str) -> tuple[str, str]:
    """'The.Matrix.1999.1080p.BluRay.mkv' -> ('The Matrix', '1999'). Handles titles like '1917 (2019)'."""
    base, ext = os.path.splitext(filename)
    s = base if ext.lower() in VIDEO_EXTS else filename  # folder names like "The.Matrix.1999" keep the year
    s = re.sub(r"\[[^\]]*\]", " ", s)
    s = re.sub(r"[._\-]+", " ", s)
    year = ""
    m = _YEAR_RX.search(s)
    if m:
        if m.start() > 0:
            year, s = m.group(1), s[:m.start()]
        else:
            m2 = _YEAR_RX.search(s, m.end())
            if m2:
                year, s = m2.group(1), s[:m2.start()]
            else:
                year = m.group(1)
    s = _TAG_RX.sub("", s)
    s = re.sub(r"[\s(\[{+=]+$", "", s.strip()).strip()
    s = " ".join(s.split())
    return (s or year), year


_SXE_RX = re.compile(r"[sS](\d{1,2})[ ._-]?[eE](\d{1,3})")
_NXN_RX = re.compile(r"\b(\d{1,2})x(\d{1,3})\b")
_SEASON_DIR_RX = re.compile(r"(?:season|series|\bs)\s*[._-]?\s*(\d{1,2})", re.IGNORECASE)
_EP_ONLY_RX = re.compile(r"(?:\bep(?:isode)?|\be)\s*[._-]?\s*(\d{1,3})", re.IGNORECASE)


def parse_episode(filename: str, season_dir: str = "") -> tuple[int, int] | None:
    name = os.path.splitext(filename)[0]
    for rx in (_SXE_RX, _NXN_RX):
        m = rx.search(name)
        if m:
            return int(m.group(1)), int(m.group(2))
    sm, em = _SEASON_DIR_RX.search(season_dir or ""), _EP_ONLY_RX.search(name)
    if sm and em:
        return int(sm.group(1)), int(em.group(1))
    return None


# ============================== IMDb lookup ==============================
def _imdb_suggest(query: str) -> list:
    first = query[0] if query[0].isalnum() else "1"
    url = f"https://v2.sg.media-imdb.com/suggestion/{first}/{urllib.parse.quote(query)}.json"
    try:
        return http_get(url, headers={"Accept": "application/json"}).json().get("d") or []
    except (requests.RequestException, ValueError):
        return []


def fetch_imdb_id(title: str, year: str = "", kind: str = "movie") -> str:
    base = norm_title(title)
    if not base:
        return ""
    wanted = SERIES_KINDS if kind == "series" else MOVIE_KINDS

    def score(item: dict) -> int:
        s = 2 if item.get("qid") in wanted else 0
        if year and str(item.get("y", "")) == str(year):
            s += 3
        if norm_title(item.get("l", "")) == base:
            s += 4
        return s

    for q in ([f"{base} {year}", base] if year else [base]):
        candidates = [r for r in _imdb_suggest(q) if str(r.get("id", "")).startswith("tt")]
        if candidates:
            return max(candidates, key=score)["id"]
    return ""


# ============================== Subtitle files ==============================
def _episode_rx(season: int, episode: int) -> list[re.Pattern]:
    return [
        re.compile(rf"s0*{season}[ ._-]?e0*{episode}(?!\d)", re.IGNORECASE),
        re.compile(rf"(?<!\d){season}x0*{episode}(?!\d)", re.IGNORECASE),
        re.compile(rf"(?:\bep?(?:isode)?)[ ._-]?0*{episode}(?!\d)", re.IGNORECASE),
    ]


def pick_srt(zip_bytes: bytes, episode: tuple[int, int] | None = None) -> tuple[bytes | None, str]:
    """Pick the right .srt from a ZIP. For season packs, picks the requested episode's file."""
    with ZipFile(BytesIO(zip_bytes)) as zf:
        srts = [i for i in zf.infolist() if not i.is_dir() and i.filename.lower().endswith(".srt")]
        if not srts:
            return None, "archive contains no .srt"
        if len(srts) == 1:
            return zf.read(srts[0]), srts[0].filename
        if episode:
            for rx in _episode_rx(*episode):
                hit = next((i for i in srts if rx.search(os.path.basename(i.filename))), None)
                if hit:
                    return zf.read(hit), hit.filename
            return None, f"season pack has no file for S{episode[0]:02d}E{episode[1]:02d}"
        best = max(srts, key=lambda i: i.file_size)
        return zf.read(best), best.filename


def extract_subtitle(data: bytes, episode: tuple[int, int] | None = None) -> bytes:
    """Accepts a ZIP or a raw .srt. Raises LookupError when nothing usable is inside."""
    if data[:2] == b"PK":
        srt, info = pick_srt(data, episode)
        if srt is None:
            raise LookupError(info)
        return srt
    head = data[:600].lstrip(codecs.BOM_UTF8).lstrip().lower()
    if not data.strip() or head.startswith((b"<!doctype", b"<html")):
        raise LookupError("download returned a web page instead of a subtitle")
    return data


def normalize_encoding(data: bytes, lang_code: str) -> bytes:
    """Convert legacy encodings (e.g. Windows-1256 Arabic) to UTF-8 so every player shows them correctly."""
    if data.startswith(codecs.BOM_UTF8):
        return data
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16").encode("utf-8")
    fallbacks = ("cp1256",) if lang_code == "ar" else ("cp1252", "latin-1")
    for enc in ("utf-8", *fallbacks):
        try:
            return data.decode(enc).encode("utf-8")
        except UnicodeDecodeError:
            continue
    return data


def write_srt(target: str, data: bytes) -> None:
    tmp = f"{target}.part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, target)


# ============================== Subtitle sources ==============================
@dataclass
class Media:
    kind: str                     # "movie" | "episode"
    path: str
    filename: str
    title: str                    # movie title, or show title for episodes
    year: str = ""
    imdb: str = ""                # movie tt, or the SHOW's tt for episodes
    season: int | None = None
    episode: int | None = None
    season_dir: str = ""

    @property
    def ep(self) -> tuple[int, int] | None:
        return (self.season, self.episode) if self.kind == "episode" and self.season is not None else None


class ProviderBusy(Exception):
    """The source is cooling down from its rate limit: try another source now, retry this one later."""


_tls = threading.local()  # per worker thread: how long a request may wait for a cooling-down source


def wait_budget() -> float:
    return getattr(_tls, "max_wait", 1e9)


class ProviderUnavailable(Exception):
    """The source can't be used for the rest of this batch (quota reached, bad key, blocked…)."""


class Provider:
    key = ""
    name = ""
    short = ""
    movies = False
    series = False
    note = ""
    key_url = ""
    serial = False        # scrapers: one request at a time
    min_interval = 0.25   # seconds between requests to this site (shared by all worker threads)

    def __init__(self, settings: dict, log=print):
        self.settings = settings
        self.log = log
        self.disabled = ""
        self.failures = 0
        self._lock = threading.Lock()
        self.serial_lock = threading.Lock() if self.serial else nullcontext()
        self._tlock = threading.Lock()
        self._next_slot = 0.0
        self._cool_until = 0.0
        self.cancel_event: threading.Event | None = None

    def cooldown(self, seconds: float) -> None:
        """Pause this source for every worker thread (rate limit reached)."""
        with self._tlock:
            self._cool_until = max(self._cool_until, time.monotonic() + seconds)

    def cooling_for(self) -> float:
        return max(0.0, self._cool_until - time.monotonic())

    def throttle(self) -> None:
        with self._tlock:
            now = time.monotonic()
            start = max(now, self._next_slot, self._cool_until)
            wait = start - now
            if wait > wait_budget():
                raise ProviderBusy(f"rate limited for {int(wait)} s")
            self._next_slot = start + self.min_interval
        if wait > 0:
            if self.cancel_event is not None:
                if self.cancel_event.wait(wait):
                    raise ProviderBusy("canceled")
            else:
                time.sleep(wait)

    def note_limits(self, r) -> None:
        """Slow down *before* the quota runs out, using the site's X-RateLimit-* headers."""
        try:
            remaining = int(r.headers.get("X-RateLimit-Remaining", ""))
        except (TypeError, ValueError, AttributeError):
            return
        if remaining <= 1:
            self.cooldown(_retry_after(r) or 60.0)

    def get(self, url: str, **kw):
        for _ in range(3):
            self.throttle()
            try:
                r = http_get(url, retry_429=False, **kw)
            except requests.HTTPError as e:
                if http_status(e) == 429:  # rate limited: pause the whole source, not just this thread
                    self.cooldown(_retry_after(e.response) or 30.0)
                    continue
                raise
            self.note_limits(r)
            return r
        raise ProviderBusy("rate limited")

    def supports(self, kind: str) -> bool:
        return self.movies if kind == "movie" else self.series

    def missing_config(self) -> str:
        return ""

    def begin(self) -> None:
        """Called once per batch (worker thread) — e.g. to log in."""

    def disable(self, reason: str) -> bool:
        with self._lock:
            if self.disabled:
                return False
            self.disabled = reason
            return True

    def fetch(self, m: Media, lang_name: str, code: str) -> bytes:
        raise NotImplementedError


# ---------- YTS Subs (movies) ----------
def find_lang_slug(movie_html: str, site_lang: str) -> str | None:
    m = re.search(rf'/subtitles/[^"\s<>]+-{re.escape(site_lang)}-(?:yify|yts)-\d+', movie_html, re.IGNORECASE)
    return m.group(0) if m else None


def _b64_to_text(s: str) -> str:
    s = (s or "").strip()
    try:
        return base64.b64decode(s + "=" * (-len(s) % 4), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


def find_zip_link(sub_html: str, base: str) -> str | None:
    soup = BeautifulSoup(sub_html, "html.parser")
    a = soup.select_one('a#btn-download-subtitle[data-link], a[class*="download-subtitle"][data-link]')
    if a:
        raw = a["data-link"].strip()
        link = raw if raw.startswith(("http", "/")) else _b64_to_text(raw)
        if link:
            return urllib.parse.urljoin(base, link)
    a = soup.select_one('a#download-subtitle[href], a[class*="download-subtitle"][href]')
    if a:
        return urllib.parse.urljoin(base, a["href"])
    m = re.search(r'(https?://[^"\'\s)]+\.zip(?:\?[^"\'\s)]+)?)', sub_html, re.IGNORECASE)
    return m.group(1) if m else None


class YtsProvider(Provider):
    key, name, short = "yts", "YTS Subs", "YTS"
    movies = True
    note = "Movies · no key · needs IMDb ID"
    BASE = "https://yts-subs.com"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._pages: dict[str, str] = {}

    def fetch(self, m, lang_name, code):
        if not m.imdb:
            raise LookupError("needs an IMDb ID")
        with self._lock:
            html = self._pages.get(m.imdb)
        if html is None:
            try:
                html = self.get(f"{self.BASE}/movie-imdb/{m.imdb}").text
            except requests.HTTPError as e:
                if http_status(e) == 404:
                    raise LookupError("movie not on YTS") from e
                raise
            with self._lock:
                self._pages[m.imdb] = html
        slug = find_lang_slug(html, lang_name)
        if not slug:
            raise LookupError(f"no {lang_name} subtitle listed")
        page_url = urllib.parse.urljoin(self.BASE, slug)
        zip_url = find_zip_link(self.get(page_url).text, self.BASE)
        if not zip_url:
            raise LookupError("no download link on the subtitle page")
        return self.get(zip_url, headers={"Referer": page_url}, timeout=DOWNLOAD_TIMEOUT).content


# ---------- SubDL official API (movies + series) ----------
class SubdlApiProvider(Provider):
    key, name, short = "subdl_api", "SubDL (API)", "SubDL"
    movies = series = True
    min_interval = 0.6   # SubDL answers 429 when hit with parallel bursts
    note = "Movies + series · free API key"
    key_url = "https://subdl.com/panel/api"
    API = "https://api.subdl.com/api/v1/subtitles"
    DL = "https://dl.subdl.com"

    def missing_config(self):
        return "" if self.settings.get("subdl_api_key") else "add a free SubDL API key in Sources"

    def fetch(self, m, lang_name, code):
        params = {"api_key": self.settings["subdl_api_key"], "languages": code.upper(), "subs_per_page": 30}
        if m.kind == "movie":
            params["type"] = "movie"
            if not m.imdb and m.year:
                params["year"] = m.year
        else:
            params.update(type="tv", season_number=m.season, episode_number=m.episode)
        if m.imdb:
            params["imdb_id"] = m.imdb
        else:
            params["film_name"] = m.title
        try:
            data = self.get(self.API, params=params, headers={"Accept": "application/json"}).json()
        except requests.HTTPError as e:
            if http_status(e) in (401, 403):
                raise ProviderUnavailable("API key rejected") from e
            raise
        except ValueError as e:
            raise LookupError("bad response") from e
        if not data.get("status"):
            err = str(data.get("error") or data.get("message") or "no results")
            if "key" in err.lower() or "limit" in err.lower():
                raise ProviderUnavailable(err)
            raise LookupError(err)

        subs = []
        for s in data.get("subtitles") or []:
            lang = str(s.get("language") or s.get("lang") or "").lower()
            if lang and lang not in (code, lang_name, LANG3[code]):
                continue
            subs.append(s)
        if m.kind == "episode":
            def matches(s):
                try:
                    if s.get("episode") not in (None, "", 0) and int(s.get("season") or m.season) == m.season:
                        return int(s["episode"]) == m.episode
                    lo, hi = s.get("episode_from"), s.get("episode_end")
                    if lo and hi:
                        return int(lo) <= m.episode <= int(hi)
                    return bool(s.get("full_season"))  # season pack -> picked by filename later
                except (TypeError, ValueError):
                    return False
            exact = [s for s in subs if not s.get("full_season") and matches(s)]
            subs = exact or [s for s in subs if matches(s)]
        if not subs:
            raise LookupError("no match")
        for s in subs[:4]:  # some listed files are gone (404) -> try the next upload
            url = str(s.get("url") or "")
            if not url:
                continue
            try:
                return self.get(url if url.startswith("http") else self.DL + url, timeout=DOWNLOAD_TIMEOUT).content
            except requests.HTTPError as e:
                if http_status(e) not in (404, 410):
                    raise
        raise LookupError("listed files are no longer available")


# ---------- OpenSubtitles.com REST API (movies + series) ----------
class OpenSubtitlesProvider(Provider):
    key, name, short = "opensubtitles", "OpenSubtitles", "OpenSubs"
    movies = series = True
    note = "Movies + series · free API key (5/day, 20 with login)"
    key_url = "https://www.opensubtitles.com/en/consumers"
    BASE = "https://api.opensubtitles.com/api/v1"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.http = requests.Session()
        self.base = self.BASE

    def missing_config(self):
        return "" if self.settings.get("os_api_key") else "add a free OpenSubtitles API key in Sources"

    def begin(self):
        self.http.headers.update({"Api-Key": self.settings.get("os_api_key", ""),
                                  "User-Agent": f"{APP_NAME.replace(' ', '')} v{APP_VERSION}",
                                  "Accept": "application/json"})
        user, pwd = self.settings.get("os_username", ""), self.settings.get("os_password", "")
        if not (user and pwd):
            return
        try:
            r = self.http.post(f"{self.BASE}/login", json={"username": user, "password": pwd}, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                data = r.json()
                self.http.headers["Authorization"] = f"Bearer {data.get('token', '')}"
                if data.get("base_url"):
                    self.base = f"https://{data['base_url']}/api/v1"
                self.log("[INFO] OpenSubtitles: logged in.")
            else:
                self.log(f"[WARN] OpenSubtitles login failed (HTTP {r.status_code}) — continuing without login.")
        except (requests.RequestException, ValueError) as e:
            self.log(f"[WARN] OpenSubtitles login error: {e}")

    def fetch(self, m, lang_name, code):
        params: dict = {"languages": code}
        if m.kind == "movie":
            params["type"] = "movie"
            if m.imdb:
                params["imdb_id"] = int(m.imdb[2:])
            else:
                params["query"] = m.title.lower()
                if m.year:
                    params["year"] = int(m.year)
        else:
            params.update(type="episode", season_number=m.season, episode_number=m.episode)
            if m.imdb:
                params["parent_imdb_id"] = int(m.imdb[2:])
            else:
                params["query"] = m.title.lower()
        query = urllib.parse.urlencode(sorted(params.items()))  # the API redirects unless sorted
        self.throttle()
        r = self.http.get(f"{self.base}/subtitles?{query}", timeout=REQUEST_TIMEOUT)
        if r.status_code in (401, 403):
            raise ProviderUnavailable("API key rejected")
        if r.status_code == 429:
            self.cooldown(_retry_after(r) or 10.0)
            raise ProviderBusy("rate limited")
        r.raise_for_status()
        results = []
        for item in r.json().get("data") or []:
            at = item.get("attributes") or {}
            if not at.get("files"):
                continue
            fd = at.get("feature_details") or {}
            if m.kind == "episode" and fd.get("episode_number") not in (None, m.episode):
                continue
            results.append(at)
        if not results:
            raise LookupError("no match")
        best = max(results, key=lambda a: int(a.get("download_count") or 0))
        file_id = best["files"][0]["file_id"]

        self.throttle()
        r = self.http.post(f"{self.base}/download", json={"file_id": file_id}, timeout=REQUEST_TIMEOUT,
                           headers={"Content-Type": "application/json"})
        if r.status_code in (406, 429):
            msg = ""
            try:
                msg = r.json().get("message", "")
            except ValueError:
                pass
            raise ProviderUnavailable(msg or "download quota reached")
        r.raise_for_status()
        info = r.json()
        if not info.get("link"):
            raise ProviderUnavailable(info.get("message") or "no download link (quota?)")
        if int(info.get("remaining", 1)) <= 0:
            reset = info.get("reset_time") or info.get("reset_time_utc") or ""
            if self.disable("daily download quota used up"):
                self.log(f"[SOURCE OFF] OpenSubtitles: daily download quota used up"
                         + (f" (resets {reset})" if reset else "") + " — skipped for the rest of this batch.")
        return self.get(info["link"], timeout=DOWNLOAD_TIMEOUT).content


# ---------- SubSource (movies + series) — successor of Subscene, strong for Arabic ----------
class SubSourceProvider(Provider):
    key, name, short = "subsource", "SubSource", "SubSource"
    movies = series = True
    note = "Movies + series · free API key · big Arabic library"
    key_url = "https://subsource.net/dashboard/api"
    API = "https://api.subsource.net/api/v1/"
    min_interval = 0.9

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._ids: dict[tuple, int | None] = {}

    def missing_config(self):
        return "" if self.settings.get("subsource_api_key") else "add a free SubSource API key in Sources"

    def _call(self, path: str, params: dict, **kw):
        params = {**params, "api_key": self.settings["subsource_api_key"]}
        try:
            return self.get(self.API + path, params=params, headers={"Accept": "application/json"}, **kw)
        except requests.HTTPError as e:
            if http_status(e) in (401, 403):
                raise ProviderUnavailable("API key rejected") from e
            raise

    def _movie_id(self, m: Media) -> int | None:
        key = (m.imdb or norm_title(m.title), m.year if not m.imdb else "", m.season if m.kind == "episode" else None)
        with self._lock:
            if key in self._ids:
                return self._ids[key]
        params: dict = {"searchType": "imdb", "imdb": m.imdb} if m.imdb else {"searchType": "text", "q": m.title.lower()}
        if m.kind == "episode":
            params["season"] = m.season
        data = self._call("movies/search", params).json().get("data") or []
        want = norm_title(m.title)

        def score(d):
            s = 0
            if norm_title(d.get("title", "")) == want or norm_title(d.get("alternateTitle", "") or "") == want:
                s += 2
            if m.year and str(d.get("releaseYear", "")) == m.year:
                s += 1
            if m.kind == "episode" and str(d.get("season", m.season)) == str(m.season):
                s += 1
            return s

        best = max(data, key=score) if data else None
        if best is not None and not m.imdb and score(best) == 0:
            best = None  # text search returned something unrelated
        mid = best.get("movieId") if best else None
        with self._lock:
            self._ids[key] = mid
        return mid

    def fetch(self, m, lang_name, code):
        mid = self._movie_id(m)
        if not mid:
            raise LookupError("title not on SubSource")
        params: dict = {"movieId": mid, "language": lang_name, "limit": 100}
        if m.kind == "episode":
            params.update(seasonNumber=m.season, episodeNumber=m.episode)
        res = self._call("subtitles", params).json()
        items = [i for i in res.get("data") or [] if str(i.get("language", lang_name)).lower() == lang_name]
        if not items:
            raise LookupError("no subtitle")
        items.sort(key=lambda i: (bool(i.get("hearingImpaired")), bool(i.get("foreignParts"))))
        for item in items[:3]:
            try:
                return self._call(f"subtitles/{item['subtitleId']}/download", {}, timeout=DOWNLOAD_TIMEOUT).content
            except requests.HTTPError as e:
                if http_status(e) not in (404, 410):
                    raise
        raise LookupError("listed files are no longer available")

# ---------- Gestdown / Addic7ed (series) ----------
class GestdownProvider(Provider):
    key, name, short = "gestdown", "Gestdown (Addic7ed)", "Addic7ed"
    series = True
    note = "Series · no key · mostly English"
    BASE = "https://api.gestdown.info"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._shows: dict[str, str | None] = {}

    def _show_id(self, title: str) -> str | None:
        key = norm_title(title)
        with self._lock:
            if key in self._shows:
                return self._shows[key]
        try:
            shows = self.get(f"{self.BASE}/shows/search/{urllib.parse.quote(title)}",
                             headers={"Accept": "application/json"}).json().get("shows") or []
        except requests.HTTPError as e:
            if http_status(e) != 404:
                raise
            shows = []
        hit = next((s for s in shows if norm_title(s.get("name", "")) == key), shows[0] if shows else None)
        sid = str(hit["id"]) if hit else None
        with self._lock:
            self._shows[key] = sid
        return sid

    def fetch(self, m, lang_name, code):
        sid = self._show_id(m.title)
        if not sid:
            raise LookupError("show not found")
        try:
            data = self.get(f"{self.BASE}/subtitles/get/{sid}/{m.season}/{m.episode}/{LANG3[code]}",
                            headers={"Accept": "application/json"}).json()
        except requests.HTTPError as e:
            if http_status(e) in (404, 423):
                raise LookupError("no subtitle") from e
            raise
        subs = [s for s in data.get("matchingSubtitles") or [] if s.get("downloadUri")]
        subs.sort(key=lambda s: (not s.get("completed", True), bool(s.get("hearingImpaired"))))
        if not subs:
            raise LookupError("no subtitle")
        r = self.get(self.BASE + subs[0]["downloadUri"], timeout=DOWNLOAD_TIMEOUT)
        if r.headers.get("Content-Type", "").startswith("text/html"):
            raise ProviderUnavailable("download limit reached")
        return r.content


# ---------- SubDL website scraper (series) ----------
class SubdlWebClient:
    BASE = "https://subdl.com"
    DL_BASE = "https://dl.subdl.com/subtitle/"

    def __init__(self):
        if cloudscraper is not None:
            self.http = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows",
                                                             "desktop": True})
        else:
            self.http = requests.Session()
            self.http.headers.update(SESSION.headers)
        self._shows: dict[str, str | None] = {}
        self._seasons: dict[tuple[str, int], tuple[BeautifulSoup, str] | None] = {}

    def _get(self, url: str, **kw):
        r = http_get(url, session=self.http, **kw)
        time.sleep(SCRAPER_DELAY)
        return r

    def show_url(self, show: str) -> str | None:
        clean = " ".join(re.sub(r"[^\w\s]", "", show).split())
        key = clean.lower()
        if key not in self._shows:
            soup = BeautifulSoup(self._get(f"{self.BASE}/search/{urllib.parse.quote(clean)}").text, "html.parser")
            exact = partial = None
            for a in soup.select('a[href*="/subtitle/"]'):
                text = " ".join(a.get_text(" ").lower().split())
                url = urllib.parse.urljoin(self.BASE, a["href"])
                if text == key:
                    exact = url
                    break
                if partial is None and key in text:
                    partial = url
            self._shows[key] = exact or partial
        return self._shows[key]

    def season_page(self, show_url: str, season: int):
        key = (show_url, season)
        if key in self._seasons:
            return self._seasons[key]
        result = None
        if 1 <= season <= len(ORDINALS):
            url = f"{show_url.rstrip('/')}/{ORDINALS[season - 1]}-season"
            try:
                result = BeautifulSoup(self._get(url).text, "html.parser"), url
            except requests.RequestException:
                result = None
        if result is None:
            try:
                show_soup = BeautifulSoup(self._get(show_url).text, "html.parser")
                rx = re.compile(rf"\bseason\s*0*{season}\b", re.IGNORECASE)
                for a in show_soup.find_all("a", href=True):
                    if "season" in a["href"].lower() and rx.search(a.get_text(" ")):
                        url = urllib.parse.urljoin(self.BASE, a["href"])
                        result = BeautifulSoup(self._get(url).text, "html.parser"), url
                        break
            except requests.RequestException:
                result = None
        self._seasons[key] = result
        return result

    def _abs_link(self, link: str) -> str:
        if link.startswith("http"):
            return link
        return self.BASE + link if link.startswith("/") else self.DL_BASE + link

    def _from_next_data(self, soup, s, e, site_lang):
        tag = soup.find("script", id="__NEXT_DATA__")
        if not tag or not tag.string:
            return None
        try:
            q = deque([json.loads(tag.string)])
        except ValueError:
            return None
        while q:
            cur = q.popleft()
            if isinstance(cur, dict):
                s_val = cur.get("season", cur.get("season_number"))
                e_val = cur.get("episode", cur.get("episode_number"))
                l_val = str(cur.get("language") or cur.get("lang") or "").lower()
                if s_val is not None and e_val is not None and site_lang in l_val:
                    try:
                        if int(s_val) == s and int(e_val) == e:
                            link = cur.get("link") or cur.get("url") or cur.get("downloadLink")
                            if link:
                                return self._abs_link(str(link))
                    except (TypeError, ValueError):
                        pass
                q.extend(cur.values())
            elif isinstance(cur, list):
                q.extend(cur)
        return None

    def _from_html(self, soup, s, e, site_lang):
        ep_rx = re.compile(rf"\b(?:episode|ep|e)\s?0*{e}(?!\d)")
        sxe_rx = re.compile(rf"s0*{s}e0*{e}(?!\d)")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "dl.subdl.com" not in href and not href.lower().endswith(".zip"):
                continue
            block = a.find_parent(["div", "tr", "li", "ul", "section"])
            if block is not None and block.find_parent(["div", "tr", "section"]) is not None:
                block = block.find_parent(["div", "tr", "section"])
            text = (block or a).get_text(" ").lower()
            if site_lang in text and (ep_rx.search(text) or sxe_rx.search(text.replace(" ", ""))):
                return self._abs_link(href)
        return None

    def find(self, show, season, episode, site_lang):
        show_url = self.show_url(show)
        if not show_url:
            raise LookupError("show not found")
        page = self.season_page(show_url, season)
        if page is None:
            raise LookupError(f"season {season} page not found")
        soup, page_url = page
        url = self._from_next_data(soup, season, episode, site_lang) or self._from_html(soup, season, episode, site_lang)
        if not url:
            raise LookupError("no subtitle for this episode")
        return url, page_url

    def download(self, url, referer):
        return self._get(url, headers={"Referer": referer}, timeout=DOWNLOAD_TIMEOUT).content


class SubdlWebProvider(Provider):
    key, name, short = "subdl_web", "SubDL (website)", "SubDL-web"
    series = True
    serial = True
    note = "Series · no key · scraper (needs cloudscraper)"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._client: SubdlWebClient | None = None

    def begin(self):
        if cloudscraper is None:
            self.log("[WARN] SubDL (website): 'cloudscraper' isn't installed — run: pip install cloudscraper")
        self._client = SubdlWebClient()

    def fetch(self, m, lang_name, code):
        try:
            url, ref = self._client.find(m.title, m.season, m.episode, lang_name)
            return self._client.download(url, ref)
        except requests.HTTPError as e:
            if http_status(e) in (403, 429):
                raise ProviderUnavailable(f"blocked by Cloudflare (HTTP {http_status(e)})") from e
            raise


# ---------- "Test" buttons in the Sources window ----------
def test_subdl_key(key: str) -> tuple[bool, str]:
    if not key:
        return False, "Enter the key first"
    r = SESSION.get(SubdlApiProvider.API, timeout=REQUEST_TIMEOUT, params={
        "api_key": key, "imdb_id": "tt0133093", "type": "movie", "languages": "EN", "subs_per_page": 1})
    try:
        data = r.json()
    except ValueError:
        return False, f"HTTP {r.status_code}"
    if data.get("status"):
        return True, "Key works"
    return False, str(data.get("error") or data.get("message") or f"HTTP {r.status_code}")


def test_subsource_key(key: str) -> tuple[bool, str]:
    if not key:
        return False, "Enter the key first"
    r = SESSION.get(SubSourceProvider.API + "movies/search", timeout=REQUEST_TIMEOUT,
                    params={"searchType": "imdb", "imdb": "tt0133093", "api_key": key},
                    headers={"Accept": "application/json"})
    if r.status_code == 200:
        return True, "Key works"
    if r.status_code in (401, 403):
        return False, "Key rejected"
    if r.status_code == 429:
        return False, "Rate limited — try again in a minute"
    return False, f"HTTP {r.status_code}"


def test_opensubtitles(api_key: str, user: str, password: str) -> tuple[bool, str]:
    """Checks the API key, then the login, and reports the daily download allowance."""
    if not api_key:
        return False, "Enter the OpenSubtitles API key first"
    base = OpenSubtitlesProvider.BASE
    headers = {"Api-Key": api_key, "User-Agent": f"{APP_NAME.replace(' ', '')} v{APP_VERSION}",
               "Accept": "application/json", "Content-Type": "application/json"}

    def msg(r, default):
        try:
            return str(r.json().get("message") or default)
        except ValueError:
            return default

    if not (user and password):
        r = requests.get(f"{base}/infos/formats", headers=headers, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            return True, "API key works · no login = 5 downloads/day"
        return False, "API key rejected" if r.status_code in (401, 403) else msg(r, f"HTTP {r.status_code}")

    r = requests.post(f"{base}/login", json={"username": user, "password": password},
                      headers=headers, timeout=REQUEST_TIMEOUT)
    if r.status_code == 429:
        return False, "Too many attempts — wait a minute and try again"
    if r.status_code == 403:
        return False, "API key rejected"
    if r.status_code != 200:
        return False, "Wrong username or password" if r.status_code == 401 else msg(r, f"HTTP {r.status_code}")
    data = r.json()
    u = data.get("user") or {}
    parts = [f"Logged in as {user}"]
    if u.get("allowed_downloads") is not None:
        parts.append(f"{u['allowed_downloads']} downloads/day")
    try:  # remaining today needs the token
        api = f"https://{data['base_url']}/api/v1" if data.get("base_url") else base
        info = requests.get(f"{api}/infos/user", timeout=REQUEST_TIMEOUT,
                            headers={**headers, "Authorization": f"Bearer {data.get('token', '')}"}).json()
        left = (info.get("data") or {}).get("remaining_downloads")
        if left is not None:
            parts.append(f"{left} left today")
    except (requests.RequestException, ValueError, KeyError):
        pass
    if u.get("vip"):
        parts.append("VIP")
    elif u.get("level"):
        parts.append(str(u["level"]))
    return True, " · ".join(parts)


PROVIDER_CLASSES = (YtsProvider, SubdlApiProvider, SubSourceProvider, OpenSubtitlesProvider, SubdlWebProvider,
                    GestdownProvider)
PROVIDERS = {cls.key: cls for cls in PROVIDER_CLASSES}
DEFAULT_PROVIDER_ORDER = [{"key": cls.key, "enabled": True} for cls in PROVIDER_CLASSES]


# ============================== Drawn widgets ==============================
def _checkbox_image(master, size: int, fill: str, border: str, tick: str | None, gap: int = 0) -> tk.PhotoImage:
    """Anti-aliased-ish rounded checkbox drawn pixel by pixel (Tk has no vector checkbox)."""
    def rgb(h):
        return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))

    bg, fill_c, border_c = rgb(C["card"]), rgb(fill), rgb(border)
    tick_c = rgb(tick) if tick else None
    r, bw = size * 0.22, max(1.0, size / 12)
    pts = [(0.24, 0.52), (0.42, 0.70), (0.77, 0.32)]
    tw = max(1.4, size / 9)

    def seg_dist(px, py, a, b):
        (ax, ay), (bx, by) = (a[0] * size, a[1] * size), (b[0] * size, b[1] * size)
        dx, dy = bx - ax, by - ay
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        return ((px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2) ** 0.5

    rows = []
    for y in range(size):
        row = []
        for x in range(size):
            px, py = x + 0.5, y + 0.5
            qx = max(r - px, 0, px - (size - r))
            qy = max(r - py, 0, py - (size - r))
            d = (qx * qx + qy * qy) ** 0.5 - r  # signed distance to rounded rect (<0 inside)
            if d > 0.5:
                col = bg
            else:
                inside = min(1.0, 0.5 - d)
                col = border_c if d > -bw else fill_c
                if tick_c and min(seg_dist(px, py, pts[0], pts[1]), seg_dist(px, py, pts[1], pts[2])) < tw:
                    col = tick_c
                col = tuple(round(b_ + (c_ - b_) * inside) for b_, c_ in zip(bg, col))
            row.append("#%02x%02x%02x" % col)
        row.extend([C["card"]] * gap)  # spacing between box and label
        rows.append("{" + " ".join(row) + "}")
    img = tk.PhotoImage(master=master, width=size + gap, height=size)
    img.put(" ".join(rows))
    return img


# ============================== App state ==============================
@dataclass
class TabState:
    key: str
    tree: ttk.Treeview | None = None
    rows: dict = field(default_factory=dict)
    order: list = field(default_factory=list)
    visible: set = field(default_factory=set)
    root: str = ""
    path: str = ""
    recent: list = field(default_factory=list)
    all_checked: bool = True
    sort_col: str = ""
    sort_rev: bool = False
    source: str = "auto"


# ============================== App ==============================
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.withdraw()
        self.title(APP_NAME)
        try:
            self._icons = [tk.PhotoImage(data=ICON_64_PNG), tk.PhotoImage(data=ICON_32_PNG)]
            self.iconphoto(True, *self._icons)  # also used by every dialog
        except tk.TclError:
            pass
        self.scale = max(1.0, self.winfo_fpixels("1i") / 96.0)
        self.configure(bg=C["bg"])

        self.tabs = {"movies": TabState("movies"), "series": TabState("series")}
        self.active = "movies"
        self.busy: str | None = None
        self.cancel = threading.Event()
        self.menu_row: str | None = None
        self._map_lock = threading.Lock()
        self._ui_q: queue.SimpleQueue = queue.SimpleQueue()
        self._saved_window: dict = {}
        self._log_px = int(190 * self.scale)
        self._log_anim = None
        self.settings = {"providers": [dict(p) for p in DEFAULT_PROVIDER_ORDER],
                         "subdl_api_key": "", "subsource_api_key": "", "os_api_key": "", "os_username": "",
                         "os_password": ""}

        self._setup_fonts()
        self._setup_styles()
        self._build_ui()
        self._load_settings()
        self._refresh_source_choices()
        self.minsize(int(1000 * self.scale), int(700 * self.scale))
        self._restore_window()
        self.deiconify()
        self._sash_pending = True
        self.paned.bind("<Configure>", self._on_paned_configure, add="+")
        self._dark_title_bar(self)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(40, self._drain_ui_queue)

    # ---------- thread-safe UI bridge ----------
    def ui(self, fn, *args) -> None:
        self._ui_q.put((fn, args))

    def _drain_ui_queue(self) -> None:
        deadline = time.perf_counter() + 0.03
        try:
            while time.perf_counter() < deadline:
                fn, args = self._ui_q.get_nowait()
                try:
                    fn(*args)
                except Exception:
                    self._append_log("[UI ERROR] " + traceback.format_exc().strip().splitlines()[-1])
        except queue.Empty:
            pass
        self.after(40, self._drain_ui_queue)

    def report_callback_exception(self, exc, val, tb):
        self._append_log(f"[UI ERROR] {val}")
        try:
            with open(CRASH_LOG, "a", encoding="utf-8") as f:
                f.write(f"\n--- {time.ctime()} ---\n{''.join(traceback.format_exception(exc, val, tb))}")
        except OSError:
            pass

    @property
    def cur(self) -> TabState:
        return self.tabs[self.active]

    # ---------- look & feel ----------
    def _setup_fonts(self) -> None:
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont",
                     "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont", "TkFixedFont"):
            try:
                tkfont.nametofont(name).configure(family=FONT, size=10, weight="bold")
            except tk.TclError:
                pass
        self.option_add("*TCombobox*Listbox.font", F(10))

    @staticmethod
    def _dark_title_bar(win: tk.Misc) -> None:
        if sys.platform != "win32":
            return
        try:
            import ctypes
            win.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
            val = ctypes.c_int(1)
            for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (Win11 / older Win10)
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val),
                                                               ctypes.sizeof(val)) == 0:
                    break
        except Exception:
            pass

    def _restore_window(self) -> None:
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        geo = self._saved_window.get("geometry", "")
        m = re.fullmatch(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", geo)
        if m and int(m[1]) <= sw and int(m[2]) <= sh and -50 < int(m[3]) < sw - 100 and -50 < int(m[4]) < sh - 100:
            self.geometry(geo)
        else:  # first run: ~90% of the screen
            w = min(int(1400 * self.scale), int(sw * 0.9))
            h = int(sh * 0.88)
            self.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 2 - 20)}")
        if self._saved_window.get("zoomed") and sys.platform == "win32":
            self.state("zoomed")

    def _center(self, w: int, h: int) -> None:
        w, h = int(w * self.scale), int(h * self.scale)
        self.update_idletasks()
        w, h = min(w, self.winfo_screenwidth() - 40), min(h, self.winfo_screenheight() - 80)
        x = max(0, (self.winfo_screenwidth() - w) // 2)
        y = max(0, (self.winfo_screenheight() - h) // 2 - 20)
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _center_child(self, win: tk.Toplevel, w: int, h: int | None = None) -> None:
        win.update_idletasks()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        w = min(max(int(w * self.scale), win.winfo_reqwidth() + 8), sw - 40)
        h = min((int(h * self.scale) if h else win.winfo_reqheight()) + 8, sh - 80)
        x = self.winfo_rootx() + (self.winfo_width() - w) // 2
        y = self.winfo_rooty() + (self.winfo_height() - h) // 2
        x, y = min(max(0, x), sw - w), min(max(0, y), sh - h - 40)
        win.geometry(f"{w}x{h}+{x}+{y}")
        win.minsize(min(w, sw - 40), min(h, sh - 80))

    def _setup_styles(self) -> None:
        st = ttk.Style(self)
        st.theme_use("clam")
        flat = dict(bordercolor=C["bg"], lightcolor=C["bg"], darkcolor=C["bg"])
        row_h = tkfont.Font(font=F(10)).metrics("linespace") + int(12 * self.scale)

        st.configure(".", background=C["bg"], foreground=C["fg"], font=F(10), focuscolor=C["bg"], **flat)
        st.configure("TFrame", background=C["bg"])
        st.configure("Card.TFrame", background=C["card"])
        st.configure("Surface.TFrame", background=C["surface"])

        st.configure("TLabel", background=C["bg"], foreground=C["fg"], font=F(10))
        st.configure("Title.TLabel", font=F(18), foreground=C["fg"])
        st.configure("Sub.TLabel", font=F(9), foreground=C["muted"])
        st.configure("Card.TLabel", background=C["card"], foreground=C["fg"])
        st.configure("CardMuted.TLabel", background=C["card"], foreground=C["muted"], font=F(9))
        st.configure("Surface.TLabel", background=C["surface"], foreground=C["muted"], font=F(9))
        st.configure("Section.TLabel", background=C["card"], foreground=C["muted"], font=F(8))
        st.configure("TestBusy.TLabel", background=C["card"], foreground=C["muted"], font=F(9))
        st.configure("TestOk.TLabel", background=C["card"], foreground=C["ok"], font=F(9))
        st.configure("TestBad.TLabel", background=C["card"], foreground=C["bad"], font=F(9))

        btn = dict(borderwidth=0, padding=[14, 7], font=F(10), relief="flat",
                   bordercolor=C["card_hi"], lightcolor=C["card_hi"], darkcolor=C["card_hi"])
        st.configure("TButton", background=C["card_hi"], foreground=C["fg"], **btn)
        st.map("TButton", background=[("disabled", C["card"]), ("pressed", C["border"]), ("active", "#323846")],
               foreground=[("disabled", C["faint"])],
               bordercolor=[("active", "#323846")], lightcolor=[("active", "#323846")],
               darkcolor=[("active", "#323846")])
        acc = dict(btn, bordercolor=C["accent"], lightcolor=C["accent"], darkcolor=C["accent"])
        st.configure("Accent.TButton", background=C["accent"], foreground=C["on_accent"], **{**acc, "font": F(10)})
        st.map("Accent.TButton", background=[("disabled", C["accent_dim"]), ("pressed", C["accent_press"]),
                                             ("active", C["accent_hi"])],
               foreground=[("disabled", C["on_accent_dim"])],
               bordercolor=[("active", C["accent_hi"]), ("disabled", C["accent_dim"])],
               lightcolor=[("active", C["accent_hi"]), ("disabled", C["accent_dim"])],
               darkcolor=[("active", C["accent_hi"]), ("disabled", C["accent_dim"])])
        donate = dict(btn, bordercolor="#ffdd00", lightcolor="#ffdd00", darkcolor="#ffdd00")
        st.configure("Donate.TButton", background="#ffdd00", foreground="#111111", **donate)
        st.map("Donate.TButton", background=[("pressed", "#e6c700"), ("active", "#ffe94d")],
               bordercolor=[("active", "#ffe94d")], lightcolor=[("active", "#ffe94d")],
               darkcolor=[("active", "#ffe94d")])
        st.configure("Big.Accent.TButton", padding=[22, 10], font=F(11))
        st.configure("Pill.TButton", padding=[12, 5], font=F(9), background="#232833",
                     bordercolor="#232833", lightcolor="#232833", darkcolor="#232833")
        st.map("Pill.TButton", background=[("active", "#323846"), ("disabled", C["surface"])],
               foreground=[("disabled", C["faint"])])
        st.configure("Ghost.TButton", background=C["bg"], foreground=C["muted"], padding=[10, 4], font=F(9),
                     bordercolor=C["bg"], lightcolor=C["bg"], darkcolor=C["bg"])
        st.map("Ghost.TButton", background=[("active", C["card"])], foreground=[("active", C["fg"])])

        field_ = dict(fieldbackground=C["surface"], foreground=C["fg"], insertcolor=C["fg"], padding=[10, 7],
                      bordercolor=C["border"], lightcolor=C["surface"], darkcolor=C["surface"],
                      selectbackground=C["accent"], selectforeground=C["on_accent"])
        st.configure("TEntry", **field_)
        st.map("TEntry", bordercolor=[("focus", C["accent"])], lightcolor=[("focus", C["surface"])])
        st.configure("TCombobox", **field_, background=C["surface"], arrowcolor=C["muted"], arrowsize=14)
        st.map("TCombobox", fieldbackground=[("disabled", C["card"]), ("readonly", C["surface"])],
               foreground=[("disabled", C["faint"]), ("readonly", C["fg"])],
               bordercolor=[("focus", C["accent"])], background=[("active", C["surface"])],
               selectbackground=[("readonly", C["surface"])], selectforeground=[("readonly", C["fg"])])
        self.option_add("*TCombobox*Listbox.background", C["card"])
        self.option_add("*TCombobox*Listbox.foreground", C["fg"])
        self.option_add("*TCombobox*Listbox.selectBackground", C["accent"])
        self.option_add("*TCombobox*Listbox.selectForeground", C["on_accent"])
        self.option_add("*TCombobox*Listbox.borderWidth", 0)

        size, gap = int(18 * self.scale), int(8 * self.scale)
        self._chk_imgs = {
            "off": _checkbox_image(self, size, C["surface"], C["border"], None, gap),
            "hover": _checkbox_image(self, size, C["card_hi"], C["muted"], None, gap),
            "on": _checkbox_image(self, size, C["accent"], C["accent"], C["on_accent"], gap),
            "dis": _checkbox_image(self, size, C["card"], C["border"], None, gap),
        }
        st.element_create("Modern.indicator", "image", self._chk_imgs["off"],
                          ("disabled", self._chk_imgs["dis"]), ("selected", self._chk_imgs["on"]),
                          ("active", self._chk_imgs["hover"]), sticky="w")
        for name, bg in (("Card.TCheckbutton", C["card"]), ("TCheckbutton", C["bg"])):
            st.layout(name, [("Checkbutton.padding", {"sticky": "nswe", "children": [
                ("Modern.indicator", {"side": "left", "sticky": ""}),
                ("Checkbutton.label", {"side": "left", "sticky": "w", "expand": 1})]})])
            st.configure(name, background=bg, foreground=C["fg"], font=F(10), padding=[2, 2, 4, 2])
            st.map(name, background=[("active", bg)], foreground=[("disabled", C["faint"])])
        st.configure("Card.TCheckbutton", focuscolor=C["card"])

        st.configure("TNotebook", background=C["bg"], borderwidth=0, tabmargins=[0, 0, 0, 0], **flat)
        st.configure("TNotebook.Tab", background=C["bg"], foreground=C["muted"], padding=[22, 8], font=F(11),
                     borderwidth=0, **flat)
        st.map("TNotebook.Tab",
               background=[("selected", C["card"]), ("active", C["surface"])],
               foreground=[("disabled", C["faint"]), ("selected", C["accent_hi"]), ("active", C["fg"])],
               bordercolor=[("selected", C["card"])], lightcolor=[("selected", C["card"])],
               expand=[("selected", [0, 0, 0, 0])])

        st.configure("Treeview", background=C["surface"], fieldbackground=C["surface"], foreground=C["fg"],
                     rowheight=row_h, borderwidth=0, font=F(10), **flat)
        st.map("Treeview", background=[("selected", C["select"])], foreground=[("selected", C["accent_hi"])])
        st.configure("Treeview.Heading", background=C["card"], foreground=C["muted"], font=F(9),
                     borderwidth=0, padding=[8, 7], relief="flat",
                     bordercolor=C["card"], lightcolor=C["card"], darkcolor=C["card"])
        st.map("Treeview.Heading", background=[("active", C["card_hi"])], foreground=[("active", C["fg"])])
        st.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])  # no outer border

        st.configure("Vertical.TScrollbar", background=C["card_hi"], troughcolor=C["surface"], borderwidth=0,
                     arrowcolor=C["muted"], gripcount=0, arrowsize=int(12 * self.scale),
                     bordercolor=C["surface"], lightcolor=C["card_hi"], darkcolor=C["card_hi"])
        st.map("Vertical.TScrollbar", background=[("active", C["border"])])
        st.configure("TPanedwindow", background=C["bg"])
        st.configure("Sash", sashthickness=int(8 * self.scale), gripcount=0, background=C["bg"],
                     bordercolor=C["bg"], lightcolor=C["bg"], darkcolor=C["bg"])
        st.configure("Horizontal.TProgressbar", background=C["accent"], troughcolor=C["card"], borderwidth=0,
                     thickness=int(6 * self.scale), bordercolor=C["card"], lightcolor=C["accent"],
                     darkcolor=C["accent"])

    # ---------- layout ----------
    def _build_ui(self) -> None:
        pad = 22
        # header
        header = ttk.Frame(self, padding=[pad, 18, pad, 12])
        header.pack(fill="x")
        title_box = ttk.Frame(header)
        title_box.pack(side="left")
        brand = ttk.Frame(title_box)
        brand.pack(anchor="w")
        self._header_icon = tk.PhotoImage(data=ICON_64_PNG)
        if self.scale < 1.25:
            self._header_icon = self._header_icon.subsample(2)  # 32 px at 100 % scaling
        ttk.Label(brand, image=self._header_icon).pack(side="left", padx=[0, 12])
        names = ttk.Frame(brand)
        names.pack(side="left")
        ttk.Label(names, text=APP_NAME, style="Title.TLabel").pack(anchor="w")
        ttk.Label(names, text=APP_TAGLINE, style="Sub.TLabel").pack(anchor="w")
        ttk.Button(header, text="Donate", command=lambda: webbrowser.open(DONATE_URL),
                   style="Donate.TButton", width=10, cursor="hand2").pack(side="right")
        ttk.Button(header, text="⚙  Sources", command=self._open_sources_dialog,
                   style="TButton", width=10).pack(side="right", padx=[0, 10])

        # folder card
        card = ttk.Frame(self, style="Card.TFrame", padding=[16, 14])
        card.pack(fill="x", padx=pad)
        ttk.Label(card, text="FOLDER", style="Section.TLabel").grid(row=0, column=0, sticky="w", columnspan=4)
        self.ent_path = ttk.Combobox(card, font=F(10))
        self.ent_path.grid(row=1, column=0, sticky="ew", pady=[4, 0])
        self.ent_path.bind("<<ComboboxSelected>>", lambda e: self._remember_path(self.ent_path.get()))
        self.ent_path.bind("<Return>", lambda e: self._action_scan())
        self.btn_browse = ttk.Button(card, text="Browse…", command=self._action_browse)
        self.btn_browse.grid(row=1, column=1, padx=[10, 0], pady=[4, 0])
        self.btn_scan = ttk.Button(card, text="Scan", style="Accent.TButton", command=self._action_scan, width=12)
        self.btn_scan.grid(row=1, column=2, padx=[8, 0], pady=[4, 0])
        self.var_recursive = tk.BooleanVar(value=True)
        self.cb_recursive = ttk.Checkbutton(card, text="Include subfolders", variable=self.var_recursive,
                                            style="Card.TCheckbutton")
        self.cb_recursive.grid(row=1, column=3, padx=[14, 0], pady=[4, 0])
        card.columnconfigure(0, weight=1)

        # list + controls on top, log below, with a draggable divider between them
        self.paned = ttk.Panedwindow(self, orient="vertical")
        self.paned.pack(fill="both", expand=True)
        top = ttk.Frame(self.paned)
        self.log_frame = ttk.Frame(self.paned, padding=[pad, 4, pad, pad])
        self.paned.add(top, weight=4)
        self.paned.add(self.log_frame, weight=1)
        foot = ttk.Frame(top, padding=[pad, 12, pad, 4])
        foot.pack(side="bottom", fill="x")
        bar = ttk.Frame(top, style="Card.TFrame", padding=[16, 12])
        bar.pack(side="bottom", fill="x", padx=pad, pady=[12, 0])

        # tabs
        self.notebook = ttk.Notebook(top)
        self.notebook.pack(fill="both", expand=True, padx=pad, pady=[16, 0])
        for key, label in (("movies", "🎬  Movies"), ("series", "📺  Series")):
            frame = ttk.Frame(self.notebook, style="Card.TFrame", padding=[12, 12, 12, 12])
            self.notebook.add(frame, text=label)
            self._build_tab(frame, self.tabs[key])
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        # action bar
        ttk.Label(bar, text="LANGUAGES", style="Section.TLabel").grid(row=0, column=0, sticky="w", columnspan=3)
        ttk.Label(bar, text="SOURCE", style="Section.TLabel").grid(row=0, column=4, sticky="w")
        self.lang_vars = {name: tk.BooleanVar(value=True) for name in LANGUAGES}
        col = 0
        for name, var in self.lang_vars.items():
            ttk.Checkbutton(bar, text=name.capitalize(), variable=var, style="Card.TCheckbutton",
                            command=self._on_langs_changed).grid(row=1, column=col, padx=[0, 18], pady=[4, 0])
            col += 1
        self.var_overwrite = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Re-download existing", variable=self.var_overwrite,
                        style="Card.TCheckbutton").grid(row=1, column=col, padx=[6, 24], pady=[4, 0])
        self.var_source = tk.StringVar()
        self.cmb_source = ttk.Combobox(bar, textvariable=self.var_source, state="readonly", width=30, font=F(10))
        self.cmb_source.grid(row=1, column=4, sticky="w", pady=[4, 0])
        self.cmb_source.bind("<<ComboboxSelected>>", self._on_source_selected)
        bar.columnconfigure(5, weight=1)
        self.btn_download = ttk.Button(bar, text="⬇  Download Subtitles", style="Big.Accent.TButton",
                                       command=self._action_download)
        self.btn_download.grid(row=0, column=6, rowspan=2, sticky="e")

        # footer: progress + log toggle
        self.progress_bar = ttk.Progressbar(foot, mode="determinate", style="Horizontal.TProgressbar")
        self.progress_bar.pack(fill="x", side="left", expand=True, pady=[2, 0])
        sp = int(18 * self.scale)
        self.spinner = tk.Canvas(foot, width=sp, height=sp, bg=C["bg"], highlightthickness=0, bd=0)
        self.spinner.pack(side="left", padx=[12, 0])
        pad_ = max(2, int(2 * self.scale))
        self._spin_track = self.spinner.create_oval(pad_, pad_, sp - pad_, sp - pad_, outline=C["bg"],
                                                    width=max(2, int(2.2 * self.scale)))
        self._spin_arc = self.spinner.create_arc(pad_, pad_, sp - pad_, sp - pad_, start=90, extent=100,
                                                 style="arc", outline=C["bg"], width=max(2, int(2.2 * self.scale)))
        self._spin_job = None
        self._spin_angle = 90
        self.lbl_pct = ttk.Label(foot, text="Ready", style="Sub.TLabel", width=10, anchor="w")
        self.lbl_pct.pack(side="left", padx=[6, 0])
        self.btn_copy_log = ttk.Button(foot, text="Copy log", style="Ghost.TButton", command=self._copy_log, width=9)
        self.btn_copy_log.pack(side="left", padx=[8, 0])
        self.btn_log = ttk.Button(foot, text="Log ▴", style="Ghost.TButton", command=self._toggle_log, width=6)
        self.btn_log.pack(side="left", padx=[2, 0])

        self.log_text = tk.Text(self.log_frame, bg=C["surface"], fg=C["muted"], font=F(9), bd=0, padx=10, pady=8,
                                highlightthickness=0, state="disabled", wrap="none", relief="flat", height=6)
        self.log_text.pack(fill="both", expand=True, side="left")
        sb = ttk.Scrollbar(self.log_frame, orient="vertical", command=self.log_text.yview)
        sb.pack(fill="y", side="right")
        self.log_text.configure(yscrollcommand=sb.set)
        for tag, color in (("ok", C["ok"]), ("err", C["bad"]), ("warn", C["warn"]), ("dim", C["faint"])):
            self.log_text.tag_configure(tag, foreground=color)

        menu_opts = dict(tearoff=0, bg=C["card"], fg=C["fg"], activebackground=C["card_hi"],
                         activeforeground=C["accent"], bd=0, font=F(10), relief="flat")
        self.context_menu = tk.Menu(self, **menu_opts)
        self.context_menu.add_command(label="Assign IMDb ID…", command=self.ctx_assign_imdb)
        self.context_menu.add_command(label="Clear IMDb ID", command=self.ctx_clear_mapping)
        self.context_menu.add_command(label="Look up IMDb ID again", command=self.ctx_lookup_imdb)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Open containing folder", command=self.ctx_open_folder)

        self.bind_all("<Control-f>", lambda e: (self.cur_search.focus_set(), "break")[1])

    def _build_tab(self, parent: ttk.Frame, t: TabState) -> None:
        bar = ttk.Frame(parent, style="Card.TFrame")
        bar.pack(fill="x", pady=[0, 10])
        ttk.Label(bar, text="🔍", style="Card.TLabel").pack(side="left", padx=[2, 6])
        var = tk.StringVar()
        ent = ttk.Entry(bar, textvariable=var, width=34, font=F(10))
        ent.pack(side="left")
        var.trace_add("write", lambda *_: self._apply_filter(t))
        ent.bind("<Escape>", lambda e: var.set(""))
        t.search_var, t.search_entry = var, ent  # type: ignore[attr-defined]
        for text, cmd in (("Select all", lambda: self._select(t, "all")),
                          ("Select none", lambda: self._select(t, "none")),
                          ("Select missing", lambda: self._select(t, "missing"))):
            b = ttk.Button(bar, text=text, style="Pill.TButton", command=cmd)
            b.pack(side="left", padx=[8, 0])
        lbl = ttk.Label(bar, text="", style="CardMuted.TLabel")
        lbl.pack(side="right", padx=[0, 4])
        t.counts_label = lbl  # type: ignore[attr-defined]

        box = ttk.Frame(parent, style="Card.TFrame")
        box.pack(fill="both", expand=True)
        is_series = t.key == "series"
        tree = ttk.Treeview(box, columns=COLUMNS, show="tree headings" if is_series else "headings",
                            selectmode="browse")
        s = self.scale
        if is_series:
            tree.column("#0", width=int(36 * s), minwidth=int(36 * s), stretch=False)
        tree.heading("selected", text=CHECK_ON, anchor="center", command=lambda: self._toggle_all(t))
        for col, title in HEADINGS.items():
            tree.heading(col, text=title, anchor="w" if col in ("filename", "parsed_title", "status") else "center",
                         command=lambda c=col: self._sort(t, c))
        widths = {"selected": (44, False), "filename": (330, True), "parsed_title": (210, True),
                  "year": (64, False), "imdb_id": (100, False), "subs": (120, False), "status": (230, True)}
        for col, (w, stretch) in widths.items():
            tree.column(col, width=int(w * s), minwidth=int(min(w, 60) * s), stretch=stretch,
                        anchor="w" if col in ("filename", "parsed_title", "status") else "center")
        tree.pack(fill="both", expand=True, side="left")
        sb = ttk.Scrollbar(box, orient="vertical", command=tree.yview)
        sb.pack(fill="y", side="right")
        tree.configure(yscrollcommand=sb.set)

        tree.tag_configure("done", foreground=C["ok"])
        tree.tag_configure("partial", foreground=C["warn"])
        tree.tag_configure("fail", foreground=C["bad"])
        tree.tag_configure("show", foreground=C["accent_hi"], font=F(10))
        tree.tag_configure("season", foreground=C["muted"])
        tree.tag_configure("stripe", background=C["stripe"])

        tree.bind("<Button-3>", lambda e: self._on_right_click(e, t))
        tree.bind("<Double-1>", lambda e: self._on_double_click(e, t))
        tree.bind("<Button-1>", lambda e: self._on_click(e, t))
        tree.bind("<space>", lambda e: self._toggle_focused(t))
        if is_series:
            tree.bind("<<TreeviewOpen>>", lambda e: self._set_show_icon(t, True))
            tree.bind("<<TreeviewClose>>", lambda e: self._set_show_icon(t, False))
        t.tree = tree

    @property
    def cur_search(self) -> ttk.Entry:
        return self.cur.search_entry  # type: ignore[attr-defined]

    def _log_visible(self) -> bool:
        return str(self.log_frame) in self.paned.panes()

    def _toggle_log(self, animate: bool = True) -> None:
        if self._log_anim is not None:  # a slide is already running: ignore extra clicks
            return
        if not animate or not self.winfo_ismapped():
            if self._log_visible():
                self.paned.forget(self.log_frame)
                self.btn_log.configure(text="Log ▾")
            else:
                self.paned.add(self.log_frame, weight=1)
                self.btn_log.configure(text="Log ▴")
                self.after(30, lambda: self._set_log_height(self._log_px))
            return
        self.update_idletasks()
        total = self.paned.winfo_height()
        if self._log_visible():  # slide down, then remove the pane
            self._log_px = total - self.paned.sashpos(0)
            self.btn_log.configure(text="Log ▾")
            self._slide_sash(self.paned.sashpos(0), total, on_done=lambda: self.paned.forget(self.log_frame))
        else:  # add the pane closed, then slide up
            self.paned.add(self.log_frame, weight=1)
            self.btn_log.configure(text="Log ▴")
            self.update_idletasks()
            total = self.paned.winfo_height()
            self.paned.sashpos(0, total)
            self._slide_sash(total, total - self._clamp_log(self._log_px, total))

    def _slide_sash(self, start: int, end: int, on_done=None, duration_ms: int = 220) -> None:
        """Ease-out slide of the list/log divider (~60 fps)."""
        t0 = time.perf_counter()

        def step():
            t = min(1.0, (time.perf_counter() - t0) * 1000 / duration_ms)
            eased = 1 - (1 - t) ** 3
            try:
                self.paned.sashpos(0, round(start + (end - start) * eased))
            except tk.TclError:
                t = 1.0
            if t < 1.0:
                self._log_anim = self.after(15, step)
            else:
                self._log_anim = None
                if on_done:
                    on_done()

        self._log_anim = self.after(0, step)

    def _copy_log(self) -> None:
        text = self.log_text.get("1.0", "end-1c").strip()
        self.clipboard_clear()
        self.clipboard_append(text)
        self.btn_copy_log.configure(text="Copied ✓")
        self.after(1500, lambda: self.btn_copy_log.configure(text="Copy log"))

    def _on_paned_configure(self, _e=None) -> None:
        # place the divider once the window has its real size (not before it is mapped)
        if self._sash_pending and self.paned.winfo_height() > 300:
            self._sash_pending = False
            self.after_idle(lambda: self._set_log_height(self._log_px))

    def _set_log_height(self, px: int) -> None:
        if not self._log_visible():
            return
        self.update_idletasks()
        total = self.paned.winfo_height()
        if total > 200:
            self.paned.sashpos(0, total - self._clamp_log(px, total))

    def _clamp_log(self, px: int, total: int) -> int:
        return max(int(90 * self.scale), min(px, int(total * 0.45)))

    # ---------- logging & progress (thread-safe) ----------
    def _error_sound(self) -> None:
        now = time.monotonic()
        if now - getattr(self, "_last_error_sound", 0.0) > 3.0:  # don't machine-gun the sound
            self._last_error_sound = now
            play_sound(SOUND_ERROR)

    def _show_error(self, title: str, message: str, parent=None) -> None:
        self._error_sound()
        messagebox.showerror(title, message, parent=parent or self)

    def log(self, message: str) -> None:
        self.ui(self._append_log, f"{time.strftime('%H:%M:%S')}  {message}")

    def _append_log(self, line: str) -> None:
        tag = ("ok" if "[OK]" in line else "err" if "[ERROR]" in line or "[UI ERROR]" in line
               else "warn" if "[WARN]" in line or "[SOURCE OFF]" in line else "dim" if "[MISS]" in line else "")
        if tag == "err":
            self._error_sound()
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n", tag)
        excess = int(self.log_text.index("end-1c").split(".")[0]) - 3000
        if excess > 0:
            self.log_text.delete("1.0", f"{excess + 1}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def set_status(self, tab: str, iid: str, text: str) -> None:
        self.ui(self._set_cell, tab, iid, "status", text)

    def _set_cell(self, tab: str, iid: str, column: str, value: str) -> None:
        tree = self.tabs[tab].tree
        if tree.exists(iid):
            tree.set(iid, column, value)

    def progress_init(self, total: int, label: str = "0%") -> None:
        def run():
            self.progress_bar.configure(value=0, maximum=max(total, 1))
            self.lbl_pct.configure(text=label)
        self.ui(run)

    def progress_step(self, n: int = 1) -> None:
        def run():
            mx = float(self.progress_bar["maximum"])
            cur = min(float(self.progress_bar["value"]) + n, mx)
            self.progress_bar["value"] = cur
            self.lbl_pct.configure(text=f"{int(cur / mx * 100)}%")
        self.ui(run)

    def progress_finish(self, canceled: bool = False) -> None:
        def run():
            if not canceled:
                self.progress_bar["value"] = self.progress_bar["maximum"]
            self.lbl_pct.configure(text="Canceled" if canceled else "Done")
        self.ui(run)

    # ---------- busy state ----------
    def _spin(self) -> None:
        """Rotating arc: shows the app is alive even when a source makes us wait."""
        self._spin_angle = (self._spin_angle - 12) % 360
        extent = 70 + 60 * abs(((self._spin_angle / 360.0) * 2) % 2 - 1)  # gently breathing arc
        self.spinner.itemconfigure(self._spin_arc, start=self._spin_angle, extent=extent)
        self._spin_job = self.after(30, self._spin)

    def _spinner_on(self, on: bool) -> None:
        if on and self._spin_job is None:
            self.spinner.itemconfigure(self._spin_track, outline=C["card_hi"])
            self.spinner.itemconfigure(self._spin_arc, outline=C["accent_hi"])
            self._spin()
        elif not on and self._spin_job is not None:
            self.after_cancel(self._spin_job)
            self._spin_job = None
            self.spinner.itemconfigure(self._spin_track, outline=C["bg"])
            self.spinner.itemconfigure(self._spin_arc, outline=C["bg"])

    def _set_busy(self, mode: str | None) -> None:
        self.busy = mode
        idle = mode is None
        self._spinner_on(not idle)
        for w in (self.btn_browse, self.ent_path, self.cb_recursive, self.cmb_source):
            w.state(["!disabled"] if idle else ["disabled"])
        if idle:
            self.cmb_source.state(["readonly"])
        self.btn_scan.configure(text="Cancel" if mode == "scan" else "Scan")
        self.btn_scan.state(["disabled"] if mode == "download" else ["!disabled"])
        self.btn_download.configure(text="■  Cancel" if mode == "download" else "⬇  Download Subtitles")
        self.btn_download.state(["disabled"] if mode == "scan" else ["!disabled"])
        other = 1 if self.active == "movies" else 0
        self.notebook.tab(other, state="normal" if idle else "disabled")
        if idle:
            self.cb_recursive.state(["!disabled"] if self.active == "movies" else ["disabled"])
            self._update_counts(self.cur)

    # ---------- settings ----------
    def _load_settings(self) -> None:
        data = read_json(APP_STATE_FILE, {})
        for key, t in self.tabs.items():
            t.path = str(data.get(f"last_dir_{key}", "") or "")
            t.recent = [p for p in data.get(f"recent_{key}", []) if isinstance(p, str)][:10]
            t.source = str(data.get(f"source_{key}", "auto") or "auto")
        langs = data.get("languages")
        if isinstance(langs, dict):
            for name, var in self.lang_vars.items():
                var.set(bool(langs.get(name, True)))
        self.var_overwrite.set(bool(data.get("overwrite", False)))
        self.var_recursive.set(bool(data.get("recursive", True)))
        if not data.get("show_log", True):
            self._toggle_log(animate=False)
        win = data.get("window")
        self._saved_window = win if isinstance(win, dict) else {}
        try:
            saved = int(data.get("log_height", 0)) if data.get("layout") == 3 else 0  # ignore pre-2.3 sizes
            self._log_px = saved or int(190 * self.scale)
        except (TypeError, ValueError):
            self._log_px = int(190 * self.scale)

        saved = data.get("providers")
        if isinstance(saved, list):
            order = [{"key": p["key"], "enabled": bool(p.get("enabled", True))}
                     for p in saved if isinstance(p, dict) and p.get("key") in PROVIDERS]
            known = {p["key"] for p in order}
            for idx, p in enumerate(DEFAULT_PROVIDER_ORDER):  # new sources go to their default slot
                if p["key"] not in known:
                    order.insert(min(idx, len(order)), dict(p))
            self.settings["providers"] = order
        for k in ("subdl_api_key", "subsource_api_key", "os_api_key", "os_username"):
            self.settings[k] = str(data.get(k, "") or "")
        self.settings["os_password"] = self._secret_get("os_password") or str(data.get("os_password", "") or "")
        self._show_path_for(self.cur)

    def _save_settings(self) -> None:
        self.cur.path = self.ent_path.get().strip()
        data = {"languages": {n: v.get() for n, v in self.lang_vars.items()},
                "overwrite": self.var_overwrite.get(), "recursive": self.var_recursive.get(),
                "show_log": self._log_visible(),
                "log_height": self._current_log_px(), "layout": 3,
                "window": self._window_state(),
                "providers": self.settings["providers"]}
        for k in ("subdl_api_key", "subsource_api_key", "os_api_key", "os_username"):
            data[k] = self.settings[k]
        if self.settings["os_password"] and not self._secret_set("os_password", self.settings["os_password"]):
            data["os_password"] = self.settings["os_password"]  # no keyring available
        for key, t in self.tabs.items():
            data[f"last_dir_{key}"] = t.path
            data[f"recent_{key}"] = t.recent
            data[f"source_{key}"] = t.source
        try:
            write_json(APP_STATE_FILE, data)
        except OSError as e:
            self._append_log(f"[WARN] Could not save settings: {e}")

    def _current_log_px(self) -> int:
        try:
            if self._log_visible() and self.paned.winfo_height() > 200:
                self._log_px = self.paned.winfo_height() - self.paned.sashpos(0)
        except tk.TclError:
            pass
        return int(self._log_px)

    def _window_state(self) -> dict:
        try:
            zoomed = self.state() == "zoomed"
            if not zoomed and self.state() == "normal":
                self._saved_window = {"geometry": self.geometry(), "zoomed": False}
            else:
                self._saved_window = {**self._saved_window, "zoomed": zoomed}
        except tk.TclError:
            pass
        return self._saved_window

    @staticmethod
    def _secret_get(name: str) -> str:
        if keyring is None:
            return ""
        try:
            return (keyring.get_password(KEYRING_SERVICE, name)
                    or keyring.get_password(OLD_KEYRING_SERVICE, name) or "")
        except Exception:
            return ""

    @staticmethod
    def _secret_set(name: str, value: str) -> bool:
        if keyring is None:
            return False
        try:
            keyring.set_password(KEYRING_SERVICE, name, value)
            return True
        except Exception:
            return False

    def _show_path_for(self, t: TabState) -> None:
        self.ent_path["values"] = t.recent
        self.ent_path.set(t.path)

    def _remember_path(self, path: str) -> None:
        path = path.strip()
        t = self.cur
        t.path = path
        if path:
            norm = os.path.normcase(os.path.normpath(path))
            t.recent = ([path] + [p for p in t.recent if os.path.normcase(os.path.normpath(p)) != norm])[:10]
        self.ent_path["values"] = t.recent
        self._save_settings()

    def _on_close(self) -> None:
        self.cancel.set()
        self._save_settings()
        self.destroy()

    # ---------- sources ----------
    def _source_choices(self, kind: str) -> list[tuple[str, str]]:
        out = [("auto", "Auto — try sources in order")]
        for p in self.settings["providers"]:
            cls = PROVIDERS[p["key"]]
            if cls.movies if kind == "movie" else cls.series:
                out.append((cls.key, cls.name + ("" if p["enabled"] else "  (off in Auto)")))
        return out

    def _refresh_source_choices(self) -> None:
        kind = "movie" if self.active == "movies" else "episode"
        choices = self._source_choices(kind)
        self._source_map = {label: key for key, label in choices}
        self.cmb_source["values"] = [label for _, label in choices]
        current = self.cur.source if self.cur.source in {k for k, _ in choices} else "auto"
        self.var_source.set(next(label for key, label in choices if key == current))
        self.cb_recursive.state(["!disabled"] if self.active == "movies" and not self.busy else ["disabled"])

    def _on_source_selected(self, _e=None) -> None:
        self.cur.source = self._source_map.get(self.var_source.get(), "auto")
        self._save_settings()

    def _build_chain(self, kind: str, source: str) -> list[Provider]:
        settings = dict(self.settings)
        if source != "auto":
            keys = [source]
        else:
            keys = [p["key"] for p in self.settings["providers"] if p["enabled"]]
        chain = []
        for key in keys:
            p = PROVIDERS[key](settings, self.log)
            if not p.supports(kind):
                continue
            reason = p.missing_config()
            if reason:
                self.log(f"[WARN] {p.name} skipped: {reason}.")
                continue
            chain.append(p)
        return chain

    def _open_sources_dialog(self) -> None:
        if self.busy:
            return
        win = tk.Toplevel(self)
        win.title("Subtitle Sources")
        win.configure(bg=C["bg"])
        win.transient(self)
        self._dark_title_bar(win)
        order = [dict(p) for p in self.settings["providers"]]

        btns = ttk.Frame(win, padding=[20, 4, 20, 18])  # footer first: it can never be pushed off-screen
        btns.pack(side="bottom", fill="x")
        body = ttk.Frame(win, padding=[20, 20, 20, 4])
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Subtitle Sources", style="Title.TLabel").pack(anchor="w")
        ttk.Label(body, text="In Auto mode the sources are tried top to bottom until one has the subtitle. "
                             "Click ☑ to switch a source on/off.", style="Sub.TLabel",
                  wraplength=int(620 * self.scale)).pack(anchor="w", pady=[2, 12])

        lst_card = ttk.Frame(body, style="Card.TFrame", padding=8)
        lst_card.pack(fill="x")
        tv = ttk.Treeview(lst_card, columns=("on", "name", "note"), show="headings", height=len(order),
                          selectmode="browse")
        tv.heading("on", text="On")
        tv.heading("name", text="Source", anchor="w")
        tv.heading("note", text="Covers", anchor="w")
        tv.column("on", width=int(50 * self.scale), stretch=False, anchor="center")
        tv.column("name", width=int(180 * self.scale), anchor="w")
        tv.column("note", width=int(390 * self.scale), anchor="w")
        tv.pack(side="left", fill="x", expand=True)

        def refresh(select: str | None = None):
            tv.delete(*tv.get_children())
            for p in order:
                cls = PROVIDERS[p["key"]]
                tv.insert("", "end", iid=p["key"], values=(CHECK_ON if p["enabled"] else CHECK_OFF, cls.name, cls.note))
            if select:
                tv.selection_set(select)

        def on_click(e):
            if tv.identify_column(e.x) == "#1" and tv.identify_region(e.x, e.y) == "cell":
                key = tv.identify_row(e.y)
                for p in order:
                    if p["key"] == key:
                        p["enabled"] = not p["enabled"]
                refresh(key)
                return "break"
            return None

        def move(delta: int):
            sel = tv.selection()
            if not sel:
                return
            i = next(n for n, p in enumerate(order) if p["key"] == sel[0])
            j = i + delta
            if 0 <= j < len(order):
                order[i], order[j] = order[j], order[i]
                refresh(sel[0])

        tv.bind("<Button-1>", on_click)
        side = ttk.Frame(lst_card, style="Card.TFrame")
        side.pack(side="right", fill="y", padx=[8, 0])
        ttk.Button(side, text="▲  Up", style="Pill.TButton", command=lambda: move(-1)).pack(fill="x", pady=[0, 6])
        ttk.Button(side, text="▼  Down", style="Pill.TButton", command=lambda: move(1)).pack(fill="x")
        refresh()

        keys = ttk.Frame(body, style="Card.TFrame", padding=[16, 12])
        keys.pack(fill="x", pady=[14, 0])
        ttk.Label(keys, text="API KEYS & LOGIN", style="Section.TLabel").grid(row=0, column=0, sticky="w", columnspan=3)
        fields = {k: tk.StringVar(value=self.settings.get(k, "")) for k in
                  ("subdl_api_key", "subsource_api_key", "os_api_key", "os_username", "os_password")}
        # each entry: (function, field values to pass) — values are read on the Tk thread, tested on a worker
        tests = {
            "subdl": (lambda key: test_subdl_key(key), ("subdl_api_key",)),
            "subsource": (lambda key: test_subsource_key(key), ("subsource_api_key",)),
            "os": (lambda key, user, pwd: test_opensubtitles(key, user, pwd), ("os_api_key", "os_username", "os_password")),
        }
        # (field, label, "get key" url, test id, test button text, show)
        rows = (("subdl_api_key", "SubDL API key", SubdlApiProvider.key_url, "subdl", "Test", ""),
                ("subsource_api_key", "SubSource API key", SubSourceProvider.key_url, "subsource", "Test", ""),
                ("os_api_key", "OpenSubtitles API key", OpenSubtitlesProvider.key_url, None, "", ""),
                ("os_username", "OpenSubtitles username", "", None, "", ""),
                ("os_password", "OpenSubtitles password", "", "os", "Test login", "•"))
        status_labels = {}

        def run_test(test_id: str, button: ttk.Button):
            lbl = status_labels[test_id]
            lbl.configure(text="Testing…", style="TestBusy.TLabel")
            button.state(["disabled"])
            func, names = tests[test_id]
            values = [fields[n].get() if n == "os_password" else fields[n].get().strip() for n in names]

            def worker():
                try:
                    ok, text = func(*values)
                except requests.RequestException as e:
                    ok, text = False, short_error(e)
                except Exception as e:  # never leave the button stuck
                    ok, text = False, str(e)

                def done():
                    if lbl.winfo_exists():
                        lbl.configure(text=("✓  " if ok else "✗  ") + text,
                                      style="TestOk.TLabel" if ok else "TestBad.TLabel")
                        button.state(["!disabled"])
                self.ui(done)

            threading.Thread(target=worker, daemon=True).start()

        grid_row = 1
        for k, label, url, test_id, test_text, show in rows:
            ttk.Label(keys, text=label, style="Card.TLabel").grid(row=grid_row, column=0, sticky="w", pady=[6, 2])
            ttk.Entry(keys, textvariable=fields[k], show=show, font=F(10)).grid(
                row=grid_row, column=1, sticky="ew", padx=[12, 8], pady=[6, 2])
            if url:
                ttk.Button(keys, text="Get key ↗", style="Pill.TButton",
                           command=lambda u=url: webbrowser.open(u)).grid(row=grid_row, column=2, sticky="ew")
            if test_id:
                b = ttk.Button(keys, text=test_text, style="Pill.TButton")
                b.configure(command=lambda t=test_id, btn=b: run_test(t, btn))
                b.grid(row=grid_row, column=3, sticky="ew", padx=[6, 0])
                grid_row += 1
                status_labels[test_id] = ttk.Label(keys, text="", style="TestBusy.TLabel")
                status_labels[test_id].grid(row=grid_row, column=1, columnspan=3, sticky="w", padx=[12, 0])
            grid_row += 1
        keys.columnconfigure(1, weight=1)
        where = ("Windows Credential Manager" if keyring is not None
                 else "settings.json (pip install keyring to store it securely)")
        ttk.Label(keys, text=f"Login is optional (raises OpenSubtitles from 5 to 20 downloads/day). "
                             f"Password is stored in {where}.", style="CardMuted.TLabel",
                  wraplength=int(640 * self.scale)).grid(row=grid_row, column=0, columnspan=4,
                                                         sticky="w", pady=[10, 0])

        def save():
            self.settings["providers"] = order
            for k, var in fields.items():
                self.settings[k] = var.get().strip()
            self._save_settings()
            self._refresh_source_choices()
            self.log("Sources updated: " + " → ".join(PROVIDERS[p["key"]].short for p in order if p["enabled"]))
            win.destroy()

        ttk.Button(btns, text="Save", style="Accent.TButton", command=save, width=12).pack(side="right")
        ttk.Button(btns, text="Cancel", command=win.destroy, width=10).pack(side="right", padx=[0, 8])
        win.bind("<Escape>", lambda e: win.destroy())
        self._center_child(win, 700)
        win.grab_set()

    # ---------- per-folder IMDb mapping cache ----------
    def _load_mapping(self, folder: str) -> dict:
        return read_json(os.path.join(folder, MAP_FILENAME), {})

    def _update_mapping(self, folder: str, changes: dict) -> None:
        if not folder or not os.path.isdir(folder) or not changes:
            return
        with self._map_lock:
            mapping = self._load_mapping(folder)
            for k, v in changes.items():
                if v is None:
                    mapping.pop(k, None)
                else:
                    mapping[k] = v
            try:
                write_json(os.path.join(folder, MAP_FILENAME), mapping)
            except OSError as e:
                self.log(f"[WARN] Could not write mapping cache: {e}")

    # ---------- tabs, filter, sort ----------
    def _on_tab_changed(self, _event=None) -> None:
        new = "series" if self.notebook.index(self.notebook.select()) == 1 else "movies"
        if new == self.active:
            return
        self.cur.path = self.ent_path.get().strip()
        self.active = new
        self._show_path_for(self.cur)
        self._refresh_source_choices()
        self._update_counts(self.cur)
        self._save_settings()

    def _apply_filter(self, t: TabState) -> None:
        tree = t.tree
        q = t.search_var.get().strip().lower()  # type: ignore[attr-defined]
        if not q:
            visible = set(t.rows)
        else:
            matched = {iid for iid, r in t.rows.items()
                       if q in r["filename"].lower() or q in r.get("parsed_title", "").lower()}
            visible = set(matched)
            for iid in matched:
                p = t.rows[iid]["parent"]
                while p:
                    visible.add(p)
                    p = t.rows[p]["parent"]
            for iid in t.order:
                if t.rows[iid]["parent"] in matched:
                    matched.add(iid)
                    visible.add(iid)
        stripe = False
        for iid in t.order:
            if iid in visible:
                tree.reattach(iid, t.rows[iid]["parent"], "end")
                if t.key == "movies":
                    t.rows[iid]["stripe"] = stripe
                    stripe = not stripe
                    self._retag(t, iid)
            else:
                tree.detach(iid)
        t.visible = visible

    def _sort(self, t: TabState, col: str) -> None:
        t.sort_rev = (t.sort_col == col) and not t.sort_rev
        t.sort_col = col
        for c, title in HEADINGS.items():
            t.tree.heading(c, text=title + ((" ▼" if t.sort_rev else " ▲") if c == col else ""))
        tops, children = [], {}
        for iid in t.order:
            p = t.rows[iid]["parent"]
            (children.setdefault(p, []) if p else tops).append(iid)

        def key(iid):
            r = t.rows[iid]
            return natural_key(r["filename"] if col == "filename" else t.tree.set(iid, col))

        tops.sort(key=key, reverse=t.sort_rev)
        new_order: list = []

        def add(iid):
            new_order.append(iid)
            for c in children.get(iid, ()):
                add(c)

        for iid in tops:
            add(iid)
        t.order = new_order
        self._apply_filter(t)

    # ---------- subtitles state, tags & counts ----------
    def _selected_langs(self) -> list[tuple[str, str]]:
        return [(name, code) for name, code in LANGUAGES.items() if self.lang_vars[name].get()]

    @staticmethod
    def _subs_text(have: set) -> str:
        return "   ".join(f"{code.upper()} {'✓' if code in have else '✗'}" for code in LANGUAGES.values())

    def _retag(self, t: TabState, iid: str) -> None:
        r = t.rows[iid]
        tags = []
        if r["type"] in LEAF:
            need = {c for _, c in self._selected_langs()}
            have = r.get("have", set())
            if need and need <= have:
                tags.append("done")
            elif r.get("failed"):
                tags.append("fail")
            elif need & have:
                tags.append("partial")
        else:
            tags.append(r["type"])
        if r.get("stripe"):
            tags.append("stripe")
        t.tree.item(iid, tags=tags)

    def _refresh_parents(self, t: TabState) -> None:
        """Series: parent checkboxes follow their episodes; show/season rows summarise progress."""
        if t.key != "series":
            return
        need = {c for _, c in self._selected_langs()}
        stats: dict[str, list[int]] = {}
        for iid, r in t.rows.items():
            if r["type"] != "episode":
                continue
            done = bool(need) and need <= r.get("have", set())
            p = r["parent"]
            while p:
                s = stats.setdefault(p, [0, 0, 0])  # total, enabled, done
                s[0] += 1
                s[1] += r["enabled"]
                s[2] += done
                p = t.rows[p]["parent"]
        for iid, r in t.rows.items():
            if r["type"] in ("show", "season"):
                total, enabled, done = stats.get(iid, [0, 0, 0])
                r["enabled"] = enabled > 0
                t.tree.set(iid, "selected", CHECK_ON if enabled else CHECK_OFF)
                t.tree.set(iid, "subs", f"{done}/{total} done" if total else "")

    def _update_counts(self, t: TabState) -> None:
        need = {c for _, c in self._selected_langs()}
        leaves = [r for r in t.rows.values() if r["type"] in LEAF]
        missing = sum(1 for r in leaves if need - r.get("have", set()))
        selected = sum(1 for r in leaves if r["enabled"])
        noun = "episodes" if t.key == "series" else "movies"
        t.counts_label.configure(  # type: ignore[attr-defined]
            text=f"{len(leaves)} {noun}   ·   {missing} missing subtitles   ·   {selected} selected" if leaves else "")

    def _on_langs_changed(self) -> None:
        for t in self.tabs.values():
            for iid in t.rows:
                self._retag(t, iid)
            self._refresh_parents(t)
            self._update_counts(t)

    # ---------- checkboxes & selection ----------
    def _descendants(self, t: TabState, iid: str) -> list:
        out, parents = [], {iid}
        for c in t.order:
            if t.rows[c]["parent"] in parents:
                out.append(c)
                parents.add(c)
        return out

    def _set_checked(self, t: TabState, iid: str, state: bool, cascade: bool = True) -> None:
        for i in [iid] + (self._descendants(t, iid) if cascade else []):
            t.rows[i]["enabled"] = state
            t.tree.set(i, "selected", CHECK_ON if state else CHECK_OFF)

    def _after_check_change(self, t: TabState) -> None:
        self._refresh_parents(t)
        self._update_counts(t)

    def _select(self, t: TabState, mode: str) -> None:
        """Select all / none / only files missing a ticked language (acts on the filtered view)."""
        if self.busy:
            return
        need = {c for _, c in self._selected_langs()}
        for iid in t.visible:
            r = t.rows[iid]
            if r["type"] not in LEAF:
                continue
            state = mode == "all" or (mode == "missing" and bool(need - r.get("have", set())))
            self._set_checked(t, iid, state, cascade=False)
        t.all_checked = mode != "none"
        t.tree.heading("selected", text=CHECK_ON if t.all_checked else CHECK_OFF)
        self._after_check_change(t)

    def _toggle_all(self, t: TabState) -> None:
        self._select(t, "none" if t.all_checked else "all")

    def _on_click(self, event, t: TabState):
        if t.tree.identify_region(event.x, event.y) != "cell" or t.tree.identify_column(event.x) != "#1":
            return None
        iid = t.tree.identify_row(event.y)
        if iid in t.rows and not self.busy:
            self._set_checked(t, iid, not t.rows[iid]["enabled"])
            self._after_check_change(t)
        return "break"

    def _toggle_focused(self, t: TabState):
        iid = t.tree.focus()
        if iid in t.rows and not self.busy:
            self._set_checked(t, iid, not t.rows[iid]["enabled"])
            self._after_check_change(t)
        return "break"

    def _set_show_icon(self, t: TabState, is_open: bool) -> None:
        iid = t.tree.focus()
        r = t.rows.get(iid)
        if r and r["type"] == "show":
            t.tree.set(iid, "filename", f"{ICON_OPEN if is_open else ICON_CLOSED}  {r['filename']}")

    # ---------- folder selection & scanning ----------
    def _action_browse(self) -> None:
        init = self.ent_path.get().strip()
        d = filedialog.askdirectory(initialdir=init if os.path.isdir(init) else os.path.expanduser("~"),
                                    title="Select Folder", parent=self)
        if d:
            path = os.path.normpath(d)
            self.ent_path.set(path)
            self._remember_path(path)

    def _clear_tab(self, t: TabState) -> None:
        t.tree.delete(*t.tree.get_children())
        t.rows.clear()
        t.order.clear()
        t.visible.clear()
        t.all_checked = True
        t.sort_col, t.sort_rev = "", False
        t.tree.heading("selected", text=CHECK_ON)
        for c, title in HEADINGS.items():
            t.tree.heading(c, text=title)

    def _action_scan(self) -> None:
        if self.busy == "scan":
            self.log("Cancelling scan…")
            self.cancel.set()
            return
        if self.busy:
            return
        folder = self.ent_path.get().strip()
        if not folder or not os.path.isdir(folder):
            self._show_error("Invalid Folder", "Please choose an existing folder first.")
            return
        folder = os.path.normpath(folder)
        self._remember_path(folder)
        t = self.cur
        t.search_var.set("")  # type: ignore[attr-defined]
        self._clear_tab(t)
        t.root = folder
        self.cancel.clear()
        self._set_busy("scan")
        self.progress_init(1, "Scanning…")
        self.log(f"Scanning {t.key}: {folder}")
        threading.Thread(target=self._scan_worker, args=(t, folder, self.var_recursive.get()), daemon=True).start()

    def _scan_worker(self, t: TabState, folder: str, recursive: bool) -> None:
        try:
            entries = self._scan_movies(folder, recursive) if t.key == "movies" else self._scan_series(folder)
            if self.cancel.is_set():
                self.log("Scan canceled.")
                self.progress_finish(canceled=True)
                return
            mapping = self._load_mapping(folder)
            done = threading.Event()
            self.ui(self._populate, t, entries, mapping, done)
            done.wait()
            self._match_imdb(t)
            if not self.cancel.is_set():
                self.ui(play_sound, SOUND_DONE)
        except OSError as e:
            self.log(f"[ERROR] Could not read folder: {e}")
            self.progress_finish(canceled=True)
        except Exception as e:
            self.log(f"[ERROR] Scan failed: {e}")
            self.progress_finish(canceled=True)
        finally:
            self.ui(self._set_busy, None)

    def _scan_movies(self, root: str, recursive: bool) -> list:
        out = []
        stack = [(root, 0)]
        while stack:
            if self.cancel.is_set():
                break
            folder, depth = stack.pop()
            try:
                with os.scandir(folder) as it:
                    entries = list(it)
            except OSError as e:
                self.log(f"[WARN] Skipping {folder}: {e}")
                continue
            names_lower = {e.name.lower() for e in entries}
            for e in entries:
                if e.is_dir() and recursive and depth < MAX_SCAN_DEPTH and not e.name.startswith((".", "$")):
                    stack.append((e.path, depth + 1))
                elif e.is_file() and is_video(e.name):
                    rel = os.path.relpath(e.path, root).replace(os.sep, "/")
                    title, year = clean_filename_to_title(e.name)
                    if not year and folder != root:  # "Movie (2010)/movie.mkv" -> use the folder name
                        f_title, f_year = clean_filename_to_title(os.path.basename(folder))
                        if f_year:
                            title, year = f_title, f_year
                    out.append({"key": rel, "path": e.path, "title": title, "year": year,
                                "have": existing_subs(e.path, names_lower)})
        out.sort(key=lambda d: natural_key(d["key"]))
        return out

    def _scan_series(self, root: str) -> list:
        with os.scandir(root) as it:
            show_dirs = sorted((e for e in it if e.is_dir() and not e.name.startswith((".", "$"))),
                               key=lambda e: natural_key(e.name))
        self.progress_init(len(show_dirs), "Scanning…")
        shows = []
        for sd in show_dirs:
            if self.cancel.is_set():
                break
            title, year = clean_filename_to_title(sd.name)
            seasons = []
            try:
                with os.scandir(sd.path) as it:
                    items = sorted(it, key=lambda e: natural_key(e.name))
            except OSError as e:
                self.log(f"[WARN] Skipping '{sd.name}': {e}")
                self.progress_step()
                continue
            loose_lower = {e.name.lower() for e in items}
            loose = [{"name": e.name, "have": existing_subs(e.path, loose_lower)}
                     for e in items if e.is_file() and is_video(e.name)]
            for item in items:
                if not item.is_dir():
                    continue
                try:
                    with os.scandir(item.path) as it2:
                        files = list(it2)
                except OSError:
                    continue
                lower = {f.name.lower() for f in files}
                eps = sorted(({"name": f.name, "have": existing_subs(f.path, lower)}
                              for f in files if f.is_file() and is_video(f.name)), key=lambda d: natural_key(d["name"]))
                if eps:
                    seasons.append({"name": item.name, "path": item.path, "episodes": eps})
            if loose:
                seasons.insert(0, {"name": "", "path": sd.path, "episodes": loose})
            if seasons:
                shows.append({"key": sd.name, "path": sd.path, "title": title, "year": year, "seasons": seasons})
            self.progress_step()
        return shows

    @staticmethod
    def _cached(mapping: dict, key: str) -> tuple[str, str, bool]:
        v = mapping.get(key, "")
        if v == NOT_FOUND:
            return "", ST_NO_ID, True
        return (v, ST_READY, False) if v else ("", ST_IDLE, False)

    def _add_row(self, t: TabState, parent: str, values: tuple, row: dict) -> str:
        iid = t.tree.insert(parent, "end", values=values, open=False)
        row.setdefault("enabled", True)
        row.setdefault("have", set())
        row["parent"] = parent
        t.rows[iid] = row
        t.order.append(iid)
        return iid

    def _populate(self, t: TabState, entries: list, mapping: dict, done: threading.Event) -> None:
        try:
            if t.key == "movies":
                for e in entries:
                    tt, status, nf = self._cached(mapping, e["key"])
                    self._add_row(t, "", (CHECK_ON, e["key"], e["title"], e["year"], tt,
                                          self._subs_text(e["have"]), status),
                                  {"type": "movie", "filename": e["key"], "key": e["key"], "path": e["path"],
                                   "parsed_title": e["title"], "year": e["year"], "tt": tt, "not_found": nf,
                                   "have": e["have"]})
                self.log(f"Found {len(entries)} movie file(s).")
            else:
                n_eps = 0
                for s in entries:
                    tt, status, nf = self._cached(mapping, s["key"])
                    show_iid = self._add_row(
                        t, "", (CHECK_ON, f"{ICON_CLOSED}  {s['key']}", s["title"], s["year"], tt, "", status),
                        {"type": "show", "filename": s["key"], "key": s["key"], "path": s["path"],
                         "parsed_title": s["title"], "year": s["year"], "tt": tt, "not_found": nf})
                    for season in s["seasons"]:
                        parent = show_iid
                        if season["name"]:
                            parent = self._add_row(
                                t, show_iid, (CHECK_ON, f"   {season['name']}", "", "", "", "", ""),
                                {"type": "season", "filename": season["name"], "path": season["path"],
                                 "show": show_iid})
                        for ep in season["episodes"]:
                            self._add_row(t, parent, (CHECK_ON, f"      {ep['name']}", "", "", "",
                                                      self._subs_text(ep["have"]), ""),
                                          {"type": "episode", "filename": ep["name"],
                                           "path": os.path.join(season["path"], ep["name"]), "show": show_iid,
                                           "season_dir": season["name"], "have": ep["have"]})
                            n_eps += 1
                self.log(f"Found {len(entries)} show(s) with {n_eps} episode file(s).")
            t.visible = set(t.rows)
            self._apply_filter(t)
            for iid in t.rows:
                self._retag(t, iid)
            self._refresh_parents(t)
            self._update_counts(t)
        finally:
            done.set()

    # ---------- IMDb matching ----------
    def _match_imdb(self, t: TabState) -> None:
        kind = "series" if t.key == "series" else "movie"
        targets = [(iid, r["key"], r["parsed_title"], r.get("year", ""))
                   for iid, r in t.rows.items()
                   if r["type"] in ("movie", "show") and not r.get("tt") and not r.get("not_found")]
        if not targets:
            self.progress_finish()
            return
        self.log(f"Looking up IMDb IDs for {len(targets)} title(s)…")
        self.progress_init(len(targets), "IMDb…")

        def work(iid, title, year):
            if self.cancel.is_set():
                return iid, None
            self.set_status(t.key, iid, "Searching IMDb…")
            return iid, fetch_imdb_id(title, year, kind)

        changes, finished = {}, set()
        names = {iid: key for iid, key, _, _ in targets}
        with ThreadPoolExecutor(max_workers=min(IMDB_WORKERS, len(targets))) as ex:
            futures = [ex.submit(work, iid, title, year) for iid, _, title, year in targets]
            for fut in as_completed(futures):
                self.progress_step()
                try:
                    iid, tt = fut.result()
                except Exception as e:
                    self.log(f"[WARN] Lookup error: {e}")
                    continue
                if tt is None:
                    continue
                finished.add(iid)
                changes[names[iid]] = tt or NOT_FOUND
                self.ui(self._apply_imdb, t, iid, tt)
                if self.cancel.is_set():
                    ex.shutdown(wait=False, cancel_futures=True)
                    break
        for iid in names.keys() - finished:
            self.set_status(t.key, iid, ST_IDLE)
        self._update_mapping(t.root, changes)
        canceled = self.cancel.is_set()
        self.progress_finish(canceled=canceled)
        found = sum(1 for v in changes.values() if v != NOT_FOUND)
        self.log(f"IMDb lookup {'canceled' if canceled else 'finished'}: {found}/{len(targets)} matched.")

    def _apply_imdb(self, t: TabState, iid: str, tt: str) -> None:
        r = t.rows.get(iid)
        if not r:
            return
        r["tt"], r["not_found"] = tt, not tt
        t.tree.set(iid, "imdb_id", tt)
        t.tree.set(iid, "status", ST_READY if tt else ST_NO_ID)

    # ---------- context menu ----------
    def _on_right_click(self, event, t: TabState) -> None:
        iid = t.tree.identify_row(event.y)
        if not iid or self.busy:
            return
        t.tree.selection_set(iid)
        t.tree.focus(iid)
        self.menu_row = iid
        is_title = t.rows[iid]["type"] in ("movie", "show")
        for i in (0, 1, 2):
            self.context_menu.entryconfigure(i, state="normal" if is_title else "disabled")
        self.context_menu.tk_popup(event.x_root, event.y_root)
        self.context_menu.grab_release()

    def _on_double_click(self, event, t: TabState):
        if t.tree.identify_region(event.x, event.y) != "cell":
            return None
        iid = t.tree.identify_row(event.y)
        if iid and t.tree.identify_column(event.x) == "#5" and not self.busy:
            self.menu_row = iid
            self.ctx_assign_imdb()
            return "break"
        return None

    def _menu_row_data(self):
        t = self.cur
        iid = self.menu_row
        if not iid or iid not in t.rows:
            return None
        return t, iid, t.rows[iid]

    def ctx_open_folder(self) -> None:
        sel = self._menu_row_data()
        if not sel:
            return
        path = sel[2]["path"]
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        try:
            if sys.platform == "win32":
                os.startfile(folder)  # type: ignore[attr-defined]
            else:
                webbrowser.open(f"file://{folder}")
        except OSError as e:
            self.log(f"[WARN] Could not open folder: {e}")

    def ctx_assign_imdb(self) -> None:
        sel = self._menu_row_data()
        if not sel:
            return
        t, iid, r = sel
        if r["type"] not in ("movie", "show"):
            messagebox.showinfo("Not Available", "Assign IMDb IDs on the show row, not on seasons/episodes.",
                                parent=self)
            return
        win = tk.Toplevel(self)
        win.title("Assign IMDb ID")
        win.configure(bg=C["bg"])
        win.resizable(False, False)
        win.transient(self)
        self._dark_title_bar(win)
        body = ttk.Frame(win, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="IMDb ID or IMDb link", style="Sub.TLabel").pack(anchor="w")
        ttk.Label(body, text=r["filename"], wraplength=int(420 * self.scale)).pack(anchor="w", pady=[0, 8])
        var = tk.StringVar(value=r.get("tt") or "tt")
        ent = ttk.Entry(body, textvariable=var, font=F(10))
        ent.pack(fill="x")
        ent.focus_set()
        ent.icursor("end")

        def save(_e=None):
            raw = var.get().strip()
            m = re.search(r"tt\d{5,}", raw)
            val = m.group(0) if m else ("" if raw in ("", "tt") else None)
            if val is None:
                self._show_error("Invalid ID", "Use the IMDb format, e.g. tt0112344 (or paste the IMDb URL).",
                                     parent=win)
                return
            self._apply_imdb(t, iid, val)
            r["not_found"] = False
            if not val:
                t.tree.set(iid, "status", ST_IDLE)
            self._update_mapping(t.root, {r["key"]: val or None})
            self.log(f"Assigned {val or '(none)'} to {r['filename']}")
            win.destroy()

        ent.bind("<Return>", save)
        win.bind("<Escape>", lambda e: win.destroy())
        ttk.Button(body, text="Save", style="Accent.TButton", command=save, width=10).pack(side="right", pady=[12, 0])
        self._center_child(win, 460)
        win.grab_set()

    def ctx_clear_mapping(self) -> None:
        sel = self._menu_row_data()
        if not sel or sel[2]["type"] not in ("movie", "show"):
            return
        t, iid, r = sel
        r["tt"], r["not_found"] = "", False
        t.tree.set(iid, "imdb_id", "")
        t.tree.set(iid, "status", ST_IDLE)
        self._update_mapping(t.root, {r["key"]: None})
        self.log(f"Cleared IMDb ID for {r['filename']}")

    def ctx_lookup_imdb(self) -> None:
        sel = self._menu_row_data()
        if not sel or sel[2]["type"] not in ("movie", "show"):
            return
        t, iid, r = sel
        kind = "series" if r["type"] == "show" else "movie"
        args = (r["parsed_title"], r.get("year", ""), kind)
        self.set_status(t.key, iid, "Searching IMDb…")

        def worker():
            tt = fetch_imdb_id(*args)
            self.ui(self._apply_imdb, t, iid, tt)
            self._update_mapping(t.root, {r["key"]: tt or NOT_FOUND})
            self.log(f"{r['filename']} -> {tt or 'not found'}")

        threading.Thread(target=worker, daemon=True).start()

    # ---------- downloads ----------
    def _action_download(self) -> None:
        if self.busy == "download":
            self.cancel.set()
            self.btn_download.configure(text="Stopping…")
            return
        if self.busy:
            return
        langs = self._selected_langs()
        if not langs:
            messagebox.showwarning("No Language", "Tick at least one subtitle language.", parent=self)
            return
        t = self.cur
        jobs = []
        for iid, r in t.rows.items():
            if r["type"] not in LEAF or not r["enabled"]:
                continue
            if r["type"] == "movie":
                m = Media("movie", r["path"], r["filename"], r["parsed_title"], r.get("year", ""), r.get("tt", ""))
            else:
                show = t.rows[r["show"]]
                ep = parse_episode(r["filename"], r.get("season_dir", ""))
                m = Media("episode", r["path"], r["filename"], show["parsed_title"], show.get("year", ""),
                          show.get("tt", ""), ep[0] if ep else None, ep[1] if ep else None, r.get("season_dir", ""))
            jobs.append((iid, m))
        if not jobs:
            messagebox.showwarning("Nothing Selected", "Tick at least one file.", parent=self)
            return
        self._save_settings()
        self.cancel.clear()
        self._set_busy("download")
        threading.Thread(target=self._download_worker,
                         args=(t, jobs, langs, self.var_overwrite.get(), t.source), daemon=True).start()

    def _download_worker(self, t: TabState, jobs: list, langs: list, overwrite: bool, source: str) -> None:
        try:
            # 1) Only-missing pre-pass: check the disk first so complete files never hit the network.
            todo, complete = [], []
            for iid, m in jobs:
                have = existing_subs(m.path)
                wanted = list(langs) if overwrite else [(n, c) for n, c in langs if c not in have]
                if wanted:
                    todo.append((iid, m, wanted))
                else:
                    complete.append((iid, have))
            if complete:
                self.ui(self._after_batch, t, complete)
                self.log(f"{len(complete)} file(s) already have all selected subtitles — skipped.")
            if not todo:
                self.log("Nothing to download.")
                self.progress_finish()
                return

            kind = "movie" if t.key == "movies" else "episode"
            chain = self._build_chain(kind, source)
            if not chain:
                self.log("[ERROR] No usable source. Open ⚙ Sources to enable one or add an API key.")
                self.progress_finish(canceled=True)
                return
            for p in chain:
                p.cancel_event = self.cancel
                p.begin()
            total = sum(len(w) for _, _, w in todo)
            self.log(f"Downloading {total} subtitle(s) for {len(todo)} file(s) "
                     f"via {' → '.join(p.short for p in chain)}"
                     + (" (YIFY/YTS releases try YTS Subs first)" if kind == "movie" and
                        any(p.key == "yts" for p in chain) else ""))
            self.progress_init(total)
            self._batch_outcomes: dict[str, dict] = {}
            workers = MOVIE_WORKERS if kind == "movie" else SERIES_WORKERS

            # Pass 1: never wait more than a few seconds for a rate-limited source — use the next one instead.
            jobs1 = [(iid, m, [(n, c, None) for n, c in wanted]) for iid, m, wanted in todo]
            deferred = self._run_pool(t, jobs1, chain, workers, max_wait=PASS1_MAX_WAIT)
            # Pass 2: only what was skipped because a source was cooling down — now we wait for it.
            if deferred and not self.cancel.is_set():
                n = sum(len(w) for _, _, w in deferred)
                self.log(f"Retrying {n} subtitle(s) that were waiting on rate-limited sources…")
                left = self._run_pool(t, deferred, chain, workers, max_wait=PASS2_MAX_WAIT)
                for iid, m, wanted in left:  # still blocked after waiting
                    for name, code, _ in wanted:
                        self.log(f"[MISS] {m.filename}: no {name} subtitle (sources still rate limited)")
                        self._record(t, iid, m, code, "✗ rate limited")
                        self.progress_step()
        except Exception as e:
            self.log(f"[ERROR] Download batch failed: {e}")
        finally:
            canceled = self.cancel.is_set()
            self.progress_finish(canceled=canceled)
            self.log("Download batch canceled." if canceled else "Download batch finished.")
            if not canceled:
                self.ui(play_sound, SOUND_DONE)
            self.ui(self._set_busy, None)

    def _run_pool(self, t: TabState, jobs: list, chain: list, workers: int, max_wait: float) -> list:
        """Runs jobs [(iid, media, [(name, code, only_sources|None)])]; returns the deferred ones."""
        deferred: list = []
        lock = threading.Lock()
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(jobs)))) as ex:
            futures = [ex.submit(self._process_item, t, iid, m, wanted, chain, max_wait) for iid, m, wanted in jobs]
            for fut in as_completed(futures):
                try:
                    item = fut.result()
                    if item:
                        with lock:
                            deferred.append(item)
                except Exception as e:
                    self.log(f"[ERROR] {e}")
                if self.cancel.is_set():
                    ex.shutdown(wait=False, cancel_futures=True)
                    break
        return deferred

    def _record(self, t: TabState, iid: str, m: Media, code: str, outcome: str) -> None:
        outcomes = self._batch_outcomes.setdefault(iid, {})
        outcomes[code] = outcome
        self.ui(self._after_item, t, iid, existing_subs(m.path), dict(outcomes))

    def _process_item(self, t: TabState, iid: str, m: Media, wanted: list, chain: list, max_wait: float):
        if self.cancel.is_set():
            return None
        _tls.max_wait = max_wait
        outcomes = self._batch_outcomes.setdefault(iid, {})
        later = []
        if m.kind == "episode" and m.ep is None:
            self.log(f"[MISS] {m.filename}: can't read SxxEyy from the filename.")
            for _, code, _ in wanted:
                outcomes[code] = "✗ bad name"
                self.progress_step()
        else:
            self.set_status(t.key, iid, "Searching…")
            for name, code, only in wanted:
                if self.cancel.is_set():
                    break
                result, busy = self._fetch_lang(m, name, code, chain, only)
                if busy:  # a source that might have it was cooling down -> retry in pass 2
                    outcomes[code] = "⏳ waiting"
                    later.append((name, code, busy))
                else:
                    outcomes[code] = result
                    self.progress_step()
        self.ui(self._after_item, t, iid, existing_subs(m.path), dict(outcomes))
        return (iid, m, later) if later else None

    @staticmethod
    def _ordered_chain(m: Media, chain: list[Provider]) -> list[Provider]:
        """YIFY/YTS releases: YTS Subs first — no key quota, and timed for exactly that release."""
        if m.kind == "movie" and _YTS_RELEASE_RX.search(m.filename):
            return sorted(chain, key=lambda p: p.key != "yts")
        return chain

    def _fetch_lang(self, m: Media, name: str, code: str, chain: list[Provider],
                    only: set | None = None) -> tuple[str, set]:
        tried, busy = [], set()
        for p in self._ordered_chain(m, chain):
            if p.disabled or not p.supports(m.kind) or (only is not None and p.key not in only):
                continue
            try:
                with p.serial_lock:
                    raw = p.fetch(m, name, code)
                srt = extract_subtitle(raw, m.ep)
                write_srt(srt_target(m.path, code), normalize_encoding(srt, code))
                p.failures = 0
                self.log(f"[OK] {m.filename}: {code.upper()} from {p.name}")
                return f"✓ {p.short}", set()
            except ProviderBusy:
                if self.cancel.is_set():
                    return "Canceled", set()
                busy.add(p.key)
            except LookupError:
                tried.append(p.short)
            except BadZipFile:
                tried.append(p.short)
                self.log(f"[WARN] {p.name}: broken archive for {m.filename}")
            except ProviderUnavailable as e:
                if p.disable(str(e)):
                    self.log(f"[SOURCE OFF] {p.name}: {e} — skipped for the rest of this batch.")
            except requests.RequestException as e:
                if is_unreachable(e):
                    if p.disable("unreachable"):
                        self.log(f"[SOURCE OFF] {p.name}: {short_error(e)} — skipped for the rest of this batch.")
                    continue
                tried.append(p.short)
                p.failures += 1
                self.log(f"[WARN] {p.name}: {short_error(e)} ({m.filename})")
                if p.failures >= 4 and p.disable("too many network errors"):
                    self.log(f"[SOURCE OFF] {p.name}: too many errors in a row — skipped for the rest of this batch.")
            except OSError as e:
                self.log(f"[ERROR] {m.filename}: could not write subtitle – {e}")
                return "✗ write error", set()
        if busy:
            return "⏳", busy
        self.log(f"[MISS] {m.filename}: no {name} subtitle"
                 + (f" (tried {', '.join(tried)})" if tried else " (no source available)"))
        return "✗", set()

    @staticmethod
    def _status_text(outcomes: dict) -> str:
        """'EN ✓ SubSource   AR ✓ SubSource' -> '✓ EN AR · SubSource' so it fits the column."""
        if not outcomes:
            return "Canceled"
        vals = set(outcomes.values())
        if len(vals) == 1 and next(iter(vals)).startswith("✓ "):
            return f"✓ {' '.join(c.upper() for c in outcomes)} · {next(iter(vals))[2:]}"
        return "   ".join(f"{c.upper()} {v}" for c, v in outcomes.items())

    def _after_batch(self, t: TabState, items: list) -> None:
        for iid, have in items:
            self._after_item(t, iid, have, None, refresh=False)
        self._refresh_parents(t)
        self._update_counts(t)

    def _after_item(self, t: TabState, iid: str, have: set, outcomes: dict | None, refresh: bool = True) -> None:
        r = t.rows.get(iid)
        if not r:
            return
        r["have"] = have
        t.tree.set(iid, "subs", self._subs_text(have))
        if outcomes is not None:
            r["failed"] = any(v.startswith("✗") for v in outcomes.values())
            t.tree.set(iid, "status", self._status_text(outcomes))
        else:
            r["failed"] = False
            t.tree.set(iid, "status", "Complete")
        self._retag(t, iid)
        if refresh:
            self._refresh_parents(t)
            self._update_counts(t)


# ============================== Entry point ==============================
def _enable_windows_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        try:  # own taskbar entry + icon instead of python.exe's
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except (AttributeError, OSError):
            pass
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _selftest() -> int:
    """Used by the release build: confirms every bundled library loads and a window can open."""
    try:
        import bs4  # noqa: F401
        import certifi  # noqa: F401
        assert cloudscraper is not None, "cloudscraper missing"
        if keyring is not None:
            keyring.get_keyring()
        root = tk.Tk()
        root.withdraw()
        tk.PhotoImage(master=root, data=ICON_64_PNG)
        ttk.Style(root).theme_use("clam")
        root.destroy()
        return 0
    except Exception:
        try:
            with open(CRASH_LOG, "a", encoding="utf-8") as f:
                f.write(f"\n--- selftest {time.ctime()} ---\n{traceback.format_exc()}")
        except OSError:
            pass
        traceback.print_exc()
        return 1


def main() -> None:
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    _enable_windows_dpi_awareness()
    try:
        App().mainloop()
    except Exception as e:
        trace = traceback.format_exc()
        try:
            with open(CRASH_LOG, "a", encoding="utf-8") as f:
                f.write(f"\n--- {time.ctime()} ---\n{trace}")
        except OSError:
            pass
        try:
            messagebox.showerror("Startup Error", f"The app failed to start:\n\n{e}\n\nDetails: {CRASH_LOG}")
        except Exception:
            print(trace, file=sys.stderr)


if __name__ == "__main__":
    main()
