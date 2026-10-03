"""What a person does to a device, without a terminal: click, double click, hold, the
plug — and a known identity for a hub configuration to find."""

from __future__ import annotations

from pathlib import Path

import pytest
from lagunbus_node.frame import Frame
from lagunbus_node.memory import QueueTransport
from lagunbus_node.sim import SimDevice

from lagunsim.control import CLICK_MS, Control
from lagunsim.runner import Runner
from lagunsim.state import State, StateError

UID = bytes.fromhex("0001020304050607")


class Clock:
    def __init__(self) -> None:
        self.now = 0

    def __call__(self) -> int:
        return self.now


def rig(name: str, state: State) -> tuple[Control, QueueTransport, Clock]:
    state.save_store({**state.load_store(), "address": b"\x42\x00"})
    bus, clock = QueueTransport(), Clock()
    runner = Runner(SimDevice(name, state.uid), bus, clock=clock, state=state)
    control = Control(runner)
    runner.start()
    bus.take()  # Booted
    return control, bus, clock


def run(control: Control, clock: Clock, ms: int) -> None:
    end = clock.now + ms
    while clock.now < end:
        clock.now += 1
        control.tick()


def sent(bus: QueueTransport) -> list[str]:
    return [f"{f.can_id:08X}#{f.data.hex().upper()}" for f in bus.take()]


@pytest.fixture
def state(tmp_path: Path) -> State:
    return State.open("dev", "key", fresh=False, root=tmp_path, uid=UID)


def test_a_click_is_one_click(state: State) -> None:
    control, bus, clock = rig("key", state)
    control.click(0)
    run(control, clock, CLICK_MS + 100)
    assert sent(bus) == ["02108403#0001"]


def test_a_double_click_in_click_mode_is_two_clicks(state: State) -> None:
    """The key starts in CLICK mode, where every release is a click of its own."""
    control, bus, clock = rig("key", state)
    control.click(0, times=2)
    run(control, clock, 600)
    assert sent(bus) == ["02108403#0001", "02108403#0101"]


def test_space_holds_until_let_go(state: State) -> None:
    control, bus, clock = rig("key", state)  # `key` holds from the start (key.json)
    control.toggle_hold(0)
    assert control.is_held(0) and control.is_pressed(0)
    run(control, clock, 600)
    assert "02108405#00" in sent(bus), "HoldStart, the first event of the endpoint"
    control.toggle_hold(0)
    assert not control.is_pressed(0)
    run(control, clock, 50)
    assert any(line.startswith("02108407#") for line in sent(bus)), "HoldEnd"


def test_a_click_on_a_held_key_does_nothing(state: State) -> None:
    control, _, _ = rig("key", state)
    control.toggle_hold(0)
    control.click(0)
    assert control.is_held(0)


def test_a_power_cut_keeps_what_the_node_stored(tmp_path: Path) -> None:
    state = State.open("relay", "output2", fresh=False, root=tmp_path, uid=UID)
    control, bus, clock = rig("output2", state)
    bus.inbox.append(Frame(0x06108501, b"\x01"))  # On, endpoint 5 (RESTORE)
    run(control, clock, 5)
    control.power_cycle()
    booted = sent(bus)[-1]
    assert booted.startswith("02108001#"), "Booted again, at the same address"
    assert control.runner.device.output_on(1)
    assert not control.runner.device.output_on(0)


def test_a_known_uid_is_kept_and_a_different_one_refused(tmp_path: Path) -> None:
    first = State.open("kitchen", "key", fresh=False, root=tmp_path, uid=UID)
    assert first.uid == UID
    assert State.open("kitchen", "key", fresh=False, root=tmp_path).uid == UID
    with pytest.raises(StateError, match="already is Uid"):
        State.open("kitchen", "key", fresh=False, root=tmp_path, uid=bytes(8))


def test_channels_are_remembered_by_the_name(tmp_path: Path) -> None:
    State.open("board", "output", fresh=False, root=tmp_path, channels=4)
    assert State.open("board", "output", fresh=False, root=tmp_path).channels == 4
    with pytest.raises(StateError, match="'output 4', not a 'output 2'"):
        State.open("board", "output", fresh=False, root=tmp_path, channels=2)
    # One channel is the default board: the same name opens without --channels.
    State.open("single", "output", fresh=False, root=tmp_path, channels=1)
    assert State.open("single", "output", fresh=False, root=tmp_path).channels == 0


def test_a_power_cut_keeps_the_channels(tmp_path: Path) -> None:
    state = State.open("panel", "key", fresh=False, root=tmp_path, uid=UID, channels=3)
    state.save_store({"address": b"\x42\x00"})
    bus, clock = QueueTransport(), Clock()
    runner = Runner(SimDevice("key", UID, channels=3), bus, clock=clock, state=state)
    control = Control(runner)
    runner.start()
    control.power_cycle()
    assert control.runner.device.input_count == 3


def test_a_factory_reset_from_the_hub_reboots_the_device(tmp_path: Path) -> None:
    """As a board with esp_restart(): acknowledged, 50 ms later the address is stored as
    none, the node restarts — a new boot from the store, without an address, announcing."""
    state = State.open("relay", "output", fresh=False, root=tmp_path, uid=UID)
    control, bus, clock = rig("output", state)
    restarts: list[str] = []
    control.runner.on_restart = restarts.append
    old = control.runner.device
    bus.inbox.append(Frame(0x06108002, bytes.fromhex("034E47414C")))  # FactoryReset, LAGN
    run(control, clock, 60)
    assert sent(bus)[0] == "0E108002#03", "acknowledged from the old address"
    assert restarts == ["restart — FactoryReset"]
    assert control.runner.device is not old
    assert control.runner.device.address == 0
    assert state.load_store()["address"] == b"\x00\x00"
    run(control, clock, 2100)
    assert any(line.startswith("1F") for line in sent(bus)), "it announces, as a new device"


# --- the service button and the status LED (zadania/zrobione/CR-05.md) ----------------------------


def test_the_service_button_resets_the_device_as_the_hub_would(tmp_path: Path) -> None:
    """`s` in the view: the button held past its threshold and let go. RESETTING at once,
    50 ms later the same restart as after FactoryReset from the hub."""
    state = State.open("relay", "output", fresh=False, root=tmp_path, uid=UID)
    control, bus, clock = rig("output", state)
    restarts: list[str] = []
    control.runner.on_restart = restarts.append
    control.service_button()
    assert control.status == "RESETTING"
    assert sent(bus) == [], "the button puts nothing on the bus"
    run(control, clock, 60)
    assert restarts == ["restart — FactoryReset"]
    assert control.runner.device.address == 0
    assert control.status == "NO_ADDRESS"


def test_a_reboot_keeps_the_address_and_comes_back_as_software(tmp_path: Path) -> None:
    state = State.open("relay", "output", fresh=False, root=tmp_path, uid=UID)
    control, bus, clock = rig("output", state)
    restarts: list[str] = []
    control.runner.on_restart = restarts.append
    bus.inbox.append(Frame(0x06108001, bytes.fromhex("013200")))  # Reboot, 50 ms
    run(control, clock, 60)
    lines = sent(bus)
    assert lines[0] == "0E108001#01"
    assert restarts == ["restart — Reboot"]
    assert control.runner.device.address == 0x042
    assert "02108001#000300010000" in lines, "Booted with LastResetReason SOFTWARE"


def test_a_power_cut_comes_back_as_power_on(state: State) -> None:
    control, bus, _ = rig("key", state)
    control.power_cycle()
    assert sent(bus) == ["02108001#000000010000"]


def test_the_led_shows_the_state_with_the_librarys_pattern(state: State) -> None:
    """READY is lit, with a dark blip every 3 s (lagunbus/tools/led.h)."""
    control, _, clock = rig("key", state)
    run(control, clock, 10)
    assert control.status == "READY" and control.led
    run(control, clock, 2900)
    assert not control.led


def faulty_relay(tmp_path: Path) -> tuple[Control, QueueTransport, Clock]:
    return rig("output", State.open("relay", "output", fresh=False, root=tmp_path, uid=UID))


TOGGLE = Frame(0x06108402, b"\x01")


def test_busy_refuses_the_next_request_in_the_nodes_name(tmp_path: Path) -> None:
    control, bus, clock = faulty_relay(tmp_path)
    control.busy()
    assert control.faults_armed() == ["BUSY ×1"]
    bus.inbox.append(TOGGLE)
    run(control, clock, 2)
    assert sent(bus) == ["0E108402#41"]
    assert not control.runner.device.output_on(0)
    assert control.faults_armed() == [], "used up"


def test_a_dropped_response_still_switches_the_relay(tmp_path: Path) -> None:
    control, bus, clock = faulty_relay(tmp_path)
    control.drop_response()
    bus.inbox.append(TOGGLE)
    run(control, clock, 2)
    assert sent(bus) == ["10108400#01"], "the report goes, the response does not"
    assert control.runner.device.output_on(0)


def test_silence_lasts_and_then_ends(tmp_path: Path) -> None:
    control, bus, clock = faulty_relay(tmp_path)
    control.silence(1000)
    assert control.faults_armed() == ["silent 1 s"]
    bus.inbox.append(TOGGLE)
    run(control, clock, 5)
    assert sent(bus) == []
    run(control, clock, 1000)
    assert control.faults_armed() == []
    bus.inbox.append(TOGGLE)
    run(control, clock, 2)
    assert sent(bus)[-1] == "0E108402#01"


def test_a_dropped_segment_leaves_a_hole(tmp_path: Path) -> None:
    control, bus, clock = faulty_relay(tmp_path)
    control.drop_segment()
    bus.inbox.append(Frame(0x0C108000, b"\x03"))  # READ Uid — a long response
    run(control, clock, 2)
    frames = sent(bus)
    assert frames[0] == "0E108000#5B", "SEGMENTED announced"
    assert len(frames) == 2, "one of the two segments lost"


def test_clear_disarms_everything(tmp_path: Path) -> None:
    control, _, _ = faulty_relay(tmp_path)
    control.busy()
    control.drop_response()
    control.silence()
    control.drop_segment()
    assert len(control.faults_armed()) == 4
    control.clear_faults()
    assert control.faults_armed() == []
