"""Adaptation du Scan EXISTANT ; aucune nouvelle classe Scan/Target."""
from collections import defaultdict
import math
import numpy as np
from .models import Observation, covariance


def observations(scan, position, sensor_rotation, world_rotation, config):
    result, warnings = [], []

    for d in scan.detections:
        try:
            kind = getattr(d, 'raw_type', getattr(d.type, 'value', str(d.type))).upper()
            if kind not in ('POLAR', 'SPHERICAL', 'BEARING_1D', 'BEARING_2D'):
                warnings.append(f'{d.id} : {kind} non cinématique, ignoré par le tracker')
                continue
            names = {'POLAR': ['range_m', 'azimuth_deg'],
                     'SPHERICAL': ['range_m', 'azimuth_deg', 'elevation_deg'],
                     'BEARING_1D': ['azimuth_deg'],
                     'BEARING_2D': ['azimuth_deg', 'elevation_deg']}[kind]
            values = []
            scales = []
            sigmas = []
            for name in names:
                value = getattr(d, name, None)
                if value is None:
                    value = getattr(d, 'raw', {}).get(name)
                values.append(float(value))
                scales.append(1. if name == 'range_m' else math.pi/180)
                sigmas.append(config.sigma_range if name == 'range_m' else
                              config.sigma_azimuth_deg if name == 'azimuth_deg' else config.sigma_elevation_deg)
            z = np.asarray(values) * scales
            if not np.isfinite(z).all() or ('range_m' in names and z[0] <= 0):
                raise ValueError('Mesure non finie ou distance non positive')
            if 'elevation_deg' in names and abs(values[names.index('elevation_deg')]) > 90:
                raise ValueError('Élévation hors [-90°,90°]')
            if kind in config.R_override:
                R = covariance(config.R_override[kind], len(names), positive=True)
            elif config.use_received_R and d.covariance is not None:
                scale = np.diag(scales)
                R = covariance(scale @ d.covariance.subset(*names) @ scale, len(names), positive=True)
            else:
                if config.use_received_R and getattr(d, 'covariance_error', ''):
                    raise ValueError(f'Covariance reçue invalide : {d.covariance_error}')
                R = np.diag((np.array(sigmas)*scales)**2)
            frame = str(d.frame).upper()
            if frame not in ('SENSOR', 'WORLD', 'WGS84'):
                raise ValueError(f'Repère {frame} inconnu')
            rot = sensor_rotation if frame == 'SENSOR' else world_rotation
            result.append(Observation(str(d.id),str(d.origin) ,float(d.timestamp), str(scan.sensor_id), kind,
                                      z, R, np.array(position), np.array(rot),))

            result[-1].sensor_type      = scan.sensor_type
            result[-1].classification   = dict(zip(d.labels, d.scores, strict=True))
            result[-1].data             = d.data


        except (ValueError, TypeError, AttributeError, IndexError, np.linalg.LinAlgError) as exc:
            warnings.append(f'{getattr(d, "id", "?")} : {exc}')
    return result, warnings
