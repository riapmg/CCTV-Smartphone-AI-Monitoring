# pc/app/web/webapp.py
import os
import time
import threading
from datetime import datetime

import cv2
import numpy as np
from flask import Flask, request, Response, jsonify, render_template

from app.config.config_store import validate_and_normalize
from app.ai.vision_client import client_signature, create_vision_client, safe_provider_info
from app.dome.aria import AriaAssistant, build_router
from app.dome.cameras import discover_cameras
from app.dome.feeds import FEED_CATALOG, FeedHub, live_url_for
from app.dome.store import DomeStore
from app.recorder.recorder_worker import stop_recorder
from app.version import RELEASE_DATE, SENTINEL_VERSION, version_info

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
SENSITIVE_CONFIG_KEYS = {"ai_api_key", "ark_api_key"}


def _redact_config_patch(patch: dict) -> dict:
    """Return a log-safe config patch without exposing provider credentials."""
    return {
        key: "******" if key in SENSITIVE_CONFIG_KEYS and value else value
        for key, value in patch.items()
    }


def create_app(cfg_store, frame_buf, stats, rec_rt, ai_rt, event_store, logger, stop_event, threads, server_log_path: str) -> Flask:
    """
    Flask app factory.

    IMPORTANT:
    - We keep all routes/JSON fields identical to your original version.
    - We explicitly bind template/static folders so moving files will not break the UI.
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    templates_dir = os.path.join(base_dir, "templates")
    static_dir = os.path.join(base_dir, "static")

    app = Flask(__name__, template_folder=templates_dir, static_folder=static_dir)
    # Leave room for multipart metadata while enforcing an explicit image limit below.
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES + 1024 * 1024

    # ------------------------------------------------------------------
    # Kaleidophone dome + ARIIA assistant
    # ------------------------------------------------------------------
    dome_path = os.path.join(os.path.dirname(os.path.abspath(cfg_store.path)), "dome.json")
    dome_store = DomeStore(path=dome_path, logger=logger)
    feed_hub = FeedHub(frame_buf)

    def _set_ingest(enabled: bool) -> bool:
        cfg_store.set_key("ingest_enabled", bool(enabled))
        if not enabled:
            # Clear the last frame so /stream goes back to the placeholder screen.
            frame_buf.clear()
        cfg = cfg_store.get_copy()
        if bool(cfg.get("autosave", True)):
            cfg_store.save_to_disk(logger)
        logger.info("ingest enabled" if enabled else "ingest disabled")
        return bool(enabled)

    def _set_recording(enabled: bool) -> bool:
        cfg_store.set_key("recording", bool(enabled))
        cfg = cfg_store.get_copy()
        if bool(cfg.get("autosave", True)):
            cfg_store.save_to_disk(logger)
        if enabled:
            with rec_rt.lock:
                rec_rt.recording_start_ts = time.time()
            logger.info("recording started")
        else:
            stop_recorder(rec_rt, logger)
            with rec_rt.lock:
                rec_rt.recording_start_ts = None
            logger.info("recording stopped")
        return bool(enabled)

    def _snapshot_path(base_root: str = "recordings"):
        now = datetime.now()
        snap_root = os.path.join(base_root, "snapshots")
        day_dir = os.path.join(snap_root, now.strftime("%Y%m%d"))
        os.makedirs(day_dir, exist_ok=True)
        fname = f"snapshot_{now.strftime('%Y%m%d_%H%M%S')}.jpg"
        return os.path.join(day_dir, fname)

    def _take_snapshot():
        frame = frame_buf.get_copy()
        if frame is None:
            return False, "no frame"
        cfg = cfg_store.get_copy()
        path = _snapshot_path(str(cfg.get("out_root", "recordings")))
        ok = cv2.imwrite(path, frame, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        if not ok:
            return False, "write failed"
        logger.info(f"snapshot saved: {path}")
        return True, path

    def _shutdown_now():
        logger.warning("Shutdown requested from web UI")

        # Graceful shutdown order
        try:
            cfg_store.set_key("recording", False)
            stop_recorder(rec_rt, logger)
            with rec_rt.lock:
                rec_rt.recording_start_ts = None
        except Exception as e:
            logger.warning(f"shutdown: stop recorder failed: {e}")

        try:
            cfg_store.set_key("ingest_enabled", False)
        except Exception as e:
            logger.warning(f"shutdown: disable ingest failed: {e}")

        # Save config if autosave
        try:
            cfg = cfg_store.get_copy()
            if bool(cfg.get("autosave", True)):
                cfg_store.save_to_disk(logger)
        except Exception as e:
            logger.warning(f"shutdown: save config failed: {e}")

        # Signal workers to exit
        try:
            stop_event.set()
        except Exception as e:
            logger.warning(f"shutdown: set stop_event failed: {e}")

        # Best-effort join so writers flush more cleanly
        try:
            for _, t in (threads or {}).items():
                if t and t.is_alive():
                    t.join(timeout=2.0)
        except Exception as e:
            logger.warning(f"shutdown: join threads failed: {e}")

        # Hard-exit fallback (avoid hanging)
        def _killer():
            time.sleep(0.2)
            os._exit(0)

        threading.Thread(target=_killer, daemon=True).start()

    def _aria_ingest_on():
        _set_ingest(True)
        return {"message": "CamFlow ingest enabled."}

    def _aria_ingest_off():
        _set_ingest(False)
        return {"message": "CamFlow ingest disabled."}

    def _aria_record_start():
        _set_recording(True)
        return {"message": "Recording started."}

    def _aria_record_stop():
        _set_recording(False)
        return {"message": "Recording stopped."}

    def _aria_snapshot():
        ok, detail = _take_snapshot()
        return {"message": f"Snapshot saved to {detail}." if ok else f"Snapshot failed: {detail}"}

    def _aria_shutdown():
        _shutdown_now()
        return {"message": "Shutting the Sentinel server down."}

    def _aria_clear_audit():
        return {"message": aria.clear_audit()}

    def _aria_discover_cameras():
        cameras = discover_cameras(timeout=1.5, logger=logger)
        if not cameras:
            return {
                "message": "No ONVIF camera answered. Discovery needs this server on the camera's "
                "LAN; otherwise enter the stream URL in the monitor panel."
            }
        listing = ", ".join(f"{item['name']} ({item['host'] or 'unknown host'})" for item in cameras)
        return {
            "message": f"Found {len(cameras)} ONVIF camera(s): {listing}. "
            "Pick one in a monitor's camera finder."
        }

    router_cache = {"signature": None, "router": None}

    def _aria_router():
        """Keep one router per AI configuration so circuit breakers survive calls."""
        cfg = cfg_store.get_copy()
        signature = f"{client_signature(cfg)}|{os.environ.get('ARIA_FALLBACK_PROVIDERS', '')}"
        if router_cache["signature"] != signature:
            router_cache["router"] = build_router(cfg)
            router_cache["signature"] = signature
        return router_cache["router"]

    aria = AriaAssistant(
        dome=dome_store,
        app_actions={
            "ingest_on": _aria_ingest_on,
            "ingest_off": _aria_ingest_off,
            "record_start": _aria_record_start,
            "record_stop": _aria_record_stop,
            "snapshot": _aria_snapshot,
            "shutdown": _aria_shutdown,
            "clear_audit": _aria_clear_audit,
            "discover_cameras": _aria_discover_cameras,
        },
        router_factory=_aria_router,
        audit_path=os.path.join(os.path.dirname(os.path.abspath(server_log_path)), "aria_audit.jsonl"),
        logger=logger,
    )

    # Disable Werkzeug access logs (keep your original behavior)
    import logging as _logging
    _logging.getLogger("werkzeug").disabled = True

    @app.errorhandler(413)
    def request_too_large(_):
        if request.path == "/upload":
            stats.mark_too_large()
            logger.warning("413 upload request too large")
            return "image too large", 413
        return "request too large", 413

    @app.get("/ping")
    def ping():
        return "OK"

    @app.post("/upload")
    def upload():
        cfg = cfg_store.get_copy()
        if not bool(cfg.get("ingest_enabled", False)):
            stats.mark_rejected()
            return "ingest disabled", 503

        f = request.files.get("image")
        if f is None:
            stats.mark_missing()
            logger.warning("400 missing image field")
            return "missing image", 400

        data = f.stream.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            stats.mark_too_large()
            logger.warning(f"413 image too large bytes>{MAX_UPLOAD_BYTES}")
            return "image too large", 413

        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            stats.mark_decode_failed()
            logger.warning(f"400 decode failed bytes={len(data)}")
            return "decode failed", 400

        frame_buf.set(img)
        stats.mark_ok()
        return "OK"

    def mjpeg_generator():
        while True:
            cfg = cfg_store.get_copy()
            stream_fps = int(cfg.get("stream_fps", 10))
            jpeg_quality = int(cfg.get("jpeg_quality", 80))
            ingest_enabled = bool(cfg.get("ingest_enabled", False))

            interval = 1.0 / max(1, stream_fps)
            time.sleep(interval)

            frame = frame_buf.get_copy()
            if frame is None:
                h, w = 360, 640
                frame = np.zeros((h, w, 3), dtype=np.uint8)
                msg = "Ingest OFF - enable in dashboard" if not ingest_enabled else "Waiting for frames..."
                cv2.putText(frame, msg, (20, 80), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)

            ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality])
            if not ok:
                continue
            jpg = buf.tobytes()
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(jpg)).encode() + b"\r\n\r\n" +
                jpg + b"\r\n"
            )

    @app.get("/stream")
    def stream():
        return Response(mjpeg_generator(), mimetype="multipart/x-mixed-replace; boundary=frame")

    @app.get("/")
    @app.get("/dashboard")
    def dashboard():
        return render_template(
            "dashboard.html",
            sentinel_version=SENTINEL_VERSION,
            release_date=RELEASE_DATE,
        )

    @app.get("/api/version")
    def api_version():
        return jsonify({"ok": True, "data": version_info()})

    @app.get("/api/config")
    def api_get_config():
        cfg = cfg_store.get_copy()
        # Do not send the real key back to the frontend
        if cfg.get("ark_api_key"):
            cfg["ark_api_key"] = "******"
        else:
            cfg["ark_api_key"] = ""
        if cfg.get("ai_api_key"):
            cfg["ai_api_key"] = "******"
        else:
            cfg["ai_api_key"] = ""
        return jsonify(cfg)

    @app.put("/api/config")
    def api_put_config():
        patch = request.get_json(silent=True) or {}

        # If frontend returns placeholder/empty value, treat it as "do not modify ark_api_key"
        if "ark_api_key" in patch:
            v = patch.get("ark_api_key")
            if v is None or str(v).strip() in ("", "******"):
                patch.pop("ark_api_key", None)
        if "ai_api_key" in patch:
            v = patch.get("ai_api_key")
            if v is None or str(v).strip() in ("", "******"):
                patch.pop("ai_api_key", None)

        ok, cleaned, err = validate_and_normalize(patch)
        if not ok:
            return jsonify({"ok": False, "error": err}), 400

        new_cfg = cfg_store.set_many(cleaned)
        autosave = bool(new_cfg.get("autosave", True))
        if autosave:
            cfg_store.save_to_disk(logger)

        # If recording is ON and key recording params changed, remind user to restart recording.
        changed_record_params = any(
            k in cleaned for k in ("record_fps", "segment_seconds", "out_root", "cam_name", "codec")
        )
        note = ""
        if bool(new_cfg.get("recording")) and changed_record_params:
            note = "Recording is ON. Some record params will fully apply after Stop+Start recording."

        logger.info(f"config updated: {_redact_config_patch(cleaned)}")
        return jsonify({"ok": True, "note": note})

    @app.post("/api/config/save")
    def api_save_config():
        cfg_store.save_to_disk(logger)
        return jsonify({"ok": True})

    @app.post("/api/config/load")
    def api_load_config():
        cfg_store.load_from_disk(logger)
        return jsonify({"ok": True})

    @app.post("/api/ingest/enable")
    def api_ingest_enable():
        _set_ingest(True)
        return jsonify({"ok": True, "ingest_enabled": True})

    @app.post("/api/ingest/disable")
    def api_ingest_disable():
        _set_ingest(False)
        return jsonify({"ok": True, "ingest_enabled": False})

    @app.post("/api/record/start")
    def api_record_start():
        _set_recording(True)
        return jsonify({"ok": True, "recording": True, "note": ""})

    @app.post("/api/record/stop")
    def api_record_stop():
        _set_recording(False)
        return jsonify({"ok": True, "recording": False})

    @app.post("/api/system/shutdown")
    def api_system_shutdown():
        _shutdown_now()
        return jsonify({"ok": True, "message": "shutting down"})

    @app.post("/api/snapshot")
    def api_snapshot():
        ok, detail = _take_snapshot()
        if not ok:
            return jsonify({"ok": False, "error": detail}), (409 if detail == "no frame" else 500)
        return jsonify({"ok": True, "path": detail})

    @app.get("/api/status")
    def api_status():
        cfg = cfg_store.get_copy()

        age = frame_buf.age_sec()
        counts = stats.snapshot_counts()
        upload_fps = round(stats.upload_fps(), 2)

        # recorder info
        with rec_rt.lock:
            opened = (rec_rt.rec is not None)
            recording_start_ts = rec_rt.recording_start_ts
            r = rec_rt.rec

        rec_file = None
        seg_remaining = None
        total_elapsed = None
        recording_codec = None

        if r is not None and getattr(r, "current_path", None) and getattr(r, "segment_start_ts", None):
            rec_file = r.current_path
            recording_codec = getattr(r, "active_codec", None)
            seg_elapsed = time.time() - r.segment_start_ts
            seg_remaining = max(0.0, float(cfg.get("segment_seconds", 60)) - seg_elapsed)

        if bool(cfg.get("recording")) and recording_start_ts is not None:
            total_elapsed = time.time() - recording_start_ts

        return jsonify({
            "sentinel_version": SENTINEL_VERSION,
            "ingest_enabled": bool(cfg.get("ingest_enabled")),
            "recording": bool(cfg.get("recording")),
            "stream_fps": int(cfg.get("stream_fps")),
            "jpeg_quality": int(cfg.get("jpeg_quality")),
            "record_fps": int(cfg.get("record_fps")),
            "segment_seconds": int(cfg.get("segment_seconds")),
            "out_root": cfg.get("out_root"),
            "cam_name": cfg.get("cam_name"),
            "codec": cfg.get("codec"),
            "last_frame_age_sec": age,
            "upload_fps": upload_fps,

            "recorder_opened": opened,
            "recording_file": rec_file,
            "recording_codec": recording_codec,
            "recording_elapsed_sec": None if total_elapsed is None else round(total_elapsed, 1),
            "segment_remaining_sec": None if seg_remaining is None else round(seg_remaining, 1),

            "upload_counts": counts
        })

    @app.get("/api/ai/status")
    def api_ai_status():
        # Return AI thread runtime state (SLEEP/OBSERVE, event id, dwell seconds, last model output, etc.)
        try:
            snap = ai_rt.snapshot()
            snap["model_info"] = safe_provider_info(cfg_store.get_copy())
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({"ok": True, "data": snap})

    @app.get("/api/ai/events")
    def api_ai_events():
        # ?n=50 returns latest N events (JSONL tail cache)
        n = request.args.get("n", "50")
        try:
            events = event_store.latest(int(n))
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({"ok": True, "data": events})

    @app.get("/api/log/tail")
    def api_log_tail():
        n = request.args.get("n", "200")
        try:
            n = max(50, min(int(n), 2000))
        except Exception:
            n = 200

        log_path = server_log_path
        if not os.path.exists(log_path):
            return jsonify({"ok": True, "data": []})

        try:
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()[-n:]
            return jsonify({"ok": True, "data": [ln.rstrip("\n") for ln in lines]})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    # ------------------------------------------------------------------
    # Kaleidophone dome page and API
    # ------------------------------------------------------------------
    def _dome_state():
        state = dome_store.snapshot()
        for monitor in state["monitors"]:
            monitor["live_url"] = live_url_for(monitor)
        return state

    @app.get("/dome")
    def dome_page():
        return render_template(
            "dome.html",
            sentinel_version=SENTINEL_VERSION,
            release_date=RELEASE_DATE,
        )

    @app.get("/api/dome")
    def api_dome():
        cfg = cfg_store.get_copy()
        return jsonify({
            "ok": True,
            "data": {
                "state": _dome_state(),
                "feeds": [dict(feed) for feed in FEED_CATALOG],
                "health": feed_hub.health(),
                "aria": aria.status(),
                "app": {
                    "ingest_enabled": bool(cfg.get("ingest_enabled")),
                    "recording": bool(cfg.get("recording")),
                    "ai_enabled": bool(cfg.get("ai_enabled")),
                },
            },
        })

    @app.get("/api/dome/feeds")
    def api_dome_feeds():
        return jsonify({
            "ok": True,
            "data": {
                "feeds": [dict(feed) for feed in FEED_CATALOG],
                "health": feed_hub.health(),
            },
        })

    @app.get("/api/dome/cameras/discover")
    def api_dome_cameras_discover():
        try:
            timeout = float(request.args.get("timeout", "1.5"))
        except (TypeError, ValueError):
            timeout = 1.5
        cameras = discover_cameras(timeout=timeout, logger=logger)
        return jsonify({
            "ok": True,
            "data": {
                "cameras": cameras,
                "note": (
                    "ONVIF discovery uses UDP multicast, so the server must share the camera's "
                    "LAN; otherwise enter the stream URL directly."
                ),
            },
        })

    @app.put("/api/dome")
    def api_dome_update():
        patch = request.get_json(silent=True) or {}
        try:
            state = dome_store.update(patch)
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        return jsonify({"ok": True, "data": _dome_state()})

    @app.put("/api/dome/monitors/<monitor_id>")
    def api_dome_assign(monitor_id):
        patch = request.get_json(silent=True) or {}
        try:
            state = dome_store.assign(monitor_id, patch)
        except KeyError:
            return jsonify({"ok": False, "error": "monitor not found"}), 404
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        for monitor in state["monitors"]:
            monitor["live_url"] = live_url_for(monitor)
        return jsonify({"ok": True, "data": state})

    @app.get("/api/dome/monitors/<monitor_id>/tile.jpg")
    def api_dome_tile(monitor_id):
        monitor = dome_store.monitor(monitor_id)
        if monitor is None:
            return "monitor not found", 404
        try:
            width = int(request.args.get("w", "640"))
        except (TypeError, ValueError):
            width = 640
        data, content_type, meta = feed_hub.tile(monitor, width=width)
        response = Response(data or b"", mimetype=content_type or "image/jpeg")
        response.headers["Cache-Control"] = "no-store"
        if meta.get("error"):
            safe_error = str(meta["error"]).encode("ascii", "ignore").decode("ascii")
            response.headers["X-Feed-Error"] = safe_error[:200]
        for header, key in (
            ("X-Feed-Source", "source"),
            ("X-Feed-Observed", "observation_date"),
            ("X-Feed-Captured", "captured"),
        ):
            if meta.get(key):
                response.headers[header] = str(meta[key]).encode("ascii", "ignore").decode("ascii")[:120]
        return response

    @app.post("/api/dome/monitors/<monitor_id>/snapshot")
    def api_dome_monitor_snapshot(monitor_id):
        monitor = dome_store.monitor(monitor_id)
        if monitor is None:
            return jsonify({"ok": False, "error": "monitor not found"}), 404
        data, _content_type, meta = feed_hub.tile(monitor, width=1280)
        if meta.get("error"):
            return jsonify({"ok": False, "error": meta["error"]}), 409
        now = datetime.now()
        directory = os.path.join("recordings", "snapshots", "dome", now.strftime("%Y%m%d"))
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{monitor_id}_{now.strftime('%H%M%S')}.jpg")
        with open(path, "wb") as handle:
            handle.write(data)
        logger.info(f"dome snapshot saved: {path}")
        return jsonify({"ok": True, "path": path})

    @app.post("/api/dome/monitors/<monitor_id>/inspect")
    def api_dome_monitor_inspect(monitor_id):
        monitor = dome_store.monitor(monitor_id)
        if monitor is None:
            return jsonify({"ok": False, "error": "monitor not found"}), 404
        data, _content_type, meta = feed_hub.tile(monitor, width=640)
        if meta.get("error"):
            return jsonify({"ok": False, "error": meta["error"]}), 409
        frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            return jsonify({"ok": False, "error": "tile could not be decoded"}), 409
        try:
            client = create_vision_client(cfg_store.get_copy())
            result = client.analyze_frame(
                frame,
                time_text=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                prompt_template="You are inspecting one tile of a monitoring dome. Describe what is visible.",
                jpeg_quality=85,
                max_w=640,
            )
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        return jsonify({"ok": True, "data": result})

    @app.post("/api/dome/aria")
    def api_dome_aria():
        body = request.get_json(silent=True) or {}
        message = str(body.get("message") or "").strip()
        if not message:
            return jsonify({"ok": False, "error": "message is required"}), 400
        level = body.get("level")
        try:
            level = int(level) if level is not None else None
        except (TypeError, ValueError):
            level = None
        return jsonify(aria.chat(message, level=level))

    @app.put("/api/dome/aria/level")
    def api_dome_aria_level():
        body = request.get_json(silent=True) or {}
        try:
            level = aria.set_level(int(body.get("level", 2)))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "level must be 1 to 4"}), 400
        return jsonify({"ok": True, "level": level})

    @app.post("/api/dome/aria/confirm")
    def api_dome_aria_confirm():
        body = request.get_json(silent=True) or {}
        return jsonify(aria.confirm(str(body.get("id") or "")))

    @app.get("/api/dome/aria/audit")
    def api_dome_aria_audit():
        n = request.args.get("n", "50")
        try:
            n = int(n)
        except (TypeError, ValueError):
            n = 50
        return jsonify({"ok": True, "data": aria.audit(n)})

    @app.delete("/api/dome/aria/audit")
    def api_dome_aria_audit_clear():
        return jsonify({"ok": True, "message": aria.clear_audit()})

    return app
