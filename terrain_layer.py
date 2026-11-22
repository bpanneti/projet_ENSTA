"""Chargement asynchrone d'un fond DTED/GeoTIFF dans la carte WGS84."""
from pathlib import Path
import math
import numpy as np
import rasterio
from rasterio.enums import ColorInterp, Resampling
from rasterio.transform import Affine, array_bounds
from rasterio.warp import calculate_default_transform, reproject
import pyqtgraph as pg
from PyQt6.QtCore import QObject, QRunnable, QThreadPool, QRectF, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import QFileDialog, QMessageBox

from rasterio.warp import transform
from rasterio.windows import Window

def load_raster(path, max_size=2048):
    """Image RGBA sud→nord, rectangle lon/lat et informations du fichier."""
    with rasterio.open(path) as src:
        if src.crs is None:
            raise ValueError('Le fichier ne contient pas de projection. Un GeoTIFF géoréférencé est requis.')
        transform, width, height = calculate_default_transform(
            src.crs, 'EPSG:4326', src.width, src.height, *src.bounds)
        factor = max(1.0, width / max_size, height / max_size)
        dst_width, dst_height = max(1, math.ceil(width / factor)), max(1, math.ceil(height / factor))
        x_scale, y_scale = width / dst_width, height / dst_height
        transform = Affine(transform.a * x_scale, transform.b * y_scale, transform.c,
                           transform.d * x_scale, transform.e * y_scale, transform.f)
        if transform.a <= 0 or transform.e >= 0:
            raise ValueError('Grille WGS84 nord en haut attendue')
        interpretations = list(src.colorinterp)
        rgb = all(c in interpretations for c in (ColorInterp.red, ColorInterp.green, ColorInterp.blue))
        bands = [interpretations.index(c) + 1 for c in (ColorInterp.red, ColorInterp.green, ColorInterp.blue)] if rgb else [1]
        arrays = []
        for band in bands:
            destination = np.full((dst_height, dst_width), np.nan, dtype=np.float32)
            reproject(source=rasterio.band(src, band), destination=destination,
                      src_nodata=src.nodatavals[band - 1], dst_transform=transform,
                      dst_crs='EPSG:4326', dst_nodata=np.nan,
                      resampling=Resampling.bilinear, num_threads=2)
            arrays.append(destination)
        valid = np.logical_and.reduce([np.isfinite(a) for a in arrays])
        if not valid.any():
            raise ValueError('Le raster ne contient aucun pixel valide')
        rgba = np.zeros((dst_height, dst_width, 4), dtype=np.uint8)
        if rgb:
            for channel, array in enumerate(arrays):
                if src.dtypes[bands[channel] - 1] == 'uint8':
                    stretched = np.nan_to_num(array)
                else:
                    low, high = np.percentile(array[valid], [2, 98])
                    stretched = np.nan_to_num((array - low) / max(float(high - low), 1e-12)) * 255
                rgba[..., channel] = np.clip(stretched, 0, 255).astype(np.uint8)
            description = 'GeoTIFF RGB'
        else:
            array = arrays[0]
            low, high = np.percentile(array[valid], [2, 98])
            normalized = np.clip(np.nan_to_num((array - low) / max(float(high - low), 1e-12)), 0, 1)
            stops = np.array([0, .25, .5, .75, 1])
            colors = np.array([[27, 53, 76], [47, 99, 89], [116, 142, 97], [177, 153, 114], [232, 231, 213]])
            for channel in range(3):
                rgba[..., channel] = np.interp(normalized, stops, colors[:, channel]).astype(np.uint8)
            description = f'Bande 1 · valeurs [{array[valid].min():g}, {array[valid].max():g}]'
        rgba[..., 3] = np.where(valid, 255, 0)
        bounds = tuple(float(v) for v in array_bounds(dst_height, dst_width, transform))
        # Raster nord→sud ; PyQtGraph latitude croissante vers le haut.
        rgba = np.ascontiguousarray(np.flipud(rgba))
        info = f'{Path(path).name} · {description} · aperçu {dst_width} × {dst_height} · WGS84'
        return rgba, bounds, info


class _Signals(QObject):
    loaded = pyqtSignal(object)
    failed = pyqtSignal(str)
    finished = pyqtSignal()


class _LoadJob(QRunnable):
    def __init__(self, path):
        super().__init__()
        self.path = path
        self.signals = _Signals()

    def run(self):
        signals = self.signals
        try:
            signals.loaded.emit(load_raster(self.path))
        except Exception as exc:
            signals.failed.emit(str(exc))
        finally:
            signals.finished.emit()


class TerrainLayer(QObject):
    def __init__(self, window, menu=None):
        super().__init__(window)
        self.window = window
        self.image = None
        self.bounds = None
        self._job = None
        menu = menu if menu is not None else window.menuBar().addMenu('Carte')
        self.load_action = menu.addAction('Charger un DTED / GeoTIFF…', self.choose_file)
        self.visible_action = menu.addAction('Afficher le fond raster')
        self.visible_action.setCheckable(True)
        self.visible_action.setChecked(True)
        self.visible_action.toggled.connect(self._set_visible)
        menu.addAction('Centrer sur le raster', self.center)
        menu.addAction('Retirer le raster', self.remove)

        self.source_path = None
        self._loading_path = None

    def choose_file(self):
        path, _ = QFileDialog.getOpenFileName(self.window, 'Charger un fond géographique', '',
            'DTED / GeoTIFF (*.dted *.dt0 *.dt1 *.dt2 *.tif *.tiff);;Tous les fichiers (*)')
        if path:
            self.load(path)

    def load(self, path):
        if self._job is not None:
            return

        self._loading_path = str(path)

        self.load_action.setEnabled(False)
        self.window.statusBar().showMessage('Chargement du raster…')
        self._job = _LoadJob(path)
        self._job.signals.loaded.connect(self._display)
        self._job.signals.failed.connect(self._error)
        self._job.signals.finished.connect(self._finished)
        QThreadPool.globalInstance().start(self._job)



    @pyqtSlot(object)
    def _display(self, result):

        self.source_path = self._loading_path

        rgba, bounds, info = result
        # Le précédent fond est conservé si le chargement échoue.
        self.remove()
        left, bottom, right, top = bounds
        self.bounds = QRectF(left, bottom, right - left, top - bottom)
        self.image = pg.ImageItem(rgba, axisOrder='row-major', autoLevels=False)
        self.image.setRect(self.bounds)
        self.image.setZValue(-1000)  # Sous les emprises, nœuds et plots.
        self.image.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.window.plot.addItem(self.image)
        self.image.setVisible(self.visible_action.isChecked())
        self.center()
        self.window.statusBar().showMessage(info, 12000)

    @pyqtSlot(str)
    def _error(self, message):
        self.window.statusBar().showMessage('Chargement du raster impossible', 8000)
        QMessageBox.warning(self.window, 'Fond géographique', message)

    @pyqtSlot()
    def _finished(self):
        self._job = None
        self.load_action.setEnabled(True)

    def _set_visible(self, visible):
        if self.image is not None:
            self.image.setVisible(visible)

    def center(self):
        if self.bounds is not None:
            self.window.plot.setRange(rect=self.bounds, padding=.02)
            self.window.plot.disableAutoRange()

    def remove(self):
        if self.image is not None:
            self.window.plot.removeItem(self.image)
        self.image = None
        self.bounds = None
        self.source_path = None

    def ground_altitude(self, longitude, latitude):
        """
        Altitude du sol à une position WGS84.

        Retourne None si aucun terrain n'est chargé,
        si la position est hors raster ou si le pixel est NoData.

        Le fichier doit être un MNT monobande dont les altitudes
        sont exprimées en mètres.
        """
        if self.source_path is None:
            return None

        longitude = float(longitude)
        latitude = float(latitude)

        if not (
                math.isfinite(longitude)
                and math.isfinite(latitude)
                and -180 <= longitude <= 180
                and -90 <= latitude <= 90
        ):
            raise ValueError("Coordonnées WGS84 invalides")

        with rasterio.open(self.source_path) as terrain:
            if terrain.crs is None:
                raise ValueError("Le terrain ne possède pas de projection")

            if terrain.count != 1:
                raise ValueError(
                    "Un MNT monobande est requis pour lire les altitudes"
                )

            # Conversion WGS84 vers la projection du fichier original.
            xs, ys = transform(
                "EPSG:4326",
                terrain.crs,
                [longitude],
                [latitude],
            )

            x, y = xs[0], ys[0]

            if not (math.isfinite(x) and math.isfinite(y)):
                return None

            row, column = terrain.index(x, y)

            if not (
                    0 <= row < terrain.height
                    and 0 <= column < terrain.width
            ):
                return None

            # Lecture d'un seul pixel, avec prise en compte de NoData.
            pixel = terrain.read(
                1,
                window=Window(column, row, 1, 1),
                masked=True,
            )

            value = pixel[0, 0]

            if np.ma.is_masked(value):
                return None

            # Certains GeoTIFF stockent une échelle et un décalage.
            altitude = (
                    float(value) * terrain.scales[0]
                    + terrain.offsets[0]
            )

            return altitude if math.isfinite(altitude) else None
