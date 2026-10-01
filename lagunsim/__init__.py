"""LagunBus device simulator — one process is one device.

The device is the C node library (`lagunbus_node.sim`), the same code that goes onto a
board; this package is only what a board's port and a person's hands would be: a bus,
a clock, a store that survives a restart, buttons and lamps. Why it is built this way:
`zadania/zrobione/FEAT-02.md` in the ProjectLagun working directory.
"""

__version__ = "0.1.0"
