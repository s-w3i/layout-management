import unittest

from warehouse_layout.slotting_strategies import (
    AbcAffinitySlottingStrategy,
    BasicSlottingStrategy,
    STRATEGY_TYPES,
    create_strategy,
)


class SlottingStrategyRegistryTests(unittest.TestCase):
    def test_builtin_strategies_are_registered_separately(self):
        self.assertEqual(set(STRATEGY_TYPES), {"basic", "abc_affinity"})
        self.assertIsInstance(create_strategy("basic"), BasicSlottingStrategy)
        self.assertIsInstance(
            create_strategy("abc_affinity"),
            AbcAffinitySlottingStrategy,
        )

    def test_unknown_strategy_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported slotting strategy"):
            create_strategy("not_registered")


if __name__ == "__main__":
    unittest.main()
