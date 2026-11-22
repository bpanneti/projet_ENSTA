"""Configuration d'un nœud réseau (code 2)."""
from __future__ import annotations
from dataclasses import dataclass, field, replace
from enum import Enum
import math
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from sensor import Sensor


class NodeType(str, Enum):
    UNKNOWN = "UNKNOWN"
    SENSOR_NODE = "SENSOR_NODE"


NODE_COLORS = {NodeType.UNKNOWN: "#B8C6D9", NodeType.SENSOR_NODE: "#56C7F3"}


@dataclass
class Node:
    id: str
    longitude: float
    latitude: float
    label: str
    sensors: dict[str, Sensor] = field(default_factory=dict)
    node_type: NodeType = NodeType.UNKNOWN
    raw_node_type: str = "UNKNOWN"
    status: str = "unknown"
    altitude: float = 0.0
    ground_altitude_m: float = 0.0
    yaw: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0
    network: str = ""
    ip: str = ""
    message: str = ""
    velocity: dict = field(default_factory=dict)

    @property
    def color(self):
        return NODE_COLORS.get(self.node_type, NODE_COLORS[NodeType.UNKNOWN])

    @classmethod
    def from_message(cls, data, previous=None):
        identifier = str(data.get("idNode", "")).strip()
        if not identifier:
            raise ValueError("Nœud sans idNode")
        node = replace(previous) if previous else cls(identifier, 0, 0, identifier)
        position = data.get("position")
        if previous is None and (not isinstance(position, dict) or not {"longitude", "latitude"} <= position.keys()):
            raise ValueError("Nouveau nœud sans position")
        if position is not None:
            if not isinstance(position, dict) or str(position.get("format", "WGS84")).upper() != "WGS84":
                raise ValueError("Position WGS84 attendue")
            for key in ("longitude", "latitude", "altitude"):
                if key in position:
                    setattr(node, key, float(position[key]))
        if not (math.isfinite(node.longitude) and math.isfinite(node.latitude)
                and -180 <= node.longitude <= 180 and -90 <= node.latitude <= 90):
            raise ValueError("Coordonnées WGS84 invalides")
        for key, attribute in (("name", "label"), ("state", "status"), ("network", "network"),
                               ("ip", "ip"), ("message", "message")):
            if key in data:
                setattr(node, attribute, str(data[key]))
        if "nodeType" in data:
            node.raw_node_type = str(data["nodeType"])
            try:
                node.node_type = NodeType(node.raw_node_type.upper())
            except ValueError:
                node.node_type = NodeType.UNKNOWN
        attitude = data.get("attitude", {})
        if not isinstance(attitude, dict):
            raise ValueError("attitude doit être un objet")
        for key in ("yaw", "pitch", "roll"):
            if key in attitude:
                setattr(node, key, float(attitude[key]))
        if not all(math.isfinite(getattr(node, key)) for key in ("altitude", "yaw", "pitch", "roll")):
            raise ValueError("Altitude / attitude non finie")
        if "velocity" in data:
            if not isinstance(data["velocity"], dict):
                raise ValueError("velocity doit être un objet")
            node.velocity = dict(data["velocity"])
        return node
