"""Slotting strategy implementations.

The public :class:\`warehouse_layout.slotting.SlottingService\` remains the
stable facade. Strategy pipelines live here so new strategies can be added
without expanding that facade.
"""

from .abc_affinity import AbcAffinitySlottingStrategy
from .base import SlottingStrategy
from .basic import BasicSlottingStrategy

STRATEGY_TYPES: dict[str, type[SlottingStrategy]] = {
    "basic": BasicSlottingStrategy,
    "abc_affinity": AbcAffinitySlottingStrategy,
}


def create_strategy(name: str) -> SlottingStrategy:
    """Build a registered strategy by its persisted configuration name."""
    try:
        strategy_type = STRATEGY_TYPES[name]
    except KeyError as exc:
        raise ValueError(f"unsupported slotting strategy: {name}") from exc
    return strategy_type()


__all__ = [
    "AbcAffinitySlottingStrategy",
    "BasicSlottingStrategy",
    "SlottingStrategy",
    "STRATEGY_TYPES",
    "create_strategy",
]
