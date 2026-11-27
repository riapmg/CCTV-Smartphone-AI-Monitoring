# pc/server.py
import os
import threading

from werkzeug.serving import make_server

from app.core.logger import setup_logger
from app.core.frame_buffer import FrameBuffer
from app.core.upload_stats import UploadStats
from app.core.runtime import RecorderRuntime
from app.config.config_store import ConfigStore, DEFAULT_CONFIG
from app.recorder.recorder_worker import start_record_thread
from app.ai.ai_store import AiRuntime, EventStore
from app.ai.ai_monitor_worker import start_ai_monitor_thread
from app.web.webapp import create_app
from app.core.server_config import (
    DEFAULT_BIND_HOST,
    INGEST_TOKEN_ENV,
    resolve_listener_hosts,
    resolve_server_config,
)
from app.version import SENTINEL_VERSION

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "log")
APP_CONFIG_DIR = os.path.join(BASE_DIR, "app", "config")
SERVER_LOG_PATH = os.path.join(LOG_DIR, "server.log")
AI_EVENTS_PATH = os.path.join(LOG_DIR, "ai_events.jsonl")
CONFIG_PATH = os.path.join(APP_CONFIG_DIR, "config.json")

os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(APP_CONFIG_DIR, exist_ok=True)


def serve_on_resolved_interfaces(app, host: str, port: int) -> None:
    """Serve the dashboard on loopback and optional ingest on one LAN interface.

    Separate sockets preserve a local dashboard when the phone sender uses a
    selected private interface. The Flask request policy still blocks remote
    dashboard/control routes and requires the configured Bearer token for
    remote frame ingest.
    """
    listener_hosts = resolve_listener_hosts(host)
    servers = []
    lan_thread = None
    try:
        for bind_host in listener_hosts:
            servers.append(make_server(bind_host, port, app, threaded=True))

        if len(servers) == 1:
            servers[0].serve_forever()
            return

        lan_server = servers[1]
        lan_thread = threading.Thread(
            target=lan_server.serve_forever,
            name="camflow-lan-http",
            daemon=True,
        )
        lan_thread.start()
        try:
            servers[0].serve_forever()
        finally:
            lan_server.shutdown()
            lan_thread.join(timeout=3.0)
    finally:
        for http_server in servers:
            http_server.server_close()


def main():
    logger = setup_logger(SERVER_LOG_PATH)
    stop_event = threading.Event()
    # Dashboard/control routes remain loopback-only; LAN ingest requires a token or explicit legacy opt-in.
    try:
        host, port = resolve_server_config(os.environ)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    cfg_store = ConfigStore(path=CONFIG_PATH, initial=DEFAULT_CONFIG.copy())
    frame_buf = FrameBuffer()
    stats = UploadStats()
    rec_rt = RecorderRuntime()

    # Load config before starting the UI session.
    cfg_store.load_from_disk(logger)

    # Force-reset on startup: open the web UI first, then enable ingest/recording manually from dashboard.
    cfg_store.set_key("ingest_enabled", False)
    cfg_store.set_key("recording", False)
    cfg_store.save_to_disk(logger)

    # Background recorder thread (original code starts it again; recorder_worker has an internal guard).
    record_thread = start_record_thread(cfg_store, frame_buf, rec_rt, logger, stop_event)

    # AI runtime + event store
    ai_rt = AiRuntime()
    event_store = EventStore(path=AI_EVENTS_PATH)

    # Background AI monitor thread (triggered mode)
    ai_thread = start_ai_monitor_thread(cfg_store, frame_buf, ai_rt, event_store, logger, stop_event)

    threads = {"record": record_thread, "ai": ai_thread}

    app = create_app(
        cfg_store=cfg_store,
        frame_buf=frame_buf,
        stats=stats,
        rec_rt=rec_rt,
        ai_rt=ai_rt,
        event_store=event_store,
        logger=logger,
        stop_event=stop_event,
        threads=threads,
        server_log_path=SERVER_LOG_PATH,
    )

    url_local = f"http://127.0.0.1:{port}/"

    print("\\n====================================================================")
    print(f" Sentinel v{SENTINEL_VERSION} Server Started")
    print(f" Dashboard:  {url_local}            open on this PC")
    if host != DEFAULT_BIND_HOST:
        print(f" CamFlow:    {host}:{port}              enter this address manually in the app")
        if os.environ.get(INGEST_TOKEN_ENV):
            print(" LAN ingest: Bearer-token authentication enabled; enter the same token in CamFlow Settings.")
        else:
            print(" SECURITY WARNING: legacy unauthenticated LAN ingest is enabled by explicit opt-in.")
        print(" Dashboard/control stay on loopback; only the selected LAN interface serves phone ingest.")
        print(" Use only on a trusted, isolated Wi-Fi/LAN. HTTP is not encrypted; never expose or port-forward this server.")
    else:
        print(" LAN access: disabled (loopback only)")
        print(" To opt in securely, set SENTINEL_BIND_HOST to a private RFC1918 IPv4 address and")
        print(" CAMFLOW_INGEST_TOKEN to a random 32+ character token before starting.")
    print(" Default ingest: OFF (enable in dashboard)")
    print("====================================================================\\n", flush=True)

    serve_on_resolved_interfaces(app, host, port)


if __name__ == "__main__":
    main()
