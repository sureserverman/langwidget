"""Pure-Python Wayland keyboard monitor + libxkbcommon XKB parser.

Connects to the Wayland compositor via Unix socket, binds wl_seat and
wl_keyboard, listens for keymap and modifiers events to track the
current keyboard layout group.

XKB keymap parsing uses ctypes bindings to libxkbcommon.so.0 (no
compiled code required).
"""

import ctypes
import logging
import mmap
import os
import socket
import struct

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# XKB parser (ctypes to libxkbcommon)
# ---------------------------------------------------------------------------

XKB_CONTEXT_NO_FLAGS = 0
XKB_KEYMAP_FORMAT_TEXT_V1 = 1
XKB_KEYMAP_COMPILE_NO_FLAGS = 0


class XkbParser:
    """Extract layout names from an XKB keymap string via libxkbcommon."""

    def __init__(self):
        try:
            self._lib = ctypes.CDLL("libxkbcommon.so.0")
        except OSError:
            logger.error("libxkbcommon.so.0 not found; install libxkbcommon0")
            self._lib = None
            self._ctx = None
            return
        self._setup()
        self._ctx = self._lib.xkb_context_new(XKB_CONTEXT_NO_FLAGS)

    def _setup(self):
        L = self._lib
        L.xkb_context_new.restype = ctypes.c_void_p
        L.xkb_context_new.argtypes = [ctypes.c_int]
        L.xkb_context_unref.argtypes = [ctypes.c_void_p]
        L.xkb_keymap_new_from_string.restype = ctypes.c_void_p
        L.xkb_keymap_new_from_string.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_int,
        ]
        L.xkb_keymap_unref.argtypes = [ctypes.c_void_p]
        L.xkb_keymap_num_layouts.restype = ctypes.c_uint32
        L.xkb_keymap_num_layouts.argtypes = [ctypes.c_void_p]
        L.xkb_keymap_layout_get_name.restype = ctypes.c_char_p
        L.xkb_keymap_layout_get_name.argtypes = [ctypes.c_void_p, ctypes.c_uint32]

    def parse(self, keymap_string: str) -> list[str]:
        """Return list of layout names, one per group index."""
        if not self._ctx:
            return self._regex_fallback(keymap_string)
        km = self._lib.xkb_keymap_new_from_string(
            self._ctx,
            keymap_string.encode("utf-8"),
            XKB_KEYMAP_FORMAT_TEXT_V1,
            XKB_KEYMAP_COMPILE_NO_FLAGS,
        )
        if not km:
            logger.warning("xkb_keymap_new_from_string failed, using regex fallback")
            return self._regex_fallback(keymap_string)
        n = self._lib.xkb_keymap_num_layouts(km)
        names = []
        for i in range(n):
            raw = self._lib.xkb_keymap_layout_get_name(km, i)
            names.append(raw.decode("utf-8") if raw else f"Group{i}")
        self._lib.xkb_keymap_unref(km)
        return names

    @staticmethod
    def _regex_fallback(keymap_string: str) -> list[str]:
        """Fallback: grep xkb_layout names from the raw keymap text."""
        import re
        names = re.findall(
            r'xkb_layout\s*{\s*"([^"]+)"', keymap_string
        )
        if not names:
            names = re.findall(
                r'name\[Group(\d+)\]\s*=\s*"([^"]+)"', keymap_string
            )
            if names:
                names.sort(key=lambda x: int(x[0]))
                names = [n[1] for n in names]
        return names if names else ["Unknown"]

    def cleanup(self):
        if self._lib and self._ctx:
            self._lib.xkb_context_unref(self._ctx)
            self._ctx = None


# ---------------------------------------------------------------------------
# Wayland wire protocol helpers
# ---------------------------------------------------------------------------

def _wayland_socket_path() -> str:
    """Determine the Wayland socket path."""
    display = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
    if display.startswith("/"):
        return display
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if not xdg:
        raise RuntimeError("XDG_RUNTIME_DIR not set")
    return os.path.join(xdg, display)


def _pack_uint(v: int) -> bytes:
    return struct.pack("<I", v)


def _pack_int(v: int) -> bytes:
    return struct.pack("<i", v)


def _pack_string(s: str) -> bytes:
    b = s.encode("utf-8") + b"\x00"
    length = len(b)
    pad = (4 - length % 4) % 4
    return struct.pack("<I", length) + b + b"\x00" * pad


def _build_msg(obj_id: int, opcode: int, payload: bytes) -> bytes:
    size = 8 + len(payload)
    header = struct.pack("<II", obj_id, (size << 16) | opcode)
    return header + payload


# Wayland protocol object IDs (well-known)
WL_DISPLAY_ID = 1

# wl_display opcodes (requests)
WL_DISPLAY_SYNC = 0
WL_DISPLAY_GET_REGISTRY = 1

# wl_display events
WL_DISPLAY_ERROR = 0
WL_DISPLAY_DELETE_ID = 1

# wl_registry opcodes (requests)
WL_REGISTRY_BIND = 0

# wl_registry events
WL_REGISTRY_GLOBAL = 0
WL_REGISTRY_GLOBAL_REMOVE = 1

# wl_seat opcodes (requests)
WL_SEAT_GET_POINTER = 0
WL_SEAT_GET_KEYBOARD = 1

# wl_seat events
WL_SEAT_CAPABILITIES = 0
WL_SEAT_NAME = 1

# wl_seat capability bits
WL_SEAT_CAP_KEYBOARD = 2

# wl_keyboard events
WL_KEYBOARD_KEYMAP = 0
WL_KEYBOARD_ENTER = 1
WL_KEYBOARD_LEAVE = 2
WL_KEYBOARD_KEY = 3
WL_KEYBOARD_MODIFIERS = 4
WL_KEYBOARD_REPEAT_INFO = 5

# wl_callback events
WL_CALLBACK_DONE = 0


class WaylandKeyboardMonitor:
    """Pure-Python Wayland client that monitors keyboard layout changes."""

    def __init__(self, on_layout_change):
        """
        on_layout_change(layout_name: str, group_index: int) is called
        whenever the active layout changes.
        """
        self._on_layout_change = on_layout_change
        self._sock: socket.socket | None = None
        self._xkb = XkbParser()

        # Object ID allocation
        self._next_id = 2
        self._registry_id: int | None = None
        self._seat_id: int | None = None
        self._keyboard_id: int | None = None
        self._callback_id: int | None = None

        # Wayland global names
        self._seat_global_name: int | None = None

        # State
        self._layout_names: list[str] = []
        self._current_group: int = 0
        self._recv_buf = bytearray()
        self._recv_fds: list[int] = []

        # Roundtrip tracking
        self._pending_sync: int | None = None
        self._sync_done = False

    def _alloc_id(self) -> int:
        oid = self._next_id
        self._next_id += 1
        return oid

    # -----------------------------------------------------------------------
    # Connection
    # -----------------------------------------------------------------------

    def connect(self) -> int:
        """Connect to Wayland and return the socket fd for event-loop use."""
        path = _wayland_socket_path()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.connect(path)
        self._sock.setblocking(False)

        # Request registry
        self._registry_id = self._alloc_id()
        self._send(_build_msg(
            WL_DISPLAY_ID, WL_DISPLAY_GET_REGISTRY,
            _pack_uint(self._registry_id),
        ))

        # Sync (roundtrip) so we get the global list
        self._callback_id = self._alloc_id()
        self._send(_build_msg(
            WL_DISPLAY_ID, WL_DISPLAY_SYNC,
            _pack_uint(self._callback_id),
        ))

        # Blocking initial roundtrip
        self._sock.setblocking(True)
        self._sync_done = False
        self._pending_sync = self._callback_id
        while not self._sync_done:
            self._read_and_dispatch()

        # Bind seat if found
        if self._seat_global_name is not None:
            self._bind_seat()
            # Another roundtrip to get capabilities + keyboard
            self._callback_id = self._alloc_id()
            self._send(_build_msg(
                WL_DISPLAY_ID, WL_DISPLAY_SYNC,
                _pack_uint(self._callback_id),
            ))
            self._sync_done = False
            self._pending_sync = self._callback_id
            while not self._sync_done:
                self._read_and_dispatch()

        self._sock.setblocking(False)
        return self._sock.fileno()

    def disconnect(self):
        self._xkb.cleanup()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # -----------------------------------------------------------------------
    # Send / Receive
    # -----------------------------------------------------------------------

    def _send(self, data: bytes, fds: list[int] | None = None):
        if fds:
            ancdata = [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                        struct.pack(f"{len(fds)}i", *fds))]
            self._sock.sendmsg([data], ancdata)
        else:
            self._sock.sendall(data)

    def _read_and_dispatch(self):
        """Read data from socket and dispatch complete messages."""
        try:
            max_fds = 4
            cmsg_space = socket.CMSG_SPACE(max_fds * struct.calcsize("i"))
            data, ancdata, _flags, _addr = self._sock.recvmsg(4096, cmsg_space)
        except BlockingIOError:
            return
        except ConnectionError:
            logger.error("Wayland connection lost")
            return

        if not data:
            logger.error("Wayland connection closed")
            return

        # Extract file descriptors from ancillary data
        for cmsg_level, cmsg_type, cmsg_data in ancdata:
            if (cmsg_level == socket.SOL_SOCKET
                    and cmsg_type == socket.SCM_RIGHTS):
                nfds = len(cmsg_data) // struct.calcsize("i")
                self._recv_fds.extend(
                    struct.unpack(f"{nfds}i", cmsg_data)
                )

        self._recv_buf.extend(data)
        self._dispatch_messages()

    def dispatch(self):
        """Non-blocking read + dispatch; call when fd is readable."""
        self._read_and_dispatch()

    def flush(self):
        """Flush is a no-op for blocking sends."""
        pass

    # -----------------------------------------------------------------------
    # Message parsing
    # -----------------------------------------------------------------------

    def _dispatch_messages(self):
        buf = self._recv_buf
        while len(buf) >= 8:
            obj_id, size_op = struct.unpack_from("<II", buf, 0)
            opcode = size_op & 0xFFFF
            msg_size = size_op >> 16
            if msg_size < 8 or len(buf) < msg_size:
                break
            payload = bytes(buf[8:msg_size])
            del buf[:msg_size]
            self._handle_event(obj_id, opcode, payload)

    def _consume_fd(self) -> int:
        """Pop the next received file descriptor."""
        if self._recv_fds:
            return self._recv_fds.pop(0)
        return -1

    def _handle_event(self, obj_id: int, opcode: int, payload: bytes):
        if obj_id == WL_DISPLAY_ID:
            self._handle_display_event(opcode, payload)
        elif obj_id == self._registry_id:
            self._handle_registry_event(opcode, payload)
        elif obj_id == self._seat_id:
            self._handle_seat_event(opcode, payload)
        elif obj_id == self._keyboard_id:
            self._handle_keyboard_event(opcode, payload)
        elif obj_id == self._callback_id:
            if opcode == WL_CALLBACK_DONE:
                self._sync_done = True

    # -- wl_display events --

    def _handle_display_event(self, opcode: int, payload: bytes):
        if opcode == WL_DISPLAY_ERROR:
            oid, code = struct.unpack_from("<II", payload, 0)
            msg_len = struct.unpack_from("<I", payload, 8)[0]
            msg = payload[12:12 + msg_len - 1].decode("utf-8", errors="replace")
            logger.error("Wayland display error: obj=%d code=%d msg=%s",
                         oid, code, msg)
        elif opcode == WL_DISPLAY_DELETE_ID:
            pass  # We don't track object deletion

    # -- wl_registry events --

    def _handle_registry_event(self, opcode: int, payload: bytes):
        if opcode == WL_REGISTRY_GLOBAL:
            name = struct.unpack_from("<I", payload, 0)[0]
            str_len = struct.unpack_from("<I", payload, 4)[0]
            iface = payload[8:8 + str_len - 1].decode("utf-8")
            padded = 8 + str_len + (4 - str_len % 4) % 4
            version = struct.unpack_from("<I", payload, padded)[0]
            if iface == "wl_seat":
                self._seat_global_name = name
                logger.info("Found wl_seat: name=%d version=%d", name, version)
        elif opcode == WL_REGISTRY_GLOBAL_REMOVE:
            pass

    # -- wl_seat events --

    def _handle_seat_event(self, opcode: int, payload: bytes):
        if opcode == WL_SEAT_CAPABILITIES:
            caps = struct.unpack_from("<I", payload, 0)[0]
            if caps & WL_SEAT_CAP_KEYBOARD and self._keyboard_id is None:
                self._bind_keyboard()

    def _handle_keyboard_event(self, opcode: int, payload: bytes):
        if opcode == WL_KEYBOARD_KEYMAP:
            fmt = struct.unpack_from("<I", payload, 0)[0]
            fd = self._consume_fd()
            size = struct.unpack_from("<I", payload, 4)[0]
            self._on_keymap(fmt, fd, size)
        elif opcode == WL_KEYBOARD_MODIFIERS:
            _serial, _dep, _lat, _lock, group = struct.unpack_from(
                "<IIIII", payload, 0
            )
            self._on_modifiers(group)
        # enter, leave, key, repeat_info: ignored

    # -----------------------------------------------------------------------
    # Bind helpers
    # -----------------------------------------------------------------------

    def _bind_seat(self):
        self._seat_id = self._alloc_id()
        iface_str = "wl_seat"
        version = 5
        payload = (
            _pack_uint(self._seat_global_name)
            + _pack_string(iface_str)
            + _pack_uint(version)
            + _pack_uint(self._seat_id)
        )
        self._send(_build_msg(self._registry_id, WL_REGISTRY_BIND, payload))
        logger.info("Bound wl_seat as object %d", self._seat_id)

    def _bind_keyboard(self):
        self._keyboard_id = self._alloc_id()
        payload = _pack_uint(self._keyboard_id)
        self._send(_build_msg(
            self._seat_id, WL_SEAT_GET_KEYBOARD, payload,
        ))
        logger.info("Created wl_keyboard as object %d", self._keyboard_id)

    # -----------------------------------------------------------------------
    # Keyboard event handlers
    # -----------------------------------------------------------------------

    def _on_keymap(self, fmt: int, fd: int, size: int):
        if fd < 0:
            logger.warning("No fd received for keymap event")
            return
        if fmt != 1:  # XKB_V1
            os.close(fd)
            return
        try:
            mm = mmap.mmap(fd, size, mmap.MAP_PRIVATE, mmap.PROT_READ)
            try:
                keymap_str = mm[:size].rstrip(b"\0").decode("utf-8")
            finally:
                mm.close()
        except Exception as e:
            logger.error("Failed to read keymap from fd: %s", e)
            return
        finally:
            os.close(fd)

        self._layout_names = self._xkb.parse(keymap_str)
        logger.info("Keymap layouts: %s", self._layout_names)
        self._notify()

    def _on_modifiers(self, group: int):
        if group != self._current_group:
            self._current_group = group
            logger.info("Layout group changed to %d", group)
            self._notify()

    def _notify(self):
        if self._layout_names and self._current_group < len(self._layout_names):
            name = self._layout_names[self._current_group]
        elif self._layout_names:
            name = self._layout_names[0]
        else:
            name = "??"
        self._on_layout_change(name, self._current_group)

    @property
    def layout_names(self) -> list[str]:
        return list(self._layout_names)

    @property
    def current_group(self) -> int:
        return self._current_group
