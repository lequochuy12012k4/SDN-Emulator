import os
from pathlib import Path
import logging

from sdnsim.api.app import create_app
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(PROJECT_ROOT / ".env", override=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")

DEFAULT = Path(__file__).resolve().parents[3] / "topologies" / "two_switch.json"
TOPOLOGY = Path(os.environ.get("SDNSIM_TOPOLOGY", DEFAULT))

# SDNSIM_CONTROLLER="127.0.0.1:6653" -> dùng controller OpenFlow bên ngoài.
# Không đặt biến này -> dùng controller học MAC nội bộ.
_controller = os.environ.get("SDNSIM_CONTROLLER")
_openflow = None
if _controller:
    _host, _, _port = _controller.rpartition(":")
    _openflow = (_host or "127.0.0.1", int(_port))

app = create_app(TOPOLOGY, openflow=_openflow)