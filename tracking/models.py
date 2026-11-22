"""Modèles modifiables par l'étudiant. États [E,N,U,vE,vN,vU,(aE,aN,aU)]."""
from dataclasses import dataclass, field, replace
import math
import numpy as np
from pyproj import Transformer
from typing import Any

def covariance(matrix, size=None, positive=False):
    matrix = np.asarray(matrix, dtype=float)
    if matrix.ndim != 2 or matrix.shape[0] != matrix.shape[1] or (size and matrix.shape != (size, size)):
        raise ValueError('Dimensions de covariance incompatibles')
    if not np.isfinite(matrix).all() or not np.allclose(matrix, matrix.T, atol=1e-9):
        raise ValueError('Covariance non finie ou non symétrique')
    values = np.linalg.eigvalsh(matrix)
    if values.min() < -1e-9 or (positive and values.min() <= 0):
        raise ValueError('Covariance non positive')
    return matrix.copy()


def rotation(yaw=0, pitch=0, roll=0):
    """Angles degrés : yaw horaire depuis Nord, pitch vers le haut.

    Rz(yaw) Rx(pitch) Ry(roll), axes locaux Est/droite, Nord/avant, Haut.
    Une autre convention producteur se change ici, pas dans les filtres.
    """
    y, p, r = np.radians([yaw, pitch, roll])
    cy, sy, cp, sp, cr, sr = np.cos(y), np.sin(y), np.cos(p), np.sin(p), np.cos(r), np.sin(r)
    return (np.array([[cy, sy, 0], [-sy, cy, 0], [0, 0, 1]]) @
            np.array([[1, 0, 0], [0, cp, -sp], [0, sp, cp]]) @
            np.array([[cr, 0, sr], [0, 1, 0], [-sr, 0, cr]]))


class LocalFrame:
    def __init__(self, lon, lat, altitude=0):
        self.origin = (float(lon), float(lat), float(altitude))
        self.forward = Transformer.from_crs(4979, 4978, always_xy=True)
        self.inverse = Transformer.from_crs(4978, 4979, always_xy=True)
        self.ecef = np.array(self.forward.transform(*self.origin))
        l, p = np.radians([lon, lat])
        self.basis = np.array([[-np.sin(l), np.cos(l), 0],
            [-np.sin(p)*np.cos(l), -np.sin(p)*np.sin(l), np.cos(p)],
            [np.cos(p)*np.cos(l), np.cos(p)*np.sin(l), np.sin(p)]])

    def to_local(self, lon, lat, altitude=0):
        return self.basis @ (np.array(self.forward.transform(lon, lat, altitude)) - self.ecef)

    def to_wgs84(self, point):
        return self.inverse.transform(*(self.ecef + self.basis.T @ np.asarray(point)[:3]))

    def sensor_basis(self, lon, lat):
        return self.basis @ LocalFrame(lon, lat).basis.T


class Dynamics:
    def __init__(self, kind='CV', noise=1., embedded=False):
        self.kind, self.noise = kind, float(noise)


        self.size = 9 if kind == 'CA' or embedded else 6

    def matrices(self, dt):
        if dt < 0:
            raise ValueError('Temps de prédiction négatif')

        #-->  student to  complete

        return f, q


@dataclass
class Observation:
    identifier: str
    origin: str
    timestamp: float
    sensor_id: str
    kind: str
    z: np.ndarray
    R: np.ndarray
    position: np.ndarray
    rotation: np.ndarray
    sensor_type: str = "UNKNOWN"
    node_id: str | None = None
    scan_id: Any = None

    classification: Any = None
    data: Any = None
    raw: dict = field(default_factory=dict)
    
    @property
    def angles(self):
        return {'POLAR': [1], 'SPHERICAL': [1, 2], 'BEARING_1D': [0], 'BEARING_2D': [0, 1]}.get(self.kind, [])

    def residual(self, actual, expected):
        difference = np.asarray(actual) - np.asarray(expected)
        for index in self.angles:
            difference[index] = (difference[index] + np.pi) % (2*np.pi) - np.pi
        return difference

    def predict(self, x):
        if self.kind == 'CARTESIAN':
            return x[:3]
        east, north, up = self.rotation.T @ (x[:3] - self.position)
        horizontal = np.hypot(east, north)
        if horizontal < 1e-7:
            raise ValueError('Azimut indéfini à la verticale du capteur')
        azimuth = np.arctan2(east, north)
        elevation = np.arctan2(up, horizontal)
        if self.kind == 'POLAR':
            return np.array([horizontal, azimuth])
        if self.kind == 'SPHERICAL':
            return np.array([np.hypot(horizontal, up), azimuth, elevation])
        if self.kind == 'BEARING_1D':
            return np.array([azimuth])
        return np.array([azimuth, elevation])

    def jacobian(self, x):
        if self.kind == 'CARTESIAN':
            h = np.zeros((3, len(x))); h[:, :3] = np.eye(3)
            return h
        e, n, u = self.rotation.T @ (x[:3] - self.position)
        d2 = e*e + n*n; d = math.sqrt(d2); r2 = d2 + u*u
        if d < 1e-7 or r2 < 1e-12:
            raise ValueError('Jacobienne de mesure singulière')
        #az = --> ENSTA to complete
        #el = --> ENSTA to  complete
        if self.kind == 'POLAR':
            local = np.array([[e/d, n/d, 0], az])
        elif self.kind == 'SPHERICAL':
            local = np.array([[e, n, u], az, el], dtype=float)
            local[0] /= math.sqrt(r2)
        elif self.kind == 'BEARING_1D':
            local = np.array([az])
        else:
            local = np.array([az, el])

        h = np.zeros((len(self.z), len(x)))
        h[:, :3] = local @ self.rotation.T

        return h

    def point(self, prior_range=None):
        """Position seulement si portée mesurée ou a priori EXPLICITE."""
        if self.kind == 'CARTESIAN':
            return self.z.copy()
        if self.kind in ('POLAR', 'SPHERICAL'):
            distance, azimuth = self.z[:2]
        elif prior_range is not None:
            distance, azimuth = prior_range, self.z[0]
        else:
            return None
        elevation = self.z[2] if self.kind == 'SPHERICAL' else (self.z[1] if self.kind == 'BEARING_2D' else 0.)
        return self.position + self.rotation @ np.array([
            distance*np.cos(elevation)*np.sin(azimuth),
            distance*np.cos(elevation)*np.cos(azimuth), distance*np.sin(elevation)])

    def initial_covariance(self, prior_range=None, prior_sigma=1000., unobserved_sigma=1000.):
        if self.kind == 'CARTESIAN':
            return self.R.copy()


        distance = self.z[0] if self.kind in ('POLAR', 'SPHERICAL') else prior_range
        az = self.z[1] if self.kind in ('POLAR', 'SPHERICAL') else self.z[0]
        el = self.z[2] if self.kind == 'SPHERICAL' else (self.z[1] if self.kind == 'BEARING_2D' else 0.)
        sa, ca, se, ce = np.sin(az), np.cos(az), np.sin(el), np.cos(el)
        j = np.array([[ce*sa, distance*ce*ca, -distance*se*sa],
                      [ce*ca, -distance*ce*sa, -distance*se*ca],
                      [se, 0, distance*ce]])
        c = np.zeros((3, 3))
        if self.kind == 'SPHERICAL':
            c[:] = self.R
        elif self.kind == 'POLAR':
            c[:2, :2] = self.R
            c[2, 2] = (unobserved_sigma/max(distance, 1))**2
        else:
            c[0, 0] = prior_sigma**2
            count = len(self.z)
            c[1:1+count, 1:1+count] = self.R
            if count == 1:
                c[2, 2] = (unobserved_sigma/max(distance, 1))**2
        transform = self.rotation @ j
        return transform @ c @ transform.T + np.eye(3)*1e-6

    def converted(self):
        if self.kind == 'CARTESIAN':
            return self
        if self.kind.startswith('BEARING'):
            raise ValueError('KF linéaire incompatible avec bearing only : choisir EKF ou IMM')
        return Observation(self.identifier, self.origin,self.timestamp, self.sensor_id, 'CARTESIAN', self.point(),
            self.initial_covariance(unobserved_sigma=10000), self.position, np.eye(3))
