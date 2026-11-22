"""Affichage cadencé des scans ; objets persistants et covariances regroupées.

Interface publique conservée : ScanDisplay(window), receive(), refresh_geometry(),
refresh_visibility(), expire(), clear(), select(). Tous les appels GUI restent
sur le thread Qt principal. Aucune modification du tracking ou du recorder.
"""
from collections import OrderedDict
from itertools import islice
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import time
import json
import logging
import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QObject, QTimer, Qt, QPointF, QRectF
from PyQt6.QtGui import QColor, QPainterPath, QRadialGradient, QTransform
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox,
    QDoubleSpinBox, QSpinBox, QPlainTextEdit, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView)
from graphics_items import GEOD, SENSOR_COLORS
import entity.sensor as sensor_module
import entity.scan as scan_module
from entity.scan import Scan, DetectionType, polar_covariance_enu
from tracking.models import LocalFrame, rotation


def _basis(lon, lat):
    l, p = np.radians([lon, lat])
    return np.array([[-np.sin(l), np.cos(l), 0.],
        [-np.sin(p)*np.cos(l), -np.sin(p)*np.sin(l), np.cos(p)],
        [np.cos(p)*np.cos(l), np.cos(p)*np.sin(l), np.sin(p)]])


def _preview(value, depth=0, budget=None):
    """Aperçu borné des grandes signatures ; le payload original reste intact."""
    budget = [500] if budget is None else budget
    budget[0] -= 1
    if budget[0] < 0 or depth > 5:return '…'
    if isinstance(value, dict):
        result = {str(k):_preview(v, depth+1, budget) for k,v in islice(value.items(),32)}
        if len(value)>32:result['…'] = f'{len(value)-32} champs supplémentaires'
        return result
    if isinstance(value,np.ndarray) and value.ndim==0:return value.item()
    if isinstance(value, (list, tuple, np.ndarray)):
        result = [_preview(v, depth+1, budget) for v in value[:32]]
        if len(value)>32:result.append(f'… {len(value)-32} éléments supplémentaires')
        return result
    if isinstance(value, str) and len(value)>2000:return value[:2000]+'…'
    if isinstance(value, np.generic):return value.item()
    return value


def _path(xs, ys):
    path = QPainterPath(QPointF(float(xs[0]), float(ys[0])))
    for x, y in zip(xs[1:], ys[1:]): path.lineTo(float(x), float(y))
    path.closeSubpath()
    return path


class CovarianceLayer(pg.GraphicsObject):
    """Un seul item par capteur ; ellipse tangentielle, dégradé natif Qt.

    L'ellipse est une approximation cartographique locale, pas un calcul
    géodésique exact pour les très grandes emprises ou près des pôles.
    """
    def __init__(self):
        super().__init__()
        self.ellipses = []; self.angular = []; self._bounds = QRectF()
        self.setZValue(-3)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

    def boundingRect(self): return self._bounds

    def set_shapes(self, shapes, color):
        ellipses = []; angular = []; bounds = None
        self.color = QColor(color)
        unit = QPainterPath(); unit.addEllipse(QRectF(-1, -1, 2, 2))
        for shape in shapes:
            if shape[0] == 'ellipse':
                _, lon, lat, matrix = shape
                values, vectors = np.linalg.eigh(matrix)
                if values.min() < -1e-8 * max(1., abs(values).max()): continue
                if values.max() <= 0: continue
                p = math.radians(lat); c = math.cos(p)
                if abs(c) < 1e-8: continue
                e2 = 6.6943799901413165e-3; a = 6378137.
                den = 1 - e2 * math.sin(p)**2
                n = a / math.sqrt(den); m = a * (1-e2) / den**1.5
                degrees_per_m = np.diag([180 / (math.pi*n*c), 180 / (math.pi*m)])
                A = degrees_per_m @ vectors @ np.diag(3*np.sqrt(np.maximum(values, 0)))
                transform = QTransform(A[0,0], A[1,0], A[0,1], A[1,1], lon, lat)
                outline = transform.map(unit)
                ellipses.append((transform, outline))
                rect = outline.boundingRect()
                bounds = rect if bounds is None else bounds.united(rect)
            else:
                _, lon, lat, azimuth, sigma, length = shape
                if sigma <= 0: continue

                edge_angles = np.linspace(azimuth-3*sigma, azimuth+3*sigma, 49)
                ex, ey, _ = GEOD.fwd(np.full(49,lon), np.full(49,lat), edge_angles, np.full(49,length))
                ex = lon+(np.asarray(ex)-lon+180)%360-180
                outline = _path(np.r_[lon,ex], np.r_[lat,ey])
                rect = outline.boundingRect(); bounds = rect if bounds is None else bounds.united(rect)
                angular.append(([], outline))
        self.prepareGeometryChange()
        self.ellipses, self.angular = ellipses, angular
        self._bounds = bounds if bounds is not None else QRectF()
        # Marge pour le contour cosmétique 3σ ; n'affecte pas sa géométrie.
        if not self._bounds.isNull():self._bounds.adjust(-1e-5,-1e-5,1e-5,1e-5)
        self.update()

    def paint(self, painter, option, widget=None):
        pen = pg.mkPen(
            self.color,
            width=1.0,
            style=Qt.PenStyle.DotLine,
            cosmetic=True,
        )

        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        # Ellipses : contour à 3 sigma.
        for _, outline in self.ellipses:
            painter.drawPath(outline)

        # Incertitudes angulaires : contour extérieur.
        for _, outline in self.angular:
            painter.drawPath(outline)
    '''
    def paint(self, painter, option, widget=None):
        gradient = QRadialGradient(0., 0., 1.)
        for radius in np.linspace(0., 1., 13):
            c = QColor(self.color); c.setAlpha(round(105*math.exp(-4.5*radius**2)))
            gradient.setColorAt(float(radius), c)
        pen = pg.mkPen(self.color, width=1.5, style=Qt.PenStyle.SolidLine)
        for transform, outline in self.ellipses:
            painter.save(); painter.setTransform(transform, True)
            painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(gradient)
            painter.drawEllipse(QRectF(-1,-1,2,2)); painter.restore()
            painter.setPen(pen); painter.setBrush(Qt.BrushStyle.NoBrush); painter.drawPath(outline)
        for paths, outline in self.angular:
            painter.setPen(Qt.PenStyle.NoPen)
            for path, alpha in paths:
                c = QColor(self.color); c.setAlpha(alpha); painter.setBrush(c); painter.drawPath(path)
            painter.setPen(pen); painter.setBrush(Qt.BrushStyle.NoBrush); painter.drawPath(outline)

    '''
@dataclass
class ScanRecord:
    scan: Scan
    received: float


class ScanDisplay(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.records = {}; self.pending = OrderedDict(); self.items = {}; self.selected = None
        self._dirty = OrderedDict(); self._layers = {}; self._prepared = {}; self._signatures = {}
        self._visibility = {}; self._contexts = {}; self.render_warnings = {}
        self._last_details_update = 0.0
        self.render_budget_ms = 8.0
        self.last_render_ms = 0.0; self.rendered_sensors = 0; self.coalesced_scans = 0
        self._build_panel()
        self.render_timer = QTimer(self); self.render_timer.setInterval(50)
        self.render_timer.timeout.connect(self.flush); self.render_timer.start()
        self.timer = QTimer(self); self.timer.setInterval(250)
        self.timer.timeout.connect(self.expire); self.timer.start()

    def _build_panel(self):
        panel = QWidget(); panel.setObjectName('root'); panel.setMinimumWidth(335)
        layout = QVBoxLayout(panel)
        self.show_plots = QCheckBox('Afficher les plots'); self.show_plots.setChecked(True)
        self.show_covariances = QCheckBox('Afficher les covariances'); self.show_covariances.setChecked(True)
        layout.addWidget(self.show_plots); layout.addWidget(self.show_covariances)
        self.show_plots.toggled.connect(self.refresh_visibility)
        self.show_covariances.toggled.connect(self.refresh_visibility)
        self.selected_covariance = QCheckBox('Covariance du plot sélectionné uniquement')
        self.selected_covariance.toggled.connect(self._invalidate_display)
        layout.addWidget(self.selected_covariance)
        row = QHBoxLayout(); row.addWidget(QLabel('Fréquence graphique'))
        self.display_fps = QSpinBox(); self.display_fps.setRange(1, 60); self.display_fps.setValue(20)
        self.display_fps.setSuffix(' Hz'); row.addWidget(self.display_fps); layout.addLayout(row)
        self.display_fps.valueChanged.connect(lambda hz: self.render_timer.setInterval(round(1000 / hz)))
        row = QHBoxLayout(); row.addWidget(QLabel('Covariances / capteur'))
        self.covariance_limit = QSpinBox(); self.covariance_limit.setRange(0, 100000)
        self.covariance_limit.setValue(200); self.covariance_limit.setSpecialValueText('Toutes')
        self.covariance_limit.valueChanged.connect(self._invalidate_display)
        row.addWidget(self.covariance_limit); layout.addLayout(row)
        row = QHBoxLayout(); row.addWidget(QLabel('Expiration après réception'))
        self.lifetime = QDoubleSpinBox(); self.lifetime.setRange(.5, 300); self.lifetime.setValue(5)
        self.lifetime.setSuffix(' s'); row.addWidget(self.lifetime); layout.addLayout(row)
        self.lifetime.valueChanged.connect(lambda _: self.expire())
        row = QHBoxLayout(); row.addWidget(QLabel('Longueur rayons sans volume'))
        self.ray_length = QDoubleSpinBox(); self.ray_length.setRange(1, 100000)
        self.ray_length.setValue(500); self.ray_length.setSuffix(' m')
        self.ray_length.valueChanged.connect(lambda _: self.refresh_geometry())
        row.addWidget(self.ray_length); layout.addLayout(row)
        caption = QLabel('Dernier scan par capteur · rendu cadencé · contour 3σ.\nUn rayon indique une direction, sans distance cible.')
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
            if (s.sensor_id or s.id) == scan.sensor_id and (scan.node_id is None or s.node_id == scan.node_id)]
        if len(matches) == 1: return matches[0], ''
        return None, f'Scan {scan.sensor_id} ' + ('ambigu : ajouter idNode' if matches else 'en attente du capteur')

    def receive(self, scan, source):
        sensor, note = self._sensor(scan)
        if sensor is None or not sensor.position_resolved:
            key = (source, scan.node_id, scan.sensor_id); old = self.pending.get(key)
            if old is None or scan.timestamp >= old.scan.timestamp:
                self.pending[key] = ScanRecord(scan, time.monotonic()); self.pending.move_to_end(key)
            while len(self.pending) > 256:self.pending.popitem(last=False)
            return [note or f'Scan {scan.sensor_id} en attente de position'] + scan.warnings
        return self._accept(sensor, ScanRecord(scan, time.monotonic()))

    def _accept(self, sensor, record):
        previous = self.records.get(sensor.id)
        if previous and record.scan.timestamp < previous.scan.timestamp:return ['Scan ancien ignoré']
        if sensor.id in self._dirty:self.coalesced_scans += 1
        self.records[sensor.id] = record; self._mark_dirty(sensor.id)
        return list(record.scan.warnings)

    def _mark_dirty(self, sensor_id):
        self._dirty[sensor_id] = None

    def _signature(self, sensor):
        node = self.window.nodes.get(sensor.node_id)
        return (sensor.longitude,sensor.latitude,sensor.altitude,sensor.yaw,sensor.pitch,sensor.roll,
            sensor.sensor_type, bool(sensor.position_resolved),
            (node.yaw,node.pitch,node.roll) if node else None,
            bool(sensor_module.SENSOR_YAW_RELATIVE_TO_NODE),bool(scan_module.DETECTION_AZIMUTH_RELATIVE_TO_SENSOR),
            max((v.range_m for v in sensor.volumes.values() if v.range_m > 0),default=self.ray_length.value()))

    def refresh_geometry(self, sensor_id=None):
        for key, record in list(self.pending.items()):
            sensor, _ = self._sensor(record.scan)
            if sensor and sensor.position_resolved:
                self.pending.pop(key);self._accept(sensor,record)
        keys = list(self.records) if sensor_id is None else [sensor_id]
        for key in keys:
            sensor = self.window.sensors.get(key)
            if sensor is None:
                self._remove(key);self.records.pop(key,None)
            elif not sensor.position_resolved:
                self._remove(key)
            else:
                signature = self._signature(sensor)
                if signature != self._signatures.get(key):
                    self._prepared.pop(key,None);self._contexts.pop(key,None)
                    self._mark_dirty(key)
        self.refresh_visibility()

    def _invalidate_display(self, *_):
        for key in self.records:self._mark_dirty(key)
        self.refresh_visibility()

    def flush(self, force=False):
        """Timer Qt ; force=True vide la file pour un test/export explicite.

        Le budget s'applique ENTRE capteurs, pas au milieu d'un scan lourd.
        """
        started = time.perf_counter()
        while self._dirty:
            key,_ = self._dirty.popitem(last=False)
            sensor = self.window.sensors.get(key);record = self.records.get(key)
            if sensor is None or record is None:continue
            if time.monotonic()-record.received >= self.lifetime.value():
                self._remove(key);self.records.pop(key,None);continue
            if sensor.position_resolved and self.window._sensor_visible(key) and (self.show_plots.isChecked() or self.show_covariances.isChecked()):
                try:

                    self.render_warnings[key] = self._draw(sensor,record.scan)
                    self.rendered_sensors += 1
                except Exception as exc:
                    logging.exception('Affichage du scan du capteur %s',key)
                    self._remove(key)
                    self.render_warnings[key] = [f'Affichage interrompu : {exc}']
            if not force and (time.perf_counter()-started)*1000 >= self.render_budget_ms:break
        self.last_render_ms = (time.perf_counter()-started)*1000
        self._apply_visibility();self._update_count()

    def _remove(self, sensor_id):
        for item in self._layers.pop(sensor_id,{}).values():self.window.plot.removeItem(item)
        for mapping in (self.items,self._dirty,self._prepared,self._signatures,self._visibility,self._contexts,self.render_warnings):mapping.pop(sensor_id,None)
        if self.selected and self.selected[0] == sensor_id:
            self.selected = None;self.summary.setText('Plot remplacé ou expiré · détails conservés')

    def _layer(self, sensor, kind):
        layers = self._layers.setdefault(sensor.id,{})
        if kind not in layers:
            if kind == 'covariance':item = CovarianceLayer()
            elif kind == 'rays':
                item = pg.PlotCurveItem();item.setClickable(True,width=12)
                item.sigClicked.connect(lambda obj,event,key=sensor.id:self._ray_clicked(key,event))
            else:
                item = pg.ScatterPlotItem(pxMode=True,hoverable=False)
                item.sigClicked.connect(self._point_clicked)
            layers[kind] = item;item.setZValue(-3 if kind=='covariance' else 15)
            self.window.plot.addItem(item)
        return layers[kind]

    def _heading(self, sensor, detection):
        node = self.window.nodes.get(sensor.node_id)
        h = sensor.yaw + (node.yaw if node and sensor_module.SENSOR_YAW_RELATIVE_TO_NODE else 0)
        if detection.frame == 'SENSOR':return (detection.azimuth_deg + (h if scan_module.DETECTION_AZIMUTH_RELATIVE_TO_SENSOR else 0))%360
        if detection.frame in ('WGS84','WORLD'):return detection.azimuth_deg%360
        raise ValueError(f'Repère de détection non pris en charge : {detection.frame}')

    def _context(self, sensor):
        signature = self._signature(sensor)
        cached = self._contexts.get(sensor.id)
        if cached is None or cached[0] != signature:
            frame = LocalFrame(sensor.longitude,sensor.latitude,sensor.altitude)
            orient = rotation(sensor.yaw,sensor.pitch,sensor.roll)
            node = self.window.nodes.get(sensor.node_id)
            if node and sensor_module.SENSOR_YAW_RELATIVE_TO_NODE:orient = rotation(node.yaw,node.pitch,node.roll) @ orient
            if not scan_module.DETECTION_AZIMUTH_RELATIVE_TO_SENSOR:orient = np.eye(3)
            cached = (signature,frame,orient);self._contexts[sensor.id] = cached
        return cached[1:]

    def _prepare(self, sensor, scan):
        signature = self._signature(sensor);cached = self._prepared.get(sensor.id)
        if cached is not None and cached[0] is scan and cached[1] == signature:return cached[2:]
        prepared = [];warnings = []
        length = signature[-1]
        for detection in scan.detections:
            try:
                geometry = {'kind':detection.type.value,'anchor':(sensor.longitude,sensor.latitude)}
                if detection.type == DetectionType.POLAR:
                    heading = self._heading(sensor,detection)
                    lon,lat,_ = GEOD.fwd(sensor.longitude,sensor.latitude,heading,detection.range_m)
                    geometry.update(position=(lon,lat),heading=heading)
                elif detection.type == DetectionType.SPHERICAL:
                    frame,orient = self._context(sensor)
                    if detection.frame not in ('SENSOR','WORLD','WGS84'):raise ValueError('Repère inconnu')
                    rot = orient if detection.frame == 'SENSOR' else np.eye(3)
                    az,el = np.radians([detection.azimuth_deg,detection.elevation_deg]);r = detection.range_m
                    sa,ca,se,ce = np.sin(az),np.cos(az),np.sin(el),np.cos(el)
                    point = rot @ np.array([r*ce*sa,r*ce*ca,r*se])
                    lon,lat,altitude = frame.to_wgs84(point)
                    geometry.update(position=(lon,lat),altitude=altitude,_sphere_rotation=rot,
                        _sphere_frame=frame)
                elif detection.type in (DetectionType.BEARING_1D,DetectionType.BEARING_2D):
                    heading = self._heading(sensor,detection)
                    lon,lat,_ = GEOD.fwd(sensor.longitude,sensor.latitude,heading,length)
                    geometry.update(heading=heading,display_length=length,_ray_end=(lon,lat))
                elif detection.type in (DetectionType.EVENT,DetectionType.ALARM):
                    geometry['alarm_note'] = 'Ancrage au capteur, sans localisation cible ni covariance spatiale déduite'
                else:
                    warnings.append(f'Type de détection non pris en charge : {detection.raw_type}');continue
                if detection.covariance_error:geometry['covariance_note'] = detection.covariance_error
                prepared.append((detection,geometry))
            except (ValueError,TypeError,AttributeError,np.linalg.LinAlgError) as exc:warnings.append(f'Plot {detection.id} : {exc}')
        self._prepared[sensor.id] = (scan,signature,prepared,warnings)
        return prepared,warnings

    def _ensure_covariance(self, sensor, detection, geometry):
        if geometry.get('_covariance_ready'):return geometry.get('_covariance_shape')
        geometry['_covariance_ready'] = True
        if detection.covariance is None:return None
        try:
            if detection.type == DetectionType.POLAR:
                matrix = polar_covariance_enu(detection.covariance,detection.range_m,geometry['heading'])
            elif detection.type == DetectionType.SPHERICAL:
                az,el = np.radians([detection.azimuth_deg,detection.elevation_deg]);r = detection.range_m
                sa,ca,se,ce = np.sin(az),np.cos(az),np.sin(el),np.cos(el)
                j = np.array([[ce*sa,r*ce*ca,-r*se*sa],[ce*ca,-r*ce*sa,-r*se*ca],[se,0,r*ce]])
                scale = np.diag([1.,math.pi/180,math.pi/180])
                R = scale @ detection.covariance.subset('range_m','azimuth_deg','elevation_deg') @ scale
                frame = geometry['_sphere_frame'];rot = geometry['_sphere_rotation']
                cov = rot @ j @ R @ j.T @ rot.T + np.eye(3)*1e-6
                lon,lat = geometry['position'];basis = frame.basis @ _basis(lon,lat).T
                matrix = (basis.T @ cov @ basis)[:2,:2]
            elif detection.type in (DetectionType.BEARING_1D,DetectionType.BEARING_2D):
                sigma = math.sqrt(max(0.,detection.covariance.subset('azimuth_deg')[0,0]))
                geometry['azimuth_sigma_deg'] = sigma
                shape = ('angular',sensor.longitude,sensor.latitude,geometry['heading'],sigma,geometry['display_length'])
                geometry['_covariance_shape'] = shape;return shape
            else:return None
            if not np.isfinite(matrix).all():raise ValueError('Covariance projetée non finie')
            geometry['covariance_enu'] = matrix
            shape = ('ellipse',*geometry['position'],matrix)
            geometry['_covariance_shape'] = shape;return shape
        except (ValueError,TypeError,KeyError,np.linalg.LinAlgError) as exc:
            geometry['covariance_note'] = str(exc);return None

    def _draw(self, sensor, scan):
        prepared,warnings = self._prepare(sensor,scan)
        color = SENSOR_COLORS.get(sensor.sensor_type,SENSOR_COLORS['unknown'])
        point_spots = [];alarm_spots = [];rays = [];ray_refs = [];shapes = []
        limit = self.covariance_limit.value();omitted = 0
        pen = pg.mkPen('#fff4ec',width=1.2);brush = pg.mkBrush(color)
        alarm_pen = pg.mkPen('#ffe5ae',width=1.5);alarm_brush = pg.mkBrush('#ff896b')
        found_selection = False
        for detection,geometry in prepared:
            ref = (sensor.id,scan,detection,geometry)
            if 'position' in geometry:
                point_spots.append(dict(pos=geometry['position'],symbol='o',size=10,pen=pen,brush=brush,data=ref))
            elif '_ray_end' in geometry:
                rays.append((sensor.longitude,sensor.latitude,*geometry['_ray_end']));ray_refs.append(ref)
            else:alarm_spots.append(dict(pos=(sensor.longitude,sensor.latitude),symbol='t',size=20,pen=alarm_pen,brush=alarm_brush,data=ref))
            is_selected = self.selected == (sensor.id,detection.id)
            if self.show_covariances.isChecked() and (not self.selected_covariance.isChecked() or is_selected) and detection.covariance is not None:
                if not limit or len(shapes)<limit:
                    shape = self._ensure_covariance(sensor,detection,geometry)
                    if shape is not None:shapes.append(shape)
                else:omitted += 1
            if is_selected:
                found_selection = True
                if time.monotonic()-self._last_details_update >= .25:
                    self.select(sensor,scan,detection,geometry,reveal=False)
        if self.selected and self.selected[0] == sensor.id and not found_selection:
            self.selected = None;self.summary.setText('Plot remplacé ou expiré · détails conservés')
        layers = self._layers.get(sensor.id,{})
        entries = []
        for kind,spots in (('points',point_spots),('alarms',alarm_spots)):
            if spots:
                item = self._layer(sensor,kind);item.setData(spots);entries.append((item,False))
            elif kind in layers:layers[kind].setData([]);layers[kind].hide()

        if rays:
            array = np.asarray(rays);item = self._layer(sensor,'rays')
            item.setData(array[:,[0,2]].ravel(),array[:,[1,3]].ravel(),connect='pairs',pen=pg.mkPen(color,width=2))
            item._scan_refs = ray_refs;item._scan_segments = array;entries.append((item,False))
        elif 'rays' in layers:layers['rays'].setData([],[]);layers['rays']._scan_refs = [];layers['rays'].hide()

        if shapes:
            item = self._layer(sensor,'covariance');item.set_shapes(shapes,color);entries.append((item,True))
        elif 'covariance' in layers:layers['covariance'].set_shapes([],color);layers['covariance'].hide()

        self.items[sensor.id] = entries
        self._signatures[sensor.id] = self._signature(sensor)
        notes = list(warnings)

        if omitted:notes.append(f'{omitted} covariance(s) non affichée(s) : limite {limit} par capteur')
        return notes

    def _point_clicked(self, item, points, event):
        if not len(points):return
        key,scan,detection,geometry = points[0].data()
        sensor = self.window.sensors.get(key)
        if sensor is not None:self.select(sensor,scan,detection,geometry)

    def _ray_clicked(self, key, event):
        item = self._layers.get(key,{}).get('rays')
        if item is None or not item._scan_refs:return
        index = 0
        if event is not None:
            p = event.pos();pixel = self.window.plot.getViewBox().viewPixelSize()
            scale = np.maximum(np.abs(pixel),1e-15)
            seg = item._scan_segments
            starts = seg[:,:2]/scale;ends = seg[:,2:]/scale;q = np.array([p.x(),p.y()])/scale
            delta = ends-starts;den = np.maximum(np.sum(delta*delta,axis=1),1e-20)
            t = np.clip(np.sum((q-starts)*delta,axis=1)/den,0,1)
            index = int(np.argmin(np.sum((starts+t[:,None]*delta-q)**2,axis=1)))
        _,scan,detection,geometry = item._scan_refs[index];sensor = self.window.sensors.get(key)
        if sensor is not None:self.select(sensor,scan,detection,geometry)

    def _apply_visibility(self):
        for key,entries in self.items.items():
            visible = self.window._sensor_visible(key)
            for item,cov in entries:item.setVisible(visible and (self.show_covariances.isChecked() if cov else self.show_plots.isChecked()))

    def refresh_visibility(self, *_):
        for key in self.records:
            state = (self.window._sensor_visible(key),self.show_plots.isChecked(),self.show_covariances.isChecked())
            if self._visibility.get(key) != state:
                self._visibility[key] = state;self._mark_dirty(key)
        self._apply_visibility();self._update_count()

    def _update_count(self):
        n = sum(len(r.scan.detections) for r in self.records.values())
        notes = [note for values in self.render_warnings.values() for note in values]
        text = f'{n} détection(s) · {len(self.records)} scan(s) · {len(self.pending)} en attente · rendu {self.last_render_ms:.1f} ms'
        if any('non affichée(s)' in note for note in notes):text += ' · covariances limitées'
        if self.count.text() != text:self.count.setText(text)
        self.count.setToolTip('\n'.join(notes[:30]) or 'Dernier scan par capteur ; tous les scans restent disponibles au tracking et au recorder.')

    def expire(self, now=None):
        now = time.monotonic() if now is None else now
        for key,record in list(self.records.items()):
            if now-record.received >= self.lifetime.value():self._remove(key);self.records.pop(key,None)
        for key,record in list(self.pending.items()):
            if now-record.received >= self.lifetime.value():self.pending.pop(key)
        self._update_count()

    def clear(self):
        for key in list(self._layers):self._remove(key)
        self.records.clear();self.pending.clear();self._dirty.clear();self._prepared.clear()
        self._signatures.clear();self._visibility.clear();self._contexts.clear();self.items.clear();self.render_warnings.clear()
        self.selected = None;self.info.clear();self.classes.setRowCount(0)
        self.summary.setText('Cliquez sur un point, un rayon ou une alarme.');self._update_count()

    def close(self):
        self.render_timer.stop();self.timer.stop();self.clear()

    def select(self, sensor, scan, detection, geometry, reveal=True):
        self._last_details_update = time.monotonic()
        previous_selection = self.selected
        self.selected = (sensor.id, detection.id)
        self._ensure_covariance(sensor, detection, geometry)
        if previous_selection != self.selected and self.selected_covariance.isChecked():
            self._invalidate_display()
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
                      'Position WGS84 calculée à partir de la mesure de portée.']
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
                lines.append(f'{field} (aperçu) : ' + json.dumps(_preview(detection.raw[field]), ensure_ascii=False))
        classification = getattr(detection, 'classification', None)
        if not isinstance(classification, dict):
            classification = detection.raw.get('classification', {})
        labels = getattr(detection, 'labels', [])
        scores = getattr(detection, 'scores', [])
        if isinstance(labels, dict): pairs = list(labels.items())
        elif isinstance(scores, dict): pairs = list(scores.items())
        elif isinstance(classification, dict) and 'labels' not in classification and classification:
            pairs = [(k, v) for k, v in classification.items() if isinstance(v, (float, int))]
        else: pairs = list(zip(labels, scores))
        lines.append(f'Somme des scores reçus : {sum(float(v) for _, v in pairs):.6f} (sans renormalisation)')
        self.info.setPlainText('\n'.join(lines))
        self.classes.setRowCount(len(pairs))
        for row, (label, score) in enumerate(pairs):
            self.classes.setItem(row, 0, QTableWidgetItem(str(label)))
            self.classes.setItem(row, 1, QTableWidgetItem(f'{float(score):.6f}'))
        if reveal:
            self.dock.show(); self.dock.raise_()
