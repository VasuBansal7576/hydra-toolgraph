"""Constants fixed before any sealed score was read.

Changing these after looking at the sealed split would be tuning on the
held-out set. Calibration that is allowed uses the dev split only and is
recorded in the results file.
"""

SEEDS = (7, 21, 42)
LEARN_FRACTION = 0.60
DEV_FRACTION = 0.20
TRACE_BUDGETS = (0, 10, 30, 90)
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

# Closure.
MAX_LEN = 4
PATH_COUNT = 64
RESULT_LIMIT = 500
SEED_PENALTY = 2.0
DIRECT_COST = 1.0
INDIRECT_COST = 1.6

# Jev. Choice returns one distribution; these cut it into a seed set.
JEV_MIN_PROB = 0.08
JEV_MAX_SEEDS = 4
# Score rubric is 0 = covered, 1 = helper missing, 2 = blocking miss.
# Edits apply only when the score reaches the "missing" side.
SCORE_APPLY_AT = 0.5
# Noul grid searched on dev at zero traces. Not refit later, not on sealed.
NOUL_GRID = (0.3, 0.5, 0.7, 0.9)
NOUL_CHUNK = 24
JEV_MODEL = "jev-latest"
JEV_URL = "https://api.typesafe.ai/v1/systemone"

MISS_RANK = 10
