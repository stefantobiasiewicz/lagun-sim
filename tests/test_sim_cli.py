"""lagun-sim — its state, bus choice, the loop around the C node, and the command line.

UDP tests take a port of their own, so a simulator or hub running on the default port
during the tests neither hears them nor disturbs them."""

from __future__ import annotations

import json
import random
import time
from pathlib import Path

import pytest
from lagunbus_node.frame import Frame
from lagunbus_node.memory import QueueTransport
from lagunbus_node.ports.udp import DEFAULT_GROUP, DEFAULT_PORT, UdpTransport
from lagunbus_node.sim import SimDevice

from lagunsim import cli
from lagunsim.bus import parse_udp
from lagunsim.runner import Runner
from lagunsim.state import State, StateError
from lagunsim.traffic import concerns, describe

UID = bytes.fromhex("A1B2C3D400010203")
ADDRESS = b"\x42\x00"  # 0x042, as the node stores it


class Clock:
    def __init__(self) -> None:
        self.now = 0

    def __call__(self) -> int:
        return self.now


def rig(name: str, state: State | None = None) -> tuple[Runner, QueueTransport, Clock]:
    bus = QueueTransport()
    clock = Clock()
    device = SimDevice(name, state.uid if state else UID)
    if state is None:
        device.store_put("address", ADDRESS)
    runner = Runner(device, bus, clock=clock, state=state)
    runner.start()
    return runner, bus, clock


def run(runner: Runner, clock: Clock, until: int) -> None:
    while clock.now < until:
        clock.now += 1
        runner.step()


def text(frames: list[Frame]) -> list[str]:
    return [f"{f.can_id:08X}#{f.data.hex().upper()}" for f in frames]


# --- state ---------------------------------------------------------------------------


def test_the_uid_is_drawn_once_and_kept(tmp_path: Path) -> None:
    first = State.open("hall", "key", fresh=False, root=tmp_path)
    again = State.open("hall", "key", fresh=False, root=tmp_path)
    other = State.open("kitchen", "key", fresh=False, root=tmp_path)
    assert first.uid == again.uid
    assert first.uid != other.uid


def test_a_name_belongs_to_one_kind_of_device(tmp_path: Path) -> None:
    State.open("hall", "key", fresh=False, root=tmp_path)
    with pytest.raises(StateError, match="--name"):
        State.open("hall", "output8", fresh=False, root=tmp_path)


def test_fresh_is_gone_after_close() -> None:
    state = State.open("key", "key", fresh=True)
    assert state.directory.exists()
    state.close()
    assert not state.directory.exists()


def test_the_store_survives_a_restart_and_is_written_atomically(tmp_path: Path) -> None:
    """A power cut: the process ends, the next one finds what the node stored."""
    state = State.open("relays", "output2", fresh=False, root=tmp_path)
    state.save_store({"address": ADDRESS})  # as if the hub had assigned it earlier
    runner, bus, clock = rig("output2", state)
    bus.inbox.append(Frame(0x06108501, b"\x01"))  # On, endpoint 5 (RESTORE)
    run(runner, clock, 5)
    stored = json.loads((state.directory / "store.json").read_text(encoding="utf-8"))
    assert stored["output5"] == "01"
    assert not list(state.directory.glob("*.tmp"))

    after, _, _ = rig("output2", State.open("relays", "output2", fresh=False, root=tmp_path))
    assert after.device.output_on(1), "the relay came back on after the power cut"
    assert after.device.address == 0x042


# --- bus -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("udp", (DEFAULT_GROUP, DEFAULT_PORT)),
        ("udp:239.1.2.3", ("239.1.2.3", DEFAULT_PORT)),
        ("udp:239.1.2.3:9000", ("239.1.2.3", 9000)),
        ("vcan0", None),
    ],
)
def test_bus_spec(spec: str, expected: tuple[str, int] | None) -> None:
    assert parse_udp(spec) == expected


# --- the loop ------------------------------------------------------------------------


def test_a_press_on_the_second_key_is_an_event_from_endpoint_5() -> None:
    runner, bus, clock = rig("panel3")
    bus.take()  # Booted
    runner.press(1)
    assert runner.is_pressed(1) and not runner.is_pressed(0)
    run(runner, clock, 80)
    runner.release(1)
    run(runner, clock, 200)
    assert text(bus.take()) == ["02108503#0001"]  # Click x1


def test_press_twice_is_still_one_press() -> None:
    runner, _, _ = rig("key")
    idle = runner.pins
    runner.press(0)
    pressed = runner.pins
    runner.press(0)
    assert runner.pins == pressed != idle
    runner.release(0)
    assert runner.pins == idle


def test_a_fault_is_visible_on_the_bus_and_not_in_the_node() -> None:
    runner, bus, clock = rig("output")
    bus.take()
    runner.faults.busy_responses = 1
    bus.inbox.append(Frame(0x06108402, b"\x01"))  # Toggle
    run(runner, clock, 2)
    assert text(bus.take()) == ["0E108402#41"]  # BUSY, in the node's name
    assert not runner.device.output_on(0)


def test_the_device_meets_a_hub_over_udp() -> None:
    """Booted goes out on the UDP bus, a command comes back in, the relay switches."""
    port = random.randint(20_000, 60_000)
    hub = UdpTransport(DEFAULT_GROUP, port)
    node_side = UdpTransport(DEFAULT_GROUP, port)
    try:
        clock = Clock()
        device = SimDevice("output", UID)
        device.store_put("address", ADDRESS)
        runner = Runner(device, node_side, clock=clock)
        runner.start()
        time.sleep(0.02)
        assert Frame(0x02108001, bytes.fromhex("000000010000")) in list(iter(hub.recv, None))
        hub.send(Frame(0x06108401, b"\x03"))  # On
        time.sleep(0.02)
        run(runner, clock, 2)
        assert runner.device.output_on(0)
    finally:
        hub.close()
        node_side.close()


# --- traffic -------------------------------------------------------------------------


def test_describe_spells_out_what_a_frame_means() -> None:
    assert describe(Frame(0x0E108402, b"\x41")).startswith("RESPONSE      0x042 ep4  el 0x02")
    assert "BUSY" in describe(Frame(0x0E108402, b"\x41"))
    assert "result" not in describe(Frame(0x12108000, b"\x80\x01\xfa")), "a segment has no result"


def test_only_what_concerns_this_device_is_logged() -> None:
    assert concerns(Frame(0x06108402, b"\x01"), 0x042, 0)  # a command to us
    assert not concerns(Frame(0x0610C402, b"\x01"), 0x042, 0)  # to 0x043
    assert not concerns(Frame(0x02108503, b"\x00\x01"), 0x042, 0)  # an event, not a request


# --- the command line ----------------------------------------------------------------


def test_no_device_prints_the_catalogue(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "output8" in out and "lagun-hub run --udp" in out


def test_an_unknown_device_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["toaster", "--fresh", "--headless"]) == 2
    assert "no device 'toaster'" in capsys.readouterr().err


def test_a_size_the_device_does_not_come_in_leaves_no_name_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LAGUN_SIM_HOME", str(tmp_path))
    assert cli.main(["bell", "--channels", "2", "--headless"]) == 2
    assert "only as it is" in capsys.readouterr().err
    assert not (tmp_path / "bell").exists()
