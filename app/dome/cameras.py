"""ONVIF camera discovery for the Kaleidophone dome.

An RTSP monitor needs a stream URL, and hunting for it in a router's admin page
is the slowest part of adding an IP camera. This module sends a WS-Discovery
Probe (the ONVIF standard, UDP multicast to 239.255.255.250:3702) and turns the
ProbeMatches into a short list of cameras the UI can offer.

Discovery is deliberately best-effort: it never raises at the API boundary, it
never scans ports, and it reports nothing rather than guessing when the server
is not on the camera's LAN (a container on a bridge network cannot see the
multicast group).
"""
from __future__ import annotations

import socket
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from typing import Any, Callable, Dict, List, Optional, Sequence

WS_DISCOVERY_GROUP = "239.255.255.250"
WS_DISCOVERY_PORT = 3702
MAX_CAMERAS = 16
MAX_RESPONSES = 64
DEFAULT_TIMEOUT_S = 1.5

PROBE_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
            xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
            xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
            xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <e:Header>
    <w:MessageID>uuid:{message_id}</w:MessageID>
    <w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
    <w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
  </e:Header>
  <e:Body>
    <d:Probe>
      <d:Types>dn:NetworkVideoTransmitter</d:Types>
    </d:Probe>
  </e:Body>
</e:Envelope>"""


def build_probe(message_id: Optional[str] = None) -> bytes:
    return PROBE_TEMPLATE.format(message_id=message_id or uuid.uuid4().hex).encode("utf-8")


def _local_name(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1].lower()


def _scope_map(scopes: str) -> Dict[str, str]:
    """Turn `onvif://www.onvif.org/name/Front%20Door` into {"name": "Front Door"}."""
    found: Dict[str, str] = {}
    for scope in str(scopes or "").split():
        parts = urllib.parse.urlsplit(scope)
        path = [urllib.parse.unquote(part) for part in parts.path.strip("/").split("/") if part]
        if parts.netloc:
            path.insert(0, parts.netloc)
        if len(path) >= 2:
            found.setdefault(path[-2].lower(), path[-1])
    return found


def parse_probe_match(xml_text: str) -> Optional[Dict[str, Any]]:
    """Parse one WS-Discovery ProbeMatch response; None when it is not one."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    endpoint = ""
    xaddrs: List[str] = []
    scopes = ""
    for node in root.iter():
        name = _local_name(node.tag)
        text = (node.text or "").strip()
        if not text:
            continue
        if name == "address" and not endpoint:
            endpoint = text
        elif name == "xaddrs":
            xaddrs.extend(text.split())
        elif name == "scopes":
            scopes = text

    if not endpoint and not xaddrs:
        return None

    rtsp = [item for item in xaddrs if item.lower().startswith("rtsp://")]
    host = ""
    for candidate in rtsp + xaddrs:
        host = urllib.parse.urlsplit(candidate).hostname or ""
        if host:
            break

    scope_map = _scope_map(scopes)
    name = scope_map.get("name") or scope_map.get("hardware") or host or endpoint
    hints = list(rtsp)
    if host and f"rtsp://{host}:554/" not in hints:
        hints.append(f"rtsp://{host}:554/")

    return {
        "name": name[:80],
        "host": host,
        "endpoint": endpoint,
        "xaddrs": xaddrs,
        "rtsp_hints": hints,
        "scopes": scope_map,
    }


def _multicast_transport(payload: bytes, timeout: float) -> List[str]:
    """Send the probe to the discovery group and collect raw responses."""
    responses: List[str] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(0.25)
        sock.bind(("", 0))
        sock.sendto(payload, (WS_DISCOVERY_GROUP, WS_DISCOVERY_PORT))

        deadline = time.monotonic() + max(0.1, timeout)
        while time.monotonic() < deadline and len(responses) < MAX_RESPONSES:
            try:
                data, _address = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            responses.append(data.decode("utf-8", "replace"))
    finally:
        sock.close()
    return responses


def discover_cameras(
    timeout: float = DEFAULT_TIMEOUT_S,
    transport: Optional[Callable[[bytes, float], Sequence[str]]] = None,
    logger=None,
) -> List[Dict[str, Any]]:
    """Return the ONVIF cameras that answered, de-duplicated and name-sorted."""
    timeout = max(0.2, min(4.0, float(timeout)))
    sender = transport or _multicast_transport
    try:
        responses = sender(build_probe(), timeout)
    except OSError as exc:
        if logger:
            logger.warning(f"ONVIF discovery failed: {exc}")
        return []

    cameras: List[Dict[str, Any]] = []
    seen = set()
    for raw in responses or []:
        camera = parse_probe_match(raw)
        if not camera:
            continue
        key = camera["endpoint"] or camera["host"] or camera["name"]
        if key in seen:
            continue
        seen.add(key)
        cameras.append(camera)
        if len(cameras) >= MAX_CAMERAS:
            break

    cameras.sort(key=lambda item: (item["name"].lower(), item["host"]))
    if logger:
        logger.info(f"ONVIF discovery found {len(cameras)} camera(s)")
    return cameras
