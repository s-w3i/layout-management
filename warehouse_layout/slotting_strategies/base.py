"""Common contract and registry for inventory slotting strategies."""

from __future__ import annotations

from typing import Protocol


class SlottingStrategy(Protocol):
    """A strategy that can generate assignments through the service facade."""

    name: str

    def generate(self, service, *args, **kwargs) -> tuple[list[dict], dict]:
        """Generate assignment rows and their summary."""
