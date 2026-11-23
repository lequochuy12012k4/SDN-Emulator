from __future__ import annotations

import json
from pathlib import Path
import socket
import threading
import time
from typing import Any
import urllib.request
import webbrowser

import uvicorn
import webview

from sdnsim.desktop.server import create_desktop_app


REPO_ROOT = Path(__file__).resolve().parent.parent
TOPOLOGIES_DIR = REPO_ROOT / "topologies"


def _pick_port(preferred: int = 8000) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(("127.0.0.1", preferred)) != 0:
            return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_ready(url: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError(f"Server không phản hồi tại {url}")


class DesktopBridge:
    """Cầu nối JS <-> Python trong PyWebView.
    Mọi thuộc tính nội bộ phải bắt đầu bằng '_' để PyWebView không quét đệ quy vào .NET Window.
    """

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
    port = _pick_port(8000)
    app = create_desktop_app(topologies_dir=TOPOLOGIES_DIR)

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    _wait_ready(f"{base_url}/api/health")

    bridge = DesktopBridge(TOPOLOGIES_DIR, sim=app.state.sim)
    window = webview.create_window(
        title="SDN Emulator — Desktop Topology Studio",
        url=f"{base_url}/desktop",
        width=1220,
        height=780,
        min_size=(900, 600),
        js_api=bridge,
    )
    bridge._window = window

    try:
        webview.start()
    finally:
        server.should_exit = True
        app.state.sim.shutdown_sync()
        thread.join(timeout=3.0)


if __name__ == "__main__":
    main()