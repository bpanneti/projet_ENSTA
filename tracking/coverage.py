"""Test de couverture sur l'état PRÉDIT, dans le repère physique du capteur.

Sans Qt. Azimut horaire depuis l'avant/Nord local, élévation vers le haut.
Les volumes sont une union, avec des portées obliques en mètres.
"""
from dataclasses import dataclass, asdict
import math
import numpy as np


@dataclass(frozen=True)
class SurveillanceVolume:
    identifier: str
    range_min: float
    range_max: float
    azimuth_width_deg: float = 360.
    elevation_width_deg: float = 0.
    azimuth_center_deg: float = 0.
    elevation_center_deg: float = 0.

    def __post_init__(self):
        numbers = [self.range_min, self.range_max, self.azimuth_width_deg,
                   self.elevation_width_deg, self.azimuth_center_deg, self.elevation_center_deg]
        if not all(math.isfinite(v) for v in numbers):
            raise ValueError('Volume avec valeur non finie')
        if not (0 <= self.range_min <= self.range_max and 0 <= self.azimuth_width_deg <= 360
                and 0 <= self.elevation_width_deg <= 180):
            raise ValueError('Portées ou ouvertures du volume invalides')

    def contains_local(self, relative):
        east, north, up = relative
        distance = float(np.linalg.norm(relative))

        if not (self.range_min-1e-8 <= distance <= self.range_max+1e-8) or distance < 1e-8:
            return False
        azimuth = math.degrees(math.atan2(east, north))
        elevation = math.degrees(math.atan2(up, math.hypot(east, north)))
        difference = (azimuth-self.azimuth_center_deg+180) % 360 - 180


        if self.azimuth_width_deg < 360 and abs(difference) > self.azimuth_width_deg/2+1e-8:
            return False
        # Convention du schéma reçu : fov_Site=0 indique une ouverture verticale non spécifiée.


        return (self.elevation_width_deg == 0 or
                abs(elevation) <= self.elevation_width_deg/2+1e-8)


@dataclass(frozen=True)
class SensorCoverage:
    sensor_id: str
    position: np.ndarray
    rotation: np.ndarray
    volumes: tuple

    def __post_init__(self):
        position = np.array(self.position, dtype=float, copy=True)
        orientation = np.array(self.rotation, dtype=float, copy=True)
        if position.shape != (3,) or orientation.shape != (3, 3):
            raise ValueError('Position/orientation de couverture de dimensions invalides')
        if not np.isfinite(position).all() or not np.isfinite(orientation).all():
            raise ValueError('Position/orientation de couverture non finie')
        if not np.allclose(orientation.T @ orientation, np.eye(3), atol=1e-7) or np.linalg.det(orientation) < 0:
            raise ValueError('Orientation de couverture non orthonormée')
        position.setflags(write=False); orientation.setflags(write=False)
        object.__setattr__(self, 'position', position)
        object.__setattr__(self, 'rotation', orientation)
        object.__setattr__(self, 'volumes', tuple(self.volumes))

    @classmethod
    def from_areas(cls, sensor_id, position, orientation, areas):
        volumes = tuple(SurveillanceVolume(str(area.id), float(area.distance_min),
            float(area.distance_max), float(area.fov_az), float(area.fov_site)) for area in areas)
        return cls(str(sensor_id), position, orientation, volumes)

    def contains(self, position_enu):
        point = np.asarray(position_enu, dtype=float)

        if point.shape != (3,) or not np.isfinite(point).all():
            return False
        local = self.rotation.T @ (point-self.position)

  
        for volume in list(self.volumes):

            if volume.contains_local(local):
                return True

        return False
        #return any(volume.contains_local(local) for volume in self.volumes)

    def to_dict(self):
        return {'sensor_id': self.sensor_id, 'position_enu': self.position.tolist(),
                'rotation_sensor_to_enu': self.rotation.tolist(),
                'range_definition': 'slant_3d', 'volumes': [asdict(v) for v in self.volumes]}
