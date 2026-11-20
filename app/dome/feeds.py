"""Feed catalog and one resilient fetch path for the Kaleidophone dome.

Every monitor tile is produced server-side, so the browser never talks to a
third-party host directly and a failing source becomes a labelled placeholder
instead of a silent black tile. Network work uses a bounded timeout, a small
retry budget for transient faults and a per-host circuit breaker.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

import cv2
import numpy as np

USER_AGENT = "Sentinel-Kaleidophone/1.0"
MAX_REMOTE_BYTES = 12 * 1024 * 1024
GIBS_BASE = "https://gibs.earthdata.nasa.gov"
GIBS_MATRIX_SET = "GoogleMapsCompatible_Level9"
# A true-colour world image is well above this; a blank/error tile is a few KB.
GIBS_MIN_USEFUL_BYTES = 8000
EPIC_API = "https://epic.gsfc.nasa.gov/api/natural"
EPIC_ARCHIVE = "https://epic.gsfc.nasa.gov/archive/natural"
NASA_LIVE_URL = "https://www.nasa.gov/live/"


class FeedError(Exception):
    """The feed cannot be produced right now. The text is shown in the UI."""


FEED_CATALOG: Tuple[Dict[str, Any], ...] = (
    {
        "id": "camflow",
        "label": "CamFlow camera",
        "kind": "stream",
        "description": "Live MJPEG from the paired CamFlow phone.",
        "attribution": "Local CamFlow node",
        "options": [],
    },
    {
        "id": "nasa_terra",
        "label": "NASA GIBS - Terra true colour",
        "kind": "image",
        "description": "Daily MODIS Terra corrected reflectance, latest available observation.",
        "attribution": "Imagery: NASA GIBS / EOSDIS",
        "layer": "MODIS_Terra_CorrectedReflectance_TrueColor",
        "options": [],
    },
    {
        "id": "nasa_aqua",
        "label": "NASA GIBS - Aqua true colour",
        "kind": "image",
        "description": "Daily MODIS Aqua corrected reflectance, latest available observation.",
        "attribution": "Imagery: NASA GIBS / EOSDIS",
        "layer": "MODIS_Aqua_CorrectedReflectance_TrueColor",
        "options": [],
    },
    {
        "id": "nasa_viirs",
        "label": "NASA GIBS - VIIRS true colour",
        "kind": "image",
        "description": "Daily VIIRS SNPP corrected reflectance, latest available observation.",
        "attribution": "Imagery: NASA GIBS / EOSDIS",
        "layer": "VIIRS_SNPP_CorrectedReflectance_TrueColor",
        "options": [],
    },
    {
        "id": "nasa_epic",
        "label": "NASA EPIC - full-disc Earth",
        "kind": "image",
        "description": "Near-real-time full-disc Earth from the DSCOVR spacecraft.",
        "attribution": "Imagery: NASA EPIC / DSCOVR",
        "options": [],
    },
    {
        "id": "nasa_live",
        "label": "NASA Live",
        "kind": "link",
        "description": (
            "Official NASA live programming. This tile shows the latest EPIC still; "
            "live video opens on NASA's own player."
        ),
        "attribution": "Source: NASA",
        "url": NASA_LIVE_URL,
        "options": [],
    },
    {
        "id": "rtsp",
        "label": "RTSP / ONVIF camera",
        "kind": "camera",
        "description": "An IP camera stream; set the stream URL on the monitor.",
        "attribution": "Local camera node",
        "options": ["url"],
    },
    {
        "id": "web",
        "label": "Web page",
        "kind": "web",
        "description": "Any page shown in the monitor detail panel; set its URL.",
        "attribution": "External page",
        "options": ["url"],
    },
)

FEED_BY_ID: Dict[str, Dict[str, Any]] = {feed["id"]: feed for feed in FEED_CATALOG}

# Feeds that can fill a monitor tile by themselves, used by the default layout.
VISUAL_FEEDS = ("camflow", "nasa_terra", "nasa_epic", "nasa_viirs", "nasa_aqua", "nasa_live")

ALLOWED_URL_SCHEMES = ("rtsp", "rtsps", "http", "https")


def validate_feed_url(url: str) -> str:
    """Return a cleaned URL or raise ValueError for an unsupported scheme."""
    url = str(url or "").strip()
    if not url:
        return ""
    if len(url) > 500:
        raise ValueError("url is too long")
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in ALLOWED_URL_SCHEMES:
        raise ValueError("url must start with rtsp://, rtsps://, http:// or https://")
    return url


def live_url_for(monitor: Dict[str, Any]) -> str:
    """The URL a detail panel should open for a monitor, if any."""
    feed = FEED_BY_ID.get(str(monitor.get("feed") or ""))
    if feed is None:
        return ""
    kind = feed["kind"]
    if kind == "stream":
        return "/stream"
    if kind == "link":
        return str(feed.get("url") or "")
    if kind == "web":
        return str(monitor.get("url") or "")
    return ""


def _encode_jpeg(img: np.ndarray, width: int, quality: int = 82) -> bytes:
    h, w = img.shape[:2]
    if w > width > 0:
        img = cv2.resize(img, (width, max(1, int(h * width / w))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise FeedError("could not encode the frame")
    return buf.tobytes()


def _wrap_text(text: str, limit: int = 34) -> list:
    lines, current = [], ""
    for word in str(text).split():
        candidate = f"{current} {word}".strip()
        if len(candidate) > limit and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines[:3]


class FeedHub:
    """Renders monitor tiles and keeps per-feed health.

    Health is deliberately simple: the last success time, a failure count, the
    last error and the last latency. That is what the dome shows on a tile and
    in its link badge.
    """

    def __init__(self, frame_buf, clock=time.time):
        self._frame_buf = frame_buf
        self._clock = clock
        self._lock = threading.Lock()
        self._health: Dict[str, Dict[str, Any]] = {}
        self._cache: Dict[str, Tuple[float, bytes, str, Dict[str, Any]]] = {}
        self._dates: Dict[str, Tuple[float, str]] = {}
        self._circuits: Dict[str, Dict[str, float]] = {}

    # -- public ---------------------------------------------------------

    def health(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {key: dict(value) for key, value in self._health.items()}

    def tile(self, monitor: Dict[str, Any], width: int = 640) -> Tuple[bytes, str, Dict[str, Any]]:
        """Return (jpeg_bytes, content_type, meta) for one monitor."""
        feed_id = str(monitor.get("feed") or "")
        feed = FEED_BY_ID.get(feed_id)
        if feed is None:
            return (
                self._placeholder("No feed assigned", width),
                "image/jpeg",
                {"feed": feed_id, "error": "no feed assigned"},
            )

        started = self._clock()
        try:
            data, ctype, meta = self._render(feed, monitor, max(160, min(1600, int(width))))
        except FeedError as exc:
            self._record(feed_id, False, str(exc), started)
            return (
                self._placeholder(f"{feed['label']}: {exc}", width),
                "image/jpeg",
                {"feed": feed_id, "error": str(exc)},
            )
        self._record(feed_id, True, "", started)
        meta = dict(meta)
        meta.setdefault("feed", feed_id)
        meta.setdefault("attribution", feed.get("attribution", ""))
        return data, ctype, meta

    # -- rendering ------------------------------------------------------

    def _render(self, feed: Dict[str, Any], monitor: Dict[str, Any], width: int):
        kind = feed["kind"]
        if kind == "stream":
            return self._camflow(width)
        if kind == "image":
            layer = feed.get("layer")
            if layer:
                return self._gibs(layer, width)
            return self._epic(width)
        if kind == "link":
            data, ctype, meta = self._epic(width)
            meta = dict(meta)
            meta["note"] = "Still frame from NASA EPIC; live video opens on nasa.gov."
            meta["live_url"] = feed.get("url", NASA_LIVE_URL)
            return data, ctype, meta
        if kind == "camera":
            return self._rtsp(monitor.get("url"), width)
        if kind == "web":
            raise FeedError("web pages open in the detail panel")
        raise FeedError("unsupported feed")

    def _camflow(self, width: int):
        frame = self._frame_buf.get_copy()
        if frame is None:
            raise FeedError("no frames yet - enable ingest in the dashboard")
        return _encode_jpeg(frame, width), "image/jpeg", {"source": "CamFlow", "live": True}

    def _gibs(self, layer: str, width: int):
        cache_key = f"gibs:{layer}:{width}"
        cached = self._cache_get(cache_key)
        if cached:
            return cached

        latest = self._gibs_latest_date(layer)
        last_error = None
        for offset in range(5):
            day = (latest - timedelta(days=offset)).isoformat()
            params = {
                "SERVICE": "WMS",
                "REQUEST": "GetMap",
                "VERSION": "1.3.0",
                "LAYERS": layer,
                "STYLES": "default",
                "CRS": "EPSG:3857",
                "BBOX": "-20037508.34,-20037508.34,20037508.34,20037508.34",
                "WIDTH": str(width),
                "HEIGHT": str(width),
                "FORMAT": "image/jpeg",
                "TIME": day,
            }
            url = f"{GIBS_BASE}/wms/epsg3857/best/wms.cgi?{urllib.parse.urlencode(params)}"
            try:
                raw, _ = self._http_get(url, timeout=15)
            except FeedError as exc:
                last_error = exc
                continue
            if len(raw) < GIBS_MIN_USEFUL_BYTES:
                last_error = FeedError(f"no imagery for {day}")
                continue
            img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                last_error = FeedError(f"unreadable tile for {day}")
                continue
            data = _encode_jpeg(img, width)
            meta = {
                "source": "NASA GIBS / EOSDIS",
                "observation_date": day,
                "note": "Processed satellite imagery, not live video.",
            }
            self._cache_put(cache_key, data, "image/jpeg", meta, ttl=600)
            return data, "image/jpeg", meta

        raise FeedError(str(last_error) if last_error else "no usable imagery")

    def _gibs_latest_date(self, layer: str) -> "datetime.date":
        cached = self._dates.get(layer)
        now = self._clock()
        if cached and cached[0] > now:
            return datetime.strptime(cached[1], "%Y-%m-%d").date()

        url = f"{GIBS_BASE}/wmts/epsg3857/best/1.0.0/{layer}/default/{GIBS_MATRIX_SET}/all/all.xml"
        raw, _ = self._http_get(url, timeout=12)
        dates = re.findall(r"\d{4}-\d{2}-\d{2}", raw.decode("utf-8", "replace"))
        if not dates:
            raise FeedError("GIBS time domain unavailable")
        latest = max(dates)
        self._dates[layer] = (now + 6 * 3600, latest)
        return datetime.strptime(latest, "%Y-%m-%d").date()

    def _epic(self, width: int):
        cache_key = f"epic:{width}"
        cached = self._cache_get(cache_key)
        if cached:
            return cached

        raw, _ = self._http_get(EPIC_API, timeout=12)
        try:
            items = json.loads(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            raise FeedError("EPIC metadata is not valid JSON") from exc
        if not isinstance(items, list) or not items:
            raise FeedError("EPIC returned no images")

        latest = max(items, key=lambda item: str(item.get("identifier") or item.get("date") or ""))
        identifier = str(latest.get("image") or "")
        captured = str(latest.get("date") or "")
        if not identifier or len(captured) < 10:
            raise FeedError("EPIC returned an incomplete record")

        year, month, day = captured[:10].split("-")
        image_url = f"{EPIC_ARCHIVE}/{year}/{month}/{day}/png/{identifier}.png"
        raw_img, _ = self._http_get(image_url, timeout=25, retries=0)
        img = cv2.imdecode(np.frombuffer(raw_img, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise FeedError("EPIC image could not be decoded")

        data = _encode_jpeg(img, width)
        meta = {
            "source": "NASA EPIC / DSCOVR",
            "captured": f"{captured} UTC",
            "caption": str(latest.get("caption") or ""),
            "note": "Near-real-time still, not live video.",
        }
        self._cache_put(cache_key, data, "image/jpeg", meta, ttl=1200)
        return data, "image/jpeg", meta

    def _rtsp(self, url: Optional[str], width: int):
        try:
            url = validate_feed_url(url or "")
        except ValueError as exc:
            raise FeedError(str(exc)) from exc
        if not url:
            raise FeedError("set the camera stream URL on this monitor")

        cache_key = f"rtsp:{url}"
        cached = self._cache_get(cache_key)
        if cached:
            return cached

        cap = cv2.VideoCapture()
        try:
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000)
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000)
            if not cap.open(url, cv2.CAP_FFMPEG):
                raise FeedError("cannot open the stream")
            ok, frame = cap.read()
            if not ok or frame is None:
                raise FeedError("no frame from the stream")
        finally:
            cap.release()

        data = _encode_jpeg(frame, width)
        meta = {"source": "RTSP camera", "live": True}
        # Share one grab between the ring tile and the detail panel.
        self._cache_put(cache_key, data, "image/jpeg", meta, ttl=3)
        return data, "image/jpeg", meta

    def _placeholder(self, text: str, width: int) -> bytes:
        width = max(160, min(1200, int(width)))
        height = int(width * 9 / 16)
        img = np.full((height, width, 3), 18, dtype=np.uint8)
        for x in range(0, width, max(24, width // 12)):
            cv2.line(img, (x, 0), (x, height), (28, 28, 28), 1)
        cv2.putText(
            img, "KALEIDOPHONE DOME", (14, 24),
            cv2.FONT_HERSHEY_SIMPLEX, 0.45, (110, 110, 110), 1, cv2.LINE_AA,
        )
        y = int(height / 2) - 10 * (len(_wrap_text(text)) - 1)
        for line in _wrap_text(text):
            cv2.putText(
                img, line, (14, y),
                cv2.FONT_HERSHEY_SIMPLEX, 0.42, (205, 205, 205), 1, cv2.LINE_AA,
            )
            y += 20
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        return buf.tobytes() if ok else b""

    # -- network --------------------------------------------------------

    def _http_get(self, url: str, timeout: float = 10.0, retries: int = 1) -> Tuple[bytes, str]:
        host = urllib.parse.urlsplit(url).netloc
        self._check_circuit(host)
        last_error: Optional[FeedError] = None

        for attempt in range(retries + 1):
            try:
                request = urllib.request.Request(
                    url,
                    headers={"User-Agent": USER_AGENT, "Accept": "image/*,application/json,*/*"},
                )
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    content_type = response.headers.get("Content-Type", "")
                    data = response.read(MAX_REMOTE_BYTES + 1)
                if len(data) > MAX_REMOTE_BYTES:
                    raise FeedError("response is too large")
                self._circuit_success(host)
                return data, content_type
            except urllib.error.HTTPError as exc:
                if exc.code in (408, 425, 429) or exc.code >= 500:
                    last_error = FeedError(f"HTTP {exc.code}")
                    time.sleep(0.3 * (attempt + 1))
                    continue
                self._circuit_failure(host)
                raise FeedError(f"HTTP {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = FeedError(f"network error: {exc}")
                time.sleep(0.3 * (attempt + 1))
                continue

        self._circuit_failure(host)
        raise last_error or FeedError("request failed")

    def _check_circuit(self, host: str) -> None:
        with self._lock:
            circuit = self._circuits.get(host)
        if circuit and circuit.get("open_until", 0) > self._clock():
            raise FeedError(f"{host} is cooling down after repeated failures")

    def _circuit_success(self, host: str) -> None:
        with self._lock:
            self._circuits.pop(host, None)

    def _circuit_failure(self, host: str) -> None:
        with self._lock:
            circuit = self._circuits.setdefault(host, {"failures": 0, "open_until": 0})
            circuit["failures"] = circuit.get("failures", 0) + 1
            if circuit["failures"] >= 3:
                circuit["open_until"] = self._clock() + 60
                circuit["failures"] = 0

    # -- cache and health -----------------------------------------------

    def _cache_get(self, key: str):
        with self._lock:
            entry = self._cache.get(key)
        if entry and entry[0] > self._clock():
            return entry[1], entry[2], dict(entry[3])
        return None

    def _cache_put(self, key: str, data: bytes, ctype: str, meta: Dict[str, Any], ttl: float) -> None:
        with self._lock:
            self._cache[key] = (self._clock() + ttl, data, ctype, dict(meta))
            if len(self._cache) > 200:
                now = self._clock()
                self._cache = {k: v for k, v in self._cache.items() if v[0] > now}

    def _record(self, feed_id: str, ok: bool, error: str, started: float) -> None:
        latency_ms = int((self._clock() - started) * 1000)
        with self._lock:
            health = self._health.setdefault(
                feed_id, {"ok": 0, "failures": 0, "last_ok": "", "last_error": "", "latency_ms": 0}
            )
            health["latency_ms"] = latency_ms
            if ok:
                health["ok"] += 1
                health["last_ok"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                health["last_error"] = ""
            else:
                health["failures"] += 1
                health["last_error"] = error[:200]
