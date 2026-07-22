from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from hashlib import blake2b

from .storage.repositories import ChunkRow

WORD_PATTERN = re.compile(r"[a-z0-9_]+")
CJK_PATTERN = re.compile(r"[一-鿿]+")

BM25_K1 = 1.5
BM25_B = 0.75


def _source_diverse_top_k(ranked: list[ScoredChunk], top_k: int) -> list[ScoredChunk]:
    """Prefer one high-scoring chunk per source before filling remaining slots.

    Overlapping line windows from one large test or generated file can otherwise
    consume every evidence slot, hiding a slightly lower-scoring but distinct
    implementation file.  The first pass preserves score order and maximizes
    source coverage; the second pass fills unused capacity with the remaining
    highest-scoring chunks, so a corpus with only one useful source still returns
    all available evidence.
    """

    if top_k <= 0:
        return []
    selected: list[ScoredChunk] = []
    selected_ids: set[str] = set()
    seen_sources: set[str] = set()
    for hit in ranked:
        source = hit.chunk.source_uri
        if source in seen_sources:
            continue
        selected.append(hit)
        selected_ids.add(hit.chunk.chunk_id)
        seen_sources.add(source)
        if len(selected) == top_k:
            return selected
    for hit in ranked:
        if hit.chunk.chunk_id in selected_ids:
            continue
        selected.append(hit)
        if len(selected) == top_k:
            break
    return selected


def tokenize(text: str) -> list[str]:
    """Lowercased word tokens plus CJK character bigrams.

    Bigrams keep Chinese queries discriminative: single characters are so
    common in code comments that they match almost anything.
    """
    lowered = text.lower()
    tokens = []
    for word in WORD_PATTERN.findall(lowered):
        tokens.append(word)
        if "_" in word:
            tokens.extend(part for part in word.split("_") if part)
    for run in CJK_PATTERN.findall(lowered):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


@dataclass(frozen=True)
class ScoredChunk:
    chunk: ChunkRow
    score: float
    coverage: float = 1.0
    """Fraction of unique query tokens matched by this chunk."""

    @property
    def citation(self) -> str:
        if self.chunk.line_start is None or self.chunk.line_end is None:
            return self.chunk.source_uri
        return f"{self.chunk.source_uri}:L{self.chunk.line_start}-L{self.chunk.line_end}"


class LexicalRetriever:
    """Offline BM25 retriever over the latest chunk rows.

    The index lives in process memory and is rebuilt from the database on
    demand, so retrieval keeps working when no embedding provider exists.
    """

    def __init__(self, rows: list[ChunkRow]) -> None:
        self.rows = rows
        self._documents = [tokenize(row.content) for row in rows]
        self._frequencies = [Counter(tokens) for tokens in self._documents]
        self._lengths = [len(tokens) for tokens in self._documents]
        self._average_length = sum(self._lengths) / len(self._lengths) if self._lengths else 0.0
        document_frequency: Counter[str] = Counter()
        for frequency in self._frequencies:
            document_frequency.update(frequency.keys())
        self._document_frequency = document_frequency

    def __len__(self) -> int:
        return len(self.rows)

    def search(self, query: str, *, top_k: int = 5, min_score: float = 0.0) -> list[ScoredChunk]:
        query_tokens = tokenize(query)
        if not query_tokens or not self.rows:
            return []
        scores = [self._score(query_tokens, index) for index in range(len(self.rows))]
        ranked = sorted(
            (
                ScoredChunk(row, round(score, 6), round(coverage, 4))
                for row, (score, coverage) in zip(self.rows, scores, strict=True)
                if score > min_score
            ),
            key=lambda item: (-item.score, item.chunk.source_uri, item.chunk.ordinal),
        )
        return _source_diverse_top_k(ranked, top_k)

    def _score(self, query_tokens: list[str], index: int) -> tuple[float, float]:
        frequency = self._frequencies[index]
        length = self._lengths[index] or 1
        total = len(self.rows)
        unique_tokens = set(query_tokens)
        score = 0.0
        matched = 0
        for token in unique_tokens:
            occurrences = frequency.get(token, 0)
            if occurrences == 0:
                continue
            matched += 1
            document_frequency = self._document_frequency.get(token, 0)
            idf = math.log(1 + (total - document_frequency + 0.5) / (document_frequency + 0.5))
            denominator = occurrences + BM25_K1 * (
                1 - BM25_B + BM25_B * length / (self._average_length or 1)
            )
            score += idf * occurrences * (BM25_K1 + 1) / denominator
        return score, matched / len(unique_tokens) if unique_tokens else 0.0


EMBED_DIM = 256
SEMANTIC_BONUS = 0.15


def embed(tokens: list[str]) -> list[float]:
    """Deterministic feature-hashing embedding, L2-normalized.

    No external embedding API: each token hashes to a signed dimension, so the
    same text always produces the same vector and tests stay offline.
    """
    vector = [0.0] * EMBED_DIM
    for token in tokens:
        digest = blake2b(token.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        index = value % EMBED_DIM
        sign = 1.0 if (value >> 62) & 1 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(component * component for component in vector))
    if norm:
        vector = [component / norm for component in vector]
    return vector


def cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


class HybridRetriever:
    """BM25 ranking modulated by a hashed-embedding cosine bonus.

    The hashed embedder is deterministic and offline, so its cosine signal is
    weak; it therefore only boosts BM25 scores instead of competing with them
    in a rank fusion. Swapping in a real embedding provider raises
    ``SEMANTIC_BONUS`` without touching callers. Lexical coverage survives on
    the fused results so the Reviewer can still reject weak matches.
    """

    def __init__(self, rows: list[ChunkRow]) -> None:
        self.lexical = LexicalRetriever(rows)
        self.rows = rows
        self._vectors = [embed(tokenize(row.content)) for row in rows]

    def __len__(self) -> int:
        return len(self.rows)

    def search(self, query: str, *, top_k: int = 5) -> list[ScoredChunk]:
        if not self.rows:
            return []
        lexical_hits = self.lexical.search(query, top_k=len(self.rows))
        if not lexical_hits:
            # Nothing matched lexically: cosine over hashed vectors alone
            # cannot distinguish signal from hash collisions, so refuse.
            return []
        query_vector = embed(tokenize(query))
        vectors = dict(zip((row.chunk_id for row in self.rows), self._vectors, strict=True))

        fused = [
            ScoredChunk(
                hit.chunk,
                round(
                    hit.score
                    * (
                        1
                        + SEMANTIC_BONUS
                        * max(cosine(query_vector, vectors[hit.chunk.chunk_id]), 0.0)
                    ),
                    6,
                ),
                hit.coverage,
            )
            for hit in lexical_hits
        ]
        fused.sort(key=lambda item: (-item.score, item.chunk.source_uri, item.chunk.ordinal))
        return _source_diverse_top_k(fused, top_k)
