from dataclasses import dataclass
from typing import Any


@dataclass
class PacketIn:
    switch: str
    in_port: int
    pkt: Any


@dataclass
class FlowMod:
    switch: str
    match: dict
    out_port: Any
    priority: int = 1
    idle_timeout: float | None = None
    cookie: int = 0
    group_id: int | None = None
    meter_id: int | None = None


@dataclass
class PacketOut:
    switch: str
    pkt: Any
    out_port: Any
    in_port: int | None = None