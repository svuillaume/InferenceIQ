semcache — semantic response cache for LLM proxy

A 3-layer caching system that reduces LLM calls by reusing exact or semantically similar past responses.

⸻

What it is

semcache is a drop-in memory layer for LLM traffic that:

* Reuses identical prompts (exact cache)
* Reuses similar prompts via embeddings (semantic cache)
* Falls back to the LLM when needed

⸻

Cache layers

1. Exact cache (fast path)

* Hash of normalized prompt + system + model
* O(1) lookup
* Perfect match reuse

⸻

2. Semantic cache (smart reuse)

* Uses ONNX embeddings (fastembed)
* Cosine similarity search
* Hit if similarity ≥ ~0.92
* Finds “same meaning, different wording” queries

⸻

3. LLM fallback

* If no cache match:
    * call model
    * store response for future reuse

⸻

How it works

Prompt
  ↓
Exact match? ───────── yes → return cached response
  ↓ no
Semantic match? ────── yes → return cached response
  ↓ no
Call LLM → store result → return response

⸻

Storage

Each cached entry includes:

* compressed response (zlib)
* embedding vector (float16, normalized)
* metadata (hits, timestamps, namespace)

Optimized for memory:

* float16 embeddings
* compressed responses
* hybrid eviction (LRU + frequency)

⸻

Safety design

* Only caches non-agentic text requests
* Blocks tool/agent traffic upstream
* Fail-open:
    * if embeddings fail → behaves like no cache

⸻

Performance features

* Exact match: O(1) lookup
* Semantic match: vector similarity search
* Optional backends:
    * numpy (default)
    * HNSW / FAISS acceleration
* Thread-safe operations

⸻

Eviction

When memory exceeds limit:

* removes least useful entries using:
    * low frequency
    * old age

⸻

Persistence (optional)

* Saves cache to disk (JSON)
* Embeddings stored as base64 float16
* Responses stored compressed
* Safe load (corruption = ignore and start empty)

⸻

Key idea

Avoid calling the LLM twice for the same or similar question.

⸻

Result

* Lower latency
* Lower token usage
* Reduced API cost
* Faster repeated interactions

⸻

Summary

Exact match → semantic match → LLM fallback → store result → reuse later
