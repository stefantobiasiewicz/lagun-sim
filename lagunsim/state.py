"""What a device keeps between runs — its identity and its persistent store.

A board has an eFuse Uid and NVS; a simulated device has a directory. One per device
NAME, so that `lagun-sim key` twice is the same key coming back after a power cut, and
`lagun-sim key --name hall` is a second key with an identity of its own
(zadania/zrobione/FEAT-02.md, N4):

    ~/.lagun-sim/<name>/
        device      which catalogue device lives here, and in how many channels
                    ("output 4"; just "output" for the default)
        uid         16 hex digits, drawn at the first start
        store.json  the node's persistent store, {key: hex} — address, boot counter,
                    output states…

Stopping the process is a power cut: the store holds what the node itself stored, not
what it had in RAM. `--fresh` puts the directory in a temporary place, removed at exit —
a device straight out of the box, every time.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import tempfile
from pathlib import Path
from typing import Final

HOME_ENV: Final = "LAGUN_SIM_HOME"


class StateError(RuntimeError):
    """The state directory cannot be used — the message says why and what to do."""


def default_root() -> Path:
    return Path(os.environ.get(HOME_ENV) or Path.home() / ".lagun-sim")


class State:
    def __init__(
        self,
        directory: Path,
        device: str,
        *,
        temporary: bool = False,
        uid: bytes | None = None,
        channels: int | None = None,
    ) -> None:
        self.directory = directory
        self.device = device
        self.temporary = temporary
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StateError(f"cannot create {directory}: {exc}") from None
        self.channels = self._check_device(channels)
        """The channel count this name was made with; 0 for the device's default."""
        self.uid = self._load_uid(uid)

    @classmethod
    def open(
        cls,
        name: str,
        device: str,
        *,
        fresh: bool,
        root: Path | None = None,
        uid: bytes | None = None,
        channels: int | None = None,
    ) -> State:
        """`uid` gives the device a known identity instead of a drawn one — so that a
        hub configuration naming it by Uid (bindings, attributes) finds it. `channels`
        `None` takes what the name was made with."""
        if fresh:
            directory = Path(tempfile.mkdtemp(prefix=f"lagun-sim-{name}-"))
            return cls(directory, device, temporary=True, uid=uid, channels=channels)
        return cls((root or default_root()) / name, device, uid=uid, channels=channels)

    def close(self) -> None:
        """Remove the directory if it was temporary; a named one stays."""
        if self.temporary:
            shutil.rmtree(self.directory, ignore_errors=True)

    # --- identity --------------------------------------------------------------------

    def _check_device(self, wanted: int | None) -> int:
        """A store written by `output8` loaded into a `key` would be nonsense that
        looks like data, and a relay board that comes back with four channels instead of
        one is, to the hub, the same Uid with a different set of endpoints. The name is
        refused rather than the store reinterpreted. Returns the channel count."""
        marker = self.directory / "device"
        asked = _spell(self.device, wanted or 0)
        if not marker.exists():
            marker.write_text(asked + "\n", encoding="utf-8")
            return wanted or 0
        found = marker.read_text(encoding="utf-8").strip()
        device, _, count = found.partition(" ")
        channels = int(count) if count.isdigit() else 0
        if device != self.device or (wanted is not None and _spell(device, wanted) != found):
            raise StateError(
                f"{self.directory} belongs to a {found!r}, not a {asked!r} — "
                f"pick another name with --name, or remove the directory"
            )
        return channels

    def _load_uid(self, wanted: bytes | None) -> bytes:
        file_ = self.directory / "uid"
        if file_.exists():
            try:
                uid = bytes.fromhex(file_.read_text(encoding="utf-8").strip())
            except ValueError:
                uid = b""
            if len(uid) != 8:
                raise StateError(f"{file_} does not hold a Uid (16 hex digits)")
            if wanted is not None and wanted != uid:
                # A Uid is burnt into a board once; a name that already has one keeps it.
                raise StateError(
                    f"{self.directory.name!r} already is Uid {uid.hex()} — pick another "
                    f"name with --name, or remove {self.directory}"
                )
            return uid
        uid = wanted if wanted is not None else secrets.token_bytes(8)
        file_.write_text(uid.hex() + "\n", encoding="utf-8")
        return uid

    # --- the store -------------------------------------------------------------------

    @property
    def _store_file(self) -> Path:
        return self.directory / "store.json"

    def load_store(self) -> dict[str, bytes]:
        if not self._store_file.exists():
            return {}
        try:
            raw = json.loads(self._store_file.read_text(encoding="utf-8"))
            return {str(key): bytes.fromhex(value) for key, value in raw.items()}
        except (ValueError, AttributeError) as exc:
            raise StateError(f"{self._store_file} is damaged ({exc}) — remove it") from None

    def save_store(self, items: dict[str, bytes]) -> None:
        """Atomically: a process killed mid-write leaves the previous store, not half of
        a new one — as NVS does on a board."""
        text = json.dumps({k: v.hex() for k, v in sorted(items.items())}, indent=2) + "\n"
        partial = self._store_file.with_suffix(".json.tmp")
        partial.write_text(text, encoding="utf-8")
        partial.replace(self._store_file)


def _spell(device: str, channels: int) -> str:
    """ "output 4", or just "output" for the default — 0 and 1 are the same board."""
    return f"{device} {channels}" if channels > 1 else device
