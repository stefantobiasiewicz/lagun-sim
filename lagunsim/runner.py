"""The device running: frames between a bus and the C node, a clock, a store on disk.

What a board's `app_main` loop does, with the hardware replaced:

    bus ──recv──▶ faults ──▶ node.rx          one turn = drain the bus, set the clock,
    bus ◀──send── faults ◀── node.take_tx     run the node's loop with the pins,
                                              send what it produced, save the store
                                              if the node wrote to it

The fault overlay sits between the node and the bus, as the node library meant it
(`lagunbus_node.faults`): the node has no idea, and every fault is visible on the bus.

Nothing here waits: `step()` is one turn, and whoever owns the loop — the headless
command or the terminal view — calls it every millisecond or two.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from typing import Literal

from lagunbus_node.faults import Faults, FaultyTransport
from lagunbus_node.frame import Frame
from lagunbus_node.interfaces import Transport
from lagunbus_node.sim import RESET_REASONS, SimDevice

from lagunsim.state import State

Direction = Literal["rx", "tx"]
FrameSeen = Callable[[Direction, Frame], None]


class Clock:
    """Milliseconds since start, as a board's `millis()`: u32, wrapping."""

    def __init__(self) -> None:
        self._start = time.monotonic()

    def __call__(self) -> int:
        return int((time.monotonic() - self._start) * 1000) & 0xFFFFFFFF


class Runner:
    def __init__(
        self,
        device: SimDevice,
        bus: Transport,
        *,
        clock: Callable[[], int],
        state: State | None = None,
        on_frame: FrameSeen | None = None,
        on_restart: Callable[[str], None] | None = None,
    ) -> None:
        self.on_restart = on_restart
        # A board restarts after FactoryReset (esp_restart); so does a simulated one.
        device.allow_restart()
        self.device = device
        self.clock = clock
        self.state = state
        self.on_frame = on_frame
        self.faulty = FaultyTransport(bus)
        self.faulty.attach(clock, lambda: self.device.address)
        self.pins = device.inputs_idle
        self._saved_version = -1

    def replace(self, device: SimDevice) -> None:
        """A new node in place of the old one — after a power cut. Call `start()`."""
        device.allow_restart()
        self.device = device
        self.pins = device.inputs_idle
        self._saved_version = -1

    def power_cycle(self, why: str = "power cut", reason: str = "POWER_ON") -> None:
        """A new node from the same identity and channels, and the store as the old one
        last saved it — RAM is gone, as on a board. Pins go back to idle: a finger on a
        button does not survive a reboot of the screen it is drawn on.

        `reason` is what `esp_reset_reason()` would say on the board — `LastResetReason`
        in the new node's `Booted`: POWER_ON for a pulled plug, SOFTWARE after the node's
        own restart."""
        old = self.device
        new = SimDevice(old.name, old.uid, jitter=secrets.randbits(32), channels=old.channels)
        new.set_reset_reason(RESET_REASONS[reason])
        old.close()
        self.replace(new)
        if self.on_restart is not None:
            self.on_restart(why)
        self.start()

    @property
    def faults(self) -> Faults:
        return self.faulty.faults

    # --- inputs ----------------------------------------------------------------------

    # The pins are LEVELS, as a board reads them; which level is a press is the board's
    # business (a key shorted to ground presses LOW). `inputs_idle` says the levels
    # with nothing pressed, so a press is "the other level" whatever the polarity.

    def press(self, index: int) -> None:
        """A finger on input `index`."""
        bit = 1 << index
        self.pins = self.pins & ~bit if self.device.inputs_idle & bit else self.pins | bit

    def release(self, index: int) -> None:
        bit = 1 << index
        self.pins = (self.pins & ~bit) | (self.device.inputs_idle & bit)

    def is_pressed(self, index: int) -> bool:
        return (self.pins ^ self.device.inputs_idle) & (1 << index) != 0

    # --- the loop --------------------------------------------------------------------

    def start(self) -> None:
        """Power on: load the store, start the node with the pins as they are."""
        if self.state is not None:
            for key, value in self.state.load_store().items():
                self.device.store_put(key, value)
            self._saved_version = self.device.store_version
        self.device.set_clock(self.clock())
        self.device.start(self.pins)
        self._flush()

    def step(self) -> None:
        """One turn of the board's loop."""
        while (frame := self.faulty.recv()) is not None:
            if self.on_frame is not None:
                self.on_frame("rx", frame)
            self.device.rx(frame)
        self.device.set_clock(self.clock())
        self.device.loop(self.pins)
        self._flush()
        if self.device.restart_requested:
            # The same restart after both; the stored address tells them apart.
            wiped = self.device.store_get("address") in (None, b"\x00\x00")
            what = "FactoryReset" if wiped else "Reboot"
            self.power_cycle(f"restart — {what}", reason="SOFTWARE")

    def _flush(self) -> None:
        for frame in self.device.take_tx():
            if self.on_frame is not None:
                self.on_frame("tx", frame)
            self.faulty.send(frame)
        self._save()

    def _save(self) -> None:
        if self.state is None or self.device.store_version == self._saved_version:
            return
        self.state.save_store(self.device.store_items())
        self._saved_version = self.device.store_version
