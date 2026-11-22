from PyQt6.QtCore import QSettings, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QFrame,
    QLabel, QLineEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)


class ConnectionPanel(QWidget):
    connect_requested = pyqtSignal(dict)
    disconnect_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("root")
        self.setWindowTitle("Paramètres de connexion")
        self.settings = QSettings("ProjetEtudiant", "VisualiseurCapteurs")
        self.mode = QComboBox()
        self.mode.addItem("TCP · Connexion classique", "TCP")
        self.mode.addItem("MQTT · Abonnement broker", "MQTT")
        self.mode.addItem("LEXLUTOR Server · Serveur TCP", "LEXLUTOR")
        self.tcp_server = QCheckBox("Mode serveur TCP (décoché : client)")
        self.tcp_server.setChecked(self.settings.value("tcp_server", False, type=bool))
        self._mode_values = {}
        self._previous_mode = None
        current_mode = str(self.settings.value("mode", "TCP"))
        self.mode.setCurrentIndex(max(0, self.mode.findData(current_mode)))
        self.description = QLabel()
        self.description.setWordWrap(True)
        self.description.setObjectName("connectionHint")

        self.host = QLineEdit(str(self.settings.value("host", "localhost")))
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(int(self.settings.value("port", 9000)))
        self.topic = QLineEdit(str(self.settings.value("topic", "capteurs/#")))
        self.username = QLineEdit(str(self.settings.value("username", "")))
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)

        form = QFormLayout()
        form.addRow("Protocole", self.mode)
        form.addRow(self.tcp_server)
        self.host_label = QLabel("Hôte / IP")
        form.addRow(self.host_label, self.host)
        form.addRow("Port", self.port)
        self.mqtt_group = QFrame()
        self.mqtt_group.setObjectName("mqttCard")
        mqtt_form = QFormLayout(self.mqtt_group)
        mqtt_form.setContentsMargins(16, 16, 16, 16)
        title = QLabel("ABONNEMENT MQTT")
        title.setObjectName("eyebrow")
        mqtt_form.addRow(title)
        mqtt_form.addRow("Topic", self.topic)
        mqtt_form.addRow("Utilisateur", self.username)
        mqtt_form.addRow("Mot de passe", self.password)

        self.connect_button = QPushButton()
        self.connect_button.setObjectName("primaryButton")
        self.connect_button.clicked.connect(self._accept)
        self.disconnect_button = QPushButton("Déconnecter / arrêter")
        self.disconnect_button.clicked.connect(self.disconnect_requested.emit)
        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        heading = QLabel("Configuration réseau")
        heading.setObjectName("dialogTitle")
        layout.addWidget(heading)
        layout.addWidget(self.description)
        layout.addLayout(form)
        layout.addWidget(self.mqtt_group)
        layout.addWidget(self.connect_button)
        layout.addWidget(self.disconnect_button)
        self.state_label = QLabel("● Déconnecté")
        self.state_label.setWordWrap(True)
        layout.addWidget(self.state_label)
        layout.addStretch()
        self.mode.currentIndexChanged.connect(self._change_mode)
        self.tcp_server.toggled.connect(self._change_mode)
        self._change_mode()

    def _change_mode(self, *_args):
        mode = self.mode.currentData()
        server = mode == "LEXLUTOR" or (mode == "TCP" and self.tcp_server.isChecked())
        key = "TCP_SERVER" if mode == "TCP" and server else mode
        if self._previous_mode is not None:
            self._mode_values[self._previous_mode] = (self.host.text(), self.port.value())
        self.mqtt_group.setVisible(mode == "MQTT")
        self.tcp_server.setVisible(mode == "TCP")
        saved_mode = str(self.settings.value("mode", "TCP"))
        saved_server = self.settings.value("tcp_server", False, type=bool)
        saved_key = "TCP_SERVER" if saved_mode == "TCP" and saved_server else saved_mode
        default_host = "0.0.0.0" if server else "localhost"
        default_port = 1883 if mode == "MQTT" else 9000
        if key == saved_key:
            default_host = str(self.settings.value("host", default_host))
            default_port = int(self.settings.value("port", default_port))
        host, port = self._mode_values.get(key, (
            str(self.settings.value(f"network/{key}/host", default_host)),
            int(self.settings.value(f"network/{key}/port", default_port)),
        ))
        self.host.setText(host)
        self.port.setValue(port)
        self._previous_mode = key
        self.host_label.setText("Adresse d'écoute" if server else "Hôte / IP")
        self.connect_button.setText({
            "MQTT": "S'abonner au broker MQTT", "TCP": "Ouvrir la connexion TCP",
            "LEXLUTOR": "Démarrer LEXLUTOR Server",
        }[mode])
        if mode == "TCP" and server:
            self.connect_button.setText("Démarrer le serveur TCP")
        self.description.setText({
            "TCP": "Le client se connecte au serveur et reçoit un flux d'octets. Le découpage des messages sera défini avec le protocole.",
            "MQTT": "Le client se connecte au broker et s'abonne au topic indiqué. Chaque publication est transmise au visualiseur.",
            "LEXLUTOR": "Le visualiseur écoute et accepte plusieurs clients TCP. 0.0.0.0 : toutes les interfaces IPv4 ; 127.0.0.1 : ce poste uniquement.",
        }[mode])
        if mode == "TCP" and server:
            self.description.setText("Le visualiseur écoute et accepte plusieurs clients TCP. Utilisez 0.0.0.0 pour toutes les interfaces IPv4 ou 127.0.0.1 pour ce poste.")

    def _accept(self):
        if not self.host.text().strip() or (self.mode.currentData() == "MQTT" and not self.topic.text().strip()):
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "Paramètres incomplets", "Renseignez l'hôte et, pour MQTT, le topic.")
            return
        for key, value in (
            ("mode", self.mode.currentData()), ("host", self.host.text().strip()),
            ("port", self.port.value()), ("topic", self.topic.text().strip()),
            ("username", self.username.text().strip()),
        ):
            self.settings.setValue(key, value)
        self.settings.setValue("tcp_server", self.tcp_server.isChecked())
        self._mode_values[self._previous_mode] = (self.host.text().strip(), self.port.value())
        for mode, (host, port) in self._mode_values.items():
            self.settings.setValue(f"network/{mode}/host", host)
            self.settings.setValue(f"network/{mode}/port", port)
        self.connect_requested.emit(self.parameters())

    def parameters(self):
        return {
            "mode": self.mode.currentData(), "host": self.host.text().strip(),
            "port": self.port.value(), "topic": self.topic.text().strip(),
            "username": self.username.text().strip(), "password": self.password.text(),
            "tcp_server": self.mode.currentData() == "LEXLUTOR" or (self.mode.currentData() == "TCP" and self.tcp_server.isChecked()),
        }
