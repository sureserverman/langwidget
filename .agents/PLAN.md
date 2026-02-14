# Wayland-Only Keyboard Layout Indicator (LXQt + labwc) - Plan

## Constraints (Hard Requirements)
- Must work on Whonix 18 on `amd64` and `arm64`.
- Must not use XWayland (no X11/XKB via X).
- Must not use C/C++ components (Python-only build and runtime).
- Must persist in the LXQt panel (tray area) and show the *current* layout.

## Reality Check (Wayland Feasibility)
Wayland is intentionally restrictive: many compositors do not expose global input state to arbitrary clients.

Go/No-Go test to run early:
1. Create a minimal Wayland client that binds `wl_seat` + `wl_keyboard`.
2. While *another app is focused*, switch layouts using the compositor keybinding.
3. Verify we still receive updates that allow determining the active layout:
   - Best case: `wl_keyboard.modifiers` continues to arrive and the `group` value changes.
   - Also acceptable: `wl_keyboard.keymap` is resent on layout changes.

If neither event updates when we are not focused, a global indicator is not implementable with standard Wayland protocols alone. In that case, the only viable paths are compositor-specific IPC/protocols or using an input method framework (IBus/Fcitx) as the source of truth.

This plan proceeds assuming labwc provides sufficient events for a passive client (common in wlroots-based compositors, but must be verified).

## High-Level Design
Two long-running components in one Python process:
1. **Wayland watcher**: connects to the compositor, listens for keyboard state changes, derives a short label like `EN` / `DE`.
2. **Tray indicator (StatusNotifierItem / SNI)**: exposes an icon + tooltip via DBus so LXQt panel shows it persistently.

Data flow:
`Wayland events -> current_layout_label -> SNI icon/tooltip update`

## Implementation Outline
### 1) Wayland watcher (Python)
Responsibilities:
- Connect to `WAYLAND_DISPLAY`.
- Bind globals: `wl_registry`, `wl_seat`, `wl_keyboard`.
- Capture:
  - `wl_keyboard.keymap` (XKB keymap as fd + size).
  - `wl_keyboard.modifiers` (includes `group`).
- Maintain:
  - Parsed keymap (XKB) -> mapping of `group index -> (layout, variant)`.
  - Current `group` -> current label.

Key technical choice: parsing XKB keymaps in Python without C code.
- Preferred: use a Python binding to libxkbcommon (if available on Debian/Whonix).
- Fallback: use `ctypes` to call a tiny subset of `libxkbcommon.so` APIs (still Python-only; no compilation).

Minimal libxkbcommon surface area to target:
- Create context.
- Build keymap from the provided keymap string.
- Query layout/group names for each group index.

### 2) Tray indicator (SNI via DBus)
Responsibilities:
- Implement a StatusNotifierItem service name on the session bus.
- Expose:
  - `Title`, `ToolTip` (include layout + variant + group index).
  - `IconPixmap` (dynamic image that contains the label text).
  - Simple menu actions: `Quit`.

Icon rendering:
- Generate a small PNG (e.g., 32x32 and 48x48) with the layout label centered.
- Convert to SNI `IconPixmap` format (ARGB32) in-process.
- Cache per label to avoid re-render cost.

### 3) Persistence
Provide both:
- XDG autostart entry: `~/.config/autostart/kbd-layout-indicator.desktop`.
- Optional systemd user service: `~/.config/systemd/user/kbd-layout-indicator.service`.

## Dependencies / Requirements
### Runtime
- `python3` (3.10+ recommended; match Whonix 18 default)
- DBus client:
  - `dbus-next` (recommended) or equivalent
- Image rendering:
  - `Pillow` (PIL) for PNG generation
- Wayland client bindings:
  - Preferred: `pywayland` (Python bindings for Wayland client + protocol support)
  - Alternative (if `pywayland` is unavailable): a small pure-Python/ctypes wrapper around `libwayland-client.so`
- XKB parsing:
  - Preferred: Python binding for `libxkbcommon`
  - Fallback: `ctypes` calls into `libxkbcommon.so.0` (no compilation)

### Packaging (recommended for Whonix)
- Debian packaging or a minimal `pyproject.toml` + `pip install --user .`

## Step-by-Step Plan (Minimal, Then Harden)
1. **Probe labwc event behavior (Go/No-Go)**
   - Build a tiny Python Wayland client that logs `keymap` and `modifiers.group`.
   - Confirm we can detect layout changes while unfocused.
2. **Derive a stable “current label”**
   - Parse keymap to get per-group names.
   - Implement label rules:
     - Prefer 2-3 chars: `us` -> `EN`, `de` -> `DE` (configurable mapping).
     - If unknown, show raw group index like `G2`.
3. **Implement SNI tray service**
   - Show tooltip + dynamically rendered icon.
   - Update immediately on layout change.
4. **Persistence**
   - Add autostart `.desktop`.
   - Add optional systemd user service.
5. **Hardening**
   - Reconnect logic if Wayland disconnects on session restart.
   - Debounce rapid event bursts.
   - Handle missing keymap/parse failures gracefully (show `??`).
6. **Cross-arch validation**
   - Test on Whonix 18 `amd64` and `arm64`.
   - Verify LXQt panel displays SNI consistently.

## Acceptance Criteria (Definition of Done)
- Visible in LXQt panel tray area after login (autostart or systemd user).
- Switching layouts updates indicator within 200 ms.
- Works without XWayland running.
- No compiled components; installable as Python-only (plus shared libs already on system).
- Survives logout/login; does not leak processes; clean exit from menu.

## Key Risks / Mitigations
- Risk: Wayland does not deliver usable layout/group updates to unfocused clients.
  - Mitigation: run the Go/No-Go probe first; if it fails, pivot to compositor-specific IPC or input-method DBus integration (IBus/Fcitx) as the layout source.
- Risk: Python Wayland bindings not available in Whonix repos.
  - Mitigation: support `pip --user` install; keep the protocol usage minimal; consider vendoring generated protocol stubs if needed.
- Risk: Layout names in XKB keymap not matching user expectations (`us` vs `en`).
  - Mitigation: configurable mapping file (`~/.config/kbd-layout-indicator/map.json`).

