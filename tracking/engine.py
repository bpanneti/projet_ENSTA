"""Filtres et association sans dépendance Qt. Les vérités terrain n'entrent pas ici."""
from collections import deque
from dataclasses import dataclass, field, asdict
import copy
import math
import numpy as np
from scipy.optimize import linear_sum_assignment
from scipy.special import logsumexp
from scipy.stats import chi2
from .models import Dynamics, covariance
from uuid import uuid4

@dataclass
class Configuration:
    family: str = 'Monocible'
    estimator: str = 'EKF'
    dynamics: str = 'CV'
    association: str = 'GNNSF'
    process_noise_cv: float = 1.
    process_noise_ca: float = .2
    process_noise_cv_2: float = 10.
    sigma_range: float = 10.
    sigma_azimuth_deg: float = 1.
    sigma_elevation_deg: float = 1.
    use_received_R: bool = True
    sigma_position: float = 20.
    sigma_velocity: float = 30.
    sigma_acceleration: float = 5.
    unobserved_altitude_sigma: float = 1000.
    gate_probability: float = .9973
    detection_probability: float = .9
    clutter_density: float = 1e-5
    confirmation_hits: int = 3
    confirmation_window: int = 5
    deletion_seconds: float = 10.
    max_tracks: int = 20
    max_hypotheses: int = 10000
    allow_bearing_prior: bool = False
    bearing_prior_range: float = 1000.
    bearing_prior_sigma: float = 1000.
    seed_detection_id: str = ''
    imm_transition: list = field(default_factory=lambda: [[.95, .05], [.05, .95]])
    imm_probabilities: list = field(default_factory=lambda: [.5, .5])
    P0_diagonal: list | None = None
    Q_override: dict = field(default_factory=dict)
    R_override: dict = field(default_factory=dict)

    def validate(self):
        if self.family not in ('Monocible', 'Multicible') or self.estimator not in ('CMKF', 'EKF', 'IMM') or self.dynamics not in ('CV', 'CA','CV+CV_2','CV+CA'):
            raise ValueError('Famille, filtre ou dynamique inconnu')

        if self.estimator in ('CMKF', 'EKF') and self.dynamics  in ('CV+CV_2','CV+CA'):
            raise ValueError('Le filtre sélectionné doit être mono-modèle')
        if self.estimator == 'IMM' and self.dynamics  in ('CV','CA'):
            raise ValueError('Le filtre sélectionné doit être multi-modèle')

        if self.association not in ('GNNSF', 'JPDAF'):
            raise ValueError('Association disponible : GNNSF ou JPDAF ; SDA est une extension ultérieure')
        for key in ('process_noise_cv', 'process_noise_ca','process_noise_cv_2', 'sigma_range', 'sigma_azimuth_deg',
                    'sigma_elevation_deg', 'sigma_position', 'sigma_velocity', 'sigma_acceleration',
                    'unobserved_altitude_sigma', 'clutter_density', 'deletion_seconds', 'bearing_prior_range', 'bearing_prior_sigma'):
            value = getattr(self, key)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f'{key} doit être strictement positif')
        if not 0 < self.gate_probability < 1 or not 0 < self.detection_probability < 1:
            raise ValueError('Probabilités strictement entre 0 et 1')
        for key in ('confirmation_hits', 'confirmation_window', 'max_tracks', 'max_hypotheses'):
            if not isinstance(getattr(self, key), int) or isinstance(getattr(self, key), bool) or getattr(self, key) < 1:
                raise ValueError(f'{key} doit être un entier positif')
        if self.confirmation_hits > self.confirmation_window or self.max_tracks > 100 or self.max_hypotheses > 100000:
            raise ValueError('Confirmation ou limites de calcul invalides')
        transition = np.asarray(self.imm_transition, float)
        weights = np.asarray(self.imm_probabilities, float)
        if (transition.shape != (2, 2) or not np.isfinite(transition).all() or np.any(transition <= 0)
            or not np.allclose(transition.sum(axis=1), 1) or weights.shape != (2,)
            or not np.isfinite(weights).all() or np.any(weights <= 0) or not np.isclose(weights.sum(), 1)):
            raise ValueError('IMM : matrice et probabilités positives, sommes égales à 1')
        n = 9 if 'CA' in self.dynamics  else 6
        if self.P0_diagonal is not None:
            p = np.asarray(self.P0_diagonal, float)
            if p.shape != (n,) or not np.isfinite(p).all() or np.any(p <= 0):
                raise ValueError(f'P0_diagonal : {n} variances positives requises')

        for key, matrix in self.Q_override.items():
            if key not in ('CV', 'CA'):
                raise ValueError('Q_override : clés CV/CA seulement')
            covariance(matrix, n)
        for key, matrix in self.R_override.items():
            size = {'POLAR': 2, 'SPHERICAL': 3, 'BEARING_1D': 1, 'BEARING_2D': 2, 'CARTESIAN': 3}.get(key)
            if size is None:
                raise ValueError('Type de R_override inconnu')
            covariance(matrix, size, positive=True)
        return self

    def to_dict(self):
        return asdict(self)


@dataclass
class Gaussian:
    x: np.ndarray
    P: np.ndarray


def moment(states, weights):
    weights = np.asarray(weights, float)
    weights = weights / weights.sum()
    x = sum(w * state.x for w, state in zip(weights, states))
    P = sum(w * (state.P + np.outer(state.x-x, state.x-x)) for w, state in zip(weights, states))
    return Gaussian(x, (P+P.T)/2)


class FilterBank:
    def __init__(self, states, weights, config):
        self.states, self.weights, self.config = states, np.asarray(weights), config

    @property
    def estimate(self):
        return moment(self.states, self.weights)

    def predict(self, dt):
        c = self.config
        states, weights = self.states, self.weights
        if c.estimator == 'IMM':
            transitions = np.asarray(c.imm_transition)
            predicted_weights = weights @ transitions
            states = [moment(states, weights*transitions[:, j]/predicted_weights[j]) for j in range(2)]
            weights = predicted_weights
        result = []

        if c.estimator == 'IMM':
            models = c.dynamics.split('+')
        else :
            models = (c.dynamics,)
        #models = ('CV', 'CA') if c.estimator == 'IMM' else (c.dynamics,)
        for state, kind in zip(states, models):
            if kind == 'CV':
                noise = c.process_noise_cv
            if kind == 'CV_2':
                noise = c.process_noise_cv_2
            else:
                noise = c.process_noise_ca

            f, q = Dynamics(kind, noise, embedded=c.dynamics == 'CV+CA').matrices(dt)
            # Override pédagogique : Q FIXE, ne dépend plus du dt.
            if kind in c.Q_override:
                q = np.asarray(c.Q_override[kind], float)
            P = f @ state.P @ f.T + q
            result.append(Gaussian(f @ state.x, (P+P.T)/2))
        return FilterBank(result, weights.copy(), c)


    def classification(self,observation):
        return
        print('in classification')
        print(observation.sensor_type)
        print(observation.classification)
        print(observation.data)

    def updated(self, observation):
        observation = observation.converted() if self.config.estimator == 'CMKF' else observation

        if self.config.estimator == 'CMKF' and 'SPHERICAL' in self.config.R_override:
            observation = copy.copy(observation)
            observation.R = np.asarray(self.config.R_override['CARTESIAN'], float)
        states, likelihoods, diagnostics = [], [], []


        for state in self.states:
   
            h = observation.jacobian(state.x)
            residual = observation.residual(observation.z, observation.predict(state.x))
            S = h @ state.P @ h.T + observation.R
            chol = np.linalg.cholesky(S)
            normalised = np.linalg.solve(chol, residual)
            nis = float(normalised @ normalised)
            loglik = -.5 * (nis + 2*np.log(np.diag(chol)).sum() + len(residual)*np.log(2*np.pi))
            K = np.linalg.solve(S, h @ state.P).T
            a = np.eye(len(state.x)) - K @ h
            # Forme de Joseph, conserve la covariance de bruit de mesure.
            #P = ENSTA  to complete
            states.append(Gaussian(state.x + K @ residual, (P+P.T)/2))
            likelihoods.append(loglik)
            diagnostics.append({'innovation': residual.tolist(), 'S': S.tolist(), 'NIS': nis})


        logs = np.log(np.maximum(self.weights, 1e-300)) + likelihoods
        loglik = float(logsumexp(logs))


        posterior = np.exp(logs-loglik)
        posterior = np.maximum(posterior, 1e-300)
        posterior /= posterior.sum()

        best = diagnostics[int(np.argmax(posterior))]
        # Gate de l'IMM sur la distribution prédite fusionnée, pas le meilleur modèle a posteriori.
        state = self.estimate
        h = observation.jacobian(state.x)
        residual = observation.residual(observation.z, observation.predict(state.x))
        S = h @ state.P @ h.T + observation.R
        gate_nis = float(residual @ np.linalg.solve(S, residual))
        best = dict(best, gate_NIS=gate_nis, log_likelihood=loglik,
                    R=observation.R.tolist(), measurement_id=observation.identifier,origin=observation.origin)
        return FilterBank(states, posterior, self.config), loglik, gate_nis, best

    @classmethod
    def combine(cls, banks, probabilities):
        probabilities = np.asarray(probabilities)
        probabilities /= probabilities.sum()
        weights = sum(p*b.weights for p, b in zip(probabilities, banks))
        states = []
        for mode in range(len(weights)):
            conditional = np.array([p*b.weights[mode] for p, b in zip(probabilities, banks)])
            if conditional.sum() > 0:
                states.append(moment([b.states[mode] for b in banks], conditional))
            else:
                states.append(copy.deepcopy(banks[int(np.argmax(probabilities))].states[mode]))
        return cls(states, weights, banks[0].config)


@dataclass
class Track:
    identifier: str
    bank: FilterBank
    timestamp: float
    last_detection: float
    hits: deque
    confirmed: bool = False
    history: deque = field(default_factory=lambda: deque(maxlen=300))
    diagnostics: dict = field(default_factory=dict)


class TrackingEngine:
    def __init__(self, config=None):
        self.config = (config or Configuration()).validate()
        self.tracks = []
        self.timestamp = None
        self.next_id = 1
        self.warnings = []

        self.run_id = uuid4().hex
        self.step_index = 0
    def _birth(self, measurement, timestamp):
        c = self.config
        distance = c.bearing_prior_range if c.allow_bearing_prior else None
        position = measurement.point(distance)
        if position is None:
            self.warnings.append('Bearing seul : activer un a priori de portée pour initialiser, ou attendre une mesure de distance')
            return
        n = 9 if 'CA' in c.dynamics  else 6

        x = np.zeros(n); x[:3] = position
        diagonal = [c.sigma_position**2]*3 + [c.sigma_velocity**2]*3
        if n == 9:
            diagonal += [c.sigma_acceleration**2]*3
        P = np.diag(diagonal)
        P[:3, :3] += measurement.initial_covariance(distance, c.bearing_prior_sigma, c.unobserved_altitude_sigma)
        if c.P0_diagonal is not None:
            P = np.diag(c.P0_diagonal)
        states = [Gaussian(x.copy(), P.copy()) for _ in range(2 if c.estimator == 'IMM' else 1)]
        weights = np.array(c.imm_probabilities) if c.estimator == 'IMM' else np.ones(1)
        track = Track(f'T{self.next_id:04d}', FilterBank(states, weights, c), timestamp, timestamp,
                      deque([True], maxlen=c.confirmation_window), c.confirmation_hits == 1)
        track.diagnostics = {'initial_measurement_id': measurement.identifier,'origin':measurement.origin,
                             'range_prior_used': measurement.kind.startswith('BEARING')}
        self.next_id += 1
        self.tracks.append(track)

    def _step(self, measurements, timestamp, coverage=None):
        c = self.config
        self.warnings = []

        if not math.isfinite(timestamp) or (self.timestamp is not None and timestamp < self.timestamp):
            raise ValueError('Scan ancien : le temps doit être croissant. Réinitialiser après un retour du timer')
        # KF linéaire : refuser explicitement bearing-only, pas de fausse portée.
        if c.estimator == 'CMKF':
            valid = [m for m in measurements if not m.kind.startswith('BEARING')]
            if len(valid) != len(measurements):
                self.warnings.append('CMKF : bearing-only ignoré, utiliser EKF ou IMM')
            measurements = valid

        self.tracks = [t for t in self.tracks if timestamp-t.last_detection <= c.deletion_seconds]
        for track in self.tracks:
            dt = timestamp - track.timestamp
            if dt > 0:
                track.bank = track.bank.predict(dt)
            track.timestamp = timestamp

        in_coverage = [
            coverage is None or coverage.contains(track.bank.estimate.x[:3])
            for track in self.tracks
        ]


        eligible = [visible for visible in in_coverage]#supported_scan and
      
        self.timestamp = timestamp
        count, size = len(self.tracks), len(measurements)
        candidates = {}

        degrees = len(measurements[0].z)  # 3 if c.estimator == 'CMKF' else

        # ---> A compléter ici pour le multi-cible
        # gating_threshold = ? 



        for i, track in enumerate(self.tracks):

            if not eligible[i]: #si pas dans le volume de surveillance
                continue

    
            for j, measurement in enumerate(measurements):
            
                try:
                    bank, likelihood, nis, diagnostics = track.bank.updated(measurement)
                    nis_classification                 = track.bank.classification(measurement)

                    if c.family == 'Monocible' : #---> A compléter ici pour le multi-cible

                        candidates[i, j] = (bank, math.log(c.detection_probability)+likelihood-math.log(c.clutter_density), diagnostics)
                except (ValueError, np.linalg.LinAlgError) as exc:
                    self.warnings.append(f'{track.identifier}/{measurement.identifier} : {exc}')
        miss_logs = [
            math.log(1 - c.detection_probability * c.gate_probability)
            if visible else 0.0
            for visible in eligible
        ]
        beta = np.zeros((count, size+1))
        if count:
            if c.family == 'Multicible' and c.association == 'JPDAF':
                events, logs = [], []

                def enumerate_events(i, used, choices, score):
                    if i == count:
                        if len(events) >= c.max_hypotheses:
                            raise ValueError('JPDA : limite d’hypothèses atteinte ; réduire les plots, les pistes ou le gating')
                        events.append(tuple(choices)); logs.append(score)
                        return
                    enumerate_events(i + 1, used, choices + [size], score + miss_logs[i])
                    for j in range(size):
                        if j not in used and (i, j) in candidates:
                            enumerate_events(i+1, used | {j}, choices+[j], score+candidates[i, j][1])
                enumerate_events(0, set(), [], 0.)
                probabilities = np.exp(np.asarray(logs)-logsumexp(logs))
                for event, probability in zip(events, probabilities):
                    for i, j in enumerate(event):
                        beta[i, j] += probability
            elif c.family == 'Multicible' and c.association == 'GNNSF':
                costs = np.full((count, size+count), 1e12)
                for (i, j), (_, score, _) in candidates.items():
                    costs[i, j] = -score
                for i in range(count):
                    costs[i, size + i] = -miss_logs[i]
                rows, cols = linear_sum_assignment(costs)
                for i, j in zip(rows, cols):
                    beta[i, j if j < size else size] = 1
            elif c.family == 'Monocible' :
                for i, track in enumerate(self.tracks):

                    for j, measurement in enumerate(measurements):

                        if  measurement.origin == track.diagnostics['origin']:

                            beta[i, j] = 1

        for i, track in enumerate(self.tracks):

            banks, probabilities = [track.bank], [beta[i, size]]
            association = {}
            for j, measurement in enumerate(measurements):
                if beta[i, j] > 0:

                    banks.append(candidates[i, j][0]); probabilities.append(beta[i, j])
                    association[measurement.identifier] = float(beta[i, j])

            nonzero = [(b, p) for b, p in zip(banks, probabilities) if p > 0]
            track.bank = FilterBank.combine([b for b, _ in nonzero], [p for _, p in nonzero])
            hit = float(beta[i, :size].sum()) >= .5
            if eligible[i]:
                track.hits.append(hit)

            if hit:
                track.last_detection = timestamp
            track.confirmed |= sum(track.hits) >= c.confirmation_hits
            origin = track.diagnostics.get('origin','')
            track.diagnostics = {'associations': association, 'miss_probability': float(beta[i, size]),'origin':origin}
            if association:
                j = int(np.argmax(beta[i, :size]))
                track.diagnostics.update(candidates[i, j][2])
        used = beta[:, :size].sum(axis=0) if count else np.zeros(size)


        for j, measurement in enumerate(measurements):
            if used[j] >= .1 or len(self.tracks) >= c.max_tracks:
                continue
            if c.family == 'Monocible' and len(self.tracks)>0:
                break
            if c.family == 'Monocible' and measurement.origin=="false alarm":
                continue
            if c.family == 'Monocible' and c.seed_detection_id and not self.tracks and measurement.identifier != c.seed_detection_id:
                continue
            self._birth(measurement, timestamp)
        for track in self.tracks:
            track.history.append((timestamp, track.bank.estimate.x[:3].copy()))

        self.step_index += 1
        return self.snapshot()

    def step(self, measurements, timestamp, coverage=None):
        # Transaction logique : un dépassement JPDA ne doit pas laisser une demi-prédiction.

        backup = copy.deepcopy((self.tracks, self.timestamp, self.next_id))
        try:
            return self._step(measurements, float(timestamp),coverage)
        except Exception:
            self.tracks, self.timestamp, self.next_id = backup
            raise

    def snapshot(self):
        result = []
        for t in self.tracks:
            state = t.bank.estimate

            result.append({'id': t.identifier, 'timestamp': t.timestamp,
                'x': state.x.copy(), 'P': state.P.copy(), 'model_probabilities': t.bank.weights.copy(),
                'status': 'confirmée' if t.confirmed else 'provisoire',
                'coasting': t.timestamp > t.last_detection,
                'history': list(t.history), 'diagnostics': copy.deepcopy(t.diagnostics)})



        return {"run_id": self.run_id,   "step_index": self.step_index, 'timestamp': t.timestamp,'tracks': result, 'warnings': list(dict.fromkeys(self.warnings))}
