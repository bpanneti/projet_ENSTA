"""Compatibilité avec la version précédente. Le stockage est dans sqlite_recorder."""
from sqlite_recorder import SQLiteRecorder, TrackingContext, TrackingWrite, TRACKING_SCHEMA
TrackingSQLiteRecorder = SQLiteRecorder
