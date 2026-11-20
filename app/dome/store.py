"""Dome layout state.

The dome is a symmetry stage: `fold` sectors of `360 / fold` degrees, one
individual monitor per sector. Layout state is local and independent of feed
transport, which is what lets feeds change without touching the rendering
contract.

Fold changes preserve every existing monitor assignment by id; growing adds
monitors with the default feed rotation, shrinking drops the tail.
"""
from __future__ import annotations

import copy
import json
import os
import threading
from typing import Any, Dict, List, Optional

from app.dome.feeds import FEED_BY_ID, VISUAL_FEEDS, validate_feed_url

FOLD_CHOICES = (6, 8, 12, 16, 24)
SPEED_LIMIT_DPS = 60.0
DEFAULT_FOLD = 8
DEFAULT_SPEED_DPS = 3.0
DEFAULT_REFRACTION = 35

MONITOR_DEFAULTS = (
    ("camflow", "CamFlow"),
    ("nasa_terra", "Terra"),
    ("nasa_epic", "EPIC Earth"),
    ("nasa_viirs", "VIIRS"),
    ("nasa_aqua", "Aqua"),
    ("nasa_live", "NASA Live"),
)


def sector_angle(fold: int) -> float:
    return round(360.0 / float(fold), 3)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class DomeStore:
    """Thread-safe dome state with atomic persistence."""

    def __init__(self, path: str = "dome.json", logger=None, initial: Optional[Dict[str, Any]] = None):
        self.path = path
        self.logger = logger
        self._lock = threading.RLock()
        self._state = self._normalize(initial or {})
        self._load()

    # -- public ---------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            state = copy.deepcopy(self._state)
        state["sector_angle"] = sector_angle(state["fold"])
        state["fold_choices"] = list(FOLD_CHOICES)
        return state

    def update(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """Apply a layout patch. Raises ValueError for an invalid value."""
        with self._lock:
            candidate = copy.deepcopy(self._state)
            if "fold" in patch:
                fold = self._as_int(patch["fold"], "fold")
                if fold not in FOLD_CHOICES:
                    raise ValueError(f"fold must be one of {list(FOLD_CHOICES)}")
                candidate["fold"] = fold
            if "speed_dps" in patch:
                candidate["speed_dps"] = round(
                    _clamp(self._as_float(patch["speed_dps"], "speed_dps"), -SPEED_LIMIT_DPS, SPEED_LIMIT_DPS), 2
                )
            if "rotation_deg" in patch:
                candidate["rotation_deg"] = round(self._as_float(patch["rotation_deg"], "rotation_deg") % 360.0, 2)
            if "paused" in patch:
                candidate["paused"] = bool(patch["paused"])
            if "refraction" in patch:
                candidate["refraction"] = int(
                    _clamp(self._as_float(patch["refraction"], "refraction"), 0, 100)
                )
            self._state = self._normalize(candidate)
            self._save()
            return self.snapshot()

    def assign(self, monitor_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        """Assign a feed, label or URL to one monitor."""
        with self._lock:
            monitor = self._find(monitor_id)
            if monitor is None:
                raise KeyError(monitor_id)

            if "feed" in patch:
                feed_id = str(patch.get("feed") or "").strip()
                if feed_id and feed_id not in FEED_BY_ID:
                    raise ValueError("unknown feed")
                monitor["feed"] = feed_id
            if "label" in patch:
                monitor["label"] = str(patch.get("label") or "").strip()[:80]
            if "url" in patch:
                monitor["url"] = validate_feed_url(patch.get("url") or "")

            self._save()
            return self.snapshot()

    def monitor(self, monitor_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            monitor = self._find(monitor_id)
            return copy.deepcopy(monitor) if monitor else None

    def apply_action(self, action: str, args: Optional[Dict[str, Any]] = None) -> str:
        """Run one assistant action and return a human-readable confirmation."""
        args = args or {}
        if action == "set_fold":
            fold = int(args.get("fold", DEFAULT_FOLD))
            state = self.update({"fold": fold})
            return f"The dome is now {state['fold']}-fold ({state['sector_angle']} degree sectors)."
        if action == "set_speed":
            state = self.update({"speed_dps": float(args.get("speed_dps", 0))})
            return f"Rotation speed set to {state['speed_dps']} degrees per second."
        if action == "set_rotation":
            state = self.update({"rotation_deg": float(args.get("rotation_deg", 0))})
            return f"Dome angle set to {state['rotation_deg']} degrees."
        if action == "set_paused":
            paused = bool(args.get("paused", True))
            state = self.update({"paused": paused})
            return "Rotation paused." if state["paused"] else "Rotation resumed."
        if action == "set_refraction":
            state = self.update({"refraction": int(args.get("refraction", DEFAULT_REFRACTION))})
            return f"Refraction set to {state['refraction']}%."
        if action == "assign_feed":
            monitor_id = str(args.get("monitor_id") or "")
            feed_id = str(args.get("feed") or "")
            state = self.assign(monitor_id, {"feed": feed_id})
            monitor = next(m for m in state["monitors"] if m["id"] == monitor_id)
            label = monitor["label"] or FEED_BY_ID.get(feed_id, {}).get("label", feed_id)
            return f"Monitor {monitor_id} now shows {label}."
        if action == "clear_monitor":
            monitor_id = str(args.get("monitor_id") or "")
            self.assign(monitor_id, {"feed": "", "label": "", "url": ""})
            return f"Monitor {monitor_id} cleared."
        if action == "arrange":
            return self._arrange()
        raise ValueError(f"unknown dome action: {action}")

    # -- internals ------------------------------------------------------

    def _arrange(self) -> str:
        with self._lock:
            for index, monitor in enumerate(self._state["monitors"]):
                monitor["feed"] = VISUAL_FEEDS[index % len(VISUAL_FEEDS)]
                monitor["label"] = ""
            self._save()
            count = len(self._state["monitors"])
        return f"Spread {len(VISUAL_FEEDS)} feeds across {count} monitors."

    def _find(self, monitor_id: str) -> Optional[Dict[str, Any]]:
        for monitor in self._state["monitors"]:
            if monitor["id"] == monitor_id:
                return monitor
        return None

    def _defaults(self) -> Dict[str, Any]:
        return {
            "fold": DEFAULT_FOLD,
            "rotation_deg": 0.0,
            "speed_dps": DEFAULT_SPEED_DPS,
            "paused": False,
            "refraction": DEFAULT_REFRACTION,
            "monitors": [],
        }

    def _normalize(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        state = self._defaults()

        fold = DEFAULT_FOLD
        try:
            requested = int(raw.get("fold", DEFAULT_FOLD))
        except (TypeError, ValueError):
            requested = DEFAULT_FOLD
        if requested in FOLD_CHOICES:
            fold = requested
        else:
            fold = min(FOLD_CHOICES, key=lambda choice: abs(choice - requested))
        state["fold"] = fold

        try:
            state["speed_dps"] = round(
                _clamp(float(raw.get("speed_dps", DEFAULT_SPEED_DPS)), -SPEED_LIMIT_DPS, SPEED_LIMIT_DPS), 2
            )
        except (TypeError, ValueError):
            pass
        try:
            state["rotation_deg"] = round(float(raw.get("rotation_deg", 0.0)) % 360.0, 2)
        except (TypeError, ValueError):
            pass
        state["paused"] = bool(raw.get("paused", False))
        try:
            state["refraction"] = int(_clamp(float(raw.get("refraction", DEFAULT_REFRACTION)), 0, 100))
        except (TypeError, ValueError):
            pass

        monitors: List[Dict[str, Any]] = []
        for index, item in enumerate(raw.get("monitors") or []):
            monitors.append(self._clean_monitor(item, index))
        while len(monitors) < fold:
            monitors.append(self._clean_monitor({}, len(monitors)))
        state["monitors"] = monitors[:fold]
        return state

    def _clean_monitor(self, item: Any, index: int) -> Dict[str, Any]:
        item = item if isinstance(item, dict) else {}
        default_feed = MONITOR_DEFAULTS[index % len(MONITOR_DEFAULTS)][0]
        default_label = MONITOR_DEFAULTS[index % len(MONITOR_DEFAULTS)][1]

        feed = str(item.get("feed") or "").strip()
        if feed and feed not in FEED_BY_ID:
            feed = default_feed
        if "feed" not in item and not feed:
            feed = default_feed

        label = str(item.get("label") or "").strip()[:80]
        if not label and feed:
            label = default_label if feed == default_feed else FEED_BY_ID[feed]["label"]

        try:
            url = validate_feed_url(item.get("url") or "")
        except ValueError:
            url = ""

        return {"id": f"m{index + 1}", "feed": feed, "label": label, "url": url}

    def _as_int(self, value: Any, name: str) -> int:
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be an integer") from exc

    def _as_float(self, value: Any, name: str) -> float:
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be a number") from exc

    def _load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            if isinstance(raw, dict):
                with self._lock:
                    self._state = self._normalize(raw)
            if self.logger:
                self.logger.info("dome layout loaded from disk")
        except Exception as exc:  # a broken layout must not stop the server
            if self.logger:
                self.logger.warning(f"load dome layout failed: {exc}")

    def _save(self) -> None:
        if not self.path:
            return
        try:
            with self._lock:
                data = copy.deepcopy(self._state)
            directory = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(directory, exist_ok=True)
            tmp_path = f"{self.path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self.path)
        except Exception as exc:
            if self.logger:
                self.logger.warning(f"save dome layout failed: {exc}")
