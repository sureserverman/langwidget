# langwidget

Keyboard layout indicator for LXQt on Wayland (labwc).

Displays the current keyboard layout as a short label (EN, DE, RU, etc.) in
the LXQt system tray. Built for Whonix 18 with the labwc compositor.

## How it works

```
Wayland compositor (labwc)
        |
        |  wl_keyboard events (keymap + modifiers)
        v
  Pure-Python Wayland client  -->  libxkbcommon (ctypes)
        |                            parse layout names
        v
  PyQt5 QSystemTrayIcon (SNI over D-Bus)
        |
        v
  LXQt panel tray area: [EN]
```

The indicator connects directly to the Wayland compositor socket, binds
`wl_seat` + `wl_keyboard`, and listens for:

- **`keymap`** events -- parsed via `libxkbcommon` to extract layout names
  per group (e.g. "English (US)", "German").
- **`modifiers`** events -- the `group` field tells us which layout is active.

The current layout name is mapped to a 2-3 character label and rendered onto a
small icon shown in the system tray via Qt's StatusNotifierItem protocol.

## Features

- Pure Python -- no compiled components, no build step.
- Wayland-native -- no XWayland required.
- Lightweight -- single process, minimal dependencies.
- LXQt integration -- uses SNI (StatusNotifierItem) via PyQt5.
- Configurable labels -- override layout-name-to-label mapping via JSON.
- Autostart -- XDG autostart desktop entry included.
- Systemd support -- optional systemd user service included.
- Multi-arch -- works on `amd64` and `arm64`.

## Dependencies

All available as Debian system packages (no pip required):

| Package | Purpose |
|---|---|
| `python3` (>= 3.10) | Runtime |
| `python3-pyqt5` | Tray icon (SNI) and icon rendering |
| `qtwayland5` | Qt Wayland platform plugin |
| `libxkbcommon0` | XKB keymap parsing (via ctypes) |

Recommended: `fonts-dejavu-core` (for consistent icon label rendering).

## Installation

Install the `.deb` package:

```
sudo dpkg -i langwidget_*.deb
sudo apt-get -f install   # resolve any missing dependencies
```

The indicator starts automatically on next login via XDG autostart.

To start it immediately without logging out:

```
kbd-layout-indicator &
```

### Optional: systemd user service

```
systemctl --user enable kbd-layout-indicator.service
systemctl --user start kbd-layout-indicator.service
```

## Configuration

### Custom label mapping

Create `~/.config/kbd-layout-indicator/map.json` to override or add
layout-name-to-label mappings:

```json
{
    "English (US)": "US",
    "English (Dvorak)": "DV",
    "German": "DE",
    "My Custom Layout": "XX"
}
```

The keys must match the XKB layout names reported by the compositor (visible
in the tooltip or with `kbd-layout-indicator -v`).

### Verbose logging

```
kbd-layout-indicator -v
```

Prints keymap parsing results and layout change events to stderr.

## File locations

| Path | Purpose |
|---|---|
| `/usr/bin/kbd-layout-indicator` | Launcher script |
| `/usr/share/kbd-layout-indicator/` | Python source |
| `/etc/xdg/autostart/kbd-layout-indicator.desktop` | XDG autostart entry |
| `/usr/lib/systemd/user/kbd-layout-indicator.service` | Systemd user service |
| `~/.config/kbd-layout-indicator/map.json` | User label overrides |

## Known limitations

Wayland does not provide a standard protocol for passively monitoring global
keyboard state. The indicator receives `wl_keyboard.modifiers` events (which
carry the active layout group) only when its surface has keyboard focus. In
practice this means:

- The **initial layout** is detected on startup from the keymap event.
- **Live layout switching** updates depend on the compositor delivering
  `modifiers` events to passive clients. wlroots-based compositors like labwc
  commonly do this, but behavior varies.
- If updates stop arriving, the indicator shows the last known layout.
  Right-clicking the tray icon (giving it focus) forces a refresh.

If the compositor does not deliver group changes to unfocused clients, a future
version may use compositor-specific IPC or input-method D-Bus integration
(IBus/Fcitx) as the layout source.

## License

GPL-3.0-or-later. See [LICENSE.md](LICENSE.md).
