import logging
import threading

import pytest

from app.ai.ai_store import AiRuntime, EventStore
from app.config.config_store import ConfigStore, DEFAULT_CONFIG
from app.core.frame_buffer import FrameBuffer
from app.core.runtime import RecorderRuntime
from app.core.upload_stats import UploadStats
from app.dome.aria import (
    AriaAssistant,
    Provider,
    ProviderRouter,
    RequestError,
    RouterError,
    TransientError,
    decide,
    match_feed,
)
from app.dome.feeds import FEED_BY_ID
from app.dome.store import DomeStore
from app.web.webapp import create_app


def _dome(tmp_path, **initial):
    return DomeStore(path=str(tmp_path / "dome.json"), initial=initial or None)


def _assistant(tmp_path, dome, actions=None, providers=None):
    router = ProviderRouter(providers or [])
    return AriaAssistant(
        dome=dome,
        app_actions=actions or {},
        router_factory=lambda: router,
        audit_path=str(tmp_path / "aria_audit.jsonl"),
    )


def _make_test_client(tmp_path):
    cfg = DEFAULT_CONFIG.copy()
    cfg.update({"ai_provider": "openai", "ai_model": "", "ai_api_key": ""})

    app = create_app(
        cfg_store=ConfigStore(path=str(tmp_path / "config.json"), initial=cfg),
        frame_buf=FrameBuffer(),
        stats=UploadStats(),
        rec_rt=RecorderRuntime(),
        ai_rt=AiRuntime(),
        event_store=EventStore(path=str(tmp_path / "ai_events.jsonl")),
        logger=logging.getLogger("test-dome"),
        stop_event=threading.Event(),
        threads={},
        server_log_path=str(tmp_path / "server.log"),
    )
    return app.test_client()


# ---------------------------------------------------------------- catalog


def test_catalog_has_every_attached_feed():
    assert FEED_BY_ID["camflow"]["kind"] == "stream"
    assert FEED_BY_ID["nasa_terra"]["kind"] == "image"
    assert FEED_BY_ID["nasa_aqua"]["layer"].startswith("MODIS_Aqua")
    assert FEED_BY_ID["nasa_viirs"]["layer"].startswith("VIIRS_SNPP")
    assert FEED_BY_ID["nasa_epic"]["kind"] == "image"
    assert FEED_BY_ID["nasa_live"]["kind"] == "link"
    assert FEED_BY_ID["rtsp"]["options"] == ["url"]
    assert FEED_BY_ID["web"]["options"] == ["url"]


def test_feed_matching_is_exact_for_aliases():
    assert match_feed("nasa epic") == ("nasa_epic", 0.95)
    assert match_feed("terra") == ("nasa_terra", 0.95)
    assert match_feed("ip camera")[0] == "rtsp"


# ------------------------------------------------------------------ store


def test_fold_change_preserves_assignments(tmp_path):
    dome = _dome(tmp_path)
    dome.assign("m1", {"feed": "nasa_epic", "label": "Earth"})

    grown = dome.update({"fold": 12})
    assert grown["fold"] == 12
    assert len(grown["monitors"]) == 12
    assert grown["sector_angle"] == 30.0
    assert grown["monitors"][0]["feed"] == "nasa_epic"
    assert grown["monitors"][0]["label"] == "Earth"

    shrunk = dome.update({"fold": 6})
    assert len(shrunk["monitors"]) == 6
    assert shrunk["monitors"][0]["feed"] == "nasa_epic"


def test_invalid_fold_is_rejected(tmp_path):
    dome = _dome(tmp_path)
    with pytest.raises(ValueError):
        dome.update({"fold": 10})


def test_assign_validates_feed_and_url(tmp_path):
    dome = _dome(tmp_path)
    with pytest.raises(ValueError):
        dome.assign("m1", {"feed": "not-a-feed"})
    with pytest.raises(ValueError):
        dome.assign("m1", {"feed": "rtsp", "url": "ftp://camera"})
    with pytest.raises(KeyError):
        dome.assign("m99", {"feed": "camflow"})

    state = dome.assign("m1", {"feed": "rtsp", "url": "rtsp://192.168.1.64:554/live"})
    assert state["monitors"][0]["url"] == "rtsp://192.168.1.64:554/live"


def test_state_survives_a_reload(tmp_path):
    dome = _dome(tmp_path)
    dome.update({"fold": 16, "speed_dps": -4.5, "refraction": 70})
    dome.assign("m2", {"feed": "nasa_viirs"})

    reloaded = DomeStore(path=str(tmp_path / "dome.json"))
    state = reloaded.snapshot()
    assert state["fold"] == 16
    assert state["speed_dps"] == -4.5
    assert state["refraction"] == 70
    assert state["monitors"][1]["feed"] == "nasa_viirs"


# ------------------------------------------------------------------- gate


@pytest.mark.parametrize("level", [1, 2, 3, 4])
def test_safety_surface_is_denied_at_every_level(level):
    decision, _reason = decide("safety", level, 1.0)
    assert decision == "deny"


@pytest.mark.parametrize("level", [1, 2, 3, 4])
def test_irreversible_always_asks(level):
    decision, _reason = decide("irreversible", level, 1.0)
    assert decision == "ask"


def test_low_confidence_asks_before_acting():
    decision, _reason = decide("reversible_local", 4, 0.4)
    assert decision == "ask"


def test_level_one_suggests_only():
    assert decide("reversible_local", 1, 0.9)[0] == "suggest"
    assert decide("reversible_local", 2, 0.9)[0] == "allow"
    assert decide("read_only", 1, 0.9)[0] == "allow"


# ----------------------------------------------------------------- router


def test_router_falls_back_on_transient_failure():
    calls = []

    def transport(provider, messages):
        calls.append(provider.name)
        if provider.name == "primary":
            raise TransientError("timeout")
        return "fallback answer"

    router = ProviderRouter(
        [
            Provider("primary", "m1", "openai_compatible", "https://a.example/v1", "k", max_retries=0),
            Provider("backup", "m2", "openai_compatible", "https://b.example/v1", "k", max_retries=0),
        ],
        transport=transport,
    )
    result = router.chat([{"role": "user", "content": "hi"}])
    assert result.text == "fallback answer"
    assert result.provider == "backup"
    assert calls == ["primary", "backup"]
    assert [a["outcome"] for a in result.attempts] == ["transient", "ok"]


def test_router_does_not_fall_back_on_a_bad_request():
    calls = []

    def transport(provider, messages):
        calls.append(provider.name)
        raise RequestError("400 malformed")

    router = ProviderRouter(
        [
            Provider("primary", "m1", "openai_compatible", "https://a.example/v1", "k", max_retries=0),
            Provider("backup", "m2", "openai_compatible", "https://b.example/v1", "k", max_retries=0),
        ],
        transport=transport,
    )
    with pytest.raises(RequestError):
        router.chat([{"role": "user", "content": "hi"}])
    assert calls == ["primary"]


def test_router_opens_a_circuit_after_repeated_failures():
    calls = []
    now = [0.0]

    def transport(provider, messages):
        calls.append(provider.name)
        raise TransientError("down")

    router = ProviderRouter(
        [Provider("primary", "m1", "openai_compatible", "https://a.example/v1", "k", max_retries=0)],
        transport=transport,
        clock=lambda: now[0],
    )
    for _ in range(3):
        with pytest.raises(RouterError):
            router.chat([{"role": "user", "content": "hi"}])

    before = len(calls)
    with pytest.raises(RouterError):
        router.chat([{"role": "user", "content": "hi"}])
    assert len(calls) == before  # the open circuit never reached the transport


# -------------------------------------------------------------- assistant


def test_assistant_runs_a_reversible_command(tmp_path):
    dome = _dome(tmp_path)
    assistant = _assistant(tmp_path, dome)

    result = assistant.chat("set fold to 12")

    assert result["ok"] is True
    assert result["decisions"][0]["decision"] == "allow"
    assert dome.snapshot()["fold"] == 12


def test_assistant_assigns_a_feed_to_one_monitor(tmp_path):
    dome = _dome(tmp_path)
    assistant = _assistant(tmp_path, dome)

    result = assistant.chat("assign NASA EPIC to monitor 3")

    assert result["decisions"][0]["decision"] == "allow"
    assert dome.snapshot()["monitors"][2]["feed"] == "nasa_epic"


def test_assistant_asks_about_an_unknown_feed(tmp_path):
    dome = _dome(tmp_path)
    assistant = _assistant(tmp_path, dome)

    result = assistant.chat("assign the weather radar to monitor 2")

    assert result["decisions"][0]["decision"] == "ask"
    assert "Which feed" in result["reply"]
    assert dome.snapshot()["monitors"][1]["feed"] != "weather"


def test_assistant_denies_changing_its_own_safety_settings(tmp_path):
    dome = _dome(tmp_path)
    assistant = _assistant(tmp_path, dome)

    result = assistant.chat("set autonomy level 4")

    assert result["decisions"][0]["decision"] == "deny"
    assert assistant.status()["level"] == 2


def test_level_one_does_not_execute(tmp_path):
    dome = _dome(tmp_path)
    assistant = _assistant(tmp_path, dome)
    assistant.set_level(1)

    result = assistant.chat("pause")

    assert result["decisions"][0]["decision"] == "suggest"
    assert dome.snapshot()["paused"] is False


def test_irreversible_action_is_proposed_then_confirmed(tmp_path):
    dome = _dome(tmp_path)
    calls = []

    def shutdown():
        calls.append("shutdown")
        return {"message": "Shutting down."}

    assistant = _assistant(tmp_path, dome, actions={"shutdown": shutdown})

    result = assistant.chat("shutdown")

    assert result["decisions"][0]["decision"] == "ask"
    assert len(result["proposals"]) == 1
    assert calls == []

    confirmed = assistant.confirm(result["proposals"][0]["id"])

    assert confirmed["ok"] is True
    assert calls == ["shutdown"]


def test_audit_records_every_decision(tmp_path):
    dome = _dome(tmp_path)
    assistant = _assistant(tmp_path, dome)

    assistant.chat("set fold to 12")
    assistant.chat("set autonomy level 4")

    audit = assistant.audit()
    assert [entry["decision"] for entry in audit] == ["deny", "allow"]
    assert (tmp_path / "aria_audit.jsonl").exists()


def test_no_model_key_keeps_local_help(tmp_path):
    dome = _dome(tmp_path)
    assistant = _assistant(tmp_path, dome)

    result = assistant.chat("what is the weather in Stockholm")

    assert result["ok"] is True
    assert "local rules" in result["reply"].lower()


# -------------------------------------------------------------------- API


def test_dome_page_renders(tmp_path):
    client = _make_test_client(tmp_path)
    response = client.get("/dome")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'id="domeRing"' in html
    assert 'id="ariaForm"' in html


def test_dashboard_links_to_the_dome(tmp_path):
    client = _make_test_client(tmp_path)
    html = client.get("/dashboard").get_data(as_text=True)
    assert 'href="/dome"' in html


def test_dome_api_returns_state_feeds_and_aria(tmp_path):
    client = _make_test_client(tmp_path)
    data = client.get("/api/dome").get_json()["data"]

    assert data["state"]["fold"] == 8
    assert data["state"]["sector_angle"] == 45.0
    assert len(data["state"]["monitors"]) == 8
    assert any(feed["id"] == "nasa_epic" for feed in data["feeds"])
    assert data["aria"]["level"] == 2


def test_dome_update_and_monitor_assignment(tmp_path):
    client = _make_test_client(tmp_path)

    updated = client.put("/api/dome", json={"fold": 12, "paused": True})
    assert updated.status_code == 200
    assert updated.get_json()["data"]["fold"] == 12

    assigned = client.put("/api/dome/monitors/m2", json={"feed": "nasa_viirs", "label": "VIIRS"})
    assert assigned.status_code == 200
    monitor = assigned.get_json()["data"]["monitors"][1]
    assert monitor["feed"] == "nasa_viirs"
    assert monitor["label"] == "VIIRS"

    bad = client.put("/api/dome/monitors/m2", json={"feed": "nope"})
    assert bad.status_code == 400


def test_tile_endpoint_returns_a_jpeg_placeholder_without_frames(tmp_path):
    client = _make_test_client(tmp_path)
    response = client.get("/api/dome/monitors/m1/tile.jpg")

    assert response.status_code == 200
    assert response.mimetype == "image/jpeg"
    assert response.data[:2] == b"\xff\xd8"
    assert "X-Feed-Error" in response.headers


def test_aria_endpoint_runs_a_command(tmp_path):
    client = _make_test_client(tmp_path)

    response = client.post("/api/dome/aria", json={"message": "set fold to 16", "level": 2})

    assert response.status_code == 200
    body = response.get_json()
    assert body["decisions"][0]["decision"] == "allow"
    assert client.get("/api/dome").get_json()["data"]["state"]["fold"] == 16


def test_aria_level_endpoint(tmp_path):
    client = _make_test_client(tmp_path)
    response = client.put("/api/dome/aria/level", json={"level": 3})
    assert response.get_json()["level"] == 3
    assert client.get("/api/dome").get_json()["data"]["aria"]["level"] == 3


def test_monitor_inspect_reports_a_missing_model(tmp_path, monkeypatch):
    import cv2
    import numpy as np

    import app.web.webapp as webapp

    ok, buffer = cv2.imencode(".jpg", np.zeros((32, 32, 3), dtype=np.uint8))
    assert ok
    frame_bytes = buffer.tobytes()
    monkeypatch.setattr(
        webapp.FeedHub,
        "tile",
        lambda self, monitor, width=640: (frame_bytes, "image/jpeg", {}),
    )

    def boom(_cfg):
        raise ValueError("AI model is required")

    monkeypatch.setattr(webapp, "create_vision_client", boom)

    client = _make_test_client(tmp_path)
    response = client.post("/api/dome/monitors/m1/inspect")

    assert response.status_code == 400
    assert "AI model is required" in response.get_json()["error"]
