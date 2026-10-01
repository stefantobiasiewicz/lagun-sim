"""`lagun-sim` — one process, one device.

    lagun-sim                     the catalogue of devices
    lagun-sim key                 a key on the UDP bus, remembered as "key"
    lagun-sim key --name hall     a second key, with an identity of its own
    lagun-sim output8 --fresh     eight relays straight out of the box, forgotten at exit
    lagun-sim output --channels 4 four relays — remembered with the name
    lagun-sim key --bus vcan0     on a SocketCAN interface instead (Linux)
    lagun-sim key --uid 0001020304050607 --name kitchen-switch
                                  a known identity, as a hub configuration names it

The hub on the other side: `lagun-hub run --udp --accept-new [--ephemeral]`.
"""

from __future__ import annotations

import argparse
import contextlib
import secrets
import signal
import sys
import threading
import time

from lagunbus_node.frame import Frame
from lagunbus_node.interfaces import Transport
from lagunbus_node.sim import BuildError, SimDevice, devices

from lagunsim import __version__
from lagunsim.bus import DEFAULT_BUS, close_bus, open_bus
from lagunsim.runner import Clock, Direction, Runner
from lagunsim.state import State, StateError
from lagunsim.traffic import concerns, describe

TURN_S = 0.001
"""One turn of the loop every millisecond — well inside a key's 15 ms bounce window,
and the node's deadlines (announce delay, multiclick window) stay millisecond-true."""


def _catalogue() -> str:
    try:
        rows = devices()
    except BuildError as exc:
        return f"the catalogue is not available — the C library did not build:\n{exc}"
    width = max(len(d.name) for d in rows)
    lines = [
        f"  {d.name:<{width}}  {d.about}"
        + (f" — --channels 1…{d.max_channels}" if d.max_channels else "")
        for d in rows
    ]
    return "devices:\n" + "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lagun-sim",
        description="A LagunBus device on a bus without hardware: the C node library, the "
        "same code as on a board. One process is one device.",
        epilog=_catalogue()
        + "\n\nthe hub on the other side:  lagun-hub run --udp --accept-new [--ephemeral]",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"lagun-sim {__version__}")
    parser.add_argument("device", nargs="?", help="a device from the list below")
    parser.add_argument(
        "--name",
        help="what this device is remembered as — its Uid and store (default: the device)",
    )
    parser.add_argument(
        "--bus",
        default=DEFAULT_BUS,
        help="udp[:GROUP[:PORT]] — the other end of `lagun-hub run --udp` (default) — "
        "or a SocketCAN interface such as vcan0",
    )
    parser.add_argument(
        "--uid",
        help="16 hex digits: a known identity instead of a drawn one, so that a hub "
        "configuration naming this Uid finds it (kept by the name from then on)",
    )
    parser.add_argument(
        "--channels",
        type=int,
        help="how many keys or relays, for a device that comes in sizes (key, output); "
        "remembered by the name from then on",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="a device out of the box: a temporary identity and store, removed at exit",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="no terminal view: print the traffic of this device and nothing else",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.device is None:
        parser.print_help()
        return 0

    uid: bytes | None = None
    if args.uid is not None:
        try:
            uid = bytes.fromhex(args.uid)
        except ValueError:
            uid = b""
        if len(uid) != 8:
            print(f"lagun-sim: --uid {args.uid!r} is not 16 hex digits", file=sys.stderr)
            return 2
    # Checked before the state is opened: a refused size must not leave a name behind.
    try:
        known = {d.name: d for d in devices()}
    except BuildError as exc:
        print(f"lagun-sim: {exc}", file=sys.stderr)
        return 2
    info = known.get(args.device)
    if info is None:
        print(f"lagun-sim: no device {args.device!r} ({', '.join(known)})", file=sys.stderr)
        return 2
    if args.channels is not None and not 1 <= args.channels <= info.max_channels:
        sizes = f"1 … {info.max_channels}" if info.max_channels else "only as it is"
        print(f"lagun-sim: {args.device!r} comes with {sizes} channels", file=sys.stderr)
        return 2
    try:
        state = State.open(
            args.name or args.device,
            args.device,
            fresh=args.fresh,
            uid=uid,
            channels=args.channels,
        )
    except StateError as exc:
        print(f"lagun-sim: {exc}", file=sys.stderr)
        return 2
    try:
        return _run(args, state)
    finally:
        state.close()


def _run(args: argparse.Namespace, state: State) -> int:
    try:
        # A random announce delay, as a board draws one — or forty devices back from a
        # power cut announce in the same millisecond (03-adresacja.md §5).
        device = SimDevice(
            args.device, state.uid, jitter=secrets.randbits(32), channels=state.channels
        )
    except (ValueError, BuildError) as exc:
        print(f"lagun-sim: {exc}", file=sys.stderr)
        return 2
    try:
        bus = open_bus(args.bus)
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"lagun-sim: cannot open the bus {args.bus!r}: {exc}", file=sys.stderr)
        return 2
    try:
        if args.headless:
            return _headless(device, bus, state, args)
        return _view(device, bus, state, args)
    finally:
        close_bus(bus)
        device.close()


def _headless(device: SimDevice, bus: Transport, state: State, args: argparse.Namespace) -> int:
    clock = Clock()
    started = time.monotonic()

    def seen(direction: Direction, frame: Frame) -> None:
        current = runner.device  # a new one after every restart
        if direction == "rx" and not concerns(frame, current.address, current.short_id):
            return
        arrow = "→" if direction == "tx" else "←"
        print(f"[{time.monotonic() - started:8.3f}] {arrow} {describe(frame)}", flush=True)

    runner = Runner(
        device,
        bus,
        clock=clock,
        state=state,
        on_frame=seen,
        on_restart=lambda why: print(f"  {why} — booting again from the store", flush=True),
    )
    where = "temporary, removed at exit" if state.temporary else str(state.directory)
    print(f"{args.device} as {args.name or args.device!r} on {args.bus}", flush=True)
    print(f"  Uid {state.uid.hex()}  ShortId 0x{device.short_id:08X}  state: {where}")
    print(f"  inputs {device.input_count}, outputs {device.output_count}", flush=True)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    runner.start()
    address = runner.device.address
    print(f"  address {_address(address)}", flush=True)
    while not stop.is_set():
        runner.step()
        if runner.device.address != address:
            address = runner.device.address
            print(f"  address {_address(address)}", flush=True)
        time.sleep(TURN_S)
    print("stopped — the store holds what the node stored, as after a power cut")
    return 0


def _view(device: SimDevice, bus: Transport, state: State, args: argparse.Namespace) -> int:
    from lagunsim.control import Control
    from lagunsim.view import Header, Log, View, frame_logger

    runner = Runner(device, bus, clock=Clock(), state=state)
    control = Control(runner)
    log = Log()
    runner.on_frame = frame_logger(control, log)
    runner.on_restart = lambda why: log.note(f"{why} — booting again from the store")
    header = Header(
        device=args.device,
        name=args.name or args.device,
        bus=args.bus,
        uid=state.uid,
        state="fresh" if state.temporary else "remembered",
    )
    with contextlib.suppress(KeyboardInterrupt):
        View(control, header, log).run()
    control.runner.device.close()
    return 0


def _address(address: int) -> str:
    return f"0x{address:03X}" if address else "none — announcing; the hub must be accepting"
