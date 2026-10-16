"""What a person does to a device — press, click, hold, pull the plug — kept apart from
the terminal so that it can be tested without one.

A terminal reports a key being typed, never a key being released (zadania/zrobione/FEAT-02.md,
F5), so a finger on a button is spelled out in time here: a click is a press held for
`CLICK_MS` and a release; a hold is a press that stays until it is let go. The mouse is
the one input that does report both edges, and the view passes them straight to
`press` / `release`.
"""

from __future__ import annotations

import heapq
import itertools
from collections.abc import Callable
from typing import ClassVar, Final, NamedTuple

from lagunsim.runner import Runner


class Light(NamedTuple):
    """One light for the view: the colour to paint, 0–254 a channel, and its line."""

    shown: tuple[int, int, int]
    text: str


CLICK_MS: Final = 80
"""How long a click holds the button: past the 15 ms bounce window with room to spare,
far below any hold threshold (the type allows 200 ms and up)."""

GAP_MS: Final = 80
"""Between the two presses of a double click — well inside a 250–350 ms window."""

SILENCE_MS: Final = 10_000
"""How long `silence()` keeps the device off the bus: past the hub's transaction retries
(three attempts, a few hundred ms), short of anything that would make it give up on the
node for good."""


_QUANTITIES: Final = {
    0x00: "TEMPERATURE",
    0x01: "HUMIDITY",
    0x02: "ILLUMINANCE",
    0x03: "CO2",
    0x04: "VOC",
    0x05: "PM25",
    0x06: "PRESSURE",
    0x07: "FLOW",
    0x08: "SOIL_MOISTURE",
    0x09: "VOLTAGE",
    0x0A: "CURRENT",
    0x0B: "POWER",
    0x0C: "DISTANCE",
}
"""`Quantity` by name (types/0x0030-sense.md §2.3) — for a label only."""


class Reading(NamedTuple):
    """How a person turns a quantity's knob: decimals and unit for the label, the step of
    `+`/`−`, where a fresh sensor starts and the range it stays in — all in the units on
    the wire (`registry/quantities.yaml`)."""

    decimals: int
    unit: str
    step: int
    start: int
    lo: int
    hi: int
    percent: int = 0
    """A step of this much of the reading when it is more than `step` — for light, which
    spans orders of magnitude (DEVICE-01 N8)."""


_READINGS: Final = {
    0x00: Reading(2, "°C", 50, 2150, -4000, 8500),  # a room; the SHT40's range
    0x01: Reading(2, "%", 200, 4500, 0, 10000),
    0x02: Reading(2, "lx", 100, 30000, 0, 12000000, percent=10),  # the VEML7700's range
}
_ANY: Final = Reading(0, "", 1, 0, -(2**31), 2**31 - 1)
"""A quantity no simulated sensor measures yet — raw numbers, step 1."""


class Control:
    def __init__(self, runner: Runner) -> None:
        self.runner = runner
        self._plan: list[tuple[int, int, Callable[[], None]]] = []
        self._order = itertools.count()
        self._held: set[int] = set()
        self._readings: dict[int, int] = {}
        """What each sensor measures, by endpoint — the room, kept by the person, not by
        the node: it survives a power cut, as the temperature does."""
        self._failed: set[int] = set()
        self._fed: object = None
        """The node the readings were last handed to; a new one after a power cut gets
        them again at its first turn."""

    @property
    def inputs(self) -> int:
        return self.runner.device.input_count

    def is_pressed(self, index: int) -> bool:
        return self.runner.is_pressed(index)

    def is_held(self, index: int) -> bool:
        return index in self._held

    # --- a finger --------------------------------------------------------------------

    def press(self, index: int) -> None:
        if 0 <= index < self.inputs:
            self.runner.press(index)

    def release(self, index: int) -> None:
        if 0 <= index < self.inputs:
            self._held.discard(index)
            self.runner.release(index)

    def click(self, index: int, times: int = 1) -> None:
        """`times` presses of `CLICK_MS`, `GAP_MS` apart, starting now."""
        if not 0 <= index < self.inputs or self.is_held(index):
            return
        now = self.runner.clock()
        for n in range(times):
            start = now + n * (CLICK_MS + GAP_MS)
            self._at(start, lambda: self.runner.press(index))
            self._at(start + CLICK_MS, lambda: self.runner.release(index))

    def toggle_hold(self, index: int) -> None:
        """Press and keep it pressed — or let go of a held one."""
        if not 0 <= index < self.inputs:
            return
        if self.is_held(index):
            self.release(index)
        else:
            self._held.add(index)
            self.runner.press(index)

    # --- the plug --------------------------------------------------------------------

    def power_cycle(self) -> None:
        """Pull the plug and put it back (`Runner.power_cycle`); clicks in progress and
        held buttons go with it."""
        self._plan.clear()
        self._held.clear()
        self.runner.power_cycle()

    # --- the service button and the status LED (zadania/zrobione/CR-05.md) ------------------------

    def service_button(self) -> None:
        """The service button held past its threshold and let go — a factory reset by the
        device itself. The hold is the board's (`lagunbus/tools/service.h`, tested in C);
        here one key stands for all of it."""
        self.runner.device.service_press()

    @property
    def screen(self) -> list[str]:
        """What each display field shows (types/0x0022-display.md), one line a field:
        `ep4  2137 TEMPERATURE`, `ep5  "Pranie gotowe"`, `ep6  --`. The number is the raw
        one: its scale and unit are the quantity's, and drawing them belongs to the device
        that has them printed on its glass.

        A device with such a glass — the e-paper `epd` — shows what the glass shows, in its
        places, with the unit printed beside them (`ep4   21.4 °C`), and a line of its own
        while a change waits for the next refresh (zadania/zrobione/DEVICE-04.md, N9) — on
        a screen paced with a gap; `epd` refreshes at every write since 2026-10-14. The
        places and the pacing are the library's, the board's own."""
        lines = []
        device = self.runner.device
        for field in device.display_fields:
            if field.glass is not None:
                unit = _READINGS.get(field.quantity or 0, _ANY).unit
                what = f"{field.glass} {unit}".rstrip()
            elif field.content == "VALUE" and field.value is not None:
                what = f"{field.value} {_QUANTITIES.get(field.quantity or 0, field.quantity)}"
            elif field.content == "TEXT":
                what = f'"{field.text}"'
            else:
                what = "--"  # nothing to show — never a zero (§5.2)
            lines.append(f"ep{field.endpoint}  {what}")
        if device.display_pending:
            lines.append("     the glass waits for its refresh")
        return lines

    # --- sensors (types/0x0030-sense.md) ---------------------------------------------
    #
    # A board's application reads its sensor and hands the result to the library; here a
    # person sets what the sensor reads. When a report goes out — hysteresis, the cycle —
    # is the library's business, so `+` by less than the hysteresis is quiet on the bus,
    # as a real sensor's noise is.

    @property
    def sensor_count(self) -> int:
        return len(self.runner.device.sensors)

    def sense_step(self, index: int, direction: int) -> None:
        """`+` or `−` on sensor `index`: one step of its quantity, within its range."""
        sensors = self.runner.device.sensors
        if not 0 <= index < len(sensors):
            return
        sensor = sensors[index]
        r = _READINGS.get(sensor.quantity, _ANY)
        now = self._reading(sensor.endpoint, sensor.quantity)
        step = max(r.step, abs(now) * r.percent // 100)
        self._readings[sensor.endpoint] = max(r.lo, min(r.hi, now + direction * step))
        self._feed()

    def sense_fail(self, index: int) -> None:
        """The sensor stops answering — or answers again, with the reading it had."""
        sensors = self.runner.device.sensors
        if not 0 <= index < len(sensors):
            return
        self._failed ^= {sensors[index].endpoint}
        self._feed()

    @property
    def sensors(self) -> list[str]:
        """What each sensor reads, one line a sensor: `ep4  21.50 °C`, `ep5  -- failed`."""
        lines = []
        for sensor in self.runner.device.sensors:
            if sensor.endpoint in self._failed:
                what = "-- failed"
            else:
                r = _READINGS.get(sensor.quantity, _ANY)
                value = self._reading(sensor.endpoint, sensor.quantity)
                what = f"{value / 10**r.decimals:.{r.decimals}f} {r.unit}".rstrip()
            lines.append(f"ep{sensor.endpoint}  {what}")
        return lines

    def _reading(self, endpoint: int, quantity: int) -> int:
        return self._readings.setdefault(endpoint, _READINGS.get(quantity, _ANY).start)

    def _feed(self) -> None:
        """Hand every reading to the node — what a board's loop does after measuring."""
        device = self.runner.device
        for sensor in device.sensors:
            failed = sensor.endpoint in self._failed
            raw = None if failed else self._reading(sensor.endpoint, sensor.quantity)
            device.sense(sensor.endpoint, raw)
        self._fed = device

    # --- knobs (types/0x0011-value-input.md) -------------------------------------------
    #
    # A board's interrupt counts detents and its loop hands the count to the library; here
    # a key press is one detent. What the value does with it — the step, the limit, the
    # wrap, the event and the report — is the library's (zadania/zrobione/DEVICE-02.md N9).

    BAR: ClassVar[int] = 20
    """Cells of the bar — a twentieth of the range each."""

    @property
    def dial_count(self) -> int:
        return len(self.runner.device.dials)

    def turn(self, detents: int, index: int = 0) -> None:
        """Turn knob `index` by `detents`, negative down — handed in at the next loop."""
        dials = self.runner.device.dials
        if 0 <= index < len(dials):
            self.runner.device.turn(dials[index].endpoint, detents)

    @property
    def dials(self) -> list[str]:
        """Each knob's value as a bar over its range: `ep4  ████░░…  420 ‰`."""
        lines = []
        for dial in self.runner.device.dials:
            span = max(dial.max_value - dial.min_value, 1)
            filled = round((dial.value - dial.min_value) * self.BAR / span)
            bar = "█" * filled + "░" * (self.BAR - filled)
            lines.append(f"ep{dial.endpoint}  {bar}  {dial.value} ‰")
        return lines

    @property
    def lights(self) -> list[Light]:
        """Each light as it shines (zadania/devices/DEVICE-03.md N8): the colour the eye
        would see — White added to R, G and B as a board without a white LED does it,
        times `Level` — and a line `62.5 %  #FE8000  fading`. The colour is the INTENT
        the library handed over: a board's brightness curve and current cap are its own
        and would only darken the picture here. Only lights; a relay stays a lamp ●."""
        device = self.runner.device
        lights = []
        for i in range(device.output_count):
            lit = device.light(i)
            if lit is None:
                continue
            level, *rgbw = lit
            white = rgbw[3]
            shown = tuple(min(254, c + white) * level // 1000 for c in rgbw[:3])
            text = f"{level / 10:g} %  #" + "".join(f"{c:02X}" for c in rgbw[:3])
            if white:
                text += f" W {white}"
            if device.light_fading(i):
                text += "  fading"
            lights.append(Light(shown, text))  # type: ignore[arg-type]
        return lights

    @property
    def status(self) -> str | None:
        """The state the node last reported for its LED — `"READY"`, `"NO_ADDRESS"`, …"""
        return self.runner.device.state

    @property
    def led(self) -> bool:
        """Whether the status LED is lit right now, by the library's pattern."""
        return self.runner.device.led

    # --- faults --------------------------------------------------------------------------
    #
    # The overlay of the node library (`lagunbus_node.faults`): between the node and the
    # bus, invisible to the node, every one visible on the bus. Each call arms ONE more —
    # a person presses the key again for two.

    def drop_response(self) -> None:
        """Lose the next RESPONSE after the command was carried out — the hard case for
        the hub: it happened, and the hub does not know."""
        self.runner.faults.drop_responses += 1

    def busy(self) -> None:
        """Answer the next request `BUSY` in the node's name; it never reaches the node."""
        self.runner.faults.busy_responses += 1

    def silence(self, ms: int = SILENCE_MS) -> None:
        """Nothing the node sends reaches the bus for `ms`."""
        self.runner.faults.silent_until = (self.runner.clock() + ms) & 0xFFFFFFFF

    def drop_segment(self) -> None:
        """Lose the first segment of the next long response — a hole the hub has to
        notice and throw the value away for (05-mapowanie-can.md §6.6)."""
        self.runner.faults.drop_segment = 0

    def clear_faults(self) -> None:
        f = self.runner.faults
        f.drop_responses = f.busy_responses = f.silent_until = 0
        f.drop_segment = None

    def faults_armed(self) -> list[str]:
        """What is waiting to go wrong, in words — for the screen."""
        f = self.runner.faults
        left = (f.silent_until - self.runner.clock()) & 0xFFFFFFFF
        silent = 0 < left < 0x80000000
        return [
            text
            for text, on in (
                (f"drop response ×{f.drop_responses}", f.drop_responses > 0),
                (f"BUSY ×{f.busy_responses}", f.busy_responses > 0),
                (f"silent {left / 1000:.0f} s", silent),
                (f"drop segment {f.drop_segment}", f.drop_segment is not None),
            )
            if on
        ]

    # --- time --------------------------------------------------------------------------

    def tick(self) -> None:
        """Carry out what is due, then one turn of the device's loop."""
        now = self.runner.clock()
        while self._plan and _due(self._plan[0][0], now):
            _, _, action = heapq.heappop(self._plan)
            action()
        if self._fed is not self.runner.device and self.runner.device.sensors:
            self._feed()  # a node just powered on measures at once
        self.runner.step()

    def _at(self, when: int, action: Callable[[], None]) -> None:
        heapq.heappush(self._plan, (when, next(self._order), action))


def _due(when: int, now: int) -> bool:
    """`now >= when` on a u32 clock that may wrap."""
    return (now - when) & 0xFFFFFFFF < 0x80000000
