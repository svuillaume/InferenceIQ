"""
semcache — 3-layer semantic response cache for the proxy.

  Layer 1  exact (hot)   : hash(namespace + normalized prompt) -> entry, O(1)
  Layer 2  semantic      : fastembed embeddings (local ONNX model, no PyTorch) in a
                           vector index; cosine similarity ≥ threshold = hit
  Layer 3  LLM fallback  : the caller forwards to the model and store()s the result

SAFETY (this is why proxy.py was deleted — do not regress):
  • The proxy only ever consults/populates this cache for NON-AGENTIC, text-only traffic
    (no tools, no tool_result). That gating lives in intercept.py — this module just
    stores/serves text.
  • Fail-open: if the embedding model can't load, every lookup is a clean miss, so the
    proxy degrades to a plain pass-through. Nothing here ever raises into the request path.

Design notes:
  • Embeddings are L2-normalized and stored as float16 (half the memory); cosine = dot.
  • Backends are pluggable (numpy | hnswlib | faiss). numpy is the authoritative store and
    always correct; ANN backends are rebuilt from it when it changes (avoids fragile deletes).
  • Responses are zlib-compressed. Hybrid eviction (LRU + frequency) keeps the store ≤ MAX_MB.
  • Namespaced by hash(system prompt) (+ model when per_model) so a cached answer is never
    served across a different system instruction.
"""

import os, re, time, zlib, json, hashlib, threading, math
import numpy as np


# ── Config (env, with sane defaults) ─────────────────────────────────────────────
def _f(name, d):
    try: return float(os.getenv(name, d))
    except Exception: return float(d)

def _i(name, d):
    try: return int(os.getenv(name, d))
    except Exception: return int(d)

HIT_THRESHOLD  = _f("CACHE_HIT", 0.92)     # ≥ → serve from cache
SOFT_THRESHOLD = _f("CACHE_SOFT", 0.88)    # [soft,hit) → "soft" near-miss (not served)
DEDUP_THRESHOLD= _f("CACHE_DEDUP", 0.97)   # ≥ on store → merge instead of insert
MAX_MB         = _f("CACHE_MAX_MB", 50.0)  # store budget (embeddings + responses)
PER_MODEL      = os.getenv("CACHE_PER_MODEL", "0") == "1"
INDEX_KIND     = os.getenv("CACHE_INDEX", "numpy").lower()
MODEL_NAME     = os.getenv("CACHE_MODEL", "BAAI/bge-small-en-v1.5")   # 384-d, ONNX, ~90MB
PERSIST_PATH   = os.getenv("CACHE_PERSIST_PATH", "")
DIM            = 384


def _norm_text(s: str) -> str:
    """Normalize a prompt to reduce noise before keying/embedding."""
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _sha(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "ignore")).hexdigest()


# ── Embedding (lazy, fail-open, background warmup) ────────────────────────────────
class _Embedder:
    def __init__(self):
        self._model = None
        self._failed = False
        self._lock = threading.Lock()
        threading.Thread(target=self._load, daemon=True).start()  # warm up off the hot path

    def _load(self):
        try:
            from fastembed import TextEmbedding
            with self._lock:
                self._model = TextEmbedding(model_name=MODEL_NAME)
        except Exception:
            self._failed = True   # stays a no-op embedder → cache is a clean pass-through

    @property
    def ready(self) -> bool:
        return self._model is not None

    def embed(self, texts):
        """Return an (n, DIM) float32 L2-normalized array, or None if the model isn't ready."""
        if self._model is None:
            return None
        try:
            vecs = np.array(list(self._model.embed(list(texts))), dtype=np.float32)
            n = np.linalg.norm(vecs, axis=1, keepdims=True)
            n[n == 0] = 1.0
            return vecs / n
        except Exception:
            return None


# ── Pluggable similarity backends over the authoritative fp16 matrix ─────────────
class _NumpyIndex:
    """Brute-force cosine. Authoritative; always correct; supports deletion."""
    kind = "numpy"
    def search(self, mat: np.ndarray, q: np.ndarray):
        if mat.shape[0] == 0:
            return -1, -1.0
        sims = mat.astype(np.float32) @ q          # vectors are pre-normalized → dot = cosine
        i = int(np.argmax(sims))
        return i, float(sims[i])


class _HnswIndex:
    kind = "hnsw"
    def __init__(self):
        import hnswlib
        self._hnswlib = hnswlib
        self._idx = None
        self._n = 0
    def rebuild(self, mat: np.ndarray):
        n = mat.shape[0]
        idx = self._hnswlib.Index(space="cosine", dim=DIM)
        idx.init_index(max_elements=max(16, n), ef_construction=200, M=16)
        if n:
            idx.add_items(mat.astype(np.float32), np.arange(n))
        idx.set_ef(min(max(16, n), 128))
        self._idx, self._n = idx, n
    def search(self, mat, q):
        if self._idx is None or self._n != mat.shape[0]:
            self.rebuild(mat)
        if self._n == 0:
            return -1, -1.0
        labels, dists = self._idx.knn_query(q.astype(np.float32), k=1)
        return int(labels[0][0]), float(1.0 - dists[0][0])   # cosine distance → similarity


class _FaissIndex:
    kind = "faiss"
    def __init__(self):
        import faiss
        self._faiss = faiss
        self._idx = None
        self._n = -1
    def rebuild(self, mat: np.ndarray):
        idx = self._faiss.IndexFlatIP(DIM)           # inner product on normalized vecs = cosine
        if mat.shape[0]:
            idx.add(np.ascontiguousarray(mat.astype(np.float32)))
        self._idx, self._n = idx, mat.shape[0]
    def search(self, mat, q):
        if self._idx is None or self._n != mat.shape[0]:
            self.rebuild(mat)
        if self._n == 0:
            return -1, -1.0
        D, I = self._idx.search(np.ascontiguousarray(q.reshape(1, -1).astype(np.float32)), 1)
        return int(I[0][0]), float(D[0][0])


def _make_index(kind: str):
    try:
        if kind == "hnsw": return _HnswIndex()
        if kind == "faiss": return _FaissIndex()
    except Exception:
        pass   # lib missing / failed → fall back to numpy (logged by the caller via .kind)
    return _NumpyIndex()


# ── The cache ────────────────────────────────────────────────────────────────────
class SemanticCache:
    def __init__(self):
        self.emb = _Embedder()
        self.index = _make_index(INDEX_KIND)
        self._lock = threading.Lock()
        # Authoritative store (parallel arrays):
        self._mat = np.zeros((0, DIM), dtype=np.float16)   # fp16 embeddings
        self._entries = []                                  # dicts: see _new_entry
        self._exact = {}                                    # exact-key -> entry index
        self.stats = {"lookups": 0, "exact_hits": 0, "semantic_hits": 0, "soft": 0,
                      "misses": 0, "stores": 0, "merges": 0, "evictions": 0}
        if PERSIST_PATH:
            self._load_disk()

    # — keys / namespaces —
    def _ns(self, system: str, model: str) -> str:
        base = _sha(system or "")
        return base + ("|" + (model or "") if PER_MODEL else "")

    def _exact_key(self, prompt: str, system: str, model: str) -> str:
        return self._ns(system, model) + "|" + _norm_text(prompt)

    # — public API —
    def lookup(self, prompt: str, system: str = "", model: str = ""):
        """Return (text, layer, similarity) on a hit, else None. Never raises."""
        try:
            with self._lock:
                self.stats["lookups"] += 1
                ek = self._exact_key(prompt, system, model)
                idx = self._exact.get(ek)
                if idx is not None:
                    self.stats["exact_hits"] += 1
                    return self._touch(idx), "exact", 1.0
                # semantic
                if not self.emb.ready or self._mat.shape[0] == 0:
                    self.stats["misses"] += 1
                    return None
                q = self.emb.embed([_norm_text(prompt)])
                if q is None:
                    self.stats["misses"] += 1
                    return None
                q = q[0]
                ns = self._ns(system, model)
                cand = self._namespace_mask(ns)
                if cand is None:
                    self.stats["misses"] += 1
                    return None
                rows, mat = cand
                i, sim = self.index.search(mat, q)
                if i < 0:
                    self.stats["misses"] += 1
                    return None
                gi = rows[i]
                if sim >= HIT_THRESHOLD:
                    self.stats["semantic_hits"] += 1
                    return self._touch(gi), "semantic", sim
                if sim >= SOFT_THRESHOLD:
                    self.stats["soft"] += 1
                self.stats["misses"] += 1
                return None
        except Exception:
            return None

    def store(self, prompt: str, system: str, model: str, response_text: str):
        """Insert (or merge a near-duplicate). Never raises."""
        try:
            if not response_text or not self.emb.ready:
                return
            v = self.emb.embed([_norm_text(prompt)])
            if v is None:
                return
            v = v[0].astype(np.float16)
            with self._lock:
                ns = self._ns(system, model)
                # dedup within the namespace
                cand = self._namespace_mask(ns)
                if cand is not None:
                    rows, mat = cand
                    i, sim = _NumpyIndex().search(mat, v.astype(np.float32))
                    if i >= 0 and sim >= DEDUP_THRESHOLD:
                        self._touch(rows[i]); self.stats["merges"] += 1
                        return
                ek = self._exact_key(prompt, system, model)
                e = self._new_entry(ns, ek, response_text)
                self._entries.append(e)
                self._mat = np.vstack([self._mat, v.reshape(1, -1)]) if self._mat.shape[0] else v.reshape(1, -1)
                self._exact[ek] = len(self._entries) - 1
                self.stats["stores"] += 1
                self._evict_if_needed()
        except Exception:
            return

    def gauges(self) -> dict:
        with self._lock:
            lu = self.stats["lookups"] or 1
            hits = self.stats["exact_hits"] + self.stats["semantic_hits"]
            return {
                "entries": len(self._entries),
                "bytes": self._bytes(),
                "max_bytes": int(MAX_MB * 1024 * 1024),
                "hit_rate": round(hits / lu * 100, 1),
                "exact_hits": self.stats["exact_hits"],
                "semantic_hits": self.stats["semantic_hits"],
                "misses": self.stats["misses"],
                "evictions": self.stats["evictions"],
                "merges": self.stats["merges"],
                "ready": self.emb.ready,
                "index": self.index.kind,
                "model": MODEL_NAME,
            }

    # — internals —
    def _new_entry(self, ns, ek, response_text):
        blob = zlib.compress(response_text.encode("utf-8", "ignore"))
        return {"ns": ns, "ek": ek, "resp": blob, "hits": 1, "last": time.time(),
                "created": time.time(), "rbytes": len(blob)}

    def _touch(self, gi: int) -> str:
        e = self._entries[gi]
        e["hits"] += 1
        e["last"] = time.time()
        return zlib.decompress(e["resp"]).decode("utf-8", "ignore")

    def _namespace_mask(self, ns: str):
        rows = [i for i, e in enumerate(self._entries) if e["ns"] == ns]
        if not rows:
            return None
        return rows, self._mat[rows].astype(np.float32)

    def _bytes(self) -> int:
        emb = self._mat.shape[0] * DIM * 2                       # fp16
        resp = sum(e["rbytes"] for e in self._entries)
        meta = len(self._entries) * 200                          # rough per-entry overhead
        return emb + resp + meta

    def _score(self, e) -> float:
        # Hybrid: frequency (log hits) + recency. Lowest score is evicted first.
        return math.log(e["hits"] + 1) + (e["last"] / 1e9)

    def _evict_if_needed(self):
        budget = int(MAX_MB * 1024 * 1024)
        if self._bytes() <= budget:
            return
        order = sorted(range(len(self._entries)), key=lambda i: self._score(self._entries[i]))
        drop = set()
        b = self._bytes()
        for i in order:
            if b <= budget:
                break
            drop.add(i)
            b -= (self._entries[i]["rbytes"] + DIM * 2 + 200)
        if drop:
            self._compact(drop)
            self.stats["evictions"] += len(drop)

    def _compact(self, drop: set):
        keep = [i for i in range(len(self._entries)) if i not in drop]
        self._entries = [self._entries[i] for i in keep]
        self._mat = self._mat[keep] if keep else np.zeros((0, DIM), dtype=np.float16)
        # rebuild the exact map from the surviving entries (indices shifted by compaction)
        self._exact = {e["ek"]: i for i, e in enumerate(self._entries) if e.get("ek")}

    # — optional persistence —
    # Safe JSON+base64 format (NOT pickle — pickle.load executes arbitrary code on a tampered
    # file). The embedding matrix is stored as base64 of its raw fp16 bytes + shape; each
    # response blob (already zlib-compressed) is base64. Load is fail-open: any error → empty.
    def _load_disk(self):
        try:
            import json as _json, base64
            with open(PERSIST_PATH, "r", encoding="utf-8") as f:
                d = _json.load(f)
            raw = base64.b64decode(d.get("mat_b64", "") or "")
            shape = tuple(d.get("mat_shape") or (0, DIM))
            self._mat = (np.frombuffer(raw, dtype=np.float16).reshape(shape).copy()
                         if raw else np.zeros((0, DIM), dtype=np.float16))
            entries = []
            for e in d.get("entries", []):
                e = dict(e)
                e["resp"] = base64.b64decode(e["resp"])   # back to zlib-compressed bytes
                entries.append(e)
            self._entries = entries
            self._exact = {k: int(v) for k, v in (d.get("exact") or {}).items()}
        except Exception:
            pass   # corrupt / missing / wrong-format file → start empty (fail-open)

    def save(self):
        if not PERSIST_PATH:
            return
        try:
            import json as _json, base64
            with self._lock:
                doc = {
                    "v": 1,
                    "mat_b64": base64.b64encode(self._mat.tobytes()).decode("ascii"),
                    "mat_shape": list(self._mat.shape),
                    "entries": [{**e, "resp": base64.b64encode(e["resp"]).decode("ascii")}
                                for e in self._entries],
                    "exact": self._exact,
                }
            tmp = PERSIST_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                _json.dump(doc, f)
            os.replace(tmp, PERSIST_PATH)   # atomic swap so a crash mid-write can't corrupt it
        except Exception:
            pass
