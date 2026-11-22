import math
import sys
import os
os.environ["QT_QPA_PLATFORM"] = "xcb"
import pyqtgraph as pg
from PyQt6.QtCore import QSignalBlocker, QTime, Qt, QElapsedTimer, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QAbstractItemView, QDockWidget, QFrame, QHeaderView,
    QHBoxLayout, QLabel, QMainWindow, QPlainTextEdit, QPushButton,QMessageBox,
    QTableWidget, QTableWidgetItem, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,QTabWidget
)

from connections import ConnectionClient
from dialogs import ConnectionPanel
from graphics_items import (
    DegreeAxisItem, DetectionItem, FootprintItem, NodeItem, SensorItem,
    SENSOR_COLORS, SENSOR_LABELS, GEOD, sensor_symbol,TargetItem
)
from models import Detection, Footprint
from entity.sensor import Sensor
from entity.node import Node
from entity.scan import Scan
from entity.target import Target
from scan_display import ScanDisplay
from message_parser import MessageParser
from theme import apply_theme
from datetime import datetime, timedelta, timezone
from timer_widget import TimerWidget
from terrain_layer import TerrainLayer
import time
from pathlib import Path
from sqlite_recorder import RecordingSession, RecordingError
from tracking.workshop import TrackingWorkshop
from performance_widget import PerformanceDock

class MainWindow(QMainWindow):

    database_error = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Capteurs Live · WGS84")
        self.resize(1480, 940)
        self.setDockNestingEnabled(True)
        self._items = {}
        self.nodes = {}
        self.sensors = {}
        self.targets = {}
        self.volumes = {}  # (sensor_id, volume_id) -> Footprint
        self.detections = {}
        self._hidden = set()
        self._visible_types = {kind: True for kind in SENSOR_COLORS}
        self._batch = False
        self.messages_count = 0
        self.message_parser = MessageParser()
        self._live_scene = False
        self._auto_frame_live = False
        pg.setConfigOptions(antialias=True, background="#101d2c", foreground="#a9bed4")
        self.plot = pg.PlotWidget(axisItems={
            "bottom": DegreeAxisItem(orientation="bottom"),
            "left": DegreeAxisItem(orientation="left"),
        })
        self.plot.setLabel("bottom", "Longitude (en °)")
        self.plot.setLabel("left", "Latitude (en °)")
        for axis in ("bottom", "left"):
            self.plot.getAxis(axis).enableAutoSIPrefix(False)
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.setAspectLocked(True, ratio=math.cos(math.radians(43.12)))
        self.plot.getViewBox().sigRangeChanged.connect(self._update_map_aspect)
        self._build_layout()

        self.terrain_layer = TerrainLayer(self)

        #Performances

        self.performance_dock = PerformanceDock(self, database_path_provider=lambda: self.recorder.path)


        #---> timer

        self.timer_widget = TimerWidget(self)

        self.timer_dock = self._dock(
            "Horloge de simulation",
            "timerDock",
            self.timer_widget,
            Qt.DockWidgetArea.RightDockWidgetArea,
        )

        self.timer_widget.time_changed.connect(
            self._on_simulation_time_changed
        )

        self.scan_display = ScanDisplay(self)
        self.scan_display.show_plots.toggled.connect(self._refresh_visibility )
        self.tracking_workshop = TrackingWorkshop(self)

        self.setDockOptions(
            QMainWindow.DockOption.AllowTabbedDocks
            | QMainWindow.DockOption.AllowNestedDocks
            | QMainWindow.DockOption.AnimatedDocks
        )

        # Position des onglets en bas.
        self.setTabPosition(
            Qt.DockWidgetArea.AllDockWidgetAreas,
            QTabWidget.TabPosition.South,
        )

        # Regrouper tous les docks à droite.
        docks = self.findChildren(
            QDockWidget,
            options=Qt.FindChildOption.FindDirectChildrenOnly,
        )
        if docks:
            first_dock = docks[0]

            for dock in docks:
                dock.setFloating(False)
                self.addDockWidget(
                    Qt.DockWidgetArea.RightDockWidgetArea,
                    dock,
                )

            for dock in docks[1:]:
                self.tabifyDockWidget(first_dock, dock)

            first_dock.show()
            first_dock.raise_()

        menu = self.menuBar().addMenu("Connexion")
        menu.addAction("Afficher les réglages", self.open_connection_dialog)
        menu.addAction("Déconnecter / arrêter", self.disconnect_client)
        view = self.menuBar().addMenu("Affichage")
        for dock in self.docks:
            view.addAction(dock.toggleViewAction())
        view.addSeparator()
        view.addAction("Réinitialiser les panneaux", lambda: self.restoreState(self._default_layout))
        #view.addAction("Scène de démonstration", self.load_demo)
        view.addAction("Effacer les objets", self.clear_objects)
        self.indicator = QLabel()
        self.statusBar().addPermanentWidget(self.indicator)
        self.counter = QLabel("Messages : 0")
        self.statusBar().addPermanentWidget(self.counter)
        self.client = ConnectionClient(self)
        self.client.state_changed.connect(self.set_connection_state)
        self.client.message_received.connect(self.handle_message)
        self.client.source_closed.connect(self.message_parser.remove_source)
        self.client.transport_reset.connect(self.message_parser.clear)
        self.client.client_event.connect(self.show_client_event)
        self.client.clients_changed.connect(lambda count: self.client_count.setText(f"Clients TCP : {count}"))
        self.client.clients_snapshot.connect(self.update_connected_clients)
        self.client.server_health.connect(self.update_server_health)
        self.verify_button.clicked.connect(self.client.check_server)
        self.connection_panel.connect_requested.connect(self.connect_from_parameters)
        self.connection_panel.disconnect_requested.connect(self.disconnect_client)
        self.client.check_server()
        self.set_connection_state("déconnecté", "Aucune connexion")

       # self.load_demo()
        self._default_layout = self.saveState()



        #base de donénes

        self.recording_indicator = QLabel("SQLite : enregistrement")
        self.statusBar().addPermanentWidget(self.recording_indicator)
        self.database_error.connect(self._show_database_error)

        self.recorder = RecordingSession(
            Path(__file__).resolve().parent / "data/bases/records",
            clock= lambda:self.timer_widget.current_datetime,
            on_error=self.database_error.emit,
        )

        recording_menu = self.menuBar().addMenu("Enregistrement")
        recording_menu.addAction("Nouvel enregistrement", self.new_recording)
        self.recording_indicator.setToolTip(self.recorder.path)

    def _on_simulation_time_changed(self, date, seconds, state):
        self.simulation_datetime = date
        self.simulation_time_s = seconds
        self.simulation_state = state


    def _update_map_aspect(self, view, ranges, *_):
        # Correction locale de l'échelle longitude selon la latitude affichée.
        latitude = max(-89.0, min(89.0, sum(ranges[1]) / 2))
        ratio = math.cos(math.radians(latitude))
        current = view.state["aspectLocked"]
        if abs(float(current) - ratio) > 1e-5:
            view.setAspectLocked(True, ratio=ratio)

    @staticmethod
    def _label(text, name="muted", wrap=False):
        label = QLabel(text)
        label.setObjectName(name)
        label.setWordWrap(wrap)
        return label

    def _dock(self, title, name, widget, area):
        dock = QDockWidget(title, self)
        dock.setObjectName(name)
        dock.setWidget(widget)
        dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable |
                         QDockWidget.DockWidgetFeature.DockWidgetMovable |
                         QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        self.addDockWidget(area, dock)
        self.docks.append(dock)
        return dock

    def _build_layout(self):
        self.docks = []
        root = QWidget()
        root.setObjectName("root")
        layout = QVBoxLayout(root)
        layout.setContentsMargins(12, 10, 12, 10)
        header = QFrame()
        header.setObjectName("headerCard")
        row = QHBoxLayout(header)
        row.setContentsMargins(18, 15, 18, 15)
        titles = QVBoxLayout()
        titles.addWidget(self._label("CONSOLE DE SURVEILLANCE", "eyebrow"))
        titles.addWidget(self._label("Carte des capteurs", "pageTitle"))
        row.addLayout(titles)
        row.addStretch()
        self.state_pill = self._label("● DÉCONNECTÉ", "statePill")
        row.addWidget(self.state_pill)
        configure = QPushButton("Connexion")
        configure.setObjectName("primaryButton")
        configure.clicked.connect(self.open_connection_dialog)
        row.addWidget(configure)
        layout.addWidget(header)
        layout.addWidget(self._label("WGS84 · longitude / latitude en degrés   |   Molette : zoom · Glisser : déplacer"))
        layout.addWidget(self.plot, 1)
        self.setCentralWidget(root)

        live = QFrame()
        live.setObjectName("sidebar")
        side = QVBoxLayout(live)
        side.setContentsMargins(14, 14, 14, 14)
        side.setSpacing(7)
        side.addWidget(self._label("TYPES DE CAPTEURS", "eyebrow"))
        side.addWidget(self._label("Cliquer pour afficher / masquer un type.", wrap=True))
        self.type_buttons = {}
        for kind in SENSOR_COLORS:
            button = QPushButton(SENSOR_LABELS[kind])
            button.setCheckable(True)
            button.setChecked(True)
            pixmap = QPixmap(26, 26)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.translate(13, 13)
            painter.scale(10, 10)
            pen = pg.mkPen(SENSOR_COLORS[kind], width=2)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(sensor_symbol(kind))
            painter.end()
            button.setIcon(QIcon(pixmap))
            color = SENSOR_COLORS[kind]
            button.setStyleSheet(f"QPushButton {{ color: #71869d; text-align: left; }} QPushButton:checked {{ color: {color}; background: #1d354b; border-color: {color}; }}")
            button.toggled.connect(lambda checked, key=kind: self.set_type_visible(key, checked))
            side.addWidget(button)
            self.type_buttons[kind] = button
        self.activity = self._label("En attente de messages", wrap=True)
        side.addWidget(self.activity)
        side.addStretch()
       # demo = QPushButton("Restaurer la démonstration")
       # demo.clicked.connect(self.load_demo)
       # side.addWidget(demo)
        self.live_dock = self._dock("Capteurs Live", "liveDock", live, Qt.DockWidgetArea.LeftDockWidgetArea)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Objet", "Type", "Statut"])
        self.tree.setColumnWidth(1, 120)
        self.tree.setColumnWidth(0, 230)
        self.tree.setMinimumWidth(450)
        self.tree.itemChanged.connect(self._tree_visibility_changed)
        self.tree.itemDoubleClicked.connect(self._center_on_tree_item)
        self.network_dock = self._dock("Vue réseau", "networkDock", self.tree, Qt.DockWidgetArea.LeftDockWidgetArea)
        self.splitDockWidget(self.live_dock, self.network_dock, Qt.Orientation.Vertical)

        self.connection_panel = ConnectionPanel()
        self.connection_panel.setMinimumWidth(350)
        self.connection_dock = self._dock("Connexion", "connectionDock", self.connection_panel, Qt.DockWidgetArea.RightDockWidgetArea)
        self.connection_dock.hide()

        events = QFrame()
        events.setObjectName("sidebarCard")
        events_layout = QVBoxLayout(events)
        events_layout.setContentsMargins(12, 8, 12, 8)
        row = QHBoxLayout()
        self.client_count = self._label("Clients TCP : 0", "sectionTitle")
        row.addWidget(self.client_count)
        row.addStretch()
        self.verify_button = QPushButton("Vérifier le serveur")
        row.addWidget(self.verify_button)
        events_layout.addLayout(row)
        self.server_monitor = self._label("Serveur arrêté", wrap=True)
        events_layout.addWidget(self.server_monitor)
        self.clients_table = QTableWidget(0, 5)
        self.clients_table.setHorizontalHeaderLabels(["IP", "Port", "Connexion", "Dernière réception", "Octets reçus"])
        self.clients_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.clients_table.verticalHeader().hide()
        self.clients_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.clients_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.clients_table.setMinimumHeight(90)
        self.clients_table.setMaximumHeight(120)
        events_layout.addWidget(self.clients_table)
        self.client_log = QPlainTextEdit()
        self.client_log.setReadOnly(True)
        self.client_log.setMaximumBlockCount(100)
        self.client_log.setMinimumHeight(42)
        self.client_log.setMaximumHeight(65)
        self.client_log.setPlaceholderText("Journal des connexions et déconnexions TCP")
        events_layout.addWidget(self.client_log)
        # Le contenu est aligné à droite ; le dock reste déplaçable et détachable.
        holder = QWidget()
        holder.setObjectName("root")
        bottom = QHBoxLayout(holder)
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.addWidget(events, 4)
        self.clients_dock = self._dock("Clients / serveur", "clientsDock", holder, Qt.DockWidgetArea.BottomDockWidgetArea)
        self.resizeDocks([self.live_dock, self.network_dock], [380, 300], Qt.Orientation.Vertical)

    def open_connection_dialog(self):
        self.connection_dock.show()
        self.connection_dock.raise_()

    def connect_from_parameters(self, p):

        self.timer_widget.reset()

        if p["mode"] == "MQTT":
            self.client.connect_mqtt(p["host"], p["port"], p["topic"], p["username"], p["password"])
        elif p["mode"] == "LEXLUTOR":
            self.client.start_server(p["host"], p["port"])
        elif p.get("tcp_server", False):
            self.client.start_server(p["host"], p["port"], name="TCP serveur")
        else:
            self.client.connect_tcp(p["host"], p["port"])

    def set_connection_state(self, state, detail):
        color = {"connecté": "#39ba84", "en écoute": "#56c7f3", "connexion…": "#e5ac4f"}.get(state, "#e76868")
        self.state_pill.setText(f"● {state.upper()}")
        self.state_pill.setStyleSheet(f"color: {color}; background: #223044; border-radius: 10px; padding: 8px 13px; font-weight: bold")
        text = f"● {state.upper()} | {detail}"
        self.indicator.setText(text)
        self.indicator.setStyleSheet(f"color: {color}; padding: 3px 8px")
        self.connection_panel.state_label.setText(text)
        self.connection_panel.state_label.setStyleSheet(f"color: {color}")

    def show_client_event(self, text):
        self.client_log.appendPlainText(f"{QTime.currentTime().toString('HH:mm:ss')}  {text}")

    def update_connected_clients(self, clients):
        self.clients_table.setRowCount(len(clients))
        for row, client in enumerate(clients):
            for col, value in enumerate([client["ip"], client["port"], client["connected_at"], client["last_received"], client["bytes_received"]]):
                self.clients_table.setItem(row, col, QTableWidgetItem(str(value)))

    def update_server_health(self, listening, endpoint, seconds, checked_at):
        if listening:
            hours, rest = divmod(seconds, 3600)
            minutes, secs = divmod(rest, 60)
            self.server_monitor.setText(f"● EN ÉCOUTE · {endpoint}\nActif depuis {hours:02d}:{minutes:02d}:{secs:02d} · contrôle local : {checked_at} (chaque seconde)")
            color = "#71d9ae"
        else:
            self.server_monitor.setText(f"● SERVEUR ARRÊTÉ · contrôle local : {checked_at}")
            color = "#e76868"
        self.server_monitor.setStyleSheet(f"color: {color}; font-size: 12px")

    def disconnect_client(self):
        self.client.disconnect()

    def handle_message(self, source, payload):
        self.messages_count += 1
        self.counter.setText(f"Messages / blocs reçus : {self.messages_count}")


        raw_payload = payload


        if isinstance(payload, dict):
            messages, errors = [payload], []
        else:
            if isinstance(payload, str):
                payload = payload.encode("utf-8")
            messages, errors = self.message_parser.feed(source, payload)


        recording_receipt = None

        try:
            recording_receipt = self.recorder.record(
                source, raw_payload, decoded=messages, errors=errors
            )
        except (RecordingError, TypeError, ValueError) as exc:
            self._show_database_error(str(exc))

        # Réserver tous les scans avant qu'un code 0 puisse changer de base.
        recording_contexts = {}
        if recording_receipt is not None:
            for frame_index, data in enumerate(messages):
                if data.get('code') == 7:
                    try:
                        recording_contexts[frame_index] = recording_receipt.recorder.reserve_tracking(recording_receipt, frame_index)
                    except RecordingError as exc:
                        self._show_database_error(str(exc))


        accepted = 0
        configured = False
        self._batch = True
        try:
            for frame_index, data in enumerate(messages):

                try:

                    self.recorder.record(
                        source, raw_payload, decoded=messages, errors=errors,simulation_datetime=self.timer_widget.current_datetime
                    )
                except (RecordingError, TypeError, ValueError) as exc:

                    self._show_database_error(str(exc))

                try:
                    if data.get("code") == 0:

                        self.timer_widget.synchronize(data, source)
                        if data.get("code") == 0 and data.get("command") == "SIMULATION_STOPPED":
                            self.new_recording()
                            try:
                                path = self.recorder.simulation_state_changed(data.get("state", ""))
                            except (RecordingError, OSError, ValueError) as exc:
                                self._show_database_error(str(exc))
                            else:
                                if path:
                                    self.recording_indicator.setToolTip(path)
                                    self.statusBar().showMessage(f"Nouvel enregistrement : {Path(path).name}", 10000)
                        continue
                    if data.get("code") == 1:

                        identifier = str(data["id"]).strip()

                        previous = (
                            self.targets.get(identifier)
                            if self._live_scene
                            else None
                        )

                        target = Target.from_message(data, previous)

                        self._begin_live_scene()
                        self.upsert_target(target)

                        if previous is None:
                            self._auto_frame_live = True

                        continue
                    if data.get("code") == 2:
                        previous = self.nodes.get(str(data.get("idNode", "")).strip()) if self._live_scene else None
                        node = Node.from_message(data, previous)

                        altitude_sol = self.terrain_layer.ground_altitude(
                            node.longitude,
                            node.latitude,
                        )

                        node.ground_altitude_m = altitude_sol

                        self._begin_live_scene()
                        self.upsert_node(node)
                        configured = True
                        if previous is None:
                            self._auto_frame_live = True
                    elif data.get("code") == 3:
                        key = Sensor.key(str(data.get("idNode", "")).strip(), str(data.get("sensorId", "")).strip())
                        previous = self.sensors.get(key) if self._live_scene else None
                        sensor = Sensor.from_message(data, previous)
                        self._begin_live_scene()
                        self.upsert_sensor(sensor)
                        configured = True

                        altitude_sol = self.terrain_layer.ground_altitude(
                            sensor.longitude,
                            sensor.latitude,
                        )

                        sensor.ground_altitude_m = altitude_sol

                        if previous is None:
                            self._auto_frame_live = True
                    elif data.get("code") == 7:

                        scan = Scan.from_message(data)

                        self._begin_live_scene()

                        errors.extend(self.scan_display.receive(scan, source))
                        context = recording_contexts.pop(frame_index, None)
                        self.tracking_workshop.receive(scan, source,recording_context=context)

                    else:
                        continue
                    accepted += 1
                except (ValueError, TypeError, OverflowError) as exc:
                    errors.append(f"Configuration ignorée : {exc}")
        finally:
            for context in recording_contexts.values():
                context.release()
            self._batch = False
            if configured:
                self._refresh_network()
        if accepted:
            self.activity.setText(f"{len(self.nodes)} nœud(s) · {len(self.sensors)} capteur(s)\n"
                                  f"{accepted} configuration(s) reçue(s)")
        # Attend des coordonnées réelles si les capteurs arrivent avant leur nœud.
        if self._auto_frame_live and (self.nodes or self.targets):
            self.plot.autoRange()
            self.plot.disableAutoRange()
            self._auto_frame_live = False
        notes = sorted({sensor.position_note for sensor in self.sensors.values()
                        if sensor.network_configured and not sensor.position_resolved})
        details = errors + notes
        self.statusBar().showMessage(" | ".join(details) if details else
                                    f"Reçu de {source} : {accepted} configuration(s) / scan(s)", 10000)

    def _begin_live_scene(self):
        if not self._live_scene:
            # La première configuration valide remplace les objets de démonstration.
            self.clear_objects()
            self._live_scene = True
            self._auto_frame_live = True

    def _remove_sensor_volumes(self, sensor_id):
        for key in [key for key in self.volumes if key[0] == sensor_id]:
            self.volumes.pop(key)
            item = self._items.pop(("footprint", key), None)
            if item is not None:
                self.plot.removeItem(item)

    @staticmethod
    def _check_position(model):
        if not (math.isfinite(model.longitude) and math.isfinite(model.latitude)
                and -180 <= model.longitude <= 180 and -90 <= model.latitude <= 90):
            raise ValueError("Coordonnées WGS84 invalides : longitude [-180, 180], latitude [-90, 90]")

    def _upsert(self, family, key, item):
        old = self._items.pop((family, key), None)
        if old is not None:
            self.plot.removeItem(old)
        self.plot.addItem(item)
        self._items[(family, key)] = item

    def upsert_node(self, node):
        self._check_position(node)
        children = {s.id: s for s in self.sensors.values() if s.node_id == node.id}
        children.update(node.sensors)
        self.nodes[node.id] = node
        self._upsert("node", node.id, NodeItem(node))
        for sensor in children.values():
            self.upsert_sensor(sensor)
        self._refresh_network()

    def upsert_sensor(self, sensor):
        if sensor.network_configured:
            node = self.nodes.get(sensor.node_id)
            sensor.resolve_position(node)
            self._remove_sensor_volumes(sensor.id)
            sensor.volumes = {v.id: v for v in sensor.footprints(node)}
        self._check_position(sensor)
        children = list(sensor.volumes.values())
        self.sensors[sensor.id] = sensor
        if sensor.position_resolved:
            self._upsert("sensor", sensor.id, SensorItem(sensor))
        else:
            old = self._items.pop(("sensor", sensor.id), None)
            if old is not None:
                self.plot.removeItem(old)
        for volume in children:
            self.upsert_footprint(volume)
        for key, volume in list(self.volumes.items()):
            if key[0] == sensor.id:
                self._upsert("footprint", key, FootprintItem(volume, SENSOR_COLORS.get(sensor.sensor_type.lower(), SENSOR_COLORS["unknown"])))
        self._refresh_network()

    def upsert_target(self, target):
        self._check_position(target)

        changed = self.targets.get(target.id,None)


        self.targets[target.id] = target
        _visible = True

        if ("target",target.id) in self._hidden or ('target_group', 'all') in self._hidden:
            _visible = False

        _targetItem = TargetItem(target)

        _targetItem.setVisible(_visible)
        self._upsert(
            "target",
            target.id,
            _targetItem,
        )

        label = pg.TextItem(
            text=target.name,
            color=target.color,
            anchor=(0.5, 1.5),
        )
        label.setVisible(_visible)

        label.setPos(target.longitude, target.latitude)
        label.setZValue(21)

        self._upsert(
            "target-label",
            target.id,
            label,
        )
        if changed is None:
            self._refresh_tree()
        self._refresh_visibility()

    def upsert_detection(self, detection):
        self._check_position(detection)
        self.detections[detection.id] = detection
        self._upsert("detection", detection.id, DetectionItem(detection))
        self._refresh_visibility()

    def upsert_footprint(self, volume):
        self._check_position(volume)
        if (not all(math.isfinite(v) for v in (volume.range_m, volume.range_min_m, volume.azimuth_deg, volume.aperture_deg))
                or not 0 <= volume.range_min_m <= volume.range_m or not 0 <= volume.aperture_deg <= 360):
            raise ValueError("La portée doit être positive ou nulle")
        key = (volume.sensor_id, volume.id)
        self.volumes[key] = volume
        sensor = self.sensors.get(volume.sensor_id)
        kind = sensor.sensor_type.lower() if sensor else "unknown"
        self._upsert("footprint", key, FootprintItem(volume, SENSOR_COLORS.get(kind, SENSOR_COLORS["unknown"])))
        self._refresh_network()

    upsert_volume = upsert_footprint

    def _refresh_network(self):
        if self._batch:
            return
        for node in self.nodes.values():
            node.sensors = {s.id: s for s in self.sensors.values() if s.node_id == node.id}
        for sensor in self.sensors.values():
            sensor.volumes = {v.id: v for v in self.volumes.values() if v.sensor_id == sensor.id}
        self._refresh_tree()
        self._refresh_visibility()
        if hasattr(self, "scan_display"):
            print('--> _refresh_network')
            #self.scan_display.refresh_geometry()

    def _refresh_tree(self):
        expanded = {}
        def remember(item):
            key = item.data(0, Qt.ItemDataRole.UserRole)
            if key:
                expanded[key] = item.isExpanded()
            for i in range(item.childCount()):
                remember(item.child(i))
        for i in range(self.tree.topLevelItemCount()):
            remember(self.tree.topLevelItem(i))
        blocker = QSignalBlocker(self.tree)
        self.tree.clear()
        parents = {}
        def add(parent, name, kind, key, color, tip="", status=""):
            item = QTreeWidgetItem([name, kind, status])
            if parent is None:
                self.tree.addTopLevelItem(item)
            else:
                parent.addChild(item)
            item.setData(0, Qt.ItemDataRole.UserRole, key)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Unchecked if key in self._hidden else Qt.CheckState.Checked)
            for col in (0, 1, 2):
                item.setForeground(col, QBrush(QColor(color)))
                item.setToolTip(col, tip)
            item.setExpanded(expanded.get(key, True))
            return item


        if self.targets:
            targets_root = add(
                None,
                "Cibles — Ground truth",
                "Groupe",
                ("target_group", "all"),
                "#a5d6a7",
            )
            for target in self.targets.values():
                parents[target.id] = add(targets_root, target.name, str("Cible"),("target", target.id),target.color, tip=f"Ground truth · identifiant : {target.id}", )

        for node in self.nodes.values():
            parents[node.id] = add(None, node.label, node.node_type.value, ("node", node.id), node.color,
                                   f"{node.id} · type reçu : {node.raw_node_type}\n"
                                   f"{node.longitude:.6f}°, {node.latitude:.6f}° · yaw {node.yaw:g}°\n"
                                   f"Statut : {node.status} · {node.message}", status=node.status)
        for sensor in self.sensors.values():
            if sensor.node_id not in parents:
                parents[sensor.node_id] = add(None, f"{sensor.node_id} (en attente)", "Nœud", ("node", sensor.node_id), "#80bdf3")
            kind = sensor.sensor_type.lower()
            color = SENSOR_COLORS.get(kind, SENSOR_COLORS["unknown"])
            child = add(parents[sensor.node_id], sensor.label, SENSOR_LABELS.get(kind, "Autre"), ("sensor", sensor.id), color,
                        f"{sensor.sensor_id or sensor.id} · type reçu : {sensor.raw_sensor_type}\n"
                        f"{sensor.position_note}\nStatut : {sensor.status} · {sensor.message}\n"
                        f"Classes : {sensor.classes}", status=sensor.status)
            for volume in sensor.volumes.values():
                altitude = ""
                if volume.altitude_min_m is not None or volume.altitude_max_m is not None:
                    altitude = f" · altitude [{volume.altitude_min_m}, {volume.altitude_max_m}] m"
                add(child, volume.label, "Volume", ("footprint", (sensor.id, volume.id)), color,
                    f"{volume.id} · classe {volume.class_type}\n"
                    f"Portée [{volume.range_min_m:g}, {volume.range_m:g}] m · azimut {volume.azimuth_deg:g}°\n"
                    f"Ouverture AZ {volume.aperture_deg:g}° · site {volume.elevation_aperture_deg:g}°{altitude}")







        del blocker

    def _refresh_simulation_clock(self):
        if self._sync_datetime is None:
            return

        elapsed_s = 0.0

        if self.simulation_state == "RUNNING":
            elapsed_s = (
                                self._sync_elapsed.elapsed() / 1000.0
                        ) * self.simulation_rate

        self.simulation_datetime = (
                self._sync_datetime + timedelta(seconds=elapsed_s)
        )

        self.simulation_time_s = self._sync_time_s + elapsed_s

        display_time = self.simulation_datetime.strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        self.clock_label.setText(
            f"Simulation : {display_time} UTC"
            f" · t={self.simulation_time_s:.1f} s"
            f" · {self.simulation_state}"
        )

    def _tree_visibility_changed(self, item, column):
        if column != 0:
            return
        key = item.data(0, Qt.ItemDataRole.UserRole)
        if item.checkState(0) == Qt.CheckState.Checked:
            self._hidden.discard(key)
        else:
            self._hidden.add(key)
        self._refresh_visibility()

    def set_type_visible(self, kind, visible):
        self._visible_types[kind] = visible
        self._refresh_visibility()


    def _sensor_visible(self, sensor_id):
        sensor = self.sensors.get(sensor_id)
        if sensor is None:
            return ("sensor", sensor_id) not in self._hidden
        kind = sensor.sensor_type.lower()
        return (sensor.position_resolved and ("sensor", sensor_id) not in self._hidden and ("node", sensor.node_id) not in self._hidden
                and self._visible_types.get(kind, self._visible_types["unknown"]))

    def _refresh_visibility(self, *_):
        if self._batch:
            return
        for (family, key), item in self._items.items():
            visible = (family, key) not in self._hidden

            if family == "sensor":
                visible = visible and self._sensor_visible(key)
            elif family == "target":
                visible = (
                        visible
                        and ("target_group", "all") not in self._hidden
                )

            elif family == "footprint":
                visible = visible and self._sensor_visible(key[0])
            elif family == "detection":
                visible = visible and self._sensor_visible(item.model.sensor_id)
                if hasattr(self, "scan_display"):
                    print('--> _refresh_viibility')
                    visible = (     visible and self.scan_display.show_plots.isChecked() )
            item.setVisible(visible)
        if hasattr(self, "scan_display"):
            print('--> _refresh_viibility 2')
            #self.scan_display.refresh_visibility()

    def _center_on_tree_item(self, item, _column):
        key = item.data(0, Qt.ItemDataRole.UserRole)
        graphic = self._items.get(key)
        if graphic is None:
            return
        if key[0] == "footprint":
            self.plot.setRange(rect=graphic.boundingRect(), padding=0.2)
        else:
            model = graphic.model
            self.plot.setRange(xRange=(model.longitude - .004, model.longitude + .004),
                               yRange=(model.latitude - .003, model.latitude + .003), padding=0)

    def clear_objects(self):
        if hasattr(self, "scan_display"):
            self.scan_display.clear()
        if hasattr(self, "tracking_workshop"):
            self.tracking_workshop.reset()
        for item in self._items.values():
            self.plot.removeItem(item)
        self._items.clear()
        self.nodes.clear()
        self.sensors.clear()
        self.targets.clear()
        self.volumes.clear()
        self.detections.clear()
        self._hidden.clear()
        self.tree.clear()
        self._auto_frame_live = False

    def load_demo(self):
        self._live_scene = False
        self.clear_objects()
        self._batch = True
        def point(east, north):
            azimuth = math.degrees(math.atan2(east, north))
            lon, lat, _ = GEOD.fwd(6.13, 43.12, azimuth, math.hypot(east, north))
            return lon, lat
        try:
            self.upsert_node(Node("n1", *point(0, 0), "Nœud A"))
            self.upsert_node(Node("n2", *point(800, 350), "Nœud B"))
            self.upsert_sensor(Sensor("s1", "n1", *point(80, 70), "Radar A", "radar"))
            self.upsert_sensor(Sensor("s2", "n2", *point(720, 270), "Caméra B", "eo"))
            self.upsert_sensor(Sensor("s3", "n1", *point(-120, 90), "Antenne RF A", "rf"))
            self.upsert_sensor(Sensor("s4", "n2", *point(900, 430), "Microphone B", "acoustic"))
            self.upsert_volume(Footprint("s1", *point(80, 70), 550, 60, 95, id="v1", label="Secteur principal"))
            self.upsert_volume(Footprint("s1", *point(80, 70), 220, 230, 100, id="v2", label="Secteur secondaire"))
            self.upsert_volume(Footprint("s2", *point(720, 270), 350, 230, 60, id="v1", label="Champ optique"))
            self.upsert_volume(Footprint("s3", *point(-120, 90), 300, 320, 110, id="v1", label="Écoute RF"))
            self.upsert_volume(Footprint("s4", *point(900, 430), 230, id="v1", label="Zone acoustique"))
            self.upsert_detection(Detection("d1", "s1", *point(390, 210), "Piste 1"))
            self.upsert_detection(Detection("d2", "s2", *point(490, 100), "Piste 2"))
        finally:
            self._batch = False
        self._refresh_network()
        self.plot.autoRange()
        self.plot.disableAutoRange()

    def _show_database_error(self, message):
        self.recording_indicator.setText("⚠ SQLite : erreur")
        self.recording_indicator.setStyleSheet("color: #ff7070")
        self.recording_indicator.setToolTip(message)
        self.statusBar().showMessage(message, 15000)

    def new_recording(self):
        try:
            path = self.recorder.new_recording()
        except (RecordingError, OSError, ValueError) as exc:
            self._show_database_error(str(exc))
            return
        self.recording_indicator.setText("SQLite : enregistrement")
        self.recording_indicator.setStyleSheet("")
        self.recording_indicator.setToolTip(str(path))
        self.statusBar().showMessage(f"Nouvel enregistrement : {Path(path).name}", 10000)
    def closeEvent(self, event):
        if not self.performance_dock.close():
            event.ignore()
            return

        self.client.disconnect()
        self.tracking_workshop.close()
        super().closeEvent(event)
        try:
            self.recorder.close()
        except RecordingError as exc:
            QMessageBox.warning(self, "Enregistrement SQLite", str(exc))

if __name__ == "__main__":
    app = QApplication(sys.argv)
    apply_theme(app)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
