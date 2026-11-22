import math
from datetime import datetime, timedelta, timezone

from PyQt6.QtCore import QElapsedTimer, QTimer, pyqtSignal
from PyQt6.QtWidgets import QFrame, QLabel, QVBoxLayout


class TimerWidget(QFrame):
    time_changed = pyqtSignal(object, float, str)

    def __init__(self, parent=None, rate=1.0):
        super().__init__(parent)

        self.setObjectName("sidebar")
        self.rate = rate

        self.current_datetime = None
        self.current_time_s = 0.0

        self._reference_datetime = None
        self._reference_time_s = 0.0
        self._sequence = -1
        self._source = None
        self._state = "UNKNOWN"
        self._elapsed = QElapsedTimer()

        layout = QVBoxLayout(self)

        title = QLabel("HORLOGE DE SIMULATION")
        title.setObjectName("eyebrow")
        layout.addWidget(title)

        self.date_label = QLabel("Non synchronisée")
        self.date_label.setObjectName("sectionTitle")
        layout.addWidget(self.date_label)

        self.time_label = QLabel("--:--:-- UTC")
        self.time_label.setObjectName("metric")
        layout.addWidget(self.time_label)

        self.status_label = QLabel("En attente du message code 0")
        self.status_label.setObjectName("muted")
        layout.addWidget(self.status_label)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

    def synchronize(self, data, source):
        if (
            data.get("code") != 0
            or (data.get("command") != "SIMULATION_TIME"
                and data.get("command") != "SIMULATION_PAUSED"
                and data.get("command") != "SIMULATION_STOPPED")
        ):
            return False

        # Une seule source est autorisée à piloter l'horloge.
        if self._source is not None and source != self._source:
            return False

        if data.get("command") == "SIMULATION_PAUSED":
            self.pause()
            return True
        if data.get("command") == "SIMULATION_STOPPED":
            self.reset()
            return True

        sequence = int(data["sequence"])
        if sequence <= self._sequence:
            return False





        reference_datetime = datetime.fromisoformat(
            data["simulation_datetime"].replace("Z", "+00:00")
        )

        if reference_datetime.tzinfo is None:
            raise ValueError(
                "simulation_datetime doit préciser son fuseau"
            )

        reference_time_s = float(data["simulation_time_s"])
        if not math.isfinite(reference_time_s):
            raise ValueError("simulation_time_s non fini")

        self._source = source
        self._sequence = sequence
        self._reference_datetime = (
            reference_datetime.astimezone(timezone.utc)
        )
        self._reference_time_s = reference_time_s
        self._state = str(data["state"]).upper()

        self._elapsed.start()

        # Affichage immédiat après une synchronisation.
        self._refresh()
        return True

    def _refresh(self):
        if self._reference_datetime is None:
            return

        elapsed_s = 0.0

        if self._state == "RUNNING":
            elapsed_s = self._elapsed.elapsed() / 1000.0 * self.rate

        self.current_datetime = (
            self._reference_datetime
            + timedelta(seconds=elapsed_s)
        )
        self.current_time_s = self._reference_time_s + elapsed_s

        self.date_label.setText(
            self.current_datetime.strftime("%d/%m/%Y")
        )
        self.time_label.setText(
            self.current_datetime.strftime("%H:%M:%S UTC")
        )
        self.status_label.setText(
            f"{self._state} · t={self.current_time_s:.1f} s"
            f" · synchronisation #{self._sequence}"
        )

        self.time_changed.emit(
            self.current_datetime,
            self.current_time_s,
            self._state,
        )

    def pause(self):
        if self._reference_datetime is None:
            return

        # Calculer l'heure exacte au moment de la pause.
        self._refresh()

        # Conserver cette heure comme nouvelle référence.
        self._reference_datetime = self.current_datetime
        self._reference_time_s = self.current_time_s
        self._state = "PAUSED"

        self._refresh()
    def reset(self):
        self._source = None
        self._sequence = -1
        self._reference_datetime = None
        self._reference_time_s = 0.0
        self._state = "UNKNOWN"

        self.current_datetime = None
        self.current_time_s = 0.0

        self.date_label.setText("Non synchronisée")
        self.time_label.setText("--:--:-- UTC")
        self.status_label.setText("En attente du message code 0")

        self.time_changed.emit(None, 0.0, self._state)