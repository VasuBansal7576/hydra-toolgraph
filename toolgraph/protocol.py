"""Constants fixed before any sealed score was read.

Changing these after looking at the sealed split would be tuning on the
held-out set. Calibration that is allowed uses the learn/dev splits only and
is recorded in the results file and docs/DECISIONS.md.
"""

SEEDS = (7, 21, 42)
LEARN_FRACTION = 0.60
DEV_FRACTION = 0.20
TRACE_BUDGETS = (0, 10, 30, 100, 300, "all")
BATCH = 10
CANARY_SIZE = 40
KS = (10, 16)

# Retrieval.
RRF_K = 60
FUSION_POOL = 32
FUSION_SEEDS = 4
BM25_CANDIDATES = 64
DENSE_CANDIDATES = 64
EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

# Closure / walk depth. SQLite recursive CTE and HydraDB MSpaths share this.
MAX_LEN = 4
PATH_COUNT = 64
RESULT_LIMIT = 500
SEED_PENALTY = 2.0
LEARNED_BASE_COST = 1.0

# Edge confidence: p = (n_obs + 1) / (n_seen_src + 2). τ on dev only.
TAU_GRID = (0.15, 0.25, 0.35, 0.45, 0.55)

# Schema I/O prior (Graph-of-Skills ζ = 0.6). Deterministic only; no LLM judge.
IO_OVERLAP_KEEP = 0.6
SCHEMA_CANDIDATES_PER_TOOL = 6
SCHEMA_VERIFY_MAX = 400

# Reverse-aware PPR (GoS): client-side because HydraDB pageRank rejects weights.
PPR_ALPHA = 0.2
PPR_ITERS = 50
PPR_WEIGHT_TRACE = 1.0
PPR_WEIGHT_IO = 0.7

# Pre-registered rise test.
RISE_SPREAD_MULTIPLIER = 3.0
BOOTSTRAP_SAMPLES = 200
BOOTSTRAP_SEED = 0
