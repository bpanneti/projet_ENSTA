# target.py
from dataclasses import dataclass, replace
import math


def unwrap(value):
    """Déballer les tuples/listes contenant une seule valeur."""
    while isinstance(value, (tuple, list)):
        if len(value) != 1:
            raise ValueError("Une seule valeur est attendue")
        value = value[0]
    return value


def finite_number(value):
    value = float(unwrap(value))

    if not math.isfinite(value):
        raise ValueError("Valeur numérique non finie")

    return value


@dataclass
class Target:
    id: str
    name: str
    longitude: float
    latitude: float

    # Conservées séparément : leur référentiel reste à préciser.
    altitude: float | None = None
    altitude_m: float | None = None

    yaw: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0
    speed_kmh: float = 0.0

    dis_domain: int = 0
    dis_kind: int = 0
    dis_category: int = 0
    dis_id: str = ""
    dis_country: int = 0
    dis_subcategory: int = 0
    dis_extra: int = 0

    @property
    def color(self):
        return {
            1: "#F6A66B",
            2: "#7EDBF4",
        }.get(self.dis_domain, "#D5B2F4")

    @classmethod
    def from_message(cls, data, previous=None):
        identifier = data.get("id")

        if identifier is None or not str(identifier).strip():
            raise ValueError("Cible sans id")

        identifier = str(identifier).strip()

        target = (
            replace(previous)
            if previous is not None
            else cls(identifier, identifier, 0.0, 0.0)
        )

        position = unwrap(data.get("position", {}))

        if not isinstance(position, dict):
            raise ValueError("position doit contenir un dictionnaire")

        if previous is None and not {
            "longitude", "latitude"
        }.issubset(position):
            raise ValueError("Nouvelle cible sans position complète")

        for attribute in (
            "longitude", "latitude", "altitude", "altitude_m"
        ):
            if attribute in position:
                setattr(
                    target,
                    attribute,
                    finite_number(position[attribute]),
                )

        if not (
            -180 <= target.longitude <= 180
            and -90 <= target.latitude <= 90
        ):
            raise ValueError("Position WGS84 invalide")

        attitude = unwrap(data.get("attitude", {}))

        if not isinstance(attitude, dict):
            raise ValueError("attitude doit contenir un dictionnaire")

        for attribute in ("yaw", "pitch", "roll"):
            if attribute in attitude:
                setattr(
                    target,
                    attribute,
                    finite_number(attitude[attribute]),
                )

        target.yaw %= 360.0

        if "name" in data:
            target.name = str(data["name"])

        if "speed_kmh" in data:
            target.speed_kmh = finite_number(data["speed_kmh"])

        if "dis_id" in data:
            target.dis_id = str(data["dis_id"])

        for attribute in (
            "dis_domain",
            "dis_kind",
            "dis_category",
            "dis_country",
            "dis_subcategory",
            "dis_extra",
        ):
            if attribute in data:
                setattr(
                    target,
                    attribute,
                    int(unwrap(data[attribute])),
                )

        return target