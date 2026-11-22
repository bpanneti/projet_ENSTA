# Capteurs Live — projet étudiant PyQt6 / PyQtGraph

Console sombre avec une carte vectorielle WGS84 (longitude et latitude en degrés), des emprises géodésiques, des pictogrammes par type de capteur et des panneaux détachables.

## Installation

Python 3.10 ou plus récent :

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

Sous Windows, utilisez `.venv\Scripts\activate` pour activer l'environnement. Le thème repose sur `qt-modern-style`, adapté dans `theme.py`.

## Panneaux

Les panneaux **Capteurs Live**, **Vue réseau**, **Connexion** et **Clients / serveur** sont des `QDockWidget`. Déplacez leur barre de titre pour les réorganiser ou les détacher. Fermez un panneau avec sa croix ; réaffichez-le via **Affichage**. **Réinitialiser les panneaux** restaure la disposition initiale. Le bouton **Connexion** affiche les réglages dans leur dock, sans boîte de dialogue modale. Masquer un panneau n'arrête pas la connexion.

Dans **Capteurs Live**, les boutons Radar, Optique/EO, RF, Acoustique et Autre activent ou désactivent l'affichage des capteurs du type choisi, ainsi que de leurs volumes et détections. Les couleurs et pictogrammes se modifient dans `graphics_items.py`.

**Vue réseau** contient un `QTreeWidget` : nœud → capteurs → volumes. Le nom et le type sont affichés avec les couleurs des capteurs. Les cases à cocher masquent les objets : décocher un nœud masque également ses capteurs et leurs volumes/détections ; décocher un capteur masque ses volumes/détections ; décocher un volume ne masque que celui-ci. Le filtre de type reste prioritaire. Double-cliquez sur un objet pour centrer la carte. Un capteur reçu avant son nœud figure sous un parent « en attente », remplacé à la réception du nœud.

## Modèle WGS84 et volumes

- Les positions sont `longitude`, puis `latitude`, en degrés WGS84. La carte présente une grille géographique ; aucun fond de tuiles n'est téléchargé.
- Les portées sont en mètres. L'azimut est géographique : 0° Nord, 90° Est ; l'ouverture est en degrés.
- `Node.sensors` contient plusieurs capteurs ; `Sensor.volumes` contient plusieurs `Footprint`.
- Les capteurs sont identifiés globalement par `sensor.id`. Les volumes ont une clé `(sensor_id, volume.id)` : deux volumes d'un même capteur doivent avoir des identifiants distincts.
- La limite au sol des emprises est calculée avec `pyproj.Geod(ellps="WGS84")`. Le dégradé translucide illustre la couverture, sans représenter une probabilité de détection.
- Des limites d'altitude facultatives sont conservées dans le modèle et l'infobulle. La carte affiche la projection au sol des volumes, en 2D.

Exemple d'ajout ou de mise à jour dans la fenêtre :

```python
from models import Node, Sensor, Footprint

node = Node("n1", 6.13, 43.12, "Nœud A")
sensor = Sensor("s1", "n1", 6.1305, 43.1204, "Radar A", "radar")
sensor.volumes["principal"] = Footprint(
    "s1", sensor.longitude, sensor.latitude,
    range_m=550, azimuth_deg=60, aperture_deg=95,
    id="principal", label="Secteur principal",
)
sensor.volumes["secondaire"] = Footprint(
    "s1", sensor.longitude, sensor.latitude,
    range_m=220, azimuth_deg=230, aperture_deg=100,
    id="secondaire", label="Secteur secondaire",
)
node.sensors[sensor.id] = sensor
window.upsert_node(node)
```

Les méthodes `upsert_node`, `upsert_sensor`, `upsert_volume` (alias `upsert_footprint`) et `upsert_detection` acceptent aussi des objets reçus séparément et mettent à jour leurs liens. Les messages réseau doivent être décodés avant ces appels, qui s'exécutent dans le thread Qt. Le format des trames est à fournir : `handle_message()` compte actuellement les octets reçus mais ne décode pas les objets. Pour TCP, les fragments doivent être reconstruits selon le cadrage à définir pour chaque client.

## Connexions et supervision

- **TCP classique** : cochez **Mode serveur TCP** pour écouter ; décochez pour agir comme client. Les réglages sont conservés séparément.
- **MQTT** : client abonné au topic configuré, avec identifiants facultatifs. Le mot de passe n'est pas sauvegardé.
- **LEXLUTOR Server** : mode serveur TCP distinct, acceptant plusieurs clients.

Pour écouter sur toutes les interfaces IPv4, utilisez `0.0.0.0` ; pour le poste seul, `127.0.0.1`. Les clients distants se connectent à l'IP réelle du poste serveur. Les transports de cette base de projet sont en clair.

**Clients / serveur**, situé en bas de la fenêtre avec son contenu aligné à droite, affiche les clients présents (IP, port, connexion, dernière réception, octets reçus) et les 100 derniers événements. La supervision vérifie l'écoute locale chaque seconde et affiche la durée d'activité et l'heure du dernier contrôle. **Vérifier le serveur** lance ce contrôle immédiatement. Ce contrôle confirme l'état local du socket ; l'accessibilité réseau se teste depuis un autre poste. **Déconnecter / arrêter** ferme l'écoute et les clients.

## Organisation

| Fichier | Rôle |
| --- | --- |
| `main.py` | Docks, carte, arbre, filtres, mise à jour des objets et supervision |
| `models.py` | Nœuds, capteurs, détections, volumes de surveillance |
| `graphics_items.py` | Items spécialisés, icônes vectorielles, contours géodésiques et dégradés |
| `dialogs.py` | `ConnectionPanel`, QWidget des paramètres réseau |
| `connections.py` | Client TCP, serveur TCP multiclient, client MQTT |
| `theme.py` | Styles du tableau de bord |
