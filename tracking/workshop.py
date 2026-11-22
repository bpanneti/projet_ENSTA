"""Docks intégrés à Capteurs Live. Le moteur s'exécute dans un thread Qt dédié."""
from collections import OrderedDict, deque
import copy
import json
import time
from socket import send_fds
import traceback
import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QObject, QThread, QTimer, Qt, pyqtSignal, pyqtSlot, QSignalBlocker
from PyQt6.QtWidgets import (QWidget, QMessageBox, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel,
    QComboBox, QDoubleSpinBox, QSpinBox, QCheckBox, QPushButton, QPlainTextEdit,
    QTreeWidget, QTreeWidgetItem, QFileDialog, QTabWidget, QApplication, QScrollArea)
from scan_graphics import ellipse_item
from .engine import Configuration, TrackingEngine
from .adapter import observations
from .models import LocalFrame, rotation
from .coverage import SensorCoverage
from graphics_items import GEOD


class TrackingWorker(QObject):
    finished = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.engines = {}

    @pyqtSlot(object)
    def execute(self, job):
        kind, payload = job


        recording_context = payload[4] if kind == 'scan' else None
        results = {}
        start = time.perf_counter()
        try:
            if kind == "shutdown":
                self.thread().quit()
                return
            elif kind == 'configure':
                name, config = payload
                self.engines[name] = TrackingEngine(config)
                results[name] = self.engines[name].snapshot()
            elif kind == 'remove':
                self.engines.pop(payload, None)
                results[payload] = {'tracks': [], 'warnings': []}
            elif kind == 'reset':
                self.engines = {name: TrackingEngine(engine.config) for name, engine in self.engines.items()}
                results = {name: engine.snapshot() for name, engine in self.engines.items()}
            elif kind == 'scan':


                scan, position, sensor_rotation, world_rotation, recording_context, reference_frame, time_reference, coverage= payload
       
                if len(scan.detections) > 100:
                    raise ValueError('Atelier limité à 100 plots par scan : scan non traité')
                for name, engine in self.engines.items():
                    try:
                        measurements, warnings = observations(scan, position, sensor_rotation, world_rotation, engine.config)
                        groups = {}

                        for measurement in measurements:
                            groups.setdefault(measurement.timestamp, []).append(measurement)
                        if not groups and scan.detections:
                            result = engine.snapshot()
                        else:
                            if not groups:
                                groups[float(scan.timestamp)] = []
                            for stamp, batch in sorted(groups.items()):
                                result = engine.step(batch, stamp,coverage=coverage)

                                '''
                                if recording_context is not None:
                                    ctx = recording_context

                                    print(
                                        "[AVANT SQL]",
                                        "step =", result.get("step_index"),
                                        "receipt =", ctx.receipt.receipt_id,
                                        "frame =", ctx.frame_index,
                                        "released =", ctx._released,
                                        flush=True,
                                    )

                                    try:
                                        future = ctx.recorder.record_tracking(
                                            result,
                                            context=ctx,
                                            config=engine.config,
                                            name=name,
                                            reference_frame=reference_frame,
                                            time_reference=time_reference,
                                        )

                                        print("[SQL SOUMIS]", flush=True)

                                        def verifier_ecriture(f):
                                            try:
                                                print("[SQL TERMINÉ]", f.result(), flush=True)
                                            except Exception:
                                                traceback.print_exc()

                                        future.add_done_callback(verifier_ecriture)

                                    except Exception:
                                        traceback.print_exc()
                                '''
                                if recording_context is not None:
                                    recording_context.save(result, engine.config, name, reference_frame, time_reference)
                                result['warnings'] = warnings + result.get('warnings', [])
                    except Exception as exc:
                        result = engine.snapshot()
                        result['warnings'] = [f'Scan non traité : {exc}']
                    results[name] = result
        except Exception as exc:
            self.finished.emit({'error': str(exc), 'results': {}})
            return
        finally:
            if recording_context is not None:
                recording_context.release()

        self.finished.emit({'results': results, 'elapsed_ms': (time.perf_counter()-start)*1000})


class TrackingWorkshop(QObject):
    command = pyqtSignal(object)
    COLORS = ['#66d9ef', '#ffbd69', '#c792ea', '#95e06c']

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.frame = None
        self.experiments = OrderedDict()
        self.snapshots = {}
        self.graphics = {}
        self.hidden = set()
        self.waiting = OrderedDict()
        self.jobs = deque()
        self.busy = False
        self.closed = False
        self.serial = 0
        self._panel()
        self.thread = QThread(self)
        self.worker = TrackingWorker()
        self.worker.moveToThread(self.thread)
        self.command.connect(self.worker.execute)
        self.worker.finished.connect(self._finished)
        self.thread.finished.connect(self.worker.deleteLater)
        self.thread.start()
        QApplication.instance().aboutToQuit.connect(self.close)
        self.retry = QTimer(self)
        self.retry.setInterval(500)
        self.retry.timeout.connect(self.refresh_pending)
        self.retry.start()
        self.add_experiment()

        self._graphics_dirty = True
        self._tree_dirty = True
        self._last_tree_refresh = 0.0

        self.render_timer = QTimer(self)
        self.render_timer.setInterval(100)  # Timer graphique : 10 Hz
        self.render_timer.timeout.connect(self._flush_tracking_view)
        self.render_timer.start()
    def _combo(self, values):
        box = QComboBox(); box.addItems(values)
        return box

    def _number(self, value, minimum=.000001, maximum=1e9, decimals=4):
        box = QDoubleSpinBox(); box.setDecimals(decimals); box.setRange(minimum, maximum); box.setValue(value)
        return box

    def _panel(self):
        panel = QWidget(); panel.setObjectName('root'); layout = QVBoxLayout(panel)
        panel.setStyleSheet('QWidget { background-color: #101c2b; color: #dce9f7; } QTabWidget::pane { border: 1px solid #31465e; }')
        self.enabled = QCheckBox('Activer le tracking des scans reçus')
        layout.addWidget(self.enabled)
        self.enabled.toggled.connect(lambda _: self.waiting.clear())
        self.family = self._combo(['Monocible', 'Multicible'])
        self.filter = self._combo(['CMKF', 'EKF', 'IMM']); self.filter.setCurrentText('EKF')
        self.dynamic = self._combo(['CV', 'CA','CV+CV_2','CV+CA'])
        #self.filter.currentTextChanged.connect(lambda text: self.dynamic.setEnabled(text != 'IMM'))
        self.association = self._combo(['GNNSF', 'JPDAF', 'SDA — étape suivante'])
        self.association.model().item(2).setEnabled(False)
        self.association.setToolTip('SDA n’est pas implémenté dans cette version')
        self.sensor = QComboBox(); self.sensor.addItem('Premier capteur résolu (conseillé)', 'first')
        self.sensor.addItem('Tous les capteurs (hypothèse Pd uniforme)', '*')
        self.sensor.setCurrentIndex(self.sensor.findData("*"))
        self.first_sensor = None
        form = QFormLayout()
        form.addRow('Famille', self.family); form.addRow('Filtre', self.filter)
        form.addRow('Dynamique', self.dynamic); form.addRow('Association multicible', self.association)
        form.addRow('Source de plots', self.sensor); layout.addLayout(form)
        note = QLabel('CMKF : mesures de portée converties en cartésien au premier ordre.\nEKF : mesures natives. IMM : EKF CV/CA en état commun 9D.\nÉtats : E,N,U,vE,vN,vU,(aE,aN,aU), en m, m/s, m/s².')
        note.setWordWrap(True); layout.addWidget(note)
        tabs = QTabWidget(); layout.addWidget(tabs)
        parameters = QWidget(); params = QFormLayout(parameters)
        self.numbers = {}
        specifications = [
            ('process_noise_cv', 'Intensité Q CV (m²/s³)', 1.),
            ('process_noise_ca', 'Intensité Q CA (m²/s⁵)', .2),
            ('process_noise_cv_2', 'Intensité (second modèle) Q CV (m²/s³)', 10.),
            ('sigma_range', 'σ distance (m)', 10.),
            ('sigma_azimuth_deg', 'σ azimut (°)', 1.),
            ('sigma_elevation_deg', 'σ élévation (°)', 1.),
            ('sigma_position', 'σ position initiale ajouté (m)', 20.),
            ('sigma_velocity', 'σ vitesse initiale (m/s)', 30.),
            ('sigma_acceleration', 'σ accélération initiale (m/s²)', 5.),
            ('deletion_seconds', 'Suppression sans mesure (s simulation)', 10.)]
        for key, label, value in specifications:
            self.numbers[key] = self._number(value)
            params.addRow(label, self.numbers[key])
        self.received_R = QCheckBox('Utiliser la covariance du plot si disponible'); self.received_R.setChecked(True)
        params.addRow(self.received_R)
        tabs.addTab(parameters, 'Bruits')
        association = QWidget(); assoc = QFormLayout(association)
        for key, label, value in [('gate_probability', 'Probabilité du gating', .9973),
                                  ('detection_probability', 'Probabilité de détection', .9),
                                  ('clutter_density', 'Densité clutter / volume de mesure', 1e-5)]:
            self.numbers[key] = self._number(value, 1e-9, .999999 if key != 'clutter_density' else 1e9, 9)
            assoc.addRow(label, self.numbers[key])
        self.hits = QSpinBox(); self.hits.setRange(1, 100); self.hits.setValue(3)
        self.window_hits = QSpinBox(); self.window_hits.setRange(1, 100); self.window_hits.setValue(5)
        assoc.addRow('Confirmation M', self.hits); assoc.addRow('Fenêtre N', self.window_hits)
        self.bearing_prior = QCheckBox('Autoriser une portée initiale supposée (bearing only)')
        assoc.addRow(self.bearing_prior)
        self.prior_range = self._number(1000); self.prior_sigma = self._number(1000)
        assoc.addRow('Portée supposée (m)', self.prior_range); assoc.addRow('σ portée supposée (m)', self.prior_sigma)
        tabs.addTab(association, 'Association / initialisation')
        advanced = QWidget(); advanced_layout = QVBoxLayout(advanced)
        label = QLabel('JSON avancé : P0_diagonal, Q_override, R_override, imm_transition, imm_probabilities…\nR_override : mètres/radians. Q_override : matrice fixe par pas.\nLes changements créent ou réinitialisent une expérience ; aucun effet rétroactif.')
        label.setWordWrap(True); advanced_layout.addWidget(label)
        self.expert = QPlainTextEdit('{}'); self.expert.setMinimumHeight(100); advanced_layout.addWidget(self.expert)
        tabs.addTab(advanced, 'Matrices / IMM')
        self.experiment = QComboBox(); layout.addWidget(self.experiment)
        row = QHBoxLayout()
        for title, slot in [('Ajouter', self.add_experiment), ('Appliquer / réinitialiser', self.apply), ('Supprimer', self.remove_experiment)]:
            button = QPushButton(title); button.clicked.connect(slot); row.addWidget(button)
        layout.addLayout(row)
        row = QHBoxLayout()
        for title, slot in [('Charger JSON', self.load_config), ('Sauver JSON', self.save_config), ('Tout effacer', self.reset)]:
            button = QPushButton(title); button.clicked.connect(slot); row.addWidget(button)
        layout.addLayout(row)
        self.status = QLabel('Atelier prêt'); self.status.setWordWrap(True); layout.addWidget(self.status)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(panel)
        self.dock = self.window._dock('Atelier tracking', 'trackingWorkshopDock', scroll, Qt.DockWidgetArea.RightDockWidgetArea)
        tracks_panel = QWidget(); tracks_panel.setObjectName('root'); tracks_layout = QVBoxLayout(tracks_panel)
        self.show_tracks = QCheckBox('Afficher les pistes'); self.show_tracks.setChecked(True)
        self.show_history = QCheckBox('Afficher les historiques'); self.show_history.setChecked(True)
        self.show_covariance = QCheckBox('Afficher la covariance de position (3σ)'); self.show_covariance.setChecked(True)
        self.show_velocity = QCheckBox('Afficher la vitesse (projection à 5 s)'); self.show_velocity.setChecked(True)
        for box in (self.show_tracks, self.show_history, self.show_covariance, self.show_velocity):
            tracks_layout.addWidget(box); box.toggled.connect(self._draw)
        self.tree = QTreeWidget(); self.tree.setHeaderLabels(['Expérience / piste', 'État', 'Date simulation'])
        self.tree.itemChanged.connect(self._checked)
        self.tree.itemClicked.connect(self._selected)
        tracks_layout.addWidget(self.tree)
        self.details = QPlainTextEdit(); self.details.setReadOnly(True); tracks_layout.addWidget(self.details)
        self.tracks_dock = self.window._dock('Pistes / diagnostics', 'trackingTracksDock', tracks_panel, Qt.DockWidgetArea.RightDockWidgetArea)
        self.window.tabifyDockWidget(self.dock, self.tracks_dock)
        self.experiment.currentTextChanged.connect(self._load_selected)

    def _configuration(self):
        values = Configuration().to_dict()
        values.update({key: box.value() for key, box in self.numbers.items()})
        values.update(family=self.family.currentText(), estimator=self.filter.currentText(), dynamics=self.dynamic.currentText(),
            association=self.association.currentText(), use_received_R=self.received_R.isChecked(),
            confirmation_hits=self.hits.value(), confirmation_window=self.window_hits.value(),
            allow_bearing_prior=self.bearing_prior.isChecked(), bearing_prior_range=self.prior_range.value(),
            bearing_prior_sigma=self.prior_sigma.value())
        advanced = json.loads(self.expert.toPlainText())
        if not isinstance(advanced, dict):
            raise ValueError('Le JSON avancé doit être un objet')
        values.update(advanced)
        return Configuration(**values).validate()

    def _display_config(self, c):
        self.family.setCurrentText(c.family); self.filter.setCurrentText(c.estimator)
        self.dynamic.setCurrentText(c.dynamics); self.association.setCurrentText(c.association)
        for key, box in self.numbers.items():
            box.setValue(getattr(c, key))
        self.received_R.setChecked(c.use_received_R); self.hits.setValue(c.confirmation_hits)
        self.window_hits.setValue(c.confirmation_window); self.bearing_prior.setChecked(c.allow_bearing_prior)
        self.prior_range.setValue(c.bearing_prior_range); self.prior_sigma.setValue(c.bearing_prior_sigma)
        visible = set(self.numbers) | {'family', 'estimator', 'dynamics', 'association', 'use_received_R',
            'confirmation_hits', 'confirmation_window', 'allow_bearing_prior', 'bearing_prior_range', 'bearing_prior_sigma'}
        self.expert.setPlainText(json.dumps({key: value for key, value in c.to_dict().items() if key not in visible}, indent=2))

    def _load_selected(self, name):
        if name in self.experiments:
            self._display_config(self.experiments[name]['config'])

    def add_experiment(self):
        try:
            if len(self.experiments) >= 4:
                raise ValueError('Maximum quatre expériences simultanées')
            config = self._configuration()
        except (ValueError, TypeError) as exc:
            self.status.setText(str(exc)); return
        self.serial += 1

        model_name = config.dynamics
        name = f'E{self.serial:02d} {config.estimator}/{model_name}'
        used = {e['color'] for e in self.experiments.values()}
        color = next(c for c in self.COLORS if c not in used)
        self.experiments[name] = {'config': config, 'color': color}
        self.experiment.addItem(name); self.experiment.setCurrentText(name)
        self._enqueue(('configure', (name, config)))

    def apply(self):
        name = self.experiment.currentText()
        if name not in self.experiments:
            return
        try:
            config = self._configuration()

        except (ValueError, TypeError) as exc:
            self.status.setText(str(exc)); return
        self.experiments[name]['config'] = config
        self._enqueue(('configure', (name, config)))

    def remove_experiment(self):
        name = self.experiment.currentText()
        self.experiments.pop(name, None); self.snapshots.pop(name, None)
        self.experiment.removeItem(self.experiment.currentIndex())
        self._enqueue(('remove', name)); self._draw()

    def save_config(self):
        try:
            data = self._configuration().to_dict()
            path, _ = QFileDialog.getSaveFileName(self.window, 'Configuration tracking', '', 'JSON (*.json)')
            if path:
                with open(path, 'w', encoding='utf-8') as output:
                    json.dump(data, output, indent=2, ensure_ascii=False)
        except (OSError, ValueError, TypeError) as exc:
            self.status.setText(str(exc))

    def load_config(self):
        path, _ = QFileDialog.getOpenFileName(self.window, 'Configuration tracking', '', 'JSON (*.json)')
        if not path:
            return
        try:
            with open(path, encoding='utf-8') as source:
                config = Configuration(**json.load(source)).validate()
            self._display_config(config)
            self.status.setText('Configuration chargée : Ajouter ou Appliquer pour l’utiliser')
        except (OSError, ValueError, TypeError) as exc:
            self.status.setText(str(exc))

    def _enqueue(self, job):
        if self.closed:
            return
        if job[0] == 'scan' and sum(j[0] == 'scan' for j in self.jobs) >= 32:
            self.status.setText('File tracking pleine : scan non traité. Réduire le débit ou les hypothèses JPDA.')
            return
        self.jobs.append(job)
        self._dispatch()

    def _dispatch(self):
        if not self.busy and self.jobs and not self.closed:
            self.busy = True
            self.command.emit(self.jobs.popleft())

    def receive(self, scan, source='' ,recording_context=None):

        try:
            self._receive(scan, source, recording_context)
        except Exception:
            if recording_context is not None:
                recording_context.release()
            raise

    def _receive(self, scan, source='', recording_context=None):
        if not self.enabled.isChecked() or self.closed:
            return
        if not self.enabled.isChecked() or self.closed:
            if recording_context is not None:
                recording_context.release()
            return

        matches = [s for s in self.window.sensors.values()
            if str(s.sensor_id or s.id) == str(scan.sensor_id)
            and (scan.node_id is None or str(s.node_id) == str(scan.node_id))]
        if len(matches) != 1 or not matches[0].position_resolved:
            key = (str(source), scan.node_id, str(scan.sensor_id))
            self.waiting[key] = (copy.deepcopy(scan), source,recording_context)
            self.waiting.move_to_end(key)
            if len(self.waiting) > 128:
                self.waiting.popitem(last=False)
                self.status.setText('Limite des scans en attente : ancien scan abandonné')
            else:
                self.status.setText(f'Tracking en attente d’un capteur unique géolocalisé : {scan.sensor_id}')
            return
        sensor = matches[0]
        if self.first_sensor is None:
            self.first_sensor = sensor.id
            
        if self.sensor.currentData() == 'first' and sensor.id != self.first_sensor:
            if recording_context is not None:
                recording_context.release()
            return
        
        
        if self.frame is None:
            self.frame = LocalFrame(sensor.longitude, sensor.latitude, sensor.altitude)
        node = self.window.nodes.get(sensor.node_id)


        local_basis = self.frame.sensor_basis(sensor.longitude, sensor.latitude)
        orientation = rotation(sensor.yaw, sensor.pitch, sensor.roll)
        if node  :
            orientation = rotation(node.yaw, node.pitch, node.roll) @ orientation

        reference_frame = {
            "origin_wgs84": list(self.frame.origin),
            "crs": "local_ENU",
            "altitude": "WGS84_ellipsoidal",
        }

        time_reference = {
            "kind": "scan_timestamp",
            "unit": "s",
        }


    

        _position = self.frame.to_local(sensor.longitude, sensor.latitude)

        coverage = SensorCoverage.from_areas(
            str(scan.sensor_id),
            _position,
            local_basis @ orientation,
            sensor.areas,
        )
   
        #orientation = np.eye(3)
        context = (copy.deepcopy(scan), self.frame.to_local(sensor.longitude, sensor.latitude, sensor.altitude),
                   local_basis @ orientation, local_basis,recording_context,reference_frame,time_reference,coverage)
        self._enqueue(('scan', context))

    def refresh_pending(self):
        for key, (scan, source,context) in list(self.waiting.items()):
            self.waiting.pop(key, None)
            self.receive(scan, source)

    def reset(self):
        self.waiting.clear(); self.first_sensor = None
        self.jobs = deque(job for job in self.jobs if job[0] != 'scan')
        self._enqueue(('reset', None))

    def _finished(self, message):
        self.busy = False
        if self.closed:
            return
        if 'error' in message:
            self.status.setText(message['error'])
        else:
            warnings = []
            for name, snapshot in message['results'].items():
                if name in self.experiments:
                    self.snapshots[name] = snapshot
                    warnings.extend(f'{name} : {w}' for w in snapshot['warnings'])
            total = sum(len(s['tracks']) for s in self.snapshots.values())
            origin = '' if self.frame is None else f' · origine ENU {self.frame.origin[0]:.6f}°, {self.frame.origin[1]:.6f}°'
            self.status.setText((' | '.join(warnings) if warnings else
                f'{total} piste(s) · calcul {message.get("elapsed_ms", 0):.1f} ms{origin}'))
            self._tree_dirty = True
            self._draw()
        self._dispatch()

    def _tree(self):
        blocker = QSignalBlocker(self.tree)
        self.tree.clear()
        for name, experiment in self.experiments.items():
            c = experiment['config']
            model_name =  c.dynamics
            root = QTreeWidgetItem([name, f'{c.estimator}/{model_name}', ''])
            root.setData(0, Qt.ItemDataRole.UserRole, (name, None))
            root.setFlags(root.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            root.setCheckState(0, Qt.CheckState.Unchecked if (name, None) in self.hidden else Qt.CheckState.Checked)
            self.tree.addTopLevelItem(root); root.setExpanded(True)
            for track in self.snapshots.get(name, {}).get('tracks', []):
                item = QTreeWidgetItem([track['id'], track['status']+(' · prédite' if track['coasting'] else ''), f'{track["timestamp"]:.3f}'])
                key = (name, track['id']); item.setData(0, Qt.ItemDataRole.UserRole, key)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, Qt.CheckState.Unchecked if key in self.hidden else Qt.CheckState.Checked)
                root.addChild(item)
        del blocker

    def _checked(self, item, column):
        if column == 0:
            key = item.data(0, Qt.ItemDataRole.UserRole)
            if item.checkState(0) == Qt.CheckState.Checked:
                self.hidden.discard(key)
            else:
                self.hidden.add(key)
            self._draw()

    def _selected(self, item, column=0):
        self._details(item.data(0, Qt.ItemDataRole.UserRole))

    def _details(self, key):
        if key is None:
            return
        name, identifier = key
        track = next((t for t in self.snapshots.get(name, {}).get('tracks', []) if t['id'] == identifier), None)
        if track is None:
            return
        data = {k: v for k, v in track.items() if k != 'history'}
        if self.frame:
            data['WGS84_lon_lat_alt_m'] = self.frame.to_wgs84(track['x'])
        self.details.setPlainText(json.dumps(data, indent=2, ensure_ascii=False,
            default=lambda value: value.tolist() if isinstance(value, np.ndarray) else str(value)))

    def _draw(self, *_):

        # Demander un affichage, sans dessiner immédiatement.
        self._graphics_dirty = True

    def _flush_tracking_view(self):
        if self.closed:
            return

        if self._graphics_dirty:
            self._graphics_dirty = False
            self._draw_now()

        # L'arbre n'a pas besoin d'être reconstruit à chaque résultat.
        now = time.monotonic()
        if self._tree_dirty and now - self._last_tree_refresh >= 0.5:
            self._tree_dirty = False
            self._last_tree_refresh = now
            self._tree()

    def _draw_now(self):

        active = set()

        if self.frame is not None and self.show_tracks.isChecked():
            angles = np.linspace(0, 2 * np.pi, 49)
            unit = np.array([np.cos(angles), np.sin(angles)])

            for name, snapshot in self.snapshots.items():
                if name not in self.experiments:
                    continue
                if (name, None) in self.hidden:
                    continue

                color = self.experiments[name]["color"]

                for track in snapshot["tracks"]:
                    key = (name, track["id"])
                    if key in self.hidden:
                        continue

                    active.add(key)

                    # Créer les objets une seule fois par piste.
                    if key not in self.graphics:
                        marker = pg.ScatterPlotItem(
                            pxMode=True,
                            hoverable=False,
                            antialias=False,
                        )
                        marker.setZValue(30)
                        marker.sigClicked.connect(
                            lambda item, points, event:
                            self._details(points[0].data())
                            if len(points) else None
                        )

                        label = pg.TextItem(anchor=(0, 1))
                        label.setZValue(31)

                        velocity = pg.PlotCurveItem(antialias=False)
                        history = pg.PlotCurveItem(antialias=False)
                        covariance = pg.PlotCurveItem(antialias=False)

                        self.graphics[key] = [
                            marker, label, velocity, history, covariance
                        ]

                        for item in self.graphics[key]:
                            self.window.plot.addItem(item)

                    marker, label, velocity, history, covariance = (
                        self.graphics[key]
                    )

                    x = np.asarray(track["x"], dtype=float)
                    lon, lat, _ = self.frame.to_wgs84(x)

                    marker.setData(
                        [lon], [lat],
                        data=[key],
                        size=10,
                        symbol="t1" if track["coasting"] else "o",
                        pen=pg.mkPen(color),
                        brush=pg.mkBrush(color),
                    )
                    
                    label.setText(
                        f'{name.split()[0]} / {track["id"]}',
                        color=color,
                    )
                    label.setPos(lon, lat)

                    # Vecteur vitesse.
                    if self.show_velocity.isChecked():
                        ex, ey, _ = self.frame.to_wgs84(
                            x[:3] + 5 * x[3:6]
                        )
                        velocity.setData(
                            [lon, ex], [lat, ey],
                            pen=pg.mkPen(color, width=2),
                        )
                        velocity.show()
                    else:
                        velocity.hide()

                    # Limiter seulement l'historique GRAPHIQUE.
                    samples = (
                        track.get("history", [])[-200:]
                        if self.show_history.isChecked() else []
                    )

                    if len(samples) > 1:
                        points = np.asarray(
                            [p for _, p in samples], dtype=float
                        )[:, :3]

                        # Conversion vectorisée de tout l'historique affiché.
                        ecef = (
                                self.frame.ecef[:, None]
                                + self.frame.basis.T @ points.T
                        )
                        hx, hy, _ = self.frame.inverse.transform(*ecef)

                        history.setData(
                            hx, hy,
                            pen=pg.mkPen(color, width=1),
                        )
                        history.show()
                    else:
                        history.setData([], [])
                        history.hide()

                    covariance.hide()

                    if self.show_covariance.isChecked():
                        # Repère tangent calculé directement :
                        # aucun nouveau LocalFrame / Transformer.
                        l, p = np.radians([lon, lat])
                        B = np.array([
                            [-np.sin(l), np.cos(l), 0],
                            [
                                -np.sin(p) * np.cos(l),
                                -np.sin(p) * np.sin(l),
                                np.cos(p),
                            ],
                            [
                                np.cos(p) * np.cos(l),
                                np.cos(p) * np.sin(l),
                                np.sin(p),
                            ],
                        ])

                        basis = self.frame.basis @ B.T
                        P = np.asarray(
                            track["P"], dtype=float
                        )[:3, :3]

                        C = (basis.T @ P @ basis)[:2, :2]
                        values, vectors = np.linalg.eigh((C + C.T) / 2)

                        if (
                                np.isfinite(values).all()
                                and values.min() >= -1e-8
                        ):
                            offsets = vectors @ (
                                    3 * np.sqrt(
                                np.maximum(values, 0)
                            )[:, None] * unit
                            )

                            distances = np.hypot(
                                offsets[0], offsets[1]
                            )
                            headings = np.degrees(
                                np.arctan2(offsets[0], offsets[1])
                            )

                            # Un appel géodésique vectorisé.
                            cx, cy, _ = GEOD.fwd(
                                np.full(49, lon),
                                np.full(49, lat),
                                headings,
                                distances,
                            )

                            cx = (
                                    lon
                                    + (np.asarray(cx) - lon + 180) % 360
                                    - 180
                            )

                            covariance.setData(
                                cx, cy,
                                pen=pg.mkPen(
                                    color,
                                    width=1,
                                    style=Qt.PenStyle.DotLine,
                                ),
                            )
                            covariance.show()

        # Supprimer seulement les pistes disparues ou masquées.
        for key in list(self.graphics):
            if key not in active:
                for item in self.graphics.pop(key):
                    self.window.plot.removeItem(item)
    '''
    def _draw(self, *_):

        for items in self.graphics.values():
            for item in items:
                self.window.plot.removeItem(item)
        self.graphics.clear()
        if self.frame is None or not self.show_tracks.isChecked():
            return
        for name, snapshot in self.snapshots.items():
            if name not in self.experiments or (name, None) in self.hidden:
                continue
            color = self.experiments[name]['color']
            for track in snapshot['tracks']:
                key = (name, track['id'])
                if key in self.hidden:
                    continue


                lon, lat, _ = self.frame.to_wgs84(track['x'])
                marker = pg.ScatterPlotItem([lon], [lat], symbol='t1' if track['coasting'] else 'o',
                    size=12, pen=pg.mkPen(color, width=2), brush=pg.mkBrush(color), data=[key],
                    hoverable=True, tip=lambda x, y, data: f'{data[0]} · {data[1]}')
                marker.setZValue(30)
                #marker.sigClicked.connect(lambda item, points, event: self._details(points[0].data()) if len(points) else None)
                label = pg.TextItem(f'{name.split()[0]} / {track["id"]}', color=color, anchor=(0, 1))
                label.setPos(lon, lat); label.setZValue(31)
                items = [marker, label]
          
                if self.show_velocity.isChecked():
                    end_lon, end_lat, _ = self.frame.to_wgs84(track['x'][:3] + 5*track['x'][3:6])
                    items.append(pg.PlotCurveItem([lon, end_lon], [lat, end_lat], pen=pg.mkPen(color, width=3)))
                if self.show_history.isChecked() and len(track['history']) > 1:
                    coords = [self.frame.to_wgs84(point) for _, point in track['history']]
                    items.append(pg.PlotCurveItem([c[0] for c in coords], [c[1] for c in coords], pen=pg.mkPen(color, width=2)))
                if self.show_covariance.isChecked():
                    # Covariance projetée dans la tangente locale de la position affichée.
                    basis = self.frame.sensor_basis(lon, lat)
                    P = basis.T @ track['P'][:3, :3] @ basis

                    item = ellipse_item(lon, lat, P[:2, :2], color)
                    if item is not None:
                        items.append(item)
        
                for item in items:
                    self.window.plot.addItem(item)
                self.graphics[key] = items
    '''
    def close(self):
        if self.closed:
            return
        self.render_timer.stop()
        self.closed = True
        self.retry.stop()

        # Les scans attendant encore un capteur ne peuvent pas être traités.
        # Format attendu : (scan, source, recording_context).
        for scan, source, context in self.waiting.values():
            if context is not None:
                context.release()

        self.waiting.clear()

        # Transmettre directement les travaux restants au worker.
        # La fermeture bloque les rafraîchissements Qt : on ne dépend donc
        # plus de _finished() pour envoyer le travail suivant.
        while self.jobs:
            self.command.emit(self.jobs.popleft())

        # Ce message sera traité après les travaux précédents.
        self.command.emit(("shutdown", None))

        # Attendre la fin du worker avant de fermer SQLite.
        self.thread.wait()
