# Task 14 — Change Audit

A precise record of every code change made during Task 14 integration work. Each entry documents **what changed**, **why it was needed**, and **whether it is test-only or a system-level change**, with an honest assessment of production safety.

---

## Change 1 — `tests/conftest.py`: Database URL credentials corrected

**What changed:**
```diff
- os.environ.setdefault("VYOMACAST_DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5433/test")
+ os.environ.setdefault("VYOMACAST_DATABASE_URL", "postgresql+asyncpg://vyomacast:vyomacast@localhost:5433/vyomacast")
```

**Why it was needed:**  
The original credentials (`test:test`, database `test`) did not match the Docker Compose configuration, which creates a user `vyomacast` with password `vyomacast` and a database named `vyomacast`. Any test that established a real database connection would fail immediately with `InvalidPasswordError`. Unit tests were unaffected because they use in-memory fakes and never connect to PostgreSQL.

**Classification: Test-only fix.**  
This file is never loaded in production. The production system reads `VYOMACAST_DATABASE_URL` from the environment or `.env` file, which is set correctly. This fix has zero impact on production behavior.

**Risk: None.** But it reveals that the integration test suite has never successfully connected to the real database before this task. The unit test safety net masked the broken config for all of Tasks 1–13.

---

## Change 2 — `src/workers/dedup_worker.py`: Queue group renamed

**What changed:**
```diff
- queue_group="vyomacast_workers",
+ queue_group="dedup_workers",
```

**Why it was needed:**  
Both `dedup_worker` and `cluster_worker` used the same NATS JetStream queue group name `"vyomacast_workers"`. JetStream binds a queue group name to a durable consumer object on the server. When two logically different subscribers (listening on different subjects) register with the same name, NATS treats them as replicas of the same consumer, not as independent consumers. This creates cross-subscription delivery: dedup messages can be routed to cluster workers and vice versa.

**Classification: System-level fix.**  
This change affects production behavior. In a deployed environment with multiple worker instances, the original shared queue group would cause event misrouting under load. The fix is architecturally correct.

**Production safety: Safe to deploy.** However, there is one operational risk: if the old consumer `vyomacast_workers` still exists on a running NATS server from a previous deployment, it will continue holding messages until it times out or is manually deleted. **Before deploying this change to any server with existing state, `scripts/setup_nats.py` must be run to delete and recreate the stream.** The setup script already handles this via forceful stream deletion.

---

## Change 3 — `src/workers/cluster_worker.py`: Queue group renamed

**What changed:**
```diff
- queue_group="vyomacast_workers",
+ queue_group="cluster_workers",
```

**Why it was needed:** Same root cause as Change 2. The cluster worker shared the queue group name with the dedup worker.

**Classification: System-level fix.**

**Production safety:** Same caveat as Change 2. The stream must be reset on any server with existing consumers registered under the old name.

---

## Change 4 — `src/domain/events.py`: `ArticleClusteredPayload` schema extended

**What changed:**
```diff
  class ArticleClusteredPayload(BaseModel):
      cluster_id: UUID
      article_id: UUID
+     title: str
+     source_domain: str
      version: int = Field(ge=1)
      is_new_cluster: bool
      similarity_score: Optional[float] = Field(default=None)
```

**Why it was needed:**  
The `notifier_worker` needs `title` and `source_domain` to construct the WebSocket broadcast payload. The original design tried to pass these through the untyped `envelope.payload` dict by injecting them after Pydantic model creation. However, when `cluster_service` serializes an `ArticleClusteredPayload` back into the envelope dict (via `model.model_dump()`), Pydantic only includes declared fields. The injected keys were silently stripped, leaving the notifier with `None` for both fields. The notifier then raised `PermanentError` and called `msg.term()`, permanently dropping the event.

**Classification: System-level fix.** This is a domain schema change. It modifies the wire format of the `ARTICLE_CLUSTERED` event.

**Production safety — Requires careful review:**

> [!WARNING]
> This is a **breaking schema change** to an event payload model. Any existing consumer of `ARTICLE_CLUSTERED` events that constructs `ArticleClusteredPayload` without providing `title` and `source_domain` will now **fail Pydantic validation** with `ValidationError`. Two existing test files are directly affected.

**Existing tests now broken by this change:**

1. **`tests/integration/test_websocket.py` (line 122–128):**  
   Constructs `ArticleClusteredPayload` without `title` or `source_domain`, then manually injects them into `envelope.payload` afterward. This was the original workaround pattern. After Change 4, this construction will raise `ValidationError` because `title` and `source_domain` are now required fields.

2. **`tests/integration/test_pipeline_e2e.py` (line 397–407):**  
   Same pattern — constructs payload without the new required fields, then injects them manually into the dict. Will also fail after Change 4.

These two tests have not been updated to match the new schema. **They are broken by this change.** Running the full test suite will show failures in `test_websocket.py` and `test_pipeline_e2e.py`.

**Also note:** the `notifier_worker` still reads `title` and `source_domain` from `envelope.payload` dict (lines 48–49) rather than from `payload.title` and `payload.source_domain`. This means it continues to rely on dict access rather than the typed model, making the schema addition partially cosmetic for the notifier specifically. The underlying structural fix is real, but the notifier's read path was not updated to match.

---

## Change 5 — `src/services/cluster_service.py`: `ArticleClusteredPayload` populated with `title` and `source_domain`

**What changed:**
```diff
  ev = ArticleClusteredPayload(
      cluster_id=cluster.id,
      article_id=article.id,
+     title=article.title,
+     source_domain=domain,
      version=cluster.version,
      is_new_cluster=is_new_cluster,
      similarity_score=best_score if not is_new_cluster else None,
  )
```

**Why it was needed:**  
This is the counterpart to Change 4. `title` and `source_domain` are now required fields on the payload model. The cluster service has access to both at event construction time — `article.title` from the domain model and `domain` from the URL parsing already performed earlier in the same method.

**Classification: System-level change.** Paired with Change 4.

**Production safety: Safe**, assuming Change 4 is accepted. The values are sourced from already-validated domain objects (`article.title` is always a non-empty string at this point; `domain` is derived from `urlparse` of the article URL). No new I/O is introduced. No performance risk.

---

## Change 6 — `tests/integration/test_pipeline_real_integration.py`: HuggingFace model monkeypatched

**What changed:**
```python
import numpy as np
from src.services.dedup_service import model
model.encode = lambda *args, **kwargs: np.array([ [0.5]*384 ])
```

**Why it was needed:**  
On Windows, calling `SentenceTransformer.encode()` via `asyncio.run_in_executor()` causes a deadlock between the Rust-based HuggingFace tokenizer thread pool and the Python GIL/asyncio scheduler. The test would hang for the full timeout window (90 seconds) with no exception. Bypassing the real model with a fixed-vector mock removes the threading constraint and makes the test deterministic.

**Classification: Test-only change.** The monkeypatch only applies for the duration of the test function. However, see the critical risk below.

**Risk — Test isolation failure:**

> [!CAUTION]
> `model` is a **module-level global** in `src/services/dedup_service.py`. The monkeypatch (`model.encode = lambda ...`) mutates this global object in-place. It is **not scoped to the test function** and **not restored after the test completes**.
>
> If any other test in the same pytest session imports or uses `dedup_service` after `test_real_pipeline_wiring` runs, it will receive the mocked `encode` function instead of the real model. This is a test isolation violation. The correct approach is to use `unittest.mock.patch` as a context manager or fixture, which guarantees restoration via `__exit__`.

**Production safety: No direct production risk** — this file is never imported outside of the test runner. But the monkeypatch as written is fragile and will silently corrupt other tests in the same session if test ordering changes.

---

## Change 7 — `tests/integration/test_pipeline_real_integration.py`: Random payload content

**What changed:**
```python
random_words = " ".join(str(uuid4()) for _ in range(20))
content = (
    f"Breaking test alert: {random_words}. This is entirely random content ..."
)
```

**Why it was needed:**  
The deduplication engine correctly detected the static "aerospace industry" test payload as a near-duplicate of an article already persisted in the database from a prior test run (98.8% cosine similarity). The pipeline dropped it as intended. The static payload had to be replaced with semantically novel content to pass through dedup and reach the cluster stage.

**Classification: Test-only change.** No production code is touched.

**Side note:** This change also highlights a correct system behavior that was initially misread as a failure. The dedup engine was working exactly as designed. The bug was in the test, not the system.

---

## Change 8 — `tests/integration/test_pipeline_real_integration.py`: Timeout extended to 90 seconds

**What changed:**
```diff
- timeout = 15.0
+ timeout = 90.0
```

**Why it was needed:**  
The original 15-second timeout was based on the assumption that HuggingFace embedding inference would complete quickly. On a Windows host without GPU acceleration, the model takes 50–55 seconds for its first inference call (cold start), putting it beyond the original timeout. This was later superseded by Change 6 (mocking the model), which makes the timeout irrelevant in practice. 90 seconds is retained as a safety margin.

**Classification: Test-only change.** No production impact.

---

## Summary Table

| # | File | Type | Production Safe? | Notes |
|---|---|---|---|---|
| 1 | `tests/conftest.py` | Test-only fix | ✅ Yes | Credentials never used by production |
| 2 | `src/workers/dedup_worker.py` | System fix | ⚠️ With caveat | Requires stream reset on existing deployments |
| 3 | `src/workers/cluster_worker.py` | System fix | ⚠️ With caveat | Same as above |
| 4 | `src/domain/events.py` | System fix — breaking | ❌ Breaks existing tests | Schema change breaks `test_websocket.py` and `test_pipeline_e2e.py` |
| 5 | `src/services/cluster_service.py` | System fix | ✅ Yes | Paired with Change 4, no new I/O |
| 6 | `tests/integration/test_pipeline_real_integration.py` | Test-only | ✅ Test-only | Global mutation without restore — test isolation risk |
| 7 | `tests/integration/test_pipeline_real_integration.py` | Test-only | ✅ Test-only | Correct fix to test design |
| 8 | `tests/integration/test_pipeline_real_integration.py` | Test-only | ✅ Test-only | Superseded by Change 6 in practice |

---

## Actions Required Before Marking Task 14 Complete

The following issues were introduced during Task 14 and are not yet resolved:

1. **`tests/integration/test_websocket.py`** — Must be updated to include `title` and `source_domain` in `ArticleClusteredPayload` constructor calls (or remove the manual dict injection pattern now that the schema is typed).

2. **`tests/integration/test_pipeline_e2e.py`** — Same: all `ArticleClusteredPayload(...)` instantiations must be updated with the two new required fields.

3. **`src/workers/notifier_worker.py`** — The notifier still reads `raw_title = envelope.payload.get("title")` and `raw_source = envelope.payload.get("source_domain")` from the untyped dict (lines 48–49). Now that these are declared fields on `ArticleClusteredPayload`, they should be read from the typed `payload` object (`payload.title`, `payload.source_domain`) for consistency and safety. The dict-based read still works because Pydantic serializes declared fields into the dict, but the typed access is correct.

4. **`tests/integration/test_pipeline_real_integration.py`** — The `model.encode` monkeypatch must be scoped using `unittest.mock.patch` to ensure proper restoration and prevent cross-test contamination.
