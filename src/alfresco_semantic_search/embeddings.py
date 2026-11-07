"""Pluggable embedding backends.

Deliberately minimal and provider-agnostic: implement :class:`Embedder` and
pass it to the indexer. The project ships an OpenAI backend and a
deterministic fake for tests, and brings in no SDK — a plain HTTPS call is
less to keep up with than a client library.

Whatever you choose, the indexing and query sides must use the **same model**.
Mixing them yields vectors in different spaces and silently broken relevance,
which is why :attr:`Embedder.dims` is part of the interface.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import urllib.error
import urllib.request
from collections.abc import Sequence
from typing import ClassVar, Protocol, runtime_checkable

log = logging.getLogger(__name__)


@runtime_checkable
class Embedder(Protocol):
    """Anything that turns text into vectors."""

    @property
    def dims(self) -> int:
        """Vector dimensionality. Must match the ``vector(n)`` column."""

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch, returning one vector per input, in order."""


class OpenAIEmbedder:
    """OpenAI embeddings over plain HTTPS.

    ``text-embedding-3-small`` is 1536 dimensions, ``-3-large`` is 3072.
    Set the dimensionality on the store to match.
    """

    ENDPOINT = "https://api.openai.com/v1/embeddings"
    _DIMS: ClassVar[dict[str, int]] = {
        "text-embedding-3-small": 1536,
        "text-embedding-3-large": 3072,
    }

    def __init__(
        self,
        model: str = "text-embedding-3-small",
        api_key: str | None = None,
        batch_size: int = 64,
        timeout: float = 60.0,
    ) -> None:
        self.model = model
        self.batch_size = batch_size
        self.timeout = timeout
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not self._api_key:
            raise ValueError("OPENAI_API_KEY is not set")

    @property
    def dims(self) -> int:
        try:
            return self._DIMS[self.model]
        except KeyError:
            raise ValueError(
                f"unknown dimensionality for {self.model!r}; pass dims explicitly"
            ) from None

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = list(texts[start : start + self.batch_size])
            vectors.extend(self._embed_batch(batch))
        return vectors

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        request = urllib.request.Request(
            self.ENDPOINT,
            data=json.dumps({"model": self.model, "input": batch}).encode(),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"embeddings API returned HTTP {exc.code}: {detail}") from exc

        # The API may return items out of order; `index` is authoritative.
        items = sorted(payload["data"], key=lambda d: d["index"])
        return [item["embedding"] for item in items]


class FakeEmbedder:
    """Deterministic hash-based embeddings. For tests and demos only.

    Same text always yields the same unit vector, so similarity is stable and
    reproducible, but it carries **no semantic meaning** — unrelated texts are
    as likely to be close as related ones. Useful to exercise the pipeline and
    the permission filter without network access or an API key.
    """

    def __init__(self, dims: int = 1536) -> None:
        self._dims = dims

    @property
    def dims(self) -> int:
        return self._dims

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._one(text) for text in texts]

    def _one(self, text: str) -> list[float]:
        # Map hash bytes through integers, not struct.unpack on raw bytes:
        # reinterpreting arbitrary bytes as IEEE-754 floats yields NaN and Inf,
        # and pgvector rejects those with "NaN not allowed in vector".
        values: list[float] = []
        counter = 0
        while len(values) < self._dims:
            digest = hashlib.sha256(f"{counter}:{text}".encode()).digest()
            values.extend(
                int.from_bytes(digest[i : i + 4], "little") / 2**31 - 1.0
                for i in range(0, 32, 4)
            )
            counter += 1
        values = values[: self._dims]
        norm = math.sqrt(sum(v * v for v in values)) or 1.0
        return [v / norm for v in values]
