import os
from pathlib import Path
from sdnsim.api.app import create_app
from dotenv import load_dotenv
load_dotenv()  # đọc biến môi trường từ .env nếu có

DEFAULT = Path(__file__).resolve().parents[3] / "topologies" / "two_switch.json"
app = create_app(Path(os.environ.get("SDNSIM_TOPOLOGY", DEFAULT)))