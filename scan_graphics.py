"""Objets cartographiques de scans et dégradés d'incertitude."""
import math
import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QColor, QPainterPath, QPen
from graphics_items import GEOD


def offset(lon, lat, east, north):
    azimuth = math.degrees(math.atan2(east, north))
    x, y, _ = GEOD.fwd(lon, lat, azimuth, math.hypot(east, north))
    return QPointF(lon + (x - lon + 180) % 360 - 180, y)


def contour(points):
    path = QPainterPath()
    for index, point in enumerate(points):
        if index == 0:
            path.moveTo(point)
        else:
            path.lineTo(point)
    path.closeSubpath()
    return path

class UncertaintyItem(pg.GraphicsObject):
    """Contour d’incertitude à 3 sigma, sans remplissage."""

    def __init__(self, shells, color, outline=None):
        super().__init__()
        self.setZValue(-3)

        # Signature conservée pour les appels existants.
        self.outline = outline
        self._bounds = (
            outline.boundingRect()
            if outline is not None
            else QPainterPath().boundingRect()
        )

        self.pen = QPen(QColor(color))
        self.pen.setWidthF(1.0)
        self.pen.setStyle(Qt.PenStyle.DotLine)
        self.pen.setCosmetic(True)

    def boundingRect(self):
        return self._bounds

    def paint(self, painter, option, widget=None):
        if self.outline is None:
            return

        painter.setPen(self.pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(self.outline)


def ellipse_item(longitude, latitude, covariance_enu, color):
    eigenvalues, vectors = np.linalg.eigh(covariance_enu)
    radii = np.sqrt(np.maximum(eigenvalues, 0))

    if radii.max() == 0:
        return None

    angles = np.linspace(0, 2 * np.pi, 73)
    unit = np.array([np.cos(angles), np.sin(angles)])

    # Un seul contour, à 3 sigma.
    points = vectors @ (3.0 * radii[:, None] * unit)

    outline = contour([
        offset(longitude, latitude, east, north)
        for east, north in points.T
    ])

    return UncertaintyItem([], color, outline=outline)


def angular_item(longitude, latitude, azimuth, sigma_deg, length_m, color):
    if sigma_deg <= 0:
        return None
    origin = QPointF(longitude, latitude)
    shells = []
    # Les bandes latérales représentent l'incertitude azimutale marginale.
    # Aucune incertitude de distance n'est déduite d'un BEARING.
    for inner, outer in zip(np.linspace(0, 3, 25)[:-1], np.linspace(0, 3, 25)[1:]):
        path = QPainterPath()
        for sign in (-1, 1):
            points = [origin]
            for angle in np.linspace(azimuth + sign * inner * sigma_deg,
                                     azimuth + sign * outer * sigma_deg, 5):
                lon, lat, _ = GEOD.fwd(longitude, latitude, float(angle), length_m)
                points.append(QPointF(longitude + (lon - longitude + 180) % 360 - 180, lat))
            path.addPath(contour(points))
        shells.append((path, max(1, round(85 * math.exp(-.5 * ((inner + outer) / 2) ** 2)))))
    return UncertaintyItem(shells, color)
