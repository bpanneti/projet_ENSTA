"""Dernier scan par capteur, expiration et panneau d'information au clic."""
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import time
import json
import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QObject, QTimer, Qt
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox,
                             QDoubleSpinBox, QPlainTextEdit, QTableWidget,
                             QTableWidgetItem, QHeaderView, QAbstractItemView)
from graphics_items import GEOD, SENSOR_COLORS
import entity.sensor as sensor_module
import entity.scan as scan_module
from entity.scan import Scan, DetectionType, polar_covariance_enu
from scan_graphics import angular_item, ellipse_item


@dataclass
class ScanRecord:
    scan: Scan
    received: float


class ScanDisplay(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.records = {}
        self.pending = OrderedDict()
        self.items = {}  # sensor key -> [(item, is_covariance)]
        self.selected = None
        self._build_panel()
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.expire)
        self.timer.start()

    def _build_panel(self):
        panel = QWidget(); panel.setObjectName('root'); panel.setMinimumWidth(335)
        layout = QVBoxLayout(panel)
        self.show_plots = QCheckBox('Afficher les plots'); self.show_plots.setChecked(True)
        self.show_covariances = QCheckBox('Afficher les covariances'); self.show_covariances.setChecked(True)
        layout.addWidget(self.show_plots); layout.addWidget(self.show_covariances)
        self.show_plots.toggled.connect(self.refresh_visibility)
        self.show_covariances.toggled.connect(self.refresh_visibility)
        row = QHBoxLayout(); row.addWidget(QLabel('Expiration après réception'))
        self.lifetime = QDoubleSpinBox(); self.lifetime.setRange(.5, 300); self.lifetime.setValue(5)
        self.lifetime.setSuffix(' s'); row.addWidget(self.lifetime); layout.addLayout(row)
        self.lifetime.valueChanged.connect(lambda _: self.expire())
        row = QHBoxLayout(); row.addWidget(QLabel('Longueur rayons sans volume'))
        self.ray_length = QDoubleSpinBox(); self.ray_length.setRange(1, 100000)
        self.ray_length.setValue(500); self.ray_length.setSuffix(' m')
        self.ray_length.valueChanged.connect(lambda _: self.refresh_geometry())
        row.addWidget(self.ray_length); layout.addLayout(row)
        caption = QLabel('Dernier scan par capteur · dégradé jusqu’à 3σ.\nUn rayon indique une direction, sans distance cible.')
        caption.setWordWrap(True); caption.setObjectName('muted'); layout.addWidget(caption)
        self.summary = QLabel('Cliquez sur un point, un rayon ou une alarme.'); self.summary.setWordWrap(True)
        self.summary.setObjectName('sectionTitle'); layout.addWidget(self.summary)
        self.info = QPlainTextEdit(); self.info.setReadOnly(True)
        self.info.setPlaceholderText('Position, mesures et covariance du plot sélectionné')
        self.info.setMinimumHeight(120); layout.addWidget(self.info, 1)
        self.classes = QTableWidget(0, 2); self.classes.setHorizontalHeaderLabels(['Classe', 'Score reçu'])
        self.classes.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.classes.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.classes.verticalHeader().hide(); layout.addWidget(self.classes, 1)
        self.count = QLabel('0 plot'); self.count.setObjectName('muted'); layout.addWidget(self.count)
        self.dock = self.window._dock('Plots / covariances', 'plotsDock', panel, Qt.DockWidgetArea.RightDockWidgetArea)

    def _sensor(self, scan):
        matches = [s for s in self.window.sensors.values()
                   if (s.sensor_id or s.id) == scan.sensor_id
                   and (scan.node_id is None or s.node_id == scan.node_id)]
        if len(matches) == 1:
            return matches[0], ''
        if matches:
            return None, f'Scan {scan.sensor_id} ambigu : ajouter idNode'
        return None, f'Scan {scan.sensor_id} en attente du capteur'

    def receive(self, scan, source):
        sensor, note = self._sensor(scan)
        if sensor is None or not sensor.position_resolved:
            key = (source, scan.node_id, scan.sensor_id)
            old = self.pending.get(key)
            if old is None or scan.timestamp >= old.scan.timestamp:
                self.pending[key] = ScanRecord(scan, time.monotonic())
                self.pending.move_to_end(key)
            while len(self.pending) > 256:
                self.pending.popitem(last=False)
            return [note or f'Scan {scan.sensor_id} en attente de position'] + scan.warnings
        return self._accept(sensor, ScanRecord(scan, time.monotonic()))

    def _accept(self, sensor, record):
        previous = self.records.get(sensor.id)
        if previous and record.scan.timestamp < previous.scan.timestamp:
            return ['Scan ancien ignoré']
        self.records[sensor.id] = record
        warnings = self._draw(sensor, record.scan)
        self.refresh_visibility()
        return record.scan.warnings + warnings

    def refresh_geometry(self):
        for key, record in list(self.pending.items()):
            sensor, _ = self._sensor(record.scan)
            if sensor and sensor.position_resolved:
                self.pending.pop(key)
                self._accept(sensor, record)
        for sensor_id, record in list(self.records.items()):
            sensor = self.window.sensors.get(sensor_id)
            if sensor is None:
                self._remove(sensor_id); self.records.pop(sensor_id)
            elif sensor.position_resolved:
                self._draw(sensor, record.scan)
            else:
                self._remove(sensor_id)
        self.refresh_visibility()

    def _remove(self, sensor_id):
        for item, _ in self.items.pop(sensor_id, []):
            self.window.plot.removeItem(item)
        if self.selected and self.selected[0] == sensor_id:
            self.selected = None
            self.summary.setText('Plot remplacé ou expiré · détails conservés')

    def _add(self, sensor, item, covariance=False):
        if item is not None:
            self.window.plot.addItem(item)
            self.items.setdefault(sensor.id, []).append((item, covariance))

    def _heading(self, sensor, detection):
        node = self.window.nodes.get(sensor.node_id)
        sensor_heading = sensor.yaw + (node.yaw if node and sensor_module.SENSOR_YAW_RELATIVE_TO_NODE else 0)
        if detection.frame == 'SENSOR':
            return (detection.azimuth_deg + (sensor_heading if scan_module.DETECTION_AZIMUTH_RELATIVE_TO_SENSOR else 0)) % 360
        if detection.frame in ('WGS84', 'WORLD'):
            return detection.azimuth_deg % 360
        raise ValueError(f'Repère de détection non pris en charge : {detection.frame}')

    def _draw(self, sensor, scan):
        selected_id = self.selected[1] if self.selected and self.selected[0] == sensor.id else None
        self._remove(sensor.id)
        color = SENSOR_COLORS.get(sensor.sensor_type, SENSOR_COLORS['unknown'])
        warnings = []
        for detection in scan.detections:
            try:

                geometry = {'kind': detection.type.value, 'anchor': (sensor.longitude, sensor.latitude)}
                if detection.type == DetectionType.POLAR or detection.type == DetectionType.SPHERICAL:
                    elevation_rad = 0.0
                    altitude =  0.0
                    if detection.type == DetectionType.SPHERICAL:
                      elevation_rad = math.radians(detection.elevation_deg)
                      altitude = sensor.altitude + detection.range_m * math.sin(elevation_rad)
                    horizontal_range = detection.range_m * math.cos(elevation_rad)

                    heading = self._heading(sensor, detection)
                    lon, lat, _ = GEOD.fwd(sensor.longitude, sensor.latitude, heading, horizontal_range)


                    geometry.update(position=(lon, lat), heading=heading)
                    item = pg.ScatterPlotItem(x=[lon], y=[lat], symbol='o', size=12,
                              pen=pg.mkPen('#fff4ec', width=1.5), brush=pg.mkBrush(color), hoverable=True,
                              tip=lambda x, y, data, d=detection: f'{d.id} · POLAR · cliquer pour les détails')
                    item.sigClicked.connect(lambda _item, points, event, s=sensor, d=detection, g=geometry:
                                            self.select(s, scan, d, g) if len(points) else None)
                    self._add(sensor, item)
                    if detection.covariance and self.show_covariances.isChecked():
                        try:
                            matrix = polar_covariance_enu(detection.covariance, detection.range_m, heading)
                            geometry['covariance_enu'] = matrix
                            self._add(sensor, ellipse_item(lon, lat, matrix, color), True)
                        except ValueError as exc:
                            geometry['covariance_note'] = str(exc)
                elif detection.type in (DetectionType.BEARING_1D, DetectionType.BEARING_2D):
                    heading = self._heading(sensor, detection)
                    length = max((v.range_m for v in sensor.volumes.values() if v.range_m > 0), default=self.ray_length.value())
                    lon, lat, _ = GEOD.fwd(sensor.longitude, sensor.latitude, heading, length)
                    geometry.update(heading=heading, display_length=length)
                    item = pg.PlotCurveItem([sensor.longitude, lon], [sensor.latitude, lat], pen=pg.mkPen(color, width=2))
                    item.setClickable(True, width=12)
                    item.sigClicked.connect(lambda _item, event, s=sensor, d=detection, g=geometry: self.select(s, scan, d, g))
                    self._add(sensor, item)
                    if detection.covariance and   self.show_covariances.isChecked():
                        try:
                            sigma = math.sqrt(max(0, detection.covariance.subset('azimuth_deg')[0, 0]))
                            geometry['azimuth_sigma_deg'] = sigma
                            self._add(sensor, angular_item(sensor.longitude, sensor.latitude, heading, sigma, length, color), True)
                        except ValueError as exc:
                            geometry['covariance_note'] = str(exc)
                elif detection.type in (DetectionType.EVENT, DetectionType.ALARM):
                    # Schéma d'alarme à confirmer : marqueur explicitement ancré au capteur.
                    item = pg.ScatterPlotItem(x=[sensor.longitude], y=[sensor.latitude], symbol='t', size=22,
                                              pen=pg.mkPen('#ffe5ae', width=2), brush=pg.mkBrush('#ff896b'))
                    item.sigClicked.connect(lambda _item, points, event, s=sensor, d=detection, g=geometry:
                                            self.select(s, scan, d, g) if len(points) else None)
                    self._add(sensor, item)
                    text = pg.TextItem('ALARME', color='#ffb395', anchor=(0, 1))
                    text.setPos(sensor.longitude, sensor.latitude); self._add(sensor, text)
                    geometry['alarm_note'] = 'Ancrage au capteur, sans localisation cible ni covariance spatiale déduite'
                else:
                    warnings.append(f'Type de détection non pris en charge : {detection.raw_type}'); continue
                item.setZValue(15)
                if detection.covariance_error:
                    geometry['covariance_note'] = detection.covariance_error
                if detection.id == selected_id:
                    self.select(sensor, scan, detection, geometry, reveal=False)
            except (ValueError, TypeError) as exc:
                warnings.append(f'Plot {detection.id} : {exc}')
        return warnings

    def refresh_visibility(self, *_):
        for sensor_id, entries in self.items.items():
            sensor_visible = self.window._sensor_visible(sensor_id)
            for item, is_covariance in entries:
                item.setVisible(sensor_visible and (self.show_covariances.isChecked() if is_covariance
                                                   else self.show_plots.isChecked()))
        n = sum(len(record.scan.detections) for record in self.records.values())
        self.count.setText(f'{n} détection(s) dans {len(self.records)} scan(s) · {len(self.pending)} en attente')

    def expire(self, now=None):
        now = time.monotonic() if now is None else now
        for key, record in list(self.records.items()):
            if now - record.received >= self.lifetime.value():
                self._remove(key); self.records.pop(key)
        for key, record in list(self.pending.items()):
            if now - record.received >= self.lifetime.value():
                self.pending.pop(key)
        self.refresh_visibility()

    def clear(self):
        for key in list(self.items):
            self._remove(key)
        self.records.clear(); self.pending.clear(); self.selected = None
        self.info.clear(); self.classes.setRowCount(0)
        self.summary.setText('Cliquez sur un point, un rayon ou une alarme.')
        self.refresh_visibility()

    def select(self, sensor, scan, detection, geometry, reveal=True):
        self.selected = (sensor.id, detection.id)
        self.summary.setText(f'{sensor.label} · {detection.raw_type}\nPlot {detection.id}')
        lines = [f'Nœud : {sensor.node_id}', f'Capteur : {sensor.sensor_id or sensor.id}',
                 f'Scan : {scan.id}', f'Timestamp : {detection.timestamp:.3f}',
                 f'Repère reçu : {detection.frame}']
        try:
            lines.append(datetime.fromtimestamp(detection.timestamp, timezone.utc).isoformat())
        except (ValueError, OverflowError, OSError):
            pass
        if 'position' in geometry:
            lon, lat = geometry['position']
            lines += [f'Longitude : {lon:.8f}°', f'Latitude : {lat:.8f}°',
                      'Position WGS84 calculée à partir de la mesure POLAR.']
        else:
            lines += ['Position cible indéterminée.', f'Ancrage capteur : {sensor.longitude:.8f}°, {sensor.latitude:.8f}°']
        for name in ('range_m', 'azimuth_deg', 'elevation_deg'):
            value = getattr(detection, name)
            if value is not None:
                lines.append(f'{name} reçu : {value:g}')
        if 'heading' in geometry:
            lines.append(f'Azimut géographique : {geometry["heading"]:.4f}°')
        if 'display_length' in geometry:
            lines.append(f'Longueur du rayon affiché : {geometry["display_length"]:g} m (pas une distance mesurée)')
        if detection.covariance:
            lines += ['Covariance reçue · ordre : ' + ', '.join(detection.covariance.order),
                      np.array2string(detection.covariance.matrix, precision=5)]
        if 'covariance_enu' in geometry:
            lines += ['Covariance projetée Est/Nord (m²), approximation locale :',
                      np.array2string(geometry['covariance_enu'], precision=5)]
        if 'azimuth_sigma_deg' in geometry:
            lines.append(f'Écart-type azimut : {geometry["azimuth_sigma_deg"]:g}°')
            lines.append('La covariance d’élévation reste dans le panneau ; la carte affiche la marginale azimutale.')
        for name in ('covariance_note', 'alarm_note'):
            if name in geometry:
                lines.append(geometry[name])
        for field in ('message', 'data'):
            if detection.raw.get(field):
                lines.append(f'{field} : ' + json.dumps(detection.raw[field], ensure_ascii=False))
        lines.append(f'Somme des scores reçus : {sum(detection.scores):.6f} (sans renormalisation)')
        self.info.setPlainText('\n'.join(lines))
        self.classes.setRowCount(len(detection.labels))
        for row, (label, score) in enumerate(zip(detection.labels, detection.scores)):
            self.classes.setItem(row, 0, QTableWidgetItem(label))
            self.classes.setItem(row, 1, QTableWidgetItem(f'{score:.6f}'))
        if reveal:
            self.dock.show(); self.dock.raise_()
