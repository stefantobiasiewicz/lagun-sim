"""Opening the bus a device sits on.

`udp` — the default — is UDP multicast on this machine: the other end of
`lagun-hub run --udp`, and the only choice on macOS. Anything else is a SocketCAN
interface (`vcan0`, `can0`), Linux only. The transports themselves are in the node
library (`lagunbus_node.ports`); the datagram format is described there.
"""

from __future__ import annotations

from lagunbus_node.interfaces import Transport
from lagunbus_node.ports.socketcan import SocketCanTransport
from lagunbus_node.ports.udp import DEFAULT_GROUP, DEFAULT_PORT, UdpTransport

DEFAULT_BUS = "udp"


def parse_udp(spec: str) -> tuple[str, int] | None:
    """`udp`, `udp:GROUP` or `udp:GROUP:PORT` → `(group, port)`; `None` if not UDP."""
    if spec != "udp" and not spec.startswith("udp:"):
        return None
    _, _, rest = spec.partition(":")
    group, _, port = rest.partition(":")
    try:
        number = int(port) if port else DEFAULT_PORT
    except ValueError:
        raise ValueError(f"{spec!r}: the port is not a number") from None
    return group or DEFAULT_GROUP, number


def open_bus(spec: str) -> Transport:
    """Raises `ValueError`, `OSError` or `RuntimeError` with a message fit for a person."""
    udp = parse_udp(spec)
    if udp is not None:
        return UdpTransport(*udp)
    return SocketCanTransport(spec)


def close_bus(bus: Transport) -> None:
    close = getattr(bus, "close", None)
    if close is not None:
        close()
