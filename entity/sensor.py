"""Capteurs (code 3) et description de leurs volumes de surveillance."""
from __future__ import annotations
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING
import json
import math
if TYPE_CHECKING:
    from models import Footprint

# À confirmer avec le producteur des messages : yaw relatif au nœud ou absolu.
SENSOR_YAW_RELATIVE_TO_NODE = True


class SensorType(str, Enum):
    UNKNOWN = "UNKNOWN"
    RADAR = "RADAR"
    OPTRONIC = "OPTRONIC"
    RADIO_FREQUENCY = "RADIO_FREQUENCY"
    ACOUSTIC = "ACOUSTIC"


DISPLAY_TYPES = {SensorType.UNKNOWN: "unknown", SensorType.RADAR: "radar",
                 SensorType.OPTRONIC: "eo", SensorType.RADIO_FREQUENCY: "rf",
                 SensorType.ACOUSTIC: "acoustic"}


@dataclass(frozen=True)
class SurveillanceArea:
    id: str
    class_type: str = "UNKNOWN"
    distance_min: float = 0.0
    distance_max: float = 0.0
    fov_az: float = 360.0
    fov_site: float = 0.0

    @classmethod
    def from_message(cls, data, index):
        if not isinstance(data, dict):
            raise ValueError("associatedArea doit contenir des objets")
        area = cls(str(data.get("id", f"area_{index}")), str(data.get("classType", "UNKNOWN")),
                   float(data.get("distanceMin", 0)), float(data.get("distanceMax", 0)),
                   float(data.get("fov_AZ", 360)), float(data.get("fov_Site", 0)))
        if not all(math.isfinite(v) for v in (area.distance_min, area.distance_max, area.fov_az, area.fov_site)):
            raise ValueError("Volume avec paramètre non fini")
        if not (0 <= area.distance_min <= area.distance_max and 0 <= area.fov_az <= 360 and 0 <= area.fov_site <= 180):
            raise ValueError("Portées ou ouvertures du volume invalides")
        return area


@dataclass
class Sensor:
    # id interne : paire (idNode, sensorId) sérialisée pour éviter les collisions.
    id: str
    node_id: str
    longitude: float
    latitude: float
    label: str
    sensor_type: str = "unknown"
    volumes: dict[str, Footprint] = field(default_factory=dict)
    sensor_id: str = ""
    type: SensorType = SensorType.UNKNOWN
    raw_sensor_type: str = "UNKNOWN"
    status: str = "unknown"
    classes: object = ""
    message: str = ""
    location: dict = field(default_factory=lambda: {"x": 0.0, "y": 0.0, "z": 0.0})
    yaw: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0
    altitude: float = 0.0
    ground_altitude_m : float = 0.0
    areas: list[SurveillanceArea] = field(default_factory=list)
    network_configured: bool = False
    position_resolved: bool = True
    position_note: str = ""

    @staticmethod
    def key(node_id, sensor_id):
        return json.dumps([node_id, sensor_id], ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_message(cls, data, previous=None):
        node_id = str(data.get("idNode", "")).strip()
        sensor_id = str(data.get("sensorId", "")).strip()
        if not node_id or not sensor_id:
            raise ValueError("Capteur sans idNode ou sensorId")
        sensor = replace(previous) if previous else cls(cls.key(node_id, sensor_id), node_id, 0, 0, sensor_id)
        sensor.network_configured = True
        sensor.sensor_id = sensor_id
        if "sensorType" in data:
            sensor.raw_sensor_type = str(data["sensorType"])
            try:
                sensor.type = SensorType(sensor.raw_sensor_type.upper())
            except ValueError:
                sensor.type = SensorType.UNKNOWN
            sensor.sensor_type = DISPLAY_TYPES[sensor.type]
        for key, attribute in (("name", "label"), ("state", "status"), ("message", "message"), ("classes", "classes")):
            if key in data:
                setattr(sensor, attribute, data[key] if key == "classes" else str(data[key]))
        if "location" in data:
            if not isinstance(data["location"], dict):
                raise ValueError("location doit être un objet")
            sensor.location = dict(sensor.location)
            for key in ("x", "y", "z"):
                if key in data["location"]:
                    sensor.location[key] = float(data["location"][key])
        attitude = data.get("attitude", {})
        if not isinstance(attitude, dict):
            raise ValueError("attitude doit être un objet")
        for key in ("yaw", "pitch", "roll"):
            if key in attitude:
                setattr(sensor, key, float(attitude[key]))
        if not all(math.isfinite(v) for v in [*sensor.location.values(), sensor.yaw, sensor.pitch, sensor.roll]):
            raise ValueError("Location / attitude du capteur non finie")
        if "associatedArea" in data:
            if not isinstance(data["associatedArea"], list):
                raise ValueError("associatedArea doit être une liste")
            sensor.areas = [SurveillanceArea.from_message(area, i) for i, area in enumerate(data["associatedArea"])]
            if len({area.id for area in sensor.areas}) != len(sensor.areas):
                raise ValueError("Identifiants de volumes dupliqués")
        return sensor

    def resolve_position(self, node):
        # Les exemples ne définissent aucun repère pour les décalages x/y/z.
        # Seul le cas nul est géolocalisé sans inventer une convention.
        self.position_resolved = False
        if node is None:
            self.position_note = "Position en attente du nœud"
        elif any(value != 0 for value in self.location.values()):
            self.position_note = "Décalage x/y/z non nul : repère à préciser"
        else:
            self.longitude, self.latitude, self.altitude = node.longitude, node.latitude, node.altitude
            self.position_resolved = True
            self.position_note = "Position du nœud (location nulle)"

    def footprints(self, node):
        from models import Footprint
        if not self.position_resolved:
            return []
        yaw = self.yaw + (node.yaw if SENSOR_YAW_RELATIVE_TO_NODE else 0)
        return [Footprint(self.id, self.longitude, self.latitude, area.distance_max,
                          yaw % 360, area.fov_az, id=area.id,
                          label=f"Volume {i + 1} · {area.class_type}",
                          range_min_m=area.distance_min, class_type=area.class_type,
                          elevation_aperture_deg=area.fov_site,
                          elevation_deg=self.pitch + (node.pitch if SENSOR_YAW_RELATIVE_TO_NODE else 0))
                for i, area in enumerate(self.areas)]
