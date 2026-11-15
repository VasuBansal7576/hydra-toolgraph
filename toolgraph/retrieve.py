"""BM25 and dense fusion. Both run outside HydraDB; the engine has no vector index."""

from __future__ import annotations

import math
from collections import Counter, defaultdict

import numpy as np
from fastembed import TextEmbedding

from toolgraph.data import Tool, tokenize
from toolgraph.protocol import (
    BM25_CANDIDATES,
    DENSE_CANDIDATES,
    EMBED_MODEL,
    FUSION_POOL,
    RRF_K,
)


class Bm25Index:
    def __init__(self, names: list[str], docs: list[list[str]]) -> None:
        self.names = names
        self.docs = docs
        self.df: Counter[str] = Counter()
        self.tfs: list[Counter[str]] = []
        inverted: dict[str, list[int]] = defaultdict(list)
        for idx, doc in enumerate(docs):
            tf = Counter(doc)
            self.tfs.append(tf)
            self.df.update(tf.keys())
            for term in tf:
                inverted[term].append(idx)
        self.inverted = inverted
        self.n = len(docs)
        self.avg = sum(len(doc) for doc in docs) / max(1, self.n)
        self.idf = {
            term: math.log(1.0 + (self.n - freq + 0.5) / (freq + 0.5))
            for term, freq in self.df.items()
        }
        self.k1 = 1.2
        self.b = 0.75

    def top(self, text: str, limit: int) -> list[str]:
        scores: dict[int, float] = defaultdict(float)
        for term in set(tokenize(text)):
            idf = self.idf.get(term)
            if idf is None:
                continue
            for idx in self.inverted.get(term, []):
                freq = self.tfs[idx][term]
                length = len(self.docs[idx])
                denom = freq + self.k1 * (1.0 - self.b + self.b * length / self.avg)
                scores[idx] += idf * (freq * (self.k1 + 1.0)) / denom
        ranked = sorted(scores, key=lambda idx: (-scores[idx], self.names[idx]))
        return [self.names[idx] for idx in ranked[:limit]]


def embed_texts(texts: list[str]) -> np.ndarray:
    model = TextEmbedding(EMBED_MODEL)
    rows = [np.asarray(vector, dtype=np.float32) for vector in model.embed(texts, batch_size=64)]
    matrix = np.vstack(rows)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.clip(norms, 1e-12, None)


def dense_top(query_vecs: np.ndarray, tool_vecs: np.ndarray, names: list[str], limit: int) -> list[list[str]]:
    scores = query_vecs @ tool_vecs.T
    out: list[list[str]] = []
    width = min(limit, len(names))
    for row in scores:
        # argpartition is unordered inside the head; sort that head.
        picked = np.argpartition(-row, width - 1)[:width]
        ordered = picked[np.argsort(-row[picked])]
        out.append([names[int(idx)] for idx in ordered])
    return out


def rrf(rankings: list[list[str]], limit: int, k: int = RRF_K) -> list[str]:
    scores: dict[str, float] = defaultdict(float)
    for ranking in rankings:
        for position, name in enumerate(ranking):
            scores[name] += 1.0 / (k + position + 1)
    ordered = sorted(scores, key=lambda name: (-scores[name], name))
    return ordered[:limit]


def build_fusion(tools: list[Tool], queries: list[str]) -> list[list[str]]:
    names = [tool.name for tool in tools]
    docs = [tokenize(tool.base_text()) for tool in tools]
    bm25 = Bm25Index(names, docs)
    tool_vecs = embed_texts([tool.base_text() for tool in tools])
    query_vecs = embed_texts(queries)
    dense = dense_top(query_vecs, tool_vecs, names, DENSE_CANDIDATES)
    fused: list[list[str]] = []
    for idx, text in enumerate(queries):
        lexical = bm25.top(text, BM25_CANDIDATES)
        fused.append(rrf([lexical, dense[idx]], FUSION_POOL))
    return fused
