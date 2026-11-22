"""Clients réseau ; les callbacks MQTT transmettent les données par signaux Qt."""
from PyQt6.QtCore import QDateTime, QElapsedTimer, QObject, QTimer, pyqtSignal
from PyQt6.QtNetwork import QHostAddress, QTcpServer, QTcpSocket
import paho.mqtt.client as mqtt


class ConnectionClient(QObject):
    state_changed = pyqtSignal(str, str)  # état, détail
    message_received = pyqtSignal(str, bytes)  # topic MQTT ou "TCP", octets
    source_closed = pyqtSignal(str)
    transport_reset = pyqtSignal()
    client_event = pyqtSignal(str)
    clients_changed = pyqtSignal(int)
    clients_snapshot = pyqtSignal(object)
    server_health = pyqtSignal(bool, str, int, str)  # écoute, endpoint, durée, heure de contrôle

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tcp = None
        self._mqtt = None
        self._server = None
        self._clients = {}
        self._client_info = {}
        self._server_name = "TCP serveur"
        self._uptime = QElapsedTimer()
        self._health_timer = QTimer(self)
        self._health_timer.setInterval(1000)
        self._health_timer.timeout.connect(self.check_server)
        self._generation = 0
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._on_timeout)
        self.state_changed.connect(self._on_state_changed)

    def _on_state_changed(self, state: str, _detail: str):
        if state == "connecté":
            self._timeout.stop()

    def _on_timeout(self):
        self.disconnect()
        self.state_changed.emit("déconnecté", "Délai de connexion dépassé")

    def disconnect(self):
        self.transport_reset.emit()
        self._generation += 1
        self._timeout.stop()
        self._health_timer.stop()
        if self._server is not None:
            server, self._server = self._server, None
            server.close()
            for socket, peer in list(self._clients.items()):
                socket.blockSignals(True)
                socket.abort()
                socket.deleteLater()
                self.client_event.emit(f"Client {peer} déconnecté · arrêt du serveur")
            self._clients.clear()
            self._client_info.clear()
            self.clients_changed.emit(0)
            self.clients_snapshot.emit([])
            server.deleteLater()
        if self._tcp is not None:
            socket, self._tcp = self._tcp, None
            socket.blockSignals(True)
            socket.abort()
            socket.deleteLater()
        if self._mqtt is not None:
            client, self._mqtt = self._mqtt, None
            client.disconnect()
            client.loop_stop()
        self.state_changed.emit("déconnecté", "Aucune connexion")
        self.check_server()

    def start_server(self, host: str, port: int, name: str = "LEXLUTOR Server"):
        """Écoute TCP non bloquante, partagée par TCP classique et LEXLUTOR."""
        self.disconnect()
        self._server_name = name
        address = QHostAddress("127.0.0.1" if host.lower() == "localhost" else host)
        if address.isNull():
            self.state_changed.emit("déconnecté", "Adresse d'écoute invalide : utilisez une IP locale")
            return
        server = QTcpServer(self)
        if not server.listen(address, port):
            self.state_changed.emit("déconnecté", f"{name} : {server.errorString()}")
            server.deleteLater()
            return
        self._server = server
        server.newConnection.connect(self._accept_clients)
        self._uptime.start()
        self._health_timer.start()
        self.state_changed.emit("en écoute", f"{name} {host}:{server.serverPort()}")
        self.client_event.emit(f"{name} en écoute sur {host}:{server.serverPort()}")
        self.check_server()

    def check_server(self):
        """Vérification locale du socket d'écoute, sans message ajouté au protocole."""
        listening = self._server is not None and self._server.isListening()
        endpoint = ""
        seconds = 0
        if self._server is not None:
            endpoint = f"{self._server_name} · {self._server.serverAddress().toString()}:{self._server.serverPort()}"
            seconds = self._uptime.elapsed() // 1000 if self._uptime.isValid() else 0
            if not listening:
                self._health_timer.stop()
                self.state_changed.emit("déconnecté", "Le serveur n'est plus en écoute")
        self.server_health.emit(listening, endpoint, seconds, QDateTime.currentDateTime().toString("HH:mm:ss"))
        self.clients_snapshot.emit([dict(info) for info in self._client_info.values()])

    def _accept_clients(self):
        if self._server is None:
            return
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            peer = f"{socket.peerAddress().toString()}:{socket.peerPort()}"
            self._clients[socket] = peer
            self._client_info[socket] = {
                "ip": socket.peerAddress().toString(), "port": socket.peerPort(),
                "connected_at": QDateTime.currentDateTime().toString("HH:mm:ss"),
                "last_received": "—", "bytes_received": 0,
            }
            socket.readyRead.connect(lambda sock=socket: self._server_read(sock))
            socket.disconnected.connect(lambda sock=socket: self._remove_client(sock))
            self.client_event.emit(f"Client {peer} connecté")
            self.clients_changed.emit(len(self._clients))
            self._server_read(socket)
        self.check_server()

    def _server_read(self, socket):
        peer = self._clients.get(socket)
        if peer is not None:
            payload = bytes(socket.readAll())
            if payload:
                info = self._client_info[socket]
                info["bytes_received"] += len(payload)
                info["last_received"] = QDateTime.currentDateTime().toString("HH:mm:ss")
                self.message_received.emit(f"{self._server_name} · {peer}", payload)

    def _remove_client(self, socket):
        peer = self._clients.pop(socket, None)
        self._client_info.pop(socket, None)
        if peer is not None:
            self._server_read_remaining(socket, peer)
            self.source_closed.emit(f"{self._server_name} · {peer}")
            self.client_event.emit(f"Client {peer} déconnecté")
            self.clients_changed.emit(len(self._clients))
            socket.deleteLater()
            self.clients_snapshot.emit([dict(info) for info in self._client_info.values()])

    def _server_read_remaining(self, socket, peer):
        payload = bytes(socket.readAll())
        if payload:
            self.message_received.emit(f"{self._server_name} · {peer}", payload)

    def connect_tcp(self, host: str, port: int):
        self.disconnect()
        generation = self._generation
        socket = QTcpSocket(self)
        self._tcp = socket
        socket.connected.connect(lambda: self._tcp_state(generation, "connecté", f"TCP {host}:{port}"))
        socket.disconnected.connect(lambda: self._tcp_state(generation, "déconnecté", "Serveur TCP déconnecté"))
        socket.errorOccurred.connect(lambda _error: self._tcp_state(generation, "déconnecté", socket.errorString()))
        socket.readyRead.connect(lambda: self._tcp_read(generation))
        self.state_changed.emit("connexion…", f"TCP {host}:{port}")
        socket.connectToHost(host, port)
        self._timeout.start(10000)

    def _tcp_state(self, generation, state, detail):
        if generation == self._generation:
            if state == "connecté":
                self._timeout.stop()
            elif state == "déconnecté":
                self.source_closed.emit("TCP")
            self.state_changed.emit(state, detail)

    def _tcp_read(self, generation):
        if generation == self._generation and self._tcp is not None:
            data = bytes(self._tcp.readAll())
            if data:
                self.message_received.emit("TCP", data)

    def connect_mqtt(self, host: str, port: int, topic: str, username: str, password: str):
        self.disconnect()
        generation = self._generation
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if username:
            client.username_pw_set(username, password)

        def on_connect(_client, _userdata, _flags, reason_code, _properties):
            if generation != self._generation:
                return
            if reason_code == 0:
                rc, _mid = _client.subscribe(topic)
                if rc == mqtt.MQTT_ERR_SUCCESS:
                    self.state_changed.emit("connecté", f"MQTT {host}:{port} • {topic}")
                else:
                    self.state_changed.emit("déconnecté", f"Abonnement refusé : {rc}")
            else:
                self.state_changed.emit("déconnecté", f"Connexion MQTT refusée : {reason_code}")

        def on_disconnect(_client, _userdata, _disconnect_flags, reason_code, _properties):
            if generation == self._generation:
                self.transport_reset.emit()
                self.state_changed.emit("déconnecté", f"MQTT déconnecté : {reason_code}")

        def on_message(_client, _userdata, message):
            if generation == self._generation:
                self.message_received.emit(message.topic, bytes(message.payload))

        def on_connect_fail(_client, _userdata):
            if generation == self._generation:
                self.state_changed.emit("déconnecté", "Broker MQTT inaccessible")

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_connect_fail = on_connect_fail
        client.on_message = on_message
        self._mqtt = client
        self.state_changed.emit("connexion…", f"MQTT {host}:{port}")
        try:
            client.connect_async(host, port, keepalive=30)
            self._timeout.start(10000)
            client.loop_start()
        except (OSError, ValueError) as exc:
            self.disconnect()
            self.state_changed.emit("déconnecté", str(exc))
            return
