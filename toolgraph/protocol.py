"""Constants fixed before any sealed score was read.

Changing these after looking at the sealed split would be tuning on the
held-out set. Calibration that is allowed uses the learn/dev splits only and
is recorded in the results file and docs/DECISIONS.md.
"""

SEEDS = (7, 21, 42)
LEARN_FRACTION = 0.60
DEV_FRACTION = 0.20
# Trace budgets for the sealed learning curve. "all" is handled as len(learn).
TRACE_BUDGETS = (0, 10, 30, 100, 300, "all")
BATCH = 10
CANARY_SIZE = 40
KS = (10, 16)

# Retrieval. Pool size is the statechart cap of 255, kept far under it.
RRF_K = 60
FUSION_POOL = 32
FUSION_SEEDS = 4
BM25_CANDIDATES = 64
DENSE_CANDIDATES = 64
EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

# Closure. SQLite recursive CTE and HydraDB MSpaths share this depth.
MAX_LEN = 4
PATH_COUNT = 64
RESULT_LIMIT = 500
SEED_PENALTY = 2.0
DIRECT_COST = 1.0
INDIRECT_COST = 1.6
LEARNED_BASE_COST = 1.0

# Jev. Choice returns one distribution; these cut it into a seed set.
JEV_MIN_PROB = 0.08
JEV_MAX_SEEDS = 4
# Edge-verifier Noul grid searched on the learn split only. Never on sealed.
EDGE_NOUL_GRID = (0.3, 0.5, 0.7, 0.9)
NOUL_CHUNK = 24
JEV_MODEL = "jev-latest"
JEV_URL = "https://api.typesafe.ai/v1/systemone"

# Schema candidate caps (learn-only verification budget).
SCHEMA_CANDIDATES_PER_TOOL = 4
SCHEMA_VERIFY_MAX = 400

# Pre-registered rise test (docs/DECISIONS.md). Written before any v2 sealed score.
RISE_SPREAD_MULTIPLIER = 3.0
