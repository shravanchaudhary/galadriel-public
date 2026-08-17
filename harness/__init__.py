"""Galadriel agent harness.

Import-time third-party defaults only. Everything else lives in submodules.
"""

import os

# litellm is a transitive dependency (semantic-router for Stage-1 recall,
# headroom-ai for compression) — we never call it directly. On first import it
# fetches model_prices_and_context_window.json from raw.githubusercontent.com
# with a 5s timeout. headroom imports it lazily on first compression, so that
# stall lands mid-turn, not at startup. The bundled backup carries the same
# 2909 models, so pin to it. Set the var explicitly to override.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
