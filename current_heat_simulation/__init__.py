"""Current-heat warehouse simulation with store/day workload scheduling."""

import os

# Worker processes import the simulator too; keep Pygame's informational banner
# out of the benchmark progress display.
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
