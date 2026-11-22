"""Base qt_modern_style adaptée à une console sombre de visualisation."""
import qt_modern_style
from pathlib import Path


def apply_theme(app):
    # La distribution 2.0.1 référence des ressources :/ dans son QSS mais
    # embarque un resources_rc.py vide. Charger les SVG livrés sur disque.
    package_dir = qt_modern_style.get_package_dir()
    base = qt_modern_style.get_stylesheet()
    base = base.replace(":/BASE/base/", f"{package_dir}/icons/base/")
    base = base.replace(":/ASSETS/assets/", f"{package_dir}/icons/assets/")
    icons = Path(__file__).resolve().parent / "assets"
    app.setStyleSheet(base + """
        QWidget { color: #dce9f7; font-family: 'Segoe UI', 'Inter', 'Noto Sans', sans-serif; font-size: 13px; }
        QMainWindow, QDialog, QWidget#root { background: #0c1522; }
        QDockWidget { titlebar-close-icon: url(""" + str(icons / "close.svg") + """); titlebar-normal-icon: url(""" + str(icons / "float.svg") + """); color: #cde5f7; background: #101c2b; font-weight: 600; }
        QDockWidget::title { background: #20364b; padding: 7px; text-align: left; }
        QDockWidget::close-button, QDockWidget::float-button { background: #263e55; border-radius: 4px; padding: 2px; }
        QTreeWidget, QTableWidget, QPlainTextEdit { background: #142235; color: #dce9f7;
            border: 1px solid #31465e; gridline-color: #263b51; }
        QTreeWidget::item { padding: 5px 2px; }
        QTreeWidget::item:selected, QTableWidget::item:selected { background: #285875; }
        QHeaderView::section { background: #22384e; color: #bed6eb; padding: 6px; border: none; font-size: 12px; }
        QFrame#sidebar, QFrame#plotCard, QFrame#headerCard, QFrame#sidebarCard, QFrame#mqttCard {
            background: #121f30; border: 1px solid #22384d; border-radius: 16px;
        }
        QFrame#sidebarCard { background: #17273a; border-radius: 12px; }
        QFrame#mqttCard { background: #17283b; border-radius: 10px; }
        QLabel#eyebrow { color: #5fceee; font-size: 11px; font-weight: 700; }
        QLabel#pageTitle { color: #f4f8ff; font-size: 26px; font-weight: 700; }
        QLabel#dialogTitle { color: #f4f8ff; font-size: 20px; font-weight: 700; }
        QLabel#sectionTitle { color: #eef5ff; font-size: 15px; font-weight: 700; }
        QLabel#muted, QLabel#connectionHint { color: #8fa8c2; font-size: 12px; }
        QLabel#metric { color: #eff7ff; font-size: 25px; font-weight: 700; }
        QLabel#statePill { background: #233044; border-radius: 10px; padding: 8px 13px; font-weight: 700; }
        QPushButton { background: #203750; border: 1px solid #365371; border-radius: 9px;
                      color: #e8f4ff; padding: 9px 15px; font-weight: 600; }
        QPushButton:hover { background: #294b69; border-color: #58bddd; }
        QPushButton#primaryButton { background: #1679a4; border-color: #3097c4; color: white; }
        QPushButton#primaryButton:hover { background: #1c92c1; }
        QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox { background: #16263a; color: #f2f8ff;
                    border: 1px solid #36516d; border-radius: 8px; padding: 7px; min-height: 19px; }
        QLineEdit:focus, QSpinBox:focus, QDoubleSpinBoxx:focus, QComboBox:focus { border-color: #58bddd; }
        QComboBox QAbstractItemView { background: #16263a; color: #f2f8ff; selection-background-color: #236080; }
        QComboBox::drop-down, QDoubleSpinBox::up-button , QDoubleSpinBox::down-button , QSpinBox::up-button, QSpinBox::down-button {
            background: #263c54; border: none; border-left: 1px solid #36516d; width: 23px;
        }
        QComboBox::down-arrow, QDoubleSpinBox::down-arrow , QSpinBox::down-arrow { image: url(""" + str(icons / "chevron-down.svg") + """); width: 11px; height: 11px; }
        QSpinBox::up-arrow,QDoubleSpinBox::up-arrow { image: url(""" + str(icons / "chevron-up.svg") + """); width: 11px; height: 11px; }
        QGroupBox { border: 1px solid #31465e; border-radius: 10px; margin-top: 14px; padding-top: 12px; }
        QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; color: #bad9f2; }
        QMenuBar, QMenu, QStatusBar { background: #101c2b; color: #d9e9f8; }
        QMenu::item:selected { background: #23435e; }
    """)
