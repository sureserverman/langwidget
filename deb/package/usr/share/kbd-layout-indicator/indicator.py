"""System-tray keyboard layout indicator using PyQt5.

Renders a short label (e.g. "EN", "DE") into a tray icon and updates
it in real time as the Wayland compositor reports layout changes.
Uses QSystemTrayIcon which speaks the StatusNotifierItem (SNI) D-Bus
protocol understood by LXQt panel.
"""

import json
import logging
import os
import signal
import sys
from pathlib import Path

from PyQt5.QtCore import Qt, QObject, QSocketNotifier, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PyQt5.QtWidgets import QAction, QApplication, QMenu, QSystemTrayIcon

from keyboard import WaylandKeyboardMonitor

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Label mapping  (XKB layout name -> short label)
# ---------------------------------------------------------------------------

DEFAULT_LABELS: dict[str, str] = {
    # Full XKB layout names (as returned by libxkbcommon)
    "English (US)": "EN",
    "English (UK)": "EN",
    "English (Dvorak)": "EN",
    "German": "DE",
    "German (Austria)": "DE",
    "German (Switzerland)": "DE",
    "French": "FR",
    "French (Canada)": "FR",
    "Spanish": "ES",
    "Spanish (Latin American)": "ES",
    "Italian": "IT",
    "Portuguese": "PT",
    "Portuguese (Brazil)": "PT",
    "Russian": "RU",
    "Ukrainian": "UA",
    "Polish": "PL",
    "Czech": "CZ",
    "Dutch": "NL",
    "Swedish": "SE",
    "Norwegian": "NO",
    "Danish": "DK",
    "Finnish": "FI",
    "Japanese": "JP",
    "Japanese (Kana)": "JP",
    "Chinese": "ZH",
    "Korean": "KO",
    "Arabic": "AR",
    "Hebrew": "HE",
    "Turkish": "TR",
    "Greek": "GR",
    "Hungarian": "HU",
    "Romanian": "RO",
    "Bulgarian": "BG",
    "Serbian": "RS",
    "Serbian (Cyrillic)": "RS",
    "Croatian": "HR",
    "Slovak": "SK",
    "Slovenian": "SI",
    "Thai": "TH",
    "Vietnamese": "VN",
    "Indonesian": "ID",
    "Latvian": "LV",
    "Lithuanian": "LT",
    "Estonian": "ET",
    "Icelandic": "IS",
}

CONFIG_DIR = Path.home() / ".config" / "kbd-layout-indicator"
MAP_FILE = CONFIG_DIR / "map.json"


def load_label_map() -> dict[str, str]:
    labels = dict(DEFAULT_LABELS)
    if MAP_FILE.exists():
        try:
            with open(MAP_FILE) as f:
                labels.update(json.load(f))
        except Exception as e:
            logger.warning("Failed to load %s: %s", MAP_FILE, e)
    return labels


def name_to_label(name: str, label_map: dict[str, str]) -> str:
    """Convert an XKB layout name to a 2-3 character display label."""
    # Exact match
    if name in label_map:
        return label_map[name]
    # Case-insensitive
    lower = name.lower()
    for k, v in label_map.items():
        if k.lower() == lower:
            return v
    # Match without parenthetical variant
    base = name.split("(")[0].strip()
    if base in label_map:
        return label_map[base]
    for k, v in label_map.items():
        if k.lower() == base.lower():
            return v
    # Last resort: first 2 chars uppercase
    return base[:2].upper() if base else "??"


# ---------------------------------------------------------------------------
# Icon rendering
# ---------------------------------------------------------------------------

_icon_cache: dict[str, QIcon] = {}


def render_icon(label: str, size: int = 48) -> QIcon:
    """Render a label string as a small tray icon (cached)."""
    key = f"{label}:{size}"
    if key in _icon_cache:
        return _icon_cache[key]

    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)

    p = QPainter(pixmap)
    p.setRenderHint(QPainter.Antialiasing)

    # Rounded dark background
    p.setBrush(QColor(55, 55, 55, 230))
    p.setPen(Qt.NoPen)
    margin = max(1, size // 16)
    radius = max(3, size // 8)
    p.drawRoundedRect(margin, margin, size - 2 * margin, size - 2 * margin,
                      radius, radius)

    # White text
    n = len(label)
    font_size = max(8, size // 3) if n <= 2 else max(6, size // 4)
    font = QFont("Sans", font_size, QFont.Bold)
    p.setFont(font)
    p.setPen(QColor(255, 255, 255))
    p.drawText(margin, margin, size - 2 * margin, size - 2 * margin,
               Qt.AlignCenter, label)
    p.end()

    icon = QIcon(pixmap)
    _icon_cache[key] = icon
    return icon


# ---------------------------------------------------------------------------
# Qt signal bridge (keyboard thread -> Qt main thread)
# ---------------------------------------------------------------------------

class _Bridge(QObject):
    layout_changed = pyqtSignal(str, int)


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class KeyboardLayoutIndicator:
    """Ties together Wayland keyboard monitoring and the Qt tray icon."""

    def __init__(self):
        # Ensure QApplication exists before any QWidget
        self._app = QApplication(sys.argv)
        self._app.setApplicationName("kbd-layout-indicator")
        self._app.setQuitOnLastWindowClosed(False)

        self._label_map = load_label_map()
        self._current_label = "??"

        # Signal bridge
        self._bridge = _Bridge()
        self._bridge.layout_changed.connect(self._update_tray)

        # System tray icon
        self._tray = QSystemTrayIcon()
        self._tray.setIcon(render_icon(self._current_label))
        self._tray.setToolTip("Keyboard Layout: detecting...")

        # Context menu
        menu = QMenu()
        quit_act = QAction("Quit", None)
        quit_act.triggered.connect(self._quit)
        menu.addAction(quit_act)
        self._tray.setContextMenu(menu)

        # Wayland monitor
        self._monitor = WaylandKeyboardMonitor(self._on_layout_change)
        self._notifier: QSocketNotifier | None = None

    # -- callbacks --

    def _on_layout_change(self, layout_name: str, group_index: int):
        label = name_to_label(layout_name, self._label_map)
        self._bridge.layout_changed.emit(label, group_index)

    def _update_tray(self, label: str, group_index: int):
        if label != self._current_label:
            self._current_label = label
            self._tray.setIcon(render_icon(label))
            self._tray.setToolTip(
                f"Keyboard Layout: {label} (group {group_index})"
            )
            logger.info("Tray updated: %s (group %d)", label, group_index)

    def _on_wayland_readable(self):
        try:
            self._monitor.dispatch()
        except Exception:
            logger.exception("Wayland dispatch error")

    def _quit(self):
        logger.info("Shutting down")
        if self._notifier:
            self._notifier.setEnabled(False)
        self._monitor.disconnect()
        self._app.quit()

    # -- main entry --

    def run(self) -> int:
        try:
            wl_fd = self._monitor.connect()
        except Exception:
            logger.exception("Failed to connect to Wayland compositor")
            return 1

        # Watch Wayland fd in Qt event loop
        self._notifier = QSocketNotifier(wl_fd, QSocketNotifier.Read)
        self._notifier.activated.connect(self._on_wayland_readable)

        self._tray.show()

        # Graceful shutdown on signals
        def _sig_handler(*_args):
            self._quit()

        signal.signal(signal.SIGINT, _sig_handler)
        signal.signal(signal.SIGTERM, _sig_handler)

        # Timer to let Python process signals (Qt blocks the GIL)
        from PyQt5.QtCore import QTimer
        timer = QTimer()
        timer.timeout.connect(lambda: None)
        timer.start(500)

        return self._app.exec_()
