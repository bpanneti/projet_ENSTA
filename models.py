from __future__ import annotations
from dataclasses import dataclass, field
from entity.node import Node
from entity.sensor import Sensor




@dataclass(frozen=True)
class Detection:
    id: str
    sensor_id: str
    longitude: float
    latitude: float
    label: str


@dataclass(frozen=True)
class Footprint:
    sensor_id: str
    longitude: float
    latitude: float
    range_m: float
    azimuth_deg: float = 0.0
    aperture_deg: float = 360.0
    id: str = "default"  # identifiant unique au sein du capteur
    label: str = "Volume de surveillance"
    altitude_min_m: float | None = None
    altitude_max_m: float | None = None
    range_min_m: float = 0.0
    class_type: str = "UNKNOWN"
    elevation_aperture_deg: float = 0.0
    elevation_deg: float = 0.0