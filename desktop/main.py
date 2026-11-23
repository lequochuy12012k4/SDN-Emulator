from __future__ import annotations

import json
import multiprocessing
from pathlib import Path
import runpy
import shutil
import socket
import sys
import threading
import time
from typing import Any
import urllib.request
import webbrowser

import uvicorn
import webview

from sdnsim.desktop.server import create_desktop_app


def _app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _bundle_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent.parent


APP_ROOT = _app_root()
BUNDLE_ROOT = _bundle_root()
TOPOLOGIES_DIR = APP_ROOT / "topologies"


def _ensure_sample_topologies() -> None:
    TOPOLOGIES_DIR.mkdir(parents=True, exist_ok=True)
    bundled_topo = BUNDLE_ROOT / "topologies"
    if bundled_topo.exists() and bundled_topo != TOPOLOGIES_DIR:
        for src in bundled_topo.glob("*.json"):
            dst = TOPOLOGIES_DIR / src.name
            if not dst.exists():
                shutil.copy2(src, dst)


def _run_bundled_osken(args: list[str]) -> None:
    script = BUNDLE_ROOT / "examples" / "os_ken_controller.py"
    sys.path.insert(0, str(BUNDLE_ROOT))
    sys.argv = [str(script), *args]
    runpy.run_path(str(script), run_name="__main__")


def _pick_port(preferred: int = 8000) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(("127.0.0.1", preferred)) != 0:
            return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_ready(url: str, timeout: float = 12.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError(f"Server không phản hồi tại {url}")

def _set_win32_icon(window_title: str, icon_path: Path, win: webview.Window | None = None) -> None:
    if sys.platform != "win32" or not icon_path.exists():
        return
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    user32.LoadImageW.argtypes = [
        wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    user32.LoadImageW.restype = wintypes.HANDLE
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = ctypes.c_ssize_t

    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x0010
    WM_SETICON = 0x0080
    ICON_SMALL = 0
    ICON_BIG = 1

    h_small = user32.LoadImageW(None, str(icon_path), IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
    h_big = user32.LoadImageW(None, str(icon_path), IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
    if not h_small and not h_big:
        return

    for _ in range(30):
        hwnd = None
        try:
            if win is not None and getattr(win, "native", None) is not None:
                hwnd = int(win.native.Handle)
        except Exception:
            hwnd = None
        if not hwnd:
            hwnd = user32.FindWindowW(None, window_title)
        if hwnd:
            if h_small:
                user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, h_small)
            if h_big:
                user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, h_big)
            return
        time.sleep(0.1)

class DesktopBridge:
    """Cầu nối JS <-> Python trong PyWebView (thuộc tính nội bộ phải bắt đầu bằng '_')."""

    def __init__(self, topologies_dir: Path, sim: Any = None) -> None:
        self._topologies_dir = topologies_dir
        self._sim = sim
        self._window: webview.Window | None = None

    def open_url_in_browser(self, url: str) -> None:
        webbrowser.open(url)

    def open_topology_dialog(self) -> str | None:
        if self._window is None:
            return None
        dlg_type = webview.FileDialog.OPEN if hasattr(webview, "FileDialog") else webview.OPEN_DIALOG
        result = self._window.create_file_dialog(
            dlg_type,
            directory=str(self._topologies_dir),
            allow_multiple=False,
            file_types=("JSON files (*.json)", "All files (*.*)"),
        )
        if not result:
            return None
        return str(result[0] if isinstance(result, (list, tuple)) else result)

    def save_topology_dialog(self, content: str) -> str | None:
        if self._window is None:
            return None
        dlg_type = webview.FileDialog.SAVE if hasattr(webview, "FileDialog") else webview.SAVE_DIALOG
        result = self._window.create_file_dialog(
            dlg_type,
            directory=str(self._topologies_dir),
            save_filename="custom_topo.json",
            file_types=("JSON files (*.json)",),
        )
        if not result:
            return None
        path = Path(result[0] if isinstance(result, (list, tuple)) else result)
        if path.suffix.lower() != ".json":
            path = path.with_suffix(".json")
        path.write_text(json.dumps(json.loads(content), indent=2, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def save_pcap_dialog(self) -> str | None:
        if self._window is None or self._sim is None:
            return None
        dlg_type = webview.FileDialog.SAVE if hasattr(webview, "FileDialog") else webview.SAVE_DIALOG
        result = self._window.create_file_dialog(
            dlg_type,
            directory=str(self._topologies_dir.parent),
            save_filename="sdnsim_capture.pcap",
            file_types=("PCAP files (*.pcap)", "All files (*.*)"),
        )
        if not result:
            return None
        path = Path(result[0] if isinstance(result, (list, tuple)) else result)
        if path.suffix.lower() != ".pcap":
            path = path.with_suffix(".pcap")
        path.write_bytes(self._sim.capture.to_pcap_bytes())
        return str(path)

def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "--run-osken":
        _run_bundled_osken(sys.argv[2:])
        return

    _ensure_sample_topologies()
    port = _pick_port(8000)
    app = create_desktop_app(topologies_dir=TOPOLOGIES_DIR)

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    _wait_ready(f"{base_url}/api/health")

    bridge = DesktopBridge(TOPOLOGIES_DIR, sim=app.state.sim)
    title = "SDN Emulator — Desktop Topology Studio"
    window = webview.create_window(
        title=title,
        url=f"{base_url}/desktop",
        width=1240,
        height=800,
        min_size=(920, 620),
        js_api=bridge,
    )
    bridge._window = window

    icon_path = BUNDLE_ROOT / "desktop" / "app.ico"
    if icon_path.exists():
        def _apply_icon(*_args: Any) -> None:
            threading.Thread(
                target=_set_win32_icon,
                args=(title, icon_path, window),
                daemon=True,
            ).start()

        window.events.shown += _apply_icon

    try:
        webview.start()
    finally:
        server.should_exit = True
        app.state.sim.shutdown_sync()
        thread.join(timeout=3.0)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()