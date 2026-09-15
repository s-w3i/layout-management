"""Pure ABC slotting strategy."""

from __future__ import annotations

from dataclasses import dataclass

from .allocation import allocate


@dataclass(frozen=True, slots=True)
class BasicSlottingStrategy:
    """Place SKUs by ABC/frequency order using the shared hard rules."""

    name: str = "basic"

    def generate(self, service, *args, **kwargs):
        kwargs["strategy"] = self.name
        return allocate(service, *args, **kwargs)
