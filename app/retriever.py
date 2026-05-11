"""
app/retriever.py
────────────────────────────────────────────────────────────────────────────
FAISS-based semantic retrieval over catalog_enriched.json.
- Builds index on first run, persists to disk for fast cold starts.
- Uses sentence-transformers/all-MiniLM-L6-v2 (free, local, fast).
- Normalised inner-product = cosine similarity.
"""

import json
import pickle
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

CATALOG_PATH = Path(__file__).parent.parent / "catalog_enriched.json"
INDEX_PATH   = Path(__file__).parent.parent / "faiss_index.bin"
STORE_PATH   = Path(__file__).parent.parent / "catalog_store.pkl"

# ── module-level singletons ───────────────────────────────────────────────────
_index   = None
_catalog: List[Dict[str, Any]] = []
_url_set: set = set()
_model   = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        logger.info("Loading embedding model …")
        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model


def _encode(texts: List[str]) -> np.ndarray:
    model = _get_model()
    embs = model.encode(texts, show_progress_bar=False, batch_size=64)
    embs = embs.astype(np.float32)
    norms = np.linalg.norm(embs, axis=1, keepdims=True)
    return embs / (norms + 1e-8)


def build_and_save_index() -> None:
    """Build FAISS index from catalog and persist to disk."""
    import faiss

    global _index, _catalog, _url_set

    logger.info("Building FAISS index from %s …", CATALOG_PATH)
    with open(CATALOG_PATH, encoding="utf-8") as f:
        _catalog = json.load(f)

    texts = [item["embedding_text"] for item in _catalog]
    embs  = _encode(texts)

    dim    = embs.shape[1]
    _index = faiss.IndexFlatIP(dim)
    _index.add(embs)

    faiss.write_index(_index, str(INDEX_PATH))
    with open(STORE_PATH, "wb") as f:
        pickle.dump(_catalog, f)

    _url_set = {item["url"] for item in _catalog}
    logger.info("Index built: %d items, dim=%d", len(_catalog), dim)


def load_index() -> None:
    """Load persisted index or build from scratch."""
    import faiss

    global _index, _catalog, _url_set

    if INDEX_PATH.exists() and STORE_PATH.exists():
        logger.info("Loading persisted FAISS index …")
        _index = faiss.read_index(str(INDEX_PATH))
        with open(STORE_PATH, "rb") as f:
            _catalog = pickle.load(f)
    else:
        build_and_save_index()

    _url_set = {item["url"] for item in _catalog}
    logger.info("Retriever ready: %d catalog items", len(_catalog))


def search(query: str, k: int = 20) -> List[Dict[str, Any]]:
    """Return top-k catalog items most semantically similar to query."""
    if _index is None:
        load_index()

    q_emb = _encode([query])
    scores, indices = _index.search(q_emb, k)
    results = []
    for idx in indices[0]:
        if 0 <= idx < len(_catalog):
            results.append(_catalog[idx])
    return results


def get_by_names(names: List[str]) -> List[Dict[str, Any]]:
    """Look up catalog items by partial name match (for compare turns)."""
    found = []
    seen_urls = set()
    for name in names:
        nl = name.lower()
        for item in _catalog:
            if nl in item["name"].lower() and item["url"] not in seen_urls:
                found.append(item)
                seen_urls.add(item["url"])
                break
    return found


def is_valid_url(url: str) -> bool:
    return url in _url_set


def get_all() -> List[Dict[str, Any]]:
    return _catalog
