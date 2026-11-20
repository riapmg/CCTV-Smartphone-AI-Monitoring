"""ARIIA: the autonomy gate, multi-provider routing and the dome assistant.

The gate is fail-closed: an action runs only when a rule explicitly permits it.
Every decision is written to an audit trail, so "why did it do that" is
answerable afterwards.

The router is the layer that decides which model answers, what happens when a
provider fails, and how provenance is reported. It never invents a provider:
with no key configured the assistant says so and keeps its local capabilities.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence

from app.ai.vision_client import PROVIDER_PRESETS, resolve_provider_settings
from app.dome.feeds import FEED_BY_ID, FEED_CATALOG
from app.dome.store import FOLD_CHOICES, SPEED_LIMIT_DPS

LEVELS: Dict[int, Dict[str, str]] = {
    1: {"name": "Assisted", "summary": "Suggests only. Executes nothing."},
    2: {"name": "Semi-autonomous", "summary": "Runs reversible local work; confirms external or irreversible actions."},
    3: {"name": "Fully autonomous", "summary": "Self-directed inside the dome envelope, including external sends."},
    4: {"name": "Bounded self-tuning", "summary": "Level 3 plus tuning named parameters inside stated bounds."},
}

ACTION_CLASSES: Dict[str, str] = {
    "set_fold": "reversible_local",
    "set_speed": "reversible_local",
    "set_rotation": "reversible_local",
    "set_paused": "reversible_local",
    "set_refraction": "reversible_local",
    "assign_feed": "reversible_local",
    "clear_monitor": "reversible_local",
    "arrange": "reversible_local",
    "ingest_on": "reversible_local",
    "ingest_off": "reversible_local",
    "record_start": "reversible_local",
    "record_stop": "reversible_local",
    "snapshot": "reversible_local",
    "status": "read_only",
    "brief": "read_only",
    "help": "read_only",
    "discover_cameras": "read_only",
    "set_level": "safety",
    "set_safety": "safety",
    "shutdown": "irreversible",
    "clear_audit": "irreversible",
}

DOME_ACTIONS = {
    "set_fold",
    "set_speed",
    "set_rotation",
    "set_paused",
    "set_refraction",
    "assign_feed",
    "clear_monitor",
    "arrange",
}

CONFIDENCE_THRESHOLD = 0.55

HELP_TEXT = (
    "I can run these dome commands locally: set fold to 6/8/12/16/24, pause or resume, "
    "speed N, angle N, refraction N, arrange feeds, assign <feed> to monitor N, "
    "clear monitor N, discover ONVIF cameras, enable or disable ingest, start or stop recording, "
    "snapshot, status. "
    "Irreversible actions are proposed and wait for your confirmation."
)


# ---------------------------------------------------------------------------
# Autonomy gate
# ---------------------------------------------------------------------------

def decide(action_class: str, level: int, confidence: float) -> tuple:
    """Return (decision, reason). Decisions: allow, ask, suggest, deny."""
    if action_class == "safety":
        return "deny", "Safety-surface changes are never autonomous, at any level."
    if confidence < CONFIDENCE_THRESHOLD:
        return "ask", (
            f"Intent confidence {confidence:.2f} is below {CONFIDENCE_THRESHOLD:.2f}; "
            "a guess is not permission."
        )
    if action_class == "read_only":
        return "allow", "Read-only work changes nothing."
    if action_class == "irreversible":
        return "ask", "Irreversible actions are proposed, never executed unattended."
    if action_class == "external_send":
        if level >= 3:
            return "allow", "External sends are allowed from level 3."
        if level == 2:
            return "ask", "External sends are confirmed at level 2."
        return "suggest", "Level 1 only suggests external sends."
    if level >= 2:
        return "allow", "Reversible local change, allowed from level 2."
    return "suggest", "Level 1 suggests only; it does not execute."


# ---------------------------------------------------------------------------
# Intent parsing
# ---------------------------------------------------------------------------

@dataclass
class Intent:
    action: str
    args: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.9
    description: str = ""


FOLD_RE = re.compile(r"\b(?:fold|symmetry|wedges?)\b\D{0,14}(\d{1,2})", re.I)
FOLD_SUFFIX_RE = re.compile(r"\b(\d{1,2})\s*[- ]?fold\b", re.I)
SPEED_RE = re.compile(r"\bspeed\b\D{0,14}(-?\d+(?:\.\d+)?)", re.I)
SPEED_UNIT_RE = re.compile(
    r"\b(?:spin|rotate|turn)\b\D{0,14}(-?\d+(?:\.\d+)?)\s*(?:°|deg|degrees)?\s*(?:/\s*s|per\s*sec|dps)\b",
    re.I,
)
ANGLE_RE = re.compile(r"\b(?:angle|rotation|rotate|turn|orient)\b\D{0,14}(\d{1,3})\b", re.I)
REFRACTION_RE = re.compile(r"\brefraction\b\D{0,14}(\d{1,3})", re.I)
ASSIGN_RE = re.compile(
    r"\b(?:assign|put|show|set|display|switch)\s+(?P<feed>.+?)\s+"
    r"(?:on|in|to|into|onto)\s+(?:monitor|panel|wedge|screen|sector)\s*(?P<idx>\d{1,2})\b",
    re.I,
)
CLEAR_RE = re.compile(
    r"\b(?:clear|empty|unassign|blank)\s+(?:monitor|panel|wedge|screen|sector)\s*(?P<idx>\d{1,2})\b",
    re.I,
)

FEED_ALIASES: Dict[str, List[str]] = {
    "camflow": ["camflow", "phone", "camera", "kaleidophone", "live camera"],
    "nasa_terra": ["terra", "modis terra", "gibs terra"],
    "nasa_aqua": ["aqua", "modis aqua", "gibs aqua"],
    "nasa_viirs": ["viirs", "snpp", "gibs viirs"],
    "nasa_epic": ["epic", "dscovr", "full disc", "full-disc", "earth disc"],
    "nasa_live": ["nasa live", "iss", "space station", "nasa tv"],
    "rtsp": ["rtsp", "onvif", "ip camera", "security camera"],
    "web": ["web page", "webpage", "iframe", "web"],
}


def match_feed(phrase: str) -> tuple:
    """Return (feed_id or None, confidence) for a free-text feed phrase."""
    phrase = re.sub(r"\s+", " ", str(phrase or "").strip().lower())
    if not phrase:
        return None, 0.0

    best_id, best_score = None, 0.0
    for feed in FEED_CATALOG:
        names = [feed["id"].replace("_", " "), feed["label"].lower()]
        names.extend(FEED_ALIASES.get(feed["id"], []))
        for name in names:
            name = name.lower()
            if phrase == name:
                score = 0.95
            elif name and (name in phrase or phrase in name):
                score = 0.8
            else:
                continue
            if score > best_score:
                best_id, best_score = feed["id"], score

    if best_id:
        return best_id, best_score

    # Last resort: token overlap, deliberately below the confidence gate.
    tokens = set(re.findall(r"[a-z0-9]+", phrase))
    for feed in FEED_CATALOG:
        haystack = " ".join([feed["label"].lower()] + FEED_ALIASES.get(feed["id"], []))
        if tokens & set(re.findall(r"[a-z0-9]+", haystack)):
            return feed["id"], 0.4
    return None, 0.0


class IntentParser:
    def parse(self, message: str) -> Optional[Intent]:
        text = re.sub(r"\s+", " ", str(message or "").strip())
        low = text.lower()
        if not low:
            return None

        # Safety surface first: it must never be shadowed by another rule.
        if re.search(r"\b(?:autonomy|safety)\b", low) and re.search(
            r"\b(?:level|permission|permissions|rule|rules|guardrail|guardrails|disable|turn off)\b", low
        ):
            return Intent("set_level", {}, 0.9, "Change ARIIA's autonomy or safety settings")

        if re.search(r"\b(?:shut ?down|shut down|stop the server|power off)\b", low):
            return Intent("shutdown", {}, 0.95, "Shut the Sentinel server down")

        if re.search(r"\bclear (?:the )?audit\b", low):
            return Intent("clear_audit", {}, 0.95, "Clear the ARIIA decision audit")

        if re.search(r"\b(?:help|what can you do|commands)\b", low):
            return Intent("help", {}, 0.95, "Explain the available commands")

        if re.search(r"\b(?:find|discover|scan for|search for|look for)\b.*\bcameras?\b", low) or re.search(
            r"\b(?:onvif|ip cameras?)\b.*\b(?:discover|scan|find)\b", low
        ):
            return Intent("discover_cameras", {}, 0.9, "Discover ONVIF cameras on the network")

        if re.search(r"\b(?:status|briefing|summary|what(?:'s| is) (?:showing|on the dome)|overview)\b", low):
            return Intent("status", {}, 0.9, "Report the dome status")

        if re.search(r"\b(?:brief me|briefing|situation report|what should i do)\b", low):
            return Intent("brief", {}, 0.85, "Give a short briefing")

        if re.search(r"\b(?:start|begin|resume|turn on)\b.*\bingest\b", low):
            return Intent("ingest_on", {}, 0.95, "Enable CamFlow ingest")
        if re.search(r"\b(?:stop|disable|turn off|end)\b.*\bingest\b", low):
            return Intent("ingest_off", {}, 0.95, "Disable CamFlow ingest")

        if re.search(r"\b(?:start|begin)\b.*\brecord", low):
            return Intent("record_start", {}, 0.95, "Start recording")
        if re.search(r"\b(?:stop|end|halt)\b.*\brecord", low):
            return Intent("record_stop", {}, 0.95, "Stop recording")

        if re.search(r"\bsnapshot\b|\btake a (?:photo|picture|snap)\b", low):
            return Intent("snapshot", {}, 0.9, "Save a snapshot of the live frame")

        if re.search(r"\b(?:pause|freeze|hold|stop rotating|stop the rotation|stop spinning)\b", low):
            return Intent("set_paused", {"paused": True}, 0.95, "Pause dome rotation")
        if re.search(r"\b(?:resume|unpause|continue|start rotating|start spinning|rotate again)\b", low):
            return Intent("set_paused", {"paused": False}, 0.95, "Resume dome rotation")

        match = REFRACTION_RE.search(text)
        if match:
            return Intent("set_refraction", {"refraction": int(match.group(1))}, 0.9,
                          f"Set refraction to {match.group(1)}%")

        match = SPEED_RE.search(text) or SPEED_UNIT_RE.search(text)
        if match:
            return Intent("set_speed", {"speed_dps": float(match.group(1))}, 0.9,
                          f"Set rotation speed to {match.group(1)} degrees per second")

        match = FOLD_RE.search(text) or FOLD_SUFFIX_RE.search(text)
        if match:
            return Intent("set_fold", {"fold": int(match.group(1))}, 0.9,
                          f"Set the dome to {match.group(1)}-fold symmetry")

        match = ANGLE_RE.search(text)
        if match:
            return Intent("set_rotation", {"rotation_deg": float(match.group(1))}, 0.9,
                          f"Set the dome angle to {match.group(1)} degrees")

        match = ASSIGN_RE.search(text)
        if match:
            feed_id, confidence = match_feed(match.group("feed"))
            index = int(match.group("idx"))
            return Intent(
                "assign_feed",
                {"feed": feed_id or "", "index": index, "phrase": match.group("feed")},
                confidence,
                f"Show {match.group('feed').strip()} on monitor {index}",
            )

        match = CLEAR_RE.search(text)
        if match:
            return Intent("clear_monitor", {"index": int(match.group("idx"))}, 0.9,
                          f"Clear monitor {match.group('idx')}")

        if re.search(r"\b(?:arrange|populate|fill|spread|distribute|assign all)\b", low):
            return Intent("arrange", {}, 0.9, "Spread the feed catalog across the monitors")

        return None


# ---------------------------------------------------------------------------
# Provider routing
# ---------------------------------------------------------------------------

class RouterError(Exception):
    """No provider produced an answer."""


class TransientError(RouterError):
    """Worth retrying or falling back: timeout, 429, 5xx."""


class RequestError(RouterError):
    """The request itself is wrong; another vendor would reject it too."""


class ConfigError(RouterError):
    """This provider cannot be used: missing key, model or transport."""


@dataclass
class Provider:
    name: str
    model: str
    kind: str
    base_url: str
    api_key: str
    timeout_s: float = 30.0
    max_retries: int = 1


@dataclass
class RouteResult:
    text: str
    provider: str
    model: str
    attempts: List[Dict[str, str]]
    latency_ms: int


def _chat_url(provider: Provider) -> str:
    base = (provider.base_url or "").strip().rstrip("/")
    if provider.kind == "ark":
        return f"{base or 'https://ark.cn-beijing.volces.com/api/v3'}/chat/completions"
    if not base:
        raise ConfigError(f"{provider.name}: base URL is required")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _http_chat(provider: Provider, messages: Sequence[Dict[str, str]]) -> str:
    if not provider.api_key:
        raise ConfigError(f"{provider.name}: no API key")
    if not provider.model:
        raise ConfigError(f"{provider.name}: no model configured")

    payload = {"model": provider.model, "messages": list(messages)}
    request = urllib.request.Request(
        _chat_url(provider),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {provider.api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=provider.timeout_s) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        if exc.code == 429 or exc.code >= 500:
            raise TransientError(f"{provider.name}: HTTP {exc.code} {detail}") from exc
        raise RequestError(f"{provider.name}: HTTP {exc.code} {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TransientError(f"{provider.name}: {exc}") from exc

    try:
        data = json.loads(body)
        content = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise RequestError(f"{provider.name}: unexpected response shape") from exc
    if isinstance(content, list):
        content = " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return str(content).strip()


class ProviderRouter:
    """Ordered fallback, retry, circuit breaker and provenance."""

    def __init__(
        self,
        providers: Sequence[Provider],
        transport: Optional[Callable[[Provider, Sequence[Dict[str, str]]], str]] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.providers = list(providers)
        self._transport = transport or _http_chat
        self._clock = clock
        self._circuits: Dict[str, Dict[str, float]] = {}
        self.log: List[Dict[str, Any]] = []

    def chat(self, messages: Sequence[Dict[str, str]]) -> RouteResult:
        if not self.providers:
            raise ConfigError("no AI provider is configured")

        started = self._clock()
        attempts: List[Dict[str, str]] = []
        last_transient: Optional[TransientError] = None

        for provider in self.providers:
            circuit = self._circuit(provider.name)
            if circuit.get("open_until", 0) > self._clock():
                attempts.append({"provider": provider.name, "outcome": "circuit_open"})
                continue

            for attempt in range(provider.max_retries + 1):
                try:
                    text = self._transport(provider, messages)
                except RequestError as exc:
                    attempts.append({"provider": provider.name, "outcome": "rejected", "detail": str(exc)})
                    self._log(attempts, started, None)
                    raise
                except ConfigError as exc:
                    attempts.append({"provider": provider.name, "outcome": "unconfigured", "detail": str(exc)})
                    break
                except TransientError as exc:
                    attempts.append({"provider": provider.name, "outcome": "transient", "detail": str(exc)})
                    last_transient = exc
                    circuit["failures"] = circuit.get("failures", 0) + 1
                    if circuit["failures"] >= 3:
                        circuit["open_until"] = self._clock() + 45
                        circuit["failures"] = 0
                    if attempt < provider.max_retries:
                        continue
                    break
                else:
                    circuit["failures"] = 0
                    attempts.append({"provider": provider.name, "outcome": "ok"})
                    result = RouteResult(
                        text=text,
                        provider=provider.name,
                        model=provider.model,
                        attempts=attempts,
                        latency_ms=int((self._clock() - started) * 1000),
                    )
                    self._log(attempts, started, result)
                    return result

        self._log(attempts, started, None)
        detail = f" Last transient: {last_transient}" if last_transient else ""
        raise RouterError(f"No provider answered.{detail}")

    def _circuit(self, name: str) -> Dict[str, float]:
        return self._circuits.setdefault(name, {"failures": 0, "open_until": 0})

    def _log(self, attempts, started, result) -> None:
        self.log.append(
            {
                "attempts": [(item["provider"], item["outcome"]) for item in attempts],
                "ok": result is not None,
                "provider": result.provider if result else None,
                "model": result.model if result else None,
                "latency_ms": int((self._clock() - started) * 1000),
            }
        )


def _env_key(names: Sequence[str]) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value.strip()
    return ""


def build_router(cfg: Dict[str, Any]) -> ProviderRouter:
    """Build the route from the dashboard's AI settings plus optional fallbacks.

    Fallbacks are opt-in: set ARIA_FALLBACK_PROVIDERS to a comma-separated list
    of `provider:model` entries. A provider without a key is skipped, loudly, at
    call time instead of silently substituting a default.
    """
    settings = resolve_provider_settings(cfg)
    providers: List[Provider] = []
    if settings["api_key"] and settings["model"]:
        providers.append(
            Provider(
                name=settings["provider"],
                model=settings["model"],
                kind=settings["kind"],
                base_url=settings["base_url"],
                api_key=settings["api_key"],
                timeout_s=float(settings["timeout_sec"]),
            )
        )

    for spec in os.environ.get("ARIA_FALLBACK_PROVIDERS", "").split(","):
        name, _, model = spec.strip().partition(":")
        name, model = name.strip().lower(), model.strip()
        preset = PROVIDER_PRESETS.get(name)
        if not preset or not model:
            continue
        key = _env_key(preset.get("env_keys") or [])
        if not key:
            continue
        providers.append(
            Provider(
                name=name,
                model=model,
                kind=preset["kind"],
                base_url=preset.get("base_url", ""),
                api_key=key,
            )
        )
    return ProviderRouter(providers)


# ---------------------------------------------------------------------------
# The assistant
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are ARIIA, the assistant inside the Kaleidophone dome dashboard for the Sentinel "
    "monitoring system. You answer questions about the dome, its monitors and its feeds. "
    "You cannot execute anything yourself: an autonomy gate in the dashboard runs commands. "
    "Never claim an action was executed. Available commands: set fold to 6/8/12/16/24, "
    "pause/resume, speed N, angle N, refraction N, arrange feeds, "
    "assign <feed> to monitor N, clear monitor N, discover ONVIF cameras, enable/disable ingest, "
    "start/stop recording, snapshot, status. Keep answers short and concrete."
)


class AriaAssistant:
    def __init__(
        self,
        dome,
        app_actions: Optional[Dict[str, Callable[[], Dict[str, Any]]]] = None,
        router_factory: Optional[Callable[[], ProviderRouter]] = None,
        audit_path: Optional[str] = None,
        logger=None,
        clock: Callable[[], float] = time.time,
    ):
        self.dome = dome
        self.app_actions = app_actions or {}
        self.router_factory = router_factory or (lambda: ProviderRouter([]))
        self.audit_path = audit_path
        self.logger = logger
        self._clock = clock
        self._lock = threading.RLock()
        self.level = 2
        self._audit: deque = deque(maxlen=200)
        self._proposals: Dict[str, Dict[str, Any]] = {}
        self._parser = IntentParser()
        self._load_audit()

    # -- status ---------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        router = self.router_factory()
        providers = [{"name": item.name, "model": item.model} for item in router.providers]
        return {
            "level": self.level,
            "levels": {str(key): value for key, value in LEVELS.items()},
            "providers": providers,
            "configured": bool(providers),
        }

    def set_level(self, level: int) -> int:
        """The user's own control, not an assistant action."""
        with self._lock:
            self.level = max(1, min(4, int(level)))
            return self.level

    def audit(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._audit)[: max(1, min(200, int(limit)))]

    def clear_audit(self) -> str:
        with self._lock:
            self._audit.clear()
        if self.audit_path and os.path.exists(self.audit_path):
            try:
                os.remove(self.audit_path)
            except OSError as exc:
                if self.logger:
                    self.logger.warning(f"aria audit clear failed: {exc}")
                return "Audit cleared in memory; the audit file could not be removed."
        return "Decision audit cleared."

    # -- conversation ---------------------------------------------------

    def chat(self, message: str, level: Optional[int] = None) -> Dict[str, Any]:
        text = str(message or "").strip()
        if not text:
            return {"ok": False, "error": "message is required"}

        with self._lock:
            if level is not None:
                self.level = max(1, min(4, int(level)))

            intent = self._parser.parse(text)
            if intent is None:
                return self._freeform(text)

            prepared, clarification = self._prepare(intent)
            if clarification:
                action_class = ACTION_CLASSES.get(intent.action, "safety")
                entry = self._record(intent, action_class, "ask", clarification)
                return {
                    "ok": True,
                    "reply": clarification,
                    "level": self.level,
                    "decisions": [entry],
                    "proposals": [],
                    "provenance": "local rules",
                }

            action_class = ACTION_CLASSES.get(prepared.action, "safety")
            decision, reason = decide(action_class, self.level, prepared.confidence)
            entry = self._record(prepared, action_class, decision, reason)
            proposals: List[Dict[str, Any]] = []

            if decision == "allow":
                reply = self._execute(prepared)
            elif decision == "ask" and action_class == "irreversible":
                proposal = self._propose(prepared)
                proposals.append(proposal)
                reply = (
                    f"{prepared.description}. That is irreversible, so I have not run it. "
                    "Confirm it below if you want that."
                )
            elif decision == "ask":
                reply = (
                    f"I need a clearer instruction before I act: {prepared.description}. {reason} "
                    "Name the feed exactly, for example 'assign NASA EPIC to monitor 3'."
                )
            elif decision == "suggest":
                reply = (
                    f"At level 1 I only suggest: {prepared.description}. "
                    "Raise autonomy to level 2 to let me run reversible work."
                )
            else:
                reply = f"I cannot do that: {reason}"

            return {
                "ok": True,
                "reply": reply,
                "level": self.level,
                "decisions": [entry],
                "proposals": proposals,
                "provenance": "local rules",
            }

    def confirm(self, proposal_id: str) -> Dict[str, Any]:
        with self._lock:
            proposal = self._proposals.pop(str(proposal_id or ""), None)
        if not proposal:
            return {"ok": False, "error": "proposal not found or expired"}
        if proposal["created"] + 600 < self._clock():
            return {"ok": False, "error": "proposal expired"}
        if ACTION_CLASSES.get(proposal["action"]) != "irreversible":
            return {"ok": False, "error": "only irreversible proposals need confirmation"}

        intent = Intent(
            proposal["action"], proposal.get("args") or {}, 1.0, proposal.get("description", "")
        )
        entry = self._record(intent, "irreversible", "confirmed", "The user confirmed this proposal.")
        message = self._execute(intent)
        return {"ok": True, "reply": message, "decisions": [entry]}

    # -- internals ------------------------------------------------------

    def _prepare(self, intent: Intent):
        """Resolve indices and validate values before the gate runs."""
        if intent.action in ("set_fold",) and intent.args.get("fold") not in FOLD_CHOICES:
            return intent, (
                f"{intent.args.get('fold')} is not a fold choice. Pick one of "
                f"{', '.join(str(choice) for choice in FOLD_CHOICES)}."
            )

        if intent.action in ("set_speed",) and abs(float(intent.args.get("speed_dps", 0))) > SPEED_LIMIT_DPS:
            return intent, f"Speed must stay within +/-{SPEED_LIMIT_DPS:.0f} degrees per second."

        if intent.action == "assign_feed":
            if not intent.args.get("feed"):
                return intent, (
                    "Which feed did you mean? I have: "
                    + ", ".join(feed["label"] for feed in FEED_CATALOG)
                    + "."
                )
            monitor_id, error = self._monitor_id(intent.args.get("index"))
            if error:
                return intent, error
            intent.args["monitor_id"] = monitor_id

        if intent.action == "clear_monitor":
            monitor_id, error = self._monitor_id(intent.args.get("index"))
            if error:
                return intent, error
            intent.args["monitor_id"] = monitor_id

        return intent, None

    def _monitor_id(self, index: Any):
        state = self.dome.snapshot()
        try:
            number = int(index)
        except (TypeError, ValueError):
            return None, "Which monitor number did you mean?"
        if number < 1 or number > len(state["monitors"]):
            return None, f"This dome has {len(state['monitors'])} monitors; pick 1 to {len(state['monitors'])}."
        return f"m{number}", None

    def _execute(self, intent: Intent) -> str:
        try:
            if intent.action in DOME_ACTIONS:
                return self.dome.apply_action(intent.action, intent.args)
            if intent.action == "status":
                return self._status_text()
            if intent.action == "brief":
                return self._brief_text()
            if intent.action == "help":
                return HELP_TEXT
            if intent.action in self.app_actions:
                result = self.app_actions[intent.action]() or {}
                return str(result.get("message") or "Done.")
        except KeyError:
            return "That monitor does not exist any more."
        except ValueError as exc:
            return f"I could not do that: {exc}"
        except Exception as exc:  # keep the chat usable on an unexpected fault
            if self.logger:
                self.logger.warning(f"aria action failed: {exc}")
            return f"I could not do that: {exc}"
        return "I do not have an action for that."

    def _status_text(self) -> str:
        state = self.dome.snapshot()
        assigned = sum(1 for monitor in state["monitors"] if monitor["feed"])
        motion = "paused" if state["paused"] else f"{state['speed_dps']} deg/s"
        return (
            f"{state['fold']}-fold dome, {state['sector_angle']} degree sectors, "
            f"{assigned}/{len(state['monitors'])} monitors assigned, rotation {motion}, "
            f"angle {state['rotation_deg']} degrees, refraction {state['refraction']}%."
        )

    def _brief_text(self) -> str:
        state = self.dome.snapshot()
        empty = [monitor["id"] for monitor in state["monitors"] if not monitor["feed"]]
        feeds = sorted({monitor["feed"] for monitor in state["monitors"] if monitor["feed"]})
        lines = [self._status_text()]
        if empty:
            lines.append(f"Unassigned monitors: {', '.join(empty)}. 'arrange feeds' fills them.")
        else:
            lines.append("Every monitor has a feed.")
        if len(feeds) < 3:
            lines.append("The dome repeats few feeds; try 'arrange feeds' for more variety.")
        return " ".join(lines)

    def _freeform(self, text: str) -> Dict[str, Any]:
        router = self.router_factory()
        if not router.providers:
            return {
                "ok": True,
                "reply": (
                    "No model key is configured, so I am running local rules only. " + HELP_TEXT
                ),
                "level": self.level,
                "decisions": [],
                "proposals": [],
                "provenance": "local rules",
            }

        messages = [
            {"role": "system", "content": f"{SYSTEM_PROMPT}\n\nDome context: {self._context()}"},
            {"role": "user", "content": text},
        ]
        try:
            result = router.chat(messages)
        except RouterError as exc:
            return {
                "ok": True,
                "reply": f"No model answered ({exc}). I can still run dome commands locally. {HELP_TEXT}",
                "level": self.level,
                "decisions": [],
                "proposals": [],
                "provenance": "local rules",
            }
        return {
            "ok": True,
            "reply": result.text,
            "level": self.level,
            "decisions": [],
            "proposals": [],
            "provenance": f"{result.provider}/{result.model}",
        }

    def _context(self) -> str:
        state = self.dome.snapshot()
        feeds = [{"id": feed["id"], "label": feed["label"]} for feed in FEED_CATALOG]
        compact = {
            "fold": state["fold"],
            "sector_angle": state["sector_angle"],
            "paused": state["paused"],
            "speed_dps": state["speed_dps"],
            "angle": state["rotation_deg"],
            "refraction": state["refraction"],
            "monitors": [{"id": m["id"], "feed": m["feed"], "label": m["label"]} for m in state["monitors"]],
            "feeds": feeds,
        }
        return json.dumps(compact, ensure_ascii=False)

    # -- audit and proposals --------------------------------------------

    def _record(self, intent: Intent, action_class: str, decision: str, reason: str) -> Dict[str, Any]:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "level": self.level,
            "action": intent.action,
            "class": action_class,
            "decision": decision,
            "reason": reason,
            "confidence": round(float(intent.confidence), 2),
            "description": intent.description,
        }
        with self._lock:
            self._audit.appendleft(entry)
        self._append_audit(entry)
        return entry

    def _propose(self, intent: Intent) -> Dict[str, Any]:
        proposal = {
            "id": uuid.uuid4().hex[:10],
            "action": intent.action,
            "args": dict(intent.args),
            "description": intent.description,
            "created": self._clock(),
        }
        with self._lock:
            now = self._clock()
            self._proposals = {
                key: value
                for key, value in self._proposals.items()
                if value["created"] + 600 > now
            }
            self._proposals[proposal["id"]] = proposal
        return {key: proposal[key] for key in ("id", "action", "description")}

    def _append_audit(self, entry: Dict[str, Any]) -> None:
        if not self.audit_path:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.audit_path)), exist_ok=True)
            with open(self.audit_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:
            if self.logger:
                self.logger.warning(f"aria audit write failed: {exc}")

    def _load_audit(self) -> None:
        if not self.audit_path or not os.path.exists(self.audit_path):
            return
        try:
            with open(self.audit_path, "r", encoding="utf-8") as handle:
                lines = handle.readlines()[-200:]
            for line in reversed(lines):
                try:
                    self._audit.append(json.loads(line))
                except ValueError:
                    continue
        except Exception as exc:
            if self.logger:
                self.logger.warning(f"aria audit load failed: {exc}")
