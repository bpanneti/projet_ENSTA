import base64
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
import json
import logging
import math
from pathlib import Path
from queue import Queue, Full
import sqlite3
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from uuid import uuid4

SCHEMA = '''
CREATE TABLE IF NOT EXISTS recording_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
 id INTEGER PRIMARY KEY, received_at TEXT NOT NULL, received_ns INTEGER NOT NULL,
 source TEXT NOT NULL, payload BLOB NOT NULL, payload_kind TEXT NOT NULL,
 payload_size INTEGER NOT NULL, simulation_datetime TEXT, simulation_time_s REAL,
 decoded_count INTEGER NOT NULL, errors_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS frames (
 id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL REFERENCES messages(id),
 frame_index INTEGER NOT NULL, code INTEGER, command TEXT, sequence INTEGER,
 raw_json TEXT NOT NULL, UNIQUE(message_id, frame_index)
);
CREATE TABLE IF NOT EXISTS nodes (
 id INTEGER PRIMARY KEY, frame_id INTEGER UNIQUE NOT NULL REFERENCES frames(id),
 node_id TEXT, name TEXT, node_type TEXT, state TEXT, network TEXT, ip TEXT, message TEXT,
 position_format TEXT, longitude REAL, latitude REAL, altitude REAL,
 yaw REAL, pitch REAL, roll REAL, velocity_json TEXT, raw_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sensors (
 id INTEGER PRIMARY KEY, frame_id INTEGER UNIQUE NOT NULL REFERENCES frames(id),
 node_id TEXT, sensor_id TEXT, sensor_type TEXT, name TEXT, state TEXT, message TEXT,
 location_x REAL, location_y REAL, location_z REAL, yaw REAL, pitch REAL, roll REAL,
 classes_json TEXT, associated_area_json TEXT, raw_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS targets (
 id INTEGER PRIMARY KEY, frame_id INTEGER UNIQUE NOT NULL REFERENCES frames(id),
 target_id TEXT, name TEXT,  timestamp REAL, longitude REAL, latitude REAL, altitude REAL, altitude_m REAL,
 yaw REAL, pitch REAL, roll REAL, speed_kmh REAL,
 dis_domain INTEGER, dis_kind INTEGER, dis_category INTEGER, dis_id TEXT,
 dis_country INTEGER, dis_subcategory INTEGER, dis_extra INTEGER, raw_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scans (
 id INTEGER PRIMARY KEY, frame_id INTEGER UNIQUE NOT NULL REFERENCES frames(id),
 scan_id TEXT, sensor_id TEXT, node_id TEXT, sensor_type TEXT, timestamp REAL,
 detection_count INTEGER, raw_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plots (
 id INTEGER PRIMARY KEY, scan_db_id INTEGER NOT NULL REFERENCES scans(id),
 detection_index INTEGER NOT NULL, detection_id TEXT, timestamp REAL,origin TEXT,
 detection_type TEXT, coordinate_format TEXT,
 range_m REAL, azimuth_deg REAL, elevation_deg REAL,
 longitude REAL, latitude REAL, altitude REAL, altitude_m REAL,
 x REAL, y REAL, z REAL,
 covariance_format TEXT, covariance_dimension INTEGER,
 covariance_order_json TEXT, covariance_values_json TEXT, covariance_json TEXT,
 classification_labels_json TEXT, classification_scores_json TEXT, classification_json TEXT,
 data_json TEXT, raw_json TEXT NOT NULL, UNIQUE(scan_db_id, detection_index)
);
CREATE INDEX IF NOT EXISTS messages_received_idx ON messages(received_ns);
CREATE INDEX IF NOT EXISTS frames_message_idx ON frames(message_id);
CREATE INDEX IF NOT EXISTS nodes_identifier_idx ON nodes(node_id, id);
CREATE INDEX IF NOT EXISTS sensors_identifier_idx ON sensors(node_id, sensor_id, id);
CREATE INDEX IF NOT EXISTS targets_identifier_idx ON targets(target_id, id);
CREATE INDEX IF NOT EXISTS scans_identifier_idx ON scans(node_id, sensor_id, timestamp);
CREATE INDEX IF NOT EXISTS plots_scan_idx ON plots(scan_db_id);
CREATE INDEX IF NOT EXISTS plots_identifier_idx ON plots(detection_id, timestamp);
CREATE VIEW IF NOT EXISTS latest_nodes AS
 SELECT * FROM nodes WHERE id IN (SELECT MAX(id) FROM nodes GROUP BY node_id);
CREATE VIEW IF NOT EXISTS latest_sensors AS
 SELECT * FROM sensors WHERE id IN (SELECT MAX(id) FROM sensors GROUP BY node_id, sensor_id);
CREATE VIEW IF NOT EXISTS latest_targets AS
 SELECT * FROM targets WHERE id IN (SELECT MAX(id) FROM targets GROUP BY target_id);

'''

TRACKING_SCHEMA = '''
CREATE TABLE tracking_runs (
 run_id TEXT PRIMARY KEY, name TEXT NOT NULL,
 estimator TEXT NOT NULL, association TEXT NOT NULL,
 configuration_json TEXT NOT NULL, state_layout_json TEXT NOT NULL,
 reference_frame_json TEXT NOT NULL, time_reference_json TEXT NOT NULL,
 implementation_version TEXT NOT NULL
);
CREATE TABLE tracks (
 id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES tracking_runs(run_id),
 track_id TEXT NOT NULL, birth_timestamp REAL NOT NULL,
 end_timestamp REAL, end_reason TEXT, latest_status TEXT,
 UNIQUE(run_id, track_id)
);
CREATE TABLE states (
 id INTEGER PRIMARY KEY, track_db_id INTEGER NOT NULL REFERENCES tracks(id),
 step_index INTEGER NOT NULL, timestamp REAL NOT NULL,
 simulation_datetime TEXT, timer_datetime TEXT, timer_time_s REAL,
 phase TEXT NOT NULL, status TEXT NOT NULL, coasting INTEGER NOT NULL,
 source_scan_id INTEGER NOT NULL REFERENCES scans(id),
 plot_id INTEGER REFERENCES plots(id), plot_refs_json TEXT NOT NULL,
 miss_probability REAL NOT NULL,
 state_json TEXT NOT NULL, covariance_json TEXT NOT NULL,
 longitude REAL, latitude REAL, altitude REAL, ve REAL, vn REAL, vu REAL,
 model_probabilities_json TEXT NOT NULL, diagnostics_json TEXT NOT NULL,
 UNIQUE(track_db_id, step_index)
);
CREATE TABLE track_state_models (
 id INTEGER PRIMARY KEY, state_id INTEGER NOT NULL REFERENCES states(id),
 model_index INTEGER NOT NULL, model_name TEXT NOT NULL,
 probability REAL NOT NULL, state_json TEXT, covariance_json TEXT,
 UNIQUE(state_id, model_index)
);
CREATE INDEX states_time_idx ON states(timestamp, id);
CREATE INDEX states_track_idx ON states(track_db_id, timestamp);
CREATE INDEX states_plot_idx ON states(plot_id);
CREATE INDEX states_source_idx ON states(source_scan_id);
CREATE INDEX models_state_idx ON track_state_models(state_id);
'''

def _safe(value):
    if isinstance(value, Enum):
        return _safe(value.value)
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return _safe(asdict(value))
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {'$bytes_base64': base64.b64encode(bytes(value)).decode('ascii')}
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return {'$non_finite': str(value)}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, 'tolist'):
        return _safe(value.tolist())
    raise TypeError(f'Type non sérialisable : {type(value).__name__}')


def json_text(value):
    return json.dumps(_safe(value), ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _unwrap(value):
    while isinstance(value, list) and len(value) == 1:
        value = value[0]
    return value


def _mapping(value):
    value = _unwrap(value)
    return value if isinstance(value, dict) else {}


def _number(value):
    value = _unwrap(value)
    try:
        if isinstance(value, (bool, dict, list)):
            return None
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _integer(value):
    value = _unwrap(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return value if -(2**63) <= value < 2**63 else None
    number = _number(value)
    return int(number) if number is not None and number.is_integer() and -(2**63) <= number < 2**63 else None


def _text(value):
    if value is None:
        return None
    return str(value) if isinstance(value, (str, int, float, bool)) else json_text(value)



class RecordingError(RuntimeError):
    pass

@dataclass(frozen=True)
class RecordingReceipt:
    """Réception unique ; future résolue APRÈS validation de la transaction."""
    recorder: object
    receipt_id: str
    future: Future


@dataclass(frozen=True)
class WriteRequest:
    job: object
    future: Future


def _finite(value, label):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f'{label} non fini')
    return number


def _gaussian(x, P):
    x = [_finite(v, 'État') for v in x]
    P = [[_finite(v, 'Covariance') for v in row] for row in P]
    if len(x) not in (6, 9) or len(P) != len(x) or any(len(row) != len(x) for row in P):
        raise ValueError('État/covariance de dimensions incompatibles (6D ou 9D)')
    if any(not math.isclose(P[i][j], P[j][i], rel_tol=1e-7, abs_tol=1e-8)
           for i in range(len(x)) for j in range(len(x))):
        raise ValueError('Covariance non symétrique')
    return x, P


class TrackingContext:
    """Réserve l'ancienne base jusqu'à soumission de TOUS les sous-pas du scan.

    release() est obligatoire, y compris si le scan est abandonné. Ne pas deepcopy.
    """
    def __init__(self, recorder, receipt, frame_index):
        self.recorder = recorder
        self.receipt = receipt
        self.frame_index = int(frame_index)
        self._released = False

    def save(self, snapshot, config, name, reference_frame, time_reference=None):
        if self._released:
            raise RecordingError('Contexte de scan déjà libéré')
        return self.recorder.record_tracking(snapshot, context=self, config=config, name=name, reference_frame=reference_frame, time_reference=time_reference)

    def release(self):
        with self.recorder._tracking_lock:
            if self._released:
                return
            self._released = True
            self.recorder._leases -= 1
            self.recorder._maybe_close_locked()


@dataclass(frozen=True)
class TrackingWrite:
    receipt: RecordingReceipt
    frame_index: int
    payload_json: str




class SQLiteRecorder:
    """Un seul thread possède la connexion. close() attend les écritures en file."""
    STOP = object()

    def __init__(self, path, on_error=None, max_pending=1000):

        self._tracking_lock = threading.RLock()
        self._leases = 0
        self._retired = False
        self._closing = False
        self._close_done = threading.Event()
        self._close_error = None
        self._frames_cache = {}
        self.path = str(Path(path).expanduser().resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)







        self.on_error = on_error
        self._queue = Queue(maxsize=max_pending)
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._closed = False
        self._failure = None
        self._thread = threading.Thread(target=self._run, name='SQLiteRecorder', daemon=False)
        self._thread.start()
        self._ready.wait()
        if self._failure:
            self._thread.join()
            raise RecordingError(self._failure)

    def record(self, source, payload, decoded=(), errors=(), received_at=None,
               received_ns=None, simulation_datetime=None, simulation_time_s=None):
        """Enregistrer un bloc reçu + les trames terminées dans ce bloc.

        Capturer received_at/ns à l'entrée du handler si le décodage prend du temps.
        Les dictionnaires sont sérialisés ; des bytes sont conservés exactement.
        """

        received_ns = time.time_ns() if received_ns is None else received_ns
        received_at = datetime.now(timezone.utc) if received_at is None else received_at
        if isinstance(received_at, datetime):
            if received_at.tzinfo is None:
                raise ValueError('La date de réception doit préciser le fuseau')
            received_at = received_at.astimezone(timezone.utc).isoformat(timespec='microseconds')
        if isinstance(payload, (bytes, bytearray, memoryview)):
            raw, kind = bytes(payload), 'bytes'

        elif isinstance(payload, str):
            raw, kind = payload.encode('utf-8'), 'text_utf8'
        else:
            raw, kind = json_text(payload).encode('utf-8'), 'serialized_json'


        # Une copie JSON stabilise les objets avant le passage au thread.
        decoded_json = json_text(list(decoded))

        job = (str(received_at), int(received_ns), str(source), raw, kind,
               _text(simulation_datetime.isoformat() if isinstance(simulation_datetime, datetime) else simulation_datetime),
               _number(simulation_time_s), decoded_json, json_text(list(errors)))
        with self._lock:
            if self._closed:
                raise RecordingError('Enregistreur fermé')
            if self._failure:
                raise RecordingError(self._failure)
            try:
                self._queue.put_nowait(job)
            except Full:
                raise RecordingError('File SQLite pleine : ce bloc n’a pas été enregistré')

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                if self._thread.is_alive():
                    self._queue.put(self.STOP)
        self._thread.join()
        if self._failure:
            raise RecordingError(self._failure)

    def _report_failure(self, exc):
        self._failure = f'Enregistrement SQLite interrompu : {exc}'
        logging.error(self._failure)
        if self.on_error:
            try:
                self.on_error(self._failure)
            except Exception:
                logging.exception('Notification de l’erreur SQLite impossible')

    def _run(self):
        connection = None
        try:
            connection = sqlite3.connect(self.path, timeout=5)
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('PRAGMA busy_timeout=5000')
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and 'recording_meta' not in tables:
                raise ValueError('Choisir un nouveau fichier : cette base ne correspond pas à l’enregistreur')
            if 'recording_meta' in tables:
                version = connection.execute("SELECT value FROM recording_meta WHERE key='schema_version'").fetchone()
                if version != ('1',):
                    raise ValueError('Version de base non prise en charge')
            connection.executescript(SCHEMA)
            connection.execute("INSERT OR IGNORE INTO recording_meta VALUES ('schema_version', '1')")
            connection.commit()
        except Exception as exc:
            self._report_failure(exc)
            if connection:
                connection.close()
            self._ready.set()
            return
        self._ready.set()
        try:
            while True:
                job = self._queue.get()
                try:
                    if job is self.STOP:
                        break
                    if self._failure:
                        continue
                    try:
                        with connection:
                            self._write(connection, job)
                    except Exception as exc:
                        self._report_failure(exc)
                finally:
                    self._queue.task_done()
            if not self._failure:
                connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        except Exception as exc:
            self._report_failure(exc)
        finally:
            connection.close()

    @staticmethod
    def _insert(connection, table, values):
        # Noms de tables/colonnes définis uniquement par ce module, valeurs paramétrées.
        columns = ','.join(values)
        placeholders = ','.join('?' for _ in values)
        cursor = connection.execute(f'INSERT INTO {table} ({columns}) VALUES ({placeholders})', tuple(values.values()))
        return cursor.lastrowid

    def _write(self, connection, job):
        received_at, received_ns, source, raw, kind, sim_date, sim_seconds, decoded_json, errors_json = job
        decoded = json.loads(decoded_json)
        message_id = self._insert(connection, 'messages', dict(
            received_at=received_at, received_ns=received_ns, source=source,
            payload=sqlite3.Binary(raw), payload_kind=kind, payload_size=len(raw),
            simulation_datetime=sim_date, simulation_time_s=sim_seconds,
            decoded_count=len(decoded), errors_json=errors_json))
        for index, data in enumerate(decoded):
            if not isinstance(data, dict):
                data = {'unrecognized_value': data}
            raw_json = json_text(data)
            code = _integer(data.get('code'))
            frame_id = self._insert(connection, 'frames', dict(message_id=message_id,
                frame_index=index, code=code, command=_text(data.get('command')),
                sequence=_integer(data.get('sequence')), raw_json=raw_json))
            position = _mapping(data.get('position'))
            attitude = _mapping(data.get('attitude'))
            common = dict(frame_id=frame_id, raw_json=raw_json)
            angles = {k: _number(attitude.get(k)) for k in ('yaw', 'pitch', 'roll')}
            if code == 2:
                self._insert(connection, 'nodes', dict(common, **angles,
                    node_id=_text(data.get('idNode')), name=_text(data.get('name')),
                    node_type=_text(data.get('nodeType')), state=_text(data.get('state')),
                    network=_text(data.get('network')), ip=_text(data.get('ip')), message=_text(data.get('message')),
                    position_format=_text(position.get('format')), longitude=_number(position.get('longitude')),
                    latitude=_number(position.get('latitude')), altitude=_number(position.get('altitude')),
                    velocity_json=json_text(data.get('velocity'))))
            elif code == 3:
                location = _mapping(data.get('location'))
                self._insert(connection, 'sensors', dict(common, **angles,
                    node_id=_text(data.get('idNode')), sensor_id=_text(data.get('sensorId')),
                    sensor_type=_text(data.get('sensorType')), name=_text(data.get('name')),
                    state=_text(data.get('state')), message=_text(data.get('message')),
                    location_x=_number(location.get('x')), location_y=_number(location.get('y')),
                    location_z=_number(location.get('z')), classes_json=json_text(data.get('classes')),
                    associated_area_json=json_text(data.get('associatedArea'))))
            elif code == 7:
                entries = data.get('detections', [])
                entries = entries if isinstance(entries, list) else []
                scan_id = self._insert(connection, 'scans', dict(common,
                    scan_id=_text(data.get('scanId')), sensor_id=_text(data.get('sensorId')),
                    node_id=_text(data.get('idNode')), sensor_type=_text(data.get('sensor_type', data.get('sensorType'))),
                    timestamp=_number(data.get('timestamp')), detection_count=len(entries)))
                for detection_index, detection in enumerate(entries):
                    self._plot(connection, scan_id, detection_index, detection, data.get('timestamp'))
            elif code == 1:#'id' in data and 'position' in data:
                fields = {k: _integer(data.get(k)) for k in ('dis_domain', 'dis_kind', 'dis_category',
                          'dis_country', 'dis_subcategory', 'dis_extra')}
                self._insert(connection, 'targets', dict(common, **angles, **fields,
                    target_id=_text(data.get('id')), name=_text(data.get('name')), timestamp =  data.get('timestamp') ,dis_id=_text(data.get('dis_id')),
                    longitude=_number(position.get('longitude')), latitude=_number(position.get('latitude')),
                    altitude=_number(position.get('altitude')), altitude_m=_number(position.get('altitude_m')),
                    speed_kmh=_number(data.get('speed_kmh'))))

    def _plot(self, connection, scan_id, index, detection, scan_timestamp):
        original = detection
        detection = detection if isinstance(detection, dict) else {}
        covariance = _mapping(detection.get('cov'))
        classification = _mapping(detection.get('classification'))
        position = _mapping(detection.get('position'))
        coordinates = {k: _number(detection.get(k, position.get(k)))
                       for k in ('range_m', 'azimuth_deg', 'elevation_deg', 'longitude', 'latitude',
                                 'altitude', 'altitude_m', 'x', 'y', 'z')}






        self._insert(connection, 'plots', dict(coordinates,
            scan_db_id=scan_id, detection_index=index, detection_id=_text(detection.get('detection_id')),
            timestamp=_number(detection.get('timestamp', scan_timestamp)),
            origin = _text(detection.get('origin','UNKNOWN')),
            detection_type=_text(detection.get('detection_type')), coordinate_format=_text(detection.get('format')),
            covariance_format=_text(covariance.get('format')), covariance_dimension=_integer(covariance.get('dimension')),
            covariance_order_json=json_text(covariance.get('order')), covariance_values_json=json_text(covariance.get('values')),
            covariance_json=json_text(detection.get('cov')),
            classification_labels_json=json_text(classification.get('labels')),
            classification_scores_json=json_text(classification.get('scores')),
            classification_json=json_text(detection.get('classification')),
            data_json=json_text(detection.get('data',[])), raw_json=json_text(original)))


class RecordingSession:
    """Bases successives ; les dates des lignes viennent exclusivement du timer.

    clock doit retourner un datetime avec fuseau. elapsed, facultatif, retourne
    les secondes de simulation. L'heure système sert uniquement au nom du fichier.
    Les appels record/new_recording/close sont faits depuis le thread Qt.
    """
    def __init__(self, directory, clock, elapsed=None, on_error=None, prefix='enregistrement'):
        '''
        if not callable(clock):
            raise TypeError('clock doit être une fonction lisant le timer')
        if not prefix or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in prefix):
            raise ValueError('Préfixe autorisé : lettres, chiffres, tirets et underscores')
        '''
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.elapsed = elapsed
        self.on_error = on_error
        self.prefix = prefix
        self.recorder = None
        self._closed = False
        self._previous_state = None
        self.new_recording()

    @property
    def path(self):
        return self.recorder.path if self.recorder else None

    def new_recording(self):
        if self._closed:
            raise RecordingError('Session fermée')
        if self.recorder is not None:
            self.recorder.close()  # Finir l'ancienne base avant le changement.
            self.recorder = None
        stamp = datetime.now().astimezone().strftime('%Y%m%d_%H%M%S_%f')
        index = 0
        while True:
            suffix = f'_{index:03d}' if index else ''
            path = self.directory / f'{self.prefix}_{stamp}{suffix}.sqlite'
            try:
                with path.open('xb'):
                    pass
                break
            except FileExistsError:
                index += 1
        try:
            self.recorder = SQLiteRecorder(path, on_error=self.on_error)
        except Exception:
            # Ne jamais retirer une base précédente.
            if path.exists() and path.stat().st_size == 0:
                path.unlink()
            raise
        return self.path

    def record(self, source, payload, decoded=(), errors=(), simulation_datetime=None):

        if self._closed or self.recorder is None:
            raise RecordingError('Aucun enregistrement actif')

        # Permet de passer une date capturée à l'entrée du handler.
        date = self.clock if simulation_datetime is None else simulation_datetime


        if not isinstance(date, datetime) or date.tzinfo is None or date.utcoffset() is None:
            raise RecordingError('Le timer doit fournir un datetime avec fuseau ; aucune heure système de secours')
        date = date.astimezone(timezone.utc)
        # Calcul entier, sans arrondi via un timestamp flottant.
        delta = date - datetime(1970, 1, 1, tzinfo=timezone.utc)
        ns = ((delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds) * 1000
        if not -(2**63) <= ns < 2**63:
            raise RecordingError('Date du timer hors plage des nanosecondes SQLite')
        seconds = self.elapsed() if self.elapsed else None

        self.recorder.record(source, payload, decoded=decoded, errors=errors,
            received_at=date, received_ns=ns, simulation_datetime=date,
            simulation_time_s=seconds)

    def simulation_state_changed(self, state):
        """Appeler après l'enregistrement du code 0 et avant un reset du timer.

        Les STOP/STOPPED répétés ne créent pas de bases supplémentaires.
        Un état initial arrêté n'est pas une transition d'arrêt.
        """
        state = str(state).strip().upper()
        stopped = {'STOP', 'STOPPED'}
        previous = self._previous_state
        if state in stopped and previous is not None and previous not in stopped:
            path = self.new_recording()
        else:
            path = None
        self._previous_state = state
        return path

    def close(self):
        self._closed = True
        if self.recorder:
            self.recorder.close()