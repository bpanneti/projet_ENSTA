"""Scans code 7 : mesures, classification et covariance dans l'ordre reçu."""
from dataclasses import dataclass, field
from enum import Enum
import math
import numpy as np


class DetectionType(str, Enum):
    UNKNOWN     = 'UNKNOWN'
    POLAR       = 'POLAR'
    SPHERICAL   = 'SPHERICAL'
    BEARING_1D  = 'BEARING_1D'
    BEARING_2D  = 'BEARING_2D'
    EVENT       = 'EVENT'
    ALARM       = 'ALARM'


# Hypothèse : azimut SENSOR relatif à l'orientation du capteur, en degrés.
DETECTION_AZIMUTH_RELATIVE_TO_SENSOR = True


def number(value, name):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f'{name} non fini')
    return value


@dataclass
class Covariance:
    order: tuple
    matrix: np.ndarray

    @classmethod
    def from_message(cls, data):

        if not isinstance(data, dict) or data.get('format') != 'UPPER_TRIANGULAR':
            raise ValueError('Covariance : UPPER_TRIANGULAR attendu')
        dimension = data.get('dimension')
        if isinstance(dimension, bool) or not isinstance(dimension, int) or not 1 <= dimension <= 6:
            raise ValueError('Dimension de covariance invalide')
        order = data.get('order', [])
        if len(order) != dimension or len(set(order)) != dimension:
            raise ValueError('Ordre de covariance invalide')
        values = np.asarray(data.get('values', []), dtype=float)
        if values.shape != (dimension * (dimension + 1) // 2,) or not np.isfinite(values).all():
            raise ValueError('Valeurs de covariance invalides')
        matrix = np.zeros((dimension, dimension))
        indices = np.triu_indices(dimension)
        matrix[indices] = values
        matrix = matrix + matrix.T - np.diag(np.diag(matrix))
        scale = max(1.0, float(np.max(np.abs(matrix))))
        if np.linalg.eigvalsh(matrix).min() < -1e-10 * scale:
            raise ValueError('Covariance non positive semi-définie')
        return cls(tuple(order), matrix)

    def subset(self, *names):
        indices = [self.order.index(name) for name in names]
        return self.matrix[np.ix_(indices, indices)]


@dataclass
class ScanDetection:
    id: str
    timestamp: float
    type: DetectionType
    raw_type: str
    frame: str
    azimuth_deg: float | None = None
    elevation_deg: float | None = None
    range_m: float | None = None
    labels: list = field(default_factory=list)
    scores: list = field(default_factory=list)
    covariance: Covariance | None = None
    covariance_error: str = ''
    data: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    origin : str=''

    @classmethod
    def from_message(cls, data, scan_timestamp):
        if not isinstance(data, dict):
            raise ValueError('Détection non objet')


        identifier = str(data.get('detection_id', '')).strip()
        origin     = str(data.get('origin', '')).strip()


        if not identifier:
            raise ValueError('Détection sans detection_id')
        raw_type = str(data.get('detection_type', 'UNKNOWN')).upper()
        try:
            kind = DetectionType(raw_type)
        except ValueError:
            kind = DetectionType.UNKNOWN
        classification = data.get('classification', {})
        labels = classification.get('labels', [])
        scores = [number(v, 'Score') for v in classification.get('scores', [])]
        if len(labels) != len(scores) or any(v < 0 for v in scores):
            raise ValueError('Vecteur de classification invalide')
        detection = cls(identifier, number(data.get('timestamp', scan_timestamp), 'Timestamp'), kind,
                        raw_type, str(data.get('format', 'SENSOR')).upper(),
                        labels=[str(v) for v in labels], scores=scores, data = data.get('data',[]), raw=dict(data),origin=origin)


        for key in ('azimuth_deg', 'elevation_deg', 'range_m'):
            if key in data:
                setattr(detection, key, number(data[key], key))
        if kind in (DetectionType.POLAR, DetectionType.BEARING_1D, DetectionType.BEARING_2D) and detection.azimuth_deg is None:
            raise ValueError('Détection sans azimut')
        if kind == DetectionType.POLAR and (detection.range_m is None or detection.range_m < 0):
            raise ValueError('Détection POLAR sans portée valide')
        if kind == DetectionType.SPHERICAL and (detection.range_m is None  or detection.range_m < 0 ):
            raise ValueError('Détection POLAR sans portée valide')
        if 'cov' in data and data['cov'] is not None:
            try:
                detection.covariance = Covariance.from_message(data['cov'])
            except (ValueError, TypeError) as exc:
                detection.covariance_error = str(exc)
        return detection


@dataclass
class Scan:
    sensor_id: str
    node_id: str | None
    id: object
    timestamp: float
    detections: list
    warnings: list = field(default_factory=list)
    sensor_type: str = "UNKNOWN"

    @classmethod
    def from_message(cls, data):
        if data.get('code') != 7:
            raise ValueError('Un scan code 7 est attendu')


        sensor_id = str(data.get('sensorId', '')).strip()


        if not sensor_id:
            raise ValueError('Scan sans sensorId')
        timestamp = number(data.get('timestamp'), 'Timestamp du scan')
        entries = data.get('detections', [])
        if not isinstance(entries, list) or len(entries) > 10000:
            raise ValueError('Liste detections invalide / trop volumineuse')
        scan = cls(sensor_id, str(data['idNode']) if 'idNode' in data else None,
                   data.get('scanId'), timestamp, [] )
        scan.sensor_type =  data.get("sensor_type")

        for entry in entries:
            try:
                scan.detections.append(ScanDetection.from_message(entry, timestamp))

            except (ValueError, TypeError, AttributeError) as exc:
                scan.warnings.append(str(exc))
        ids = [d.id for d in scan.detections]
        if len(set(ids)) != len(ids):
            raise ValueError('detection_id dupliqué au sein du scan')
        return scan


def polar_covariance_enu(covariance, distance, azimuth_deg):
    """Jacobien distance(m)/azimut(deg) vers Est/Nord(m), à l'azimut absolu."""
    matrix = covariance.subset('range_m', 'azimuth_deg')
    theta = math.radians(azimuth_deg)
    angular_scale = math.pi / 180
    jacobian = np.array([[math.sin(theta), distance * math.cos(theta) * angular_scale],
                         [math.cos(theta), -distance * math.sin(theta) * angular_scale]])
    return jacobian @ matrix @ jacobian.T
