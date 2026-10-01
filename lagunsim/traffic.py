"""Frames in words, for the traffic log — through the node library's own frame codec.

Same reasoning as `lagun-node --sniff`: bytes are what `candump` shows; this shows what
the frame MEANS, and it reads it with the codec the node itself uses.
"""

from __future__ import annotations

from lagunbus_node.frame import (
    ADDRESS_BROADCAST,
    Frame,
    decode_header,
    decode_id,
    is_join,
    join_is_announce,
    join_short_id,
)
from lagunbus_node.registry import MessageKind, ResultCode

_REQUESTS = frozenset({MessageKind.READ, MessageKind.WRITE, MessageKind.COMMAND})


def describe(frame: Frame) -> str:
    """One line: kind, address, endpoint, element, and the bytes — the result code
    spelled out for a response."""
    data = " ".join(f"{b:02X}" for b in frame.data)
    if is_join(frame.can_id):
        what = "ANNOUNCE" if join_is_announce(frame.can_id) else "ASSIGN  "
        return f"JOIN     {what} ShortId …{join_short_id(frame.can_id):06X}  [{data}]"
    f = decode_id(frame.can_id)
    try:
        kind = MessageKind(f.kind).name
    except ValueError:
        kind = f"kind {f.kind:X}"
    text = f"{kind:<13} 0x{f.address:03X} ep{f.endpoint:<2} el 0x{f.element:02X}"
    # Only a RESPONSE opens with the Tid/result header; a LONG_RESPONSE segment opens
    # with a segment header of its own (05-mapowanie-can.md §6), not a result code.
    if f.kind == MessageKind.RESPONSE and frame.data:
        tid, result = decode_header(frame.data[0])
        try:
            name = ResultCode(result).name
        except ValueError:
            name = f"result {result}"
        text += f"  tid {tid} {name}"
    return f"{text}  [{data}]"


def concerns(frame: Frame, address: int, short_id: int) -> bool:
    """Is a RECEIVED frame about this device? On a bus a node hears everything —
    other nodes' events, the hub talking to others — and a log of all of it buries the
    one line that matters. Kept: requests to this address or to everyone, and joining
    frames for this ShortId."""
    if is_join(frame.can_id):
        return join_short_id(frame.can_id) == short_id & 0xFFFFFF
    f = decode_id(frame.can_id)
    return f.kind in _REQUESTS and f.address in (address, ADDRESS_BROADCAST)
