"""Part kinds and placements: plain names, importable without loading the CAD kernel."""

from __future__ import annotations

PartKind = str
ENCLOSURE = "enclosure"  # a molded part of the enclosure: the molding standards apply
INSERT = "insert"  # bought-in hardware pressed into the enclosure (heat-set inserts)
BOARD = "board"  # the board, added to the assembly from the board data
PART_KINDS = (ENCLOSURE, INSERT, BOARD)
Placement = tuple[float, float, float]
ORIGIN: Placement = (0.0, 0.0, 0.0)
