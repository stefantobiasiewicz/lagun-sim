"""The terminal view — buttons for the inputs, lamps for the outputs, the traffic below.

`curses` from the standard library, as the hub's own view: no dependency to install on
a Raspberry Pi. The view only draws and forwards; what a click or a hold means is in
`control.py`, which is tested without a terminal.

    ┌──────────────────────────────────────────────────────────────────────┐
    │ panel3 'hall' · udp · Uid a1b2… · address 0x002                      │
    │                                                                      │
    │  inputs   [ 1 ]  [ 2 ]  [ 3 ]        ← mouse: press and hold          │
    │  outputs  ● 1  ○ 2  ○ 3 …                                            │
    │  status   ● READY                    ← the status LED, blinking       │
    │                                                                      │
    │ [  12.345] → EVENT  0x002 ep5 el 0x03  [00 01]                       │
    │ …                                                                    │
    │ 1-8 click · ←→ pick · Enter click · d double · space hold · p power · q│
    └──────────────────────────────────────────────────────────────────────┘

The loop turns every millisecond, as a board's does; the screen is redrawn a few dozen
times a second — a lamp that changes for less than that is not something a person sees.
"""

from __future__ import annotations

import contextlib
import curses
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Final

from lagunbus_node.frame import Frame

from lagunsim.control import Control
from lagunsim.runner import Direction, FrameSeen
from lagunsim.traffic import concerns, describe

TURN_S: Final = 0.001
REDRAW_S: Final = 0.04
LOG_LINES: Final = 500

BUTTON_W: Final = 7  # "[ 1 ]" plus a gap


@dataclass
class Header:
    device: str
    name: str
    bus: str
    uid: bytes
    state: str


@dataclass
class Log:
    started: float = field(default_factory=time.monotonic)
    lines: deque[tuple[Direction, str]] = field(default_factory=lambda: deque(maxlen=LOG_LINES))

    def add(self, direction: Direction, frame: Frame) -> None:
        stamp = time.monotonic() - self.started
        arrow = "→" if direction == "tx" else "←"
        self.lines.append((direction, f"[{stamp:9.3f}] {arrow} {describe(frame)}"))

    def note(self, text: str) -> None:
        stamp = time.monotonic() - self.started
        self.lines.append(("rx", f"[{stamp:9.3f}]   {text}"))


def frame_logger(control: Control, log: Log) -> FrameSeen:
    """The runner's `on_frame`: everything sent, and what was received for this device."""

    def seen(direction: Direction, frame: Frame) -> None:
        device = control.runner.device
        if direction == "rx" and not concerns(frame, device.address, device.short_id):
            return
        log.add(direction, frame)

    return seen


class View:
    def __init__(self, control: Control, header: Header, log: Log) -> None:
        self.control = control
        self.header = header
        self.log = log
        self.selected = 0
        self._buttons_y = -1
        self._buttons_x = 0
        self._mouse_down: int | None = None
        self._address = -1
        self._fault_menu = False

    # --- running -----------------------------------------------------------------------

    def run(self) -> None:
        curses.wrapper(self._main)

    def _main(self, screen: curses.window) -> None:
        curses.curs_set(0)
        screen.nodelay(True)
        screen.keypad(True)
        # Both edges of a mouse button, not a "click" guessed from them: interval 0.
        curses.mousemask(curses.BUTTON1_PRESSED | curses.BUTTON1_RELEASED)
        curses.mouseinterval(0)
        self._colours()

        self.control.runner.start()
        next_draw = 0.0
        while True:
            key = screen.getch()
            while key != -1:
                if not self._key(key):
                    return
                key = screen.getch()
            self.control.tick()
            self._watch_address()
            now = time.monotonic()
            if now >= next_draw:
                self._draw(screen)
                next_draw = now + REDRAW_S
            time.sleep(TURN_S)

    def _colours(self) -> None:
        self._on = curses.A_BOLD
        self._pressed = curses.A_REVERSE
        self._tx = curses.A_NORMAL
        self._rx = curses.A_DIM
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_GREEN, -1)
            curses.init_pair(2, curses.COLOR_YELLOW, -1)
            curses.init_pair(3, curses.COLOR_CYAN, -1)
            self._on = curses.color_pair(1) | curses.A_BOLD
            self._pressed = curses.color_pair(2) | curses.A_REVERSE
            self._rx = curses.color_pair(3)

    def _watch_address(self) -> None:
        address = self.control.runner.device.address
        if address != self._address:
            self._address = address
            self.log.note(f"address 0x{address:03X}" if address else "no address — announcing")

    # --- input ---------------------------------------------------------------------------

    def _key(self, key: int) -> bool:
        """False to quit."""
        c = self.control
        if self._fault_menu:
            self._fault_menu = False
            self._fault(key)
            return True
        if key in (ord("q"), ord("Q")):
            return False
        if key == ord("f"):
            self._fault_menu = True
            return True
        if ord("1") <= key <= ord("9"):
            self.selected = min(key - ord("1"), max(c.inputs - 1, 0))
            c.click(key - ord("1"))
        elif key == curses.KEY_LEFT:
            self.selected = max(self.selected - 1, 0)
        elif key == curses.KEY_RIGHT:
            self.selected = min(self.selected + 1, max(c.inputs - 1, 0))
        elif key in (curses.KEY_ENTER, 10, 13):
            c.click(self.selected)
        elif key == ord("d"):
            c.click(self.selected, times=2)
        elif key == ord("t"):
            c.click(self.selected, times=3)
        elif key == ord(" "):
            c.toggle_hold(self.selected)
        elif key == ord("p"):
            c.power_cycle()
        elif key == ord("s"):
            c.service_button()
            self.log.note("service button: held, let go — factory reset")
        elif key == curses.KEY_MOUSE:
            self._mouse()
        return True

    def _fault(self, key: int) -> None:
        """The second key after `f`; anything else closes the menu."""
        c = self.control
        actions = {
            ord("r"): (c.drop_response, "fault armed: drop the next response"),
            ord("b"): (c.busy, "fault armed: BUSY to the next request"),
            ord("s"): (c.silence, "fault: silent for 10 s"),
            ord("g"): (c.drop_segment, "fault armed: drop a segment of the next long response"),
            ord("c"): (c.clear_faults, "faults cleared"),
        }
        if key in actions:
            action, note = actions[key]
            action()
            self.log.note(note)

    def _mouse(self) -> None:
        try:
            _, x, y, _, state = curses.getmouse()
        except curses.error:
            return
        if state & curses.BUTTON1_PRESSED and y == self._buttons_y:
            index = (x - self._buttons_x) // BUTTON_W
            if 0 <= index < self.control.inputs:
                self.selected = index
                self._mouse_down = index
                self.control.press(index)
        elif state & curses.BUTTON1_RELEASED and self._mouse_down is not None:
            self.control.release(self._mouse_down)
            self._mouse_down = None

    # --- drawing -------------------------------------------------------------------------

    def _draw(self, screen: curses.window) -> None:
        screen.erase()
        height, width = screen.getmaxyx()
        device = self.control.runner.device
        h = self.header
        address = f"0x{device.address:03X}" if device.address else "none — announcing"
        where = "fresh" if h.state == "fresh" else "remembered"
        top = (
            f" {h.device} '{h.name}' · {h.bus} · Uid {h.uid.hex()} · "
            f"ShortId 0x{device.short_id:08X} · address {address} · {where}"
        )
        _put(screen, 0, 0, top, width, curses.A_BOLD)

        y = 2
        if self.control.inputs:
            _put(screen, y, 1, "inputs", width)
            self._buttons_y, self._buttons_x = y, 11
            for i in range(self.control.inputs):
                attr = self._pressed if self.control.is_pressed(i) else curses.A_NORMAL
                if i == self.selected:
                    attr |= curses.A_UNDERLINE
                label = f"[ {i + 1} ]" + ("*" if self.control.is_held(i) else " ")
                _put(screen, y, self._buttons_x + i * BUTTON_W, label, width, attr)
            y += 1
        if device.output_count:
            _put(screen, y, 1, "outputs", width)
            for i in range(device.output_count):
                on = device.output_on(i)
                lamp = f"{'●' if on else '○'} {i + 1}"
                _put(screen, y, 11 + i * BUTTON_W, lamp, width, self._on if on else curses.A_DIM)
            y += 1

        # The LED blinks by the library's own pattern; the state is written next to it,
        # since a terminal redrawn 25 times a second shows a 100 ms flash only barely.
        lit = self.control.led
        _put(screen, y, 1, "status", width)
        lamp = f"{'●' if lit else '○'} {self.control.status or '—'}"
        _put(screen, y, 11, lamp, width, self._on if lit else curses.A_DIM)
        y += 1

        armed = self.control.faults_armed()
        if armed:
            _put(screen, y, 1, "faults   " + " · ".join(armed), width, curses.A_BOLD)
        y += 1

        help_y = height - 1
        log_top = y + 1
        room = max(help_y - log_top, 0)
        for i, (direction, line) in enumerate(list(self.log.lines)[-room:] if room else []):
            _put(screen, log_top + i, 0, line, width, self._tx if direction == "tx" else self._rx)

        if self._fault_menu:
            keys = (
                " fault:  r drop next response · b BUSY next request · s silent 10 s"
                " · g drop a segment · c clear · any other key: back"
            )
        elif self.control.inputs:
            keys = (
                " 1-9 click · ←→ pick · Enter click · d double · t triple · space hold"
                " · mouse: press & hold · s service · f faults · p power cut · q quit"
            )
        else:
            keys = " s service · f faults · p power cut · q quit"
        _put(screen, help_y, 0, keys, width, curses.A_REVERSE)
        screen.refresh()


def _put(screen: curses.window, y: int, x: int, text: str, width: int, attr: int = 0) -> None:
    """Write what fits; curses raises on the last cell of the screen, which is not an
    error worth stopping a device for."""
    if x >= width - 1 or y < 0:
        return
    with contextlib.suppress(curses.error):
        screen.addnstr(y, x, text, width - x - 1, attr)
