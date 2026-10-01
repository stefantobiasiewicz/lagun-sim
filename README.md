# lagun-sim

A LagunBus device without hardware. **One process is one device**: a key, a wall panel,
a relay board — running the C node library from [`../lagunbus-node`](../lagunbus-node/),
the same code that goes onto a board. Next to a hub started with `--udp` it is a real
installation on one computer, macOS included: the hub commissions it, enumerates it,
binds it and reconciles it as it would a board on `can0`.

Why it is built this way — one process per device, the C node through `ctypes`, UDP
multicast as the bus — is in [`../zadania/zrobione/FEAT-02.md`](../zadania/zrobione/FEAT-02.md).

```sh
# terminal 1 — the hub, on a temporary copy of the configuration, taking new nodes
lagun-hub run --udp --ephemeral --accept-new

# terminals 2, 3, … — one device each
lagun-sim key
lagun-sim output8
lagun-sim key --name hall          # a second key, with an identity of its own
lagun-sim output --channels 4      # four relays on endpoints 4 … 7

lagun-hub tui --config <the path the hub printed>
```

### A key that switches a relay

The hub's example configuration (`hub/config/`) binds a key to a relay by their Uids.
Give the simulated devices those Uids and the binding works as on the boards:

```sh
lagun-hub run --udp --ephemeral --accept-new --config config/     # in hub/
lagun-sim output --uid 08090a0b0c0d0e0f --name kitchen-relay
lagun-sim key    --uid 0001020304050607 --name kitchen-switch
```

Click the key (`1`) — the relay's lamp goes on; again — off. `--uid` fixes the identity
the first time a name is used; after that the name keeps it.

`lagun-sim` with no arguments lists the devices. The list is `c/sim/devices.c` in the
node library — the same list the conformance scenarios run on, so every device here is
one the tests check.

## Install

It needs the node library checked out next to it (`../lagunbus-node`), a C compiler and
CMake — the C library is built on first use and rebuilt when its sources change.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e ../lagunbus-node/python --config-settings editable_mode=compat
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest -q
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy lagunsim tests          # strict
```

`editable_mode=compat` installs the library as a plain path, which `mypy` can follow; the
default editable mode uses an import hook it cannot.

## What a device remembers

| | |
|---|---|
| `~/.lagun-sim/<name>/uid` | the Uid, drawn at the first start |
| `~/.lagun-sim/<name>/store.json` | the node's persistent store — address, boot counter, output states |
| `~/.lagun-sim/<name>/device` | which device lives under this name; another one is refused |

`<name>` is the device's name unless `--name` says otherwise. Stopping the process is a
power cut: the next start finds what the node itself stored. `--fresh` is a device out of
the box — a temporary identity and store, removed at exit. `LAGUN_SIM_HOME` moves the
root.

## Options

| | |
|---|---|
| `--bus udp[:GROUP[:PORT]]` | the default: UDP multicast on this machine, the other end of `lagun-hub run --udp` |
| `--bus vcan0` | a SocketCAN interface instead — Linux only |
| `--name NAME` | what the device is remembered as |
| `--uid HEX` | a known identity instead of a drawn one — for a hub configuration that names it |
| `--channels N` | 1 … 8 keys (`key`) or relays (`output`) on endpoints 4, 5, …; one by default, as the boards. Remembered by the name: a name made with 4 opens with 4, and another count is refused — to the hub that would be the same Uid with different endpoints |
| `--fresh` | nothing remembered |
| `--headless` | no terminal view: the device's traffic, printed line by line |

## The view

Buttons for the inputs, lamps for the outputs, the device's traffic below.

| | |
|---|---|
| `1` … `9` | click that input (a press held 80 ms) |
| `←` `→`, `Enter` | pick an input, click it |
| `d`, `t` | double, triple click on the picked input |
| `space` | press the picked input and keep it pressed; again to let go (`*` marks it) |
| mouse | press on a button and hold — released when you let go |
| `f` | faults, then: `r` lose the next response, `b` answer the next request `BUSY`, `s` fall silent for 10 s, `g` lose a segment of the next long response, `c` clear. The overlay of `lagunbus_node.faults`: the node knows nothing, the hub sees it all — the device's own log does not show what was answered or lost in its name |
| `p` | power cut: a new node from the same Uid and the stored state; RAM is gone. A `FactoryReset` from the hub does the same by itself, as `esp_restart()` on a board |
| `q` | quit — also a power cut, as far as the device is concerned |

A terminal reports a key being typed, never released, so a keyboard click is a press
of fixed length; the mouse reports both edges, so only the mouse gives a hold of
exactly the length you want.

The traffic log shows what the device sends and what concerns it — requests to its
address or to everyone, joining frames for its ShortId — read through the node library's
own frame codec.

## Layout

| | |
|---|---|
| `lagunsim/view.py` | the terminal view (`curses`) — draws and forwards, nothing else |
| `lagunsim/control.py` | what a person does: click, hold, the plug, the faults — tested without a terminal |
| `lagunsim/runner.py` | one turn of a board's loop: bus → fault overlay → C node → fault overlay → bus; the store saved when the node wrote to it |
| `lagunsim/state.py` | the Uid and the store on disk |
| `lagunsim/bus.py` | `udp` or a SocketCAN interface, from `lagunbus_node.ports` |
| `lagunsim/traffic.py` | frames in words, and which received ones concern this device |
| `lagunsim/cli.py` | the command |
