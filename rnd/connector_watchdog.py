"""
Background helper for the R&D UI — keeps connector_api.py running while main.html is open.

Listens on http://127.0.0.1:5056 (does not conflict with the connector API on 5055).
Run once at logon (hidden), e.g.:
    pythonw connector_watchdog.py

Or install for the current user:
    powershell -ExecutionPolicy Bypass -File install_ui_watchdog.ps1
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

_RND_DIR = Path(__file__).resolve().parent
_API_SCRIPT = _RND_DIR / "connector_api.py"
_HOST = "127.0.0.1"
_API_PORT = 5055
_WATCHDOG_PORT = 5056
_IDLE_SECONDS = 45
_POLL_SECONDS = 8

log = logging.getLogger("datahive.connector_watchdog")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

_child: subprocess.Popen | None = None
_we_started_api = False
_tabs: dict[str, float] = {}
_lock = threading.Lock()


def _api_port_open() -> bool:
    try:
        with socket.create_connection((_HOST, _API_PORT), timeout=0.4):
            return True
    except OSError:
        return False


def _api_health_payload() -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(
            f"http://{_HOST}:{_API_PORT}/health",
            timeout=2,
        ) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def _api_query_log_ready() -> bool:
    payload = _api_health_payload()
    return bool(payload and payload.get("query_log_api"))


def _listener_pids(port: int) -> list[int]:
    pids: list[int] = []
    if sys.platform == "win32":
        try:
            out = subprocess.check_output(
                ["netstat", "-ano"], text=True, errors="ignore"
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            return pids
        token = f":{port}"
        for line in out.splitlines():
            if "LISTENING" not in line.upper() or token not in line:
                continue
            parts = line.split()
            if parts and parts[-1].isdigit():
                pids.append(int(parts[-1]))
    else:
        try:
            out = subprocess.check_output(["lsof", "-ti", f":{port}"], text=False)
            for part in out.decode().split():
                if part.isdigit():
                    pids.append(int(part))
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
    return list(dict.fromkeys(pids))


def _stop_listeners_on_port(port: int) -> None:
    for pid in _listener_pids(port):
        if pid == os.getpid():
            continue
        log.info("Stopping process %s on port %s", pid, port)
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True,
                check=False,
            )
        else:
            subprocess.run(["kill", str(pid)], capture_output=True, check=False)
    deadline = time.time() + 6
    while time.time() < deadline and _api_port_open():
        time.sleep(0.2)


def _start_api() -> None:
    global _child, _we_started_api
    with _lock:
        if _api_port_open():
            return
        if _child is not None and _child.poll() is None:
            return
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        _child = subprocess.Popen(
            [sys.executable, str(_API_SCRIPT)],
            cwd=str(_RND_DIR),
            creationflags=flags,
        )
        _we_started_api = True
        log.info("Started connector_api.py (pid %s)", _child.pid)


def _stop_api() -> None:
    global _child, _we_started_api
    with _lock:
        if not _we_started_api:
            return
        proc = _child
        _child = None
        _we_started_api = False
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.info("Stopped connector_api.py")


def _wait_for_api(timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _api_port_open():
            return True
        time.sleep(0.35)
    return _api_port_open()


def _tab_id(request: Request) -> str:
    header = request.headers.get("X-DataHive-Tab")
    if header and header.strip():
        return header.strip()[:128]
    return "default"


def _reap_idle_tabs() -> None:
    global _tabs
    now = time.time()
    with _lock:
        for tid, seen in list(_tabs.items()):
            if now - seen > _IDLE_SECONDS:
                del _tabs[tid]
        if not _tabs:
            _stop_api()


def _supervisor_loop() -> None:
    while True:
        time.sleep(_POLL_SECONDS)
        with _lock:
            has_tabs = bool(_tabs)
        if has_tabs and not _api_port_open():
            _start_api()
            _wait_for_api(timeout=15.0)
        _reap_idle_tabs()


app = FastAPI(title="DataHive UI Watchdog")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "api_port_open": _api_port_open(),
        "active_tabs": len(_tabs),
        "we_started_api": _we_started_api,
    }


@app.post("/ensure")
def ensure() -> dict:
    _start_api()
    up = _wait_for_api()
    return {"ok": up, "api_port_open": up}


@app.post("/presence")
async def presence(request: Request) -> dict:
    tid = _tab_id(request)
    with _lock:
        _tabs[tid] = time.time()
    if not _api_port_open():
        _start_api()
        _wait_for_api(timeout=15.0)
    return {"ok": True, "api_port_open": _api_port_open(), "tab": tid}


@app.post("/release")
async def release(request: Request) -> dict:
    tid = _tab_id(request)
    body = (await request.body()).decode("utf-8", errors="ignore").strip()
    if body:
        tid = body[:128]
    with _lock:
        _tabs.pop(tid, None)
        if not _tabs:
            _stop_api()
    return {"ok": True, "api_port_open": _api_port_open()}


if __name__ == "__main__":
    threading.Thread(target=_supervisor_loop, daemon=True).start()
    uvicorn.run(app, host=_HOST, port=_WATCHDOG_PORT, log_level="warning")
