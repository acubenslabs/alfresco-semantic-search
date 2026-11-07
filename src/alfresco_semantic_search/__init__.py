"""Permission-aware semantic search over an Alfresco repository.

Keeps embeddings in your own pgvector store and resolves read permissions
against Alfresco, as the end user, at query time. Nothing about permissions is
copied — which is the point. See the README for why the alternative fails open.
"""

from .alfresco import AlfrescoClient, AlfrescoError, NodeInfo, site_from_path_elements
from .chunking import chunk_text, normalise
from .embeddings import Embedder, FakeEmbedder, OpenAIEmbedder
from .events import Change, Event2Listener, NodeEvent, classify
from .extract import ExtractionError, TransformClient, guess_mime
from .indexer import Indexer, IndexerStats
from .reconcile import ReconcileReport, reconcile
from .retrieval import Caller, RetrievalResult, permission_filter, search
from .store import ChunkStore, Hit

__version__ = "0.1.0"

__all__ = [
    "AlfrescoClient",
    "AlfrescoError",
    "Caller",
    "Change",
    "ChunkStore",
    "Embedder",
    "Event2Listener",
    "ExtractionError",
    "FakeEmbedder",
    "Hit",
    "Indexer",
    "IndexerStats",
    "NodeEvent",
    "NodeInfo",
    "OpenAIEmbedder",
    "ReconcileReport",
    "RetrievalResult",
    "TransformClient",
    "chunk_text",
    "classify",
    "guess_mime",
    "normalise",
    "permission_filter",
    "reconcile",
    "search",
    "site_from_path_elements",
]
