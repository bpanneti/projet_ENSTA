"""Items PyQtGraph spécialisés : chaque capteur conserve une icône vectorielle."""
import math

import pyqtgraph as pg
from PyQt6.QtCore import QPointF, QRectF
from PyQt6.QtGui import QBrush, QColor, QPainterPath, QRadialGradient, QTransform
from pyproj import Geod

from models import Detection, Footprint, Node, Sensor
from PyQt6.QtGui import QTransform
from entity.target import Target

GEOD = Geod(ellps="WGS84")


class DegreeAxisItem(pg.AxisItem):
    def tickStrings(self, values, scale, spacing):
        decimals = max(2, min(7, math.ceil(-math.log10(max(abs(spacing), 1e-7)))))
        return [f"{value:.{decimals}f}°" for value in values]


SENSOR_COLORS = {
    "radar": "#56C7F3",
    "eo": "#F8B85D",
    "rf": "#BD9BF7",
    "acoustic": "#71D9AE",
    "unknown": "#B8C6D9",
}
SENSOR_LABELS = {
    "radar": "Radar",
    "eo": "Optique / EO",
    "rf": "Radiofréquence",
    "acoustic": "Acoustique",
    "unknown": "Autre",
}


def sensor_symbol(sensor_type: str) -> QPainterPath:
    """Pictogrammes normalisés entre -1 et 1, utilisables comme symboles ScatterPlotItem."""
    path = QPainterPath()
    if sensor_type == "radar":
        path.moveTo(-0.6, 0.7)
        path.lineTo(0.05, 0.05)
        path.lineTo(0.64, 0.7)
        path.moveTo(-0.75, 0.75)
        path.lineTo(0.75, 0.75)
        path.moveTo(-0.32, -0.18)
        path.cubicTo(-0.32, -0.6, 0.32, -0.6, 0.32, -0.18)
        path.moveTo(-0.62, -0.12)
        path.cubicTo(-0.62, -0.96, 0.62, -0.96, 0.62, -0.12)
    elif sensor_type == "eo":
        path.addRoundedRect(QRectF(-0.8, -0.48, 1.6, 0.98), 0.18, 0.18)
        path.addEllipse(QRectF(-0.32, -0.3, 0.64, 0.64))
        path.moveTo(-0.48, -0.48)
        path.lineTo(-0.26, -0.77)
        path.lineTo(0.28, -0.77)
        path.lineTo(0.47, -0.48)
    elif sensor_type == "rf":
        path.moveTo(0, 0.8)
        path.lineTo(0, -0.22)
        path.moveTo(-0.47, 0.8)
        path.lineTo(0.47, 0.8)
        path.moveTo(-0.28, -0.05)
        path.cubicTo(-0.55, -0.28, -0.55, -0.61, -0.28, -0.76)
        path.moveTo(0.28, -0.05)
        path.cubicTo(0.55, -0.28, 0.55, -0.61, 0.28, -0.76)
        path.moveTo(-0.55, 0.13)
        path.cubicTo(-1.0, -0.35, -1.0, -0.68, -0.64, -0.94)
        path.moveTo(0.55, 0.13)
        path.cubicTo(1.0, -0.35, 1.0, -0.68, 0.64, -0.94)
    elif sensor_type == "acoustic":
        path.addRoundedRect(QRectF(-0.28, -0.75, 0.56, 1.0), 0.25, 0.25)
        path.moveTo(-0.55, -0.1)
        path.cubicTo(-0.55, 0.82, 0.55, 0.82, 0.55, -0.1)
        path.moveTo(0, 0.6)
        path.lineTo(0, 0.85)
        path.moveTo(-0.37, 0.85)
        path.lineTo(0.37, 0.85)
    else:
        path.moveTo(0, -0.9)
        path.lineTo(0.9, 0)
        path.lineTo(0, 0.9)
        path.lineTo(-0.9, 0)
        path.closeSubpath()
    return path


class NodeItem(pg.ScatterPlotItem):
    def __init__(self, node: Node):
        super().__init__(
            x=[node.longitude], y=[node.latitude], symbol="s", size=19,
            pen=pg.mkPen("#e4f0ff" if node.status.lower() == "running" else "#f07878", width=2), brush=pg.mkBrush(node.color),
            hoverable=True, tip=lambda x, y, data: f"Nœud {node.label} ({node.id})\n{node.raw_node_type} · statut {node.status}\nYaw {node.yaw:g}°",
        )
        self.model = node


class SensorItem(pg.ScatterPlotItem):
    def __init__(self, sensor: Sensor):
        kind = sensor.sensor_type.lower()
        color = SENSOR_COLORS.get(kind, SENSOR_COLORS["unknown"])
        super().__init__(
            x=[sensor.longitude], y=[sensor.latitude], symbol=sensor_symbol(kind), size=27,
            pen=pg.mkPen(color, width=2.4), brush=pg.mkBrush(15, 31, 47, 235),
            hoverable=True,
            tip=lambda x, y, data: f"{SENSOR_LABELS.get(kind, 'Capteur')} · {sensor.label}\nID : {sensor.sensor_id or sensor.id}\nStatut : {sensor.status}\n{sensor.position_note}",
        )
        self.model = sensor


class DetectionItem(pg.ScatterPlotItem):
    def __init__(self, detection: Detection):
        super().__init__(
            x=[detection.longitude], y=[detection.latitude], symbol="o", size=13,
            pen=pg.mkPen("#fff4ec", width=2), brush=pg.mkBrush("#fb796c"),
            hoverable=True, tip=lambda x, y, data: f"Détection {detection.label} ({detection.id})",
        )
        self.model = detection

class TargetItem(pg.ScatterPlotItem):
    def __init__(self, target: Target):
        # Flèche pointant vers le haut de l'écran : Nord.
        arrow = QPainterPath()
        arrow.moveTo(0.0, -0.7)
        arrow.lineTo(0.5, 0.6)
        arrow.lineTo(0.0, 0.3)
        arrow.lineTo(-0.5, 0.6)
        arrow.closeSubpath()

        # Les symboles ScatterPlotItem sont dessinés en pixels.
        # Rotation positive : sens horaire sur l'écran.
        rotation = QTransform()
        rotation.rotate(target.yaw)
        symbol = rotation.map(arrow)

        super().__init__(
            x=[target.longitude],
            y=[target.latitude],
            symbol=symbol,
            size=25,
            pxMode=True,
            pen=pg.mkPen("#FFFFFF", width=1.5),
            brush=pg.mkBrush(target.color),
            hoverable=True,
            tip=lambda x, y, data: (
                f"{target.name}\n"
                f"ID : {target.id}\n"
                f"Longitude : {target.longitude:.7f}°\n"
                f"Latitude : {target.latitude:.7f}°\n"
                f"altitude : {target.altitude}\n"
                f"altitude_m : {target.altitude_m}\n"
                f"Yaw : {target.yaw:.1f}°\n"
                f"Pitch : {target.pitch:.1f}°\n"
                f"Roll : {target.roll:.1f}°\n"
                f"Vitesse : {target.speed_kmh:.1f} km/h\n"
                f"DIS : {target.dis_id}"
            ),
        )

        self.model = target
        self.setZValue(20)

class FootprintItem(pg.GraphicsObject):
    def __init__(self, footprint: Footprint, color: str = "#56C7F3"):
        super().__init__()
        self.model = footprint
        self._color = QColor(color)
        self._path = self._build_path(footprint)
        self.setZValue(-10)

    @staticmethod
    def _build_path(f: Footprint) -> QPainterPath:
        path = QPainterPath()
        r = max(0.0, f.range_m)
        if r == 0:
            path.moveTo(f.longitude, f.latitude)
            return path
        full_circle = f.aperture_deg >= 360
        aperture = min(360.0, max(0.0, f.aperture_deg))
        start = f.azimuth_deg - aperture / 2
        steps = max(2, math.ceil(aperture / 2))
        def point(azimuth, distance):
            lon, lat, _ = GEOD.fwd(f.longitude, f.latitude, azimuth, distance)
            lon = f.longitude + (lon - f.longitude + 180) % 360 - 180
            return QPointF(lon, lat)
        # Un secteur annulaire respecte distanceMin : le centre est exclu.
        for index in range(steps + 1):
            p = point(start + aperture * index / steps, r)
            if index == 0:
                path.moveTo(p)
            else:
                path.lineTo(p)
        if full_circle:
            path.closeSubpath()
            if f.range_min_m > 0:
                for index in range(steps + 1):
                    p = point(start + aperture * index / steps, f.range_min_m)
                    if index == 0:
                        path.moveTo(p)
                    else:
                        path.lineTo(p)
                path.closeSubpath()
        else:
            if f.range_min_m > 0:
                for index in range(steps, -1, -1):
                    path.lineTo(point(start + aperture * index / steps, f.range_min_m))
            else:
                path.lineTo(QPointF(f.longitude, f.latitude))
            path.closeSubpath()
        return path

    def boundingRect(self) -> QRectF:
        return self._path.boundingRect()

    def paint(self, painter, option, widget=None):
        f = self.model
        if f.range_m <= 0:
            return
        gradient = QRadialGradient(QPointF(0, 0), f.range_m)
        center = QColor(self._color)
        middle = QColor(self._color)
        edge = QColor(self._color)
        center.setAlpha(88)
        middle.setAlpha(47)
        edge.setAlpha(3)
        gradient.setColorAt(0, center)
        gradient.setColorAt(0.55, middle)
        gradient.setColorAt(1, edge)
        # Le dégradé utilise un repère métrique local ; la limite du volume
        # est calculée sur l'ellipsoïde WGS84 par GEOD.fwd.
        brush = QBrush(gradient)
        transform = QTransform()
        transform.translate(f.longitude, f.latitude)
        transform.scale(1 / (111320 * max(0.01, math.cos(math.radians(f.latitude)))), 1 / 111320)
        brush.setTransform(transform)
        painter.setBrush(brush)
        outline = QColor(self._color)
        outline.setAlpha(160)
        painter.setPen(pg.mkPen(outline, width=1))
        painter.drawPath(self._path)
