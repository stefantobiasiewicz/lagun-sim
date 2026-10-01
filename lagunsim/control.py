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
from typing import Final

from lagunsim.runner import Runner

CLICK_MS: Final = 80
"""How long a click holds the button: past the 15 ms bounce window with room to spare,
far below any hold threshold (the type allows 200 ms and up)."""

GAP_MS: Final = 80
"""Between the two presses of a double click — well inside a 250–350 ms window."""

SILENCE_MS: Final = 10_000
"""How long `silence()` keeps the device off the bus: past the hub's transaction retries
(three attempts, a few hundred ms), short of anything that would make it give up on the
node for good."""


class Control:
    def __init__(self, runner: Runner) -> None:
        self.runner = runner
        self._plan: list[tuple[int, int, Callable[[], None]]] = []
        self._order = itertools.count()
        self._held: set[int] = set()

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
        self.runner.step()

    def _at(self, when: int, action: Callable[[], None]) -> None:
        heapq.heappush(self._plan, (when, next(self._order), action))


def _due(when: int, now: int) -> bool:
    """`now >= when` on a u32 clock that may wrap."""
    return (now - when) & 0xFFFFFFFF < 0x80000000
