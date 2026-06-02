# VyomaCast — Full System Assessment: Task 14 Issues

**Document Purpose:** A precise record of every issue identified during the Task 14 integration work.  
**Scope:** All issues — whether pre-existing, introduced during Task 14, or exposed by Task 14 — are catalogued here.  
**Status:** Analysis only. No fixes applied.

---

## How to Read This Document

Each issue entry follows this structure:

- **What it is** — a plain description of the problem
- **Root cause** — exactly why it exists and what created it
- **Problems involved** — what breaks, what is at risk, and under what conditions

Issues are ordered by priority: broken-now before at-risk before operational.

---

## Issue 1 — Schema Drift: Existing Tests Construct Broken Payloads

**Classification: Test-level issue. Currently broken. Tests fail on every run.**

### What it is

`ArticleClusteredPayload` in `src/domain/events.py` was extended during Task 14 to add two required fields:

```python
title: str
source_domain: str
```

Two existing test files construct this payload without supplying these fields:

- `tests/integration/test_websocket.py` — line 122–128
- `tests/integration/test_pipeline_e2e.py` — line 397–402

Both construct it like this:

```python
ArticleClusteredPayload(
    cluster_id=cluster_id,
    article_id=article_id,
    version=2,
    is_new_cluster=False,
    similarity_score=0.88
)
```

Both also then manually inject the missing data into the raw dict after construction:

```python
envelope.payload["title"] = "Financial News Surge"
envelope.payload["source_domain"] = "reuters.com"
```

### Root cause

**This was the original workaround design from Task 11.** When the WebSocket notifier was first built, the architectural decision was deliberately made that `title` and `source_domain` are not clustering metrics and therefore do not belong in `ArticleClusteredPayload`. Instead, the notifier would read them from the raw `envelope.payload` dict.

The tests in `test_websocket.py` and `test_pipeline_e2e.py` were written to match this original design — constructing the payload without those fields, then manually injecting them into the dict.

During Task 14 integration work, this design was found to be broken in practice: Pydantic's `model_dump()` only serializes declared fields. When the cluster service constructs an `ArticleClusteredPayload` and serializes it into the envelope, any keys injected manually into the dict after construction are **not present** — they were never in the Pydantic model and were silently dropped during the next serialize/deserialize cycle across the NATS wire.

The fix applied during Task 14 added `title` and `source_domain` as declared required fields on the model. This was correct. But the two existing test files that used the old construction pattern **were not updated** to match. They were left behind.

### Problems involved

- **Immediate failure.** Any run of the full test suite will fail with `ValidationError: 2 validation errors for ArticleClusteredPayload — title: Field required — source_domain: Field required`. This is not a silent failure. Every run of `pytest` will report these tests as broken.
- **Broken test confidence.** `test_websocket.py` specifically validates the WebSocket broadcast path — one of the most critical flows in the system. While it is broken, the WebSocket layer has no automated coverage of its end-to-end validation.
- **Misleading CI.** If a CI pipeline runs the full suite, the test failures will be attributed to the websocket and e2e tests, which could be misread as a regression in those features rather than a schema propagation gap.

---

## Issue 2 — Global Monkeypatch: Model Mock Not Restored After Test

**Classification: Test-level issue. Causes cross-test contamination. Produces unreliable results silently.**

### What it is

Inside `tests/integration/test_pipeline_real_integration.py`, the HuggingFace `SentenceTransformer` model's encode function is replaced at the start of the test:

```python
import numpy as np
from src.services.dedup_service import model
model.encode = lambda *args, **kwargs: np.array([ [0.5]*384 ])
```

`model` is a module-level singleton defined at import time in `src/services/dedup_service.py`:

```python
model = SentenceTransformer(MODEL_NAME, device="cpu")
```

This singleton is shared across the entire Python process for the lifetime of the pytest session.

The monkeypatch is applied directly to the object in-place. There is no context manager, no `unittest.mock.patch`, and no teardown that restores the original `encode` function.

### Root cause

**This was introduced as a pragmatic workaround to a Windows threading deadlock.** On Windows, calling `model.encode` via `asyncio.run_in_executor` causes a deadlock between the Rust-based HuggingFace tokenizer thread pool and the Python GIL. The test was hanging for 90 seconds with no exception.

The immediate fix was to bypass the real model entirely by replacing `encode` with a lambda that returns a constant vector. The intent was correct — integration tests should not depend on real ML inference. The implementation was incorrect — the mock was applied globally and permanently for the duration of the pytest session, not scoped to the test function.

The correct approach is `unittest.mock.patch`, which wraps the replacement in a context manager that restores the original value on `__exit__`.

### Problems involved

- **Silent cross-test contamination.** Any test that imports `dedup_service` and exercises the real embedding pipeline after `test_real_pipeline_wiring` runs will silently receive the constant `[0.5]*384` vector instead of a real embedding. The test will not fail with an error — it will simply assert on meaningless data.
- **Ordering-dependent results.** The test suite currently produces different outcomes depending on whether `test_pipeline_real_integration.py` has run before other tests in the same session. This makes the test suite non-deterministic and unreliable as a correctness signal.
- **All embeddings become identical.** With the mock in place, every article injected through the dedup service in subsequent tests receives `[0.5]*384`. Because all embeddings are identical, every article will look like a 100% cosine duplicate of every other article that also has this embedding. This will cause the dedup service to drop all articles after the first one in any test that runs after the integration test. Vector dedup assertions will report false negatives silently.
- **Unit tests for dedup lose meaning.** If `test_dedup.py` runs after `test_pipeline_real_integration.py` in the same session, the `model.encode` mock is already in place. The `patch.object(dedup, "_compute_embedding", ...)` mocks in those tests are stacked on top of an already-mocked model, making them doubly artificial.

---

## Issue 3 — Incomplete Migration: Notifier Reads Raw Dict Instead of Typed Model

**Classification: System-level inconsistency. Not currently broken in practice. Latent correctness risk.**

### What it is

In `src/workers/notifier_worker.py`, after Change 4 added `title` and `source_domain` as declared fields on `ArticleClusteredPayload`, the notifier's read path was not updated to use the typed model:

```python
# Current code — still reads from the raw untyped dict:
payload = envelope.parse_payload(ArticleClusteredPayload)

raw_title = envelope.payload.get("title")       # dict access
raw_source = envelope.payload.get("source_domain")  # dict access

if not raw_title or not raw_source:
    raise ValueError("Missing title or source_domain in envelope payload")
```

The typed fields are now available directly:

```python
payload.title       # would be correct
payload.source_domain  # would be correct
```

The notifier parses the envelope into a typed `payload` object on line 45, but then ignores the typed fields in favour of re-accessing the raw dict.

### Root cause

**The notifier was written for the original Task 11 design**, where `title` and `source_domain` were deliberately not declared on the payload and the only way to access them was through the raw dict. The comment in the code reflects this original intent:

```python
# Zero I/O Path: Extract external metadata directly from the envelope dict
```

When Task 14 changed the schema to include `title` and `source_domain` as declared fields, only `cluster_service.py` (the publisher) and `events.py` (the schema) were updated. The notifier (the consumer) was not updated to match — it still reads data the old way.

### Problems involved

- **Currently works by accident.** Because `title` and `source_domain` are now declared Pydantic fields, they are included in `model_dump()` output and therefore present in `envelope.payload` as keys. The dict `.get()` calls succeed because the declared fields are serialized into the dict. The functional result is correct — but for the wrong reason.
- **The validation guard (`if not raw_title or not raw_source`) is redundant and misleading.** Since `title` and `source_domain` are now required `str` fields on the Pydantic model, they cannot be absent after `parse_payload()` succeeds. Pydantic would have already raised `ValidationError` if they were missing. The dict-based guard is checking something that has already been guaranteed. It creates a false impression that dict access is a necessary safety measure.
- **Future regression risk.** If the schema is ever changed and `title` or `source_domain` are renamed or made optional, the `parse_payload()` call will succeed but `envelope.payload.get("old_name")` will silently return `None`. The error will be caught by the `ValueError` guard and routed to `PermanentError` / `msg.term()`, silently dropping legitimate events. The failure mode is invisible — no stack trace, just a permanent termination logged at `ERROR` level.
- **Architectural inconsistency.** The rest of the codebase accesses payload data through typed Pydantic model attributes. This is the only place in the system that parses a typed payload and then discards it in favour of the raw dict.

---

## Issue 4 — Dead Code / Split Embedding Path: `_compute_embedding` Is Mocked But Never Called

**Classification: System-level bug. Unit tests are testing a dead code path. Real behavior is untested.**

### What it is

`DedupService` contains a proper method on the class for embedding generation:

```python
# src/services/dedup_service.py — line 86
async def _compute_embedding(self, text: str) -> Optional[list[float]]:
    """Generate 384-dimensional dense representation of text."""
    try:
        embeds = await asyncio.to_thread(
            model.encode, text, max_length=512, truncation=True
        )
        return embeds.tolist()
    except Exception as e:
        logger.warning("Embedding generation failed: %s", e)
        return None
```

This method has error handling, a `max_length=512` truncation limit, `asyncio.to_thread` scheduling, and returns `None` on failure.

The **actual code path** called during `process_article()` is a completely different inline closure defined at line 174:

```python
def _compute_embed(text: str) -> list[float]:
    return model.encode([text])[0].tolist()

loop = asyncio.get_running_loop()
embedding = await loop.run_in_executor(None, _compute_embed, payload.content)
```

This inline closure has no error handling, no `max_length` limit, no truncation, wraps `encode` differently (list input `[text]` vs single string), and uses `run_in_executor` instead of `asyncio.to_thread`.

The class method `_compute_embedding` still exists but is **never called anywhere in the current production code**.

All unit tests that mock embedding behaviour target the class method:

```python
# test_dedup.py — line 78
with patch.object(dedup, "_compute_embedding", return_value=[0.1]*384):
    ...

# test_pipeline_e2e.py — line 148
with patch.object(dedup_service, "_compute_embedding", return_value=embedding):
    ...
```

### Root cause

**This split was created during Task 14 debugging.** The original production code in `dedup_service.py` called `self._compute_embedding(payload.content)`. During Task 14 integration work, when trying to diagnose the Windows threading deadlock, the embedding call was refactored several times:

1. First, `run_in_executor` was tried as an alternative to `asyncio.to_thread`.
2. Then direct synchronous execution was tried (temporarily).
3. Finally, `run_in_executor` was restored — but as an inline closure rather than through the class method.

The class method `_compute_embedding` was left in place but the call site in `process_article` was changed to use the inline `_compute_embed` closure directly, bypassing the class method entirely. The unit tests were never updated because the class method still exists — its presence gives the impression that it is still the active code path.

### Problems involved

- **All unit tests for embedding behaviour are testing dead code.** When `test_dedup.py` patches `_compute_embedding`, it patches a method that `process_article` never calls. The mock has no effect on the actual execution. During those tests, the real `model.encode` is called via the inline closure — which means the unit tests are not actually controlling the embedding output they believe they are controlling.
- **Truncation protection is lost.** The class method caps inference at `max_length=512`. The inline closure calls `model.encode([text])` with no limits. Articles that are longer than 512 tokens — which is common for long news articles — will be silently truncated differently inside the model's tokenizer. This changes embedding quality without any observable signal.
- **Error handling is lost.** The class method wraps inference in `try/except` and returns `None` on failure, which is checked by the caller. The inline closure has no try/except. Any exception from `model.encode` (e.g. CUDA OOM, model corruption, OS signal) is an unhandled exception that propagates through `run_in_executor` as a `concurrent.futures.Future` exception, falls through to the NATS message handler's bare `except Exception` clause, and results in a `msg.nak()`. The article is retried up to `max_deliver=5` times, all failing identically, then permanently dropped with no log entry that identifies the root cause.
- **`asyncio.to_thread` vs `run_in_executor` difference.** The class method uses `asyncio.to_thread`, which creates a fresh thread for each call. The inline closure uses `run_in_executor` with the default `ThreadPoolExecutor`. On Windows specifically, these have different GIL interaction patterns, which was the entire reason for the refactoring. The current state mixes both styles — the class method remains as dead code with the older approach, while the active code uses the newer approach. This makes the intent completely unclear.

---

## Issue 5 — `[TRACE]` Debug Logs Left in Production Code

**Classification: Operational risk. Active in production builds. Causes log flooding under load.**

### What it is

Seven `logger.info` calls remain in `src/services/dedup_service.py` tagged with the `[TRACE]` prefix, placed at `INFO` level:

| Line | Log message |
|------|-------------|
| 123 | `[TRACE] Computing SimHash bands...` |
| 130 | `[TRACE] Checking SimHash bands in Redis...` |
| 132 | `[TRACE] Cache check returned %d collisions` |
| 180 | `[TRACE] Embedding computed. Starting exact vector duplicate check...` |
| 270 | `[TRACE] Saving article to PostgreSQL...` |
| 272 | `[TRACE] Article persistently saved to Postgres.` |
| 295 | `[TRACE] Publishing ARTICLE_UNIQUE to NATS bus.` |

These are all fired for every article that passes through the dedup service.

### Root cause

**These were added deliberately during Task 14 debugging** to gain visibility into which stage of the dedup pipeline was completing, because the integration test was hanging silently and there was no way to determine where the freeze occurred.

After the debugging session ended, the logs were never removed. The original intent was to clean them up once the integration test passed.

### Problems involved

- **Log flooding under load.** Every article that reaches the dedup service fires 7 INFO-level log lines. At a moderate throughput of 20 articles per minute, this produces 140 extra INFO log lines per minute. At burst rates of 50 articles per second, this is 350 INFO lines per second from a single worker. This saturates most log aggregators and makes it nearly impossible to find meaningful signals (real errors, latency warnings, circuit breaker trips).
- **`[TRACE]` as INFO is incorrect by convention.** Trace-level messages represent step-by-step execution traces for debugging active issues. They belong at `DEBUG` or a dedicated `TRACE` level. Publishing them at `INFO` means they cannot be filtered out without also suppressing all real informational messages. Production systems typically run at `INFO` level. There is no way to silence these without changing code.
- **Log storage cost.** In cloud environments with per-GB log storage costs (Datadog, CloudWatch, GCP Logging), 350 extra INFO lines per second per worker adds up to significant cost at scale.
- **Noise makes real errors invisible.** When a genuine error occurs — a Redis circuit breaker trip, a NATS publish failure, a database connection exhaustion — that error log line is buried in the stream of `[TRACE]` messages. On-call engineers are slower to detect real problems.

---

## Issue 6 — NATS `subscribe()` Default Queue Group is a Ghost Consumer Name

**Classification: Architectural risk. Creates a footgun for future developers.**

### What it is

`NatsEventBus.subscribe()` in `src/infrastructure/messaging/nats_bus.py` has a default fallback for the queue group name:

```python
async def subscribe(
    self,
    subject: str,
    handler: EventHandler,
    *,
    queue_group: Optional[str] = None,
    durable_name: Optional[str] = None,
) -> None:
    q_group = queue_group or "vyomacast_workers"   # line 120
```

All current workers now pass explicit queue group names:
- `dedup_worker` passes `"dedup_workers"`
- `cluster_worker` passes `"cluster_workers"`
- `notifier_worker` passes `"vyomacast_notifier"`

Nothing uses `"vyomacast_workers"` anymore. But it remains as the silent default.

### Root cause

**The default `"vyomacast_workers"` was the original queue group name used by both the dedup and cluster workers before Task 14.** It was a reasonable default before the workers were differentiated. When Task 14 corrected the queue group collision by giving each worker its own name, only the call sites in the individual workers were updated — the default fallback in `NatsEventBus.subscribe()` was not changed.

The result is a default value that references a now-meaningless consumer name. Any future subscriber that calls `bus.subscribe()` without specifying a queue group will silently register under `"vyomacast_workers"` — a name that has no clear ownership and whose behavior in a multi-worker environment is unpredictable.

### Problems involved

- **Silent misrouting for future workers.** If a developer adds a new worker (e.g., a writeback worker, a decay worker) and calls `bus.subscribe()` without specifying `queue_group`, it will default to `"vyomacast_workers"`. JetStream will place it in a consumer group with that name. If any other future worker also forgets to specify a group, they share the same consumer — cross-topic message theft occurs again, exactly as it did before Task 14.
- **No error or warning.** The default is applied silently. There is no log message, no deprecation warning, and no assertion that forces callers to be explicit. The bug reproduces itself without any visible signal.
- **Ghost consumer on live servers.** On any server where the old `"vyomacast_workers"` consumer still exists from a pre-Task 14 deployment, a new worker that accidentally uses the default will be joined to that stale consumer. It will receive messages from whatever subjects that consumer was watching previously — potentially messages from a different deployment configuration.

---

## Issue 7 — NATS Consumers Are Ephemeral, Not Durable

**Classification: System-level architectural risk. Pre-existing. Undermines the reliability guarantees of JetStream.**

### What it is

`NatsEventBus.subscribe()` passes `durable=durable_name` to the NATS JetStream `js.subscribe()` call on line 129. The `durable_name` parameter defaults to `None`. No worker ever passes a durable name when calling `bus.subscribe()`.

This means all consumers — dedup, cluster, notifier — are **ephemeral consumers**.

In NATS JetStream, an ephemeral consumer is deleted by the server automatically when the client connection drops. A durable consumer persists on the server even when the client is disconnected, and resumes delivering from where it left off when the client reconnects.

The worker configurations (`ack_wait=120s`, `max_deliver=5`) are meaningless for ephemeral consumers because there is no state to resume from after a disconnect.

### Root cause

**This is a pre-existing design gap from Task 6**, not introduced in Task 14. The original NATS implementation correctly defined the consumer config with `ack_wait` and `max_deliver`, but never assigned persistent durable names. The system was built and tested under the assumption that these settings provided durability. They do not, because durability in JetStream requires a named durable consumer.

The integration work in Task 14 exposed this because for the first time workers were started and stopped within the same server session (as asyncio tasks), and their subscriptions were cancelled at the end of the test. In a production deployment where the worker process restarts, the same issue applies.

### Problems involved

- **Messages are lost on worker restart.** When a worker is restarted — for any reason: deployment, crash, memory kill — the ephemeral consumer is deleted. Any messages that arrived while the worker was down are not delivered when it comes back. The worker starts fresh from the latest delivered position, not from where it left off. If the dedup worker restarts during a burst of extract events, those events are silently dropped.
- **`ack_wait` provides no restart durability.** Even with `ack_wait=120s`, if the consumer itself is deleted before the ack window expires, the message is also gone. The ack window is only meaningful while the consumer exists on the server.
- **`max_deliver=5` is not honoured across restarts.** If a message fails 3 times before a worker restart, the new ephemeral consumer treats it as a new message and starts counting from 0. The `max_deliver` guarantee only holds within a single consumer lifetime.
- **The system appears to handle failures but does not.** Because the code has `ack_wait`, `max_deliver`, `RetryableError` routing, and `PermanentError` termination, it gives the impression of a robust retry system. The absence of durable consumer names silently voids this for any scenario involving worker downtime. Under normal operation (workers always running), this is invisible. Under any operational failure, it causes silent data loss.

---

## Issue 8 — `development_log.md` Contains Injected Chat History

**Classification: Documentation integrity issue. No code impact.**

### What it is

The file `docs/development_log.md` was edited by the user and now contains raw chat interface text between the first `---` separator and the `**Four Pipeline Discoveries**` heading in the Task 14 section. The injected text includes lines like:

```
from chat:
Viewed development_log.md:1-581
Edited development_log.md
...
```

This text was accidentally pasted from the chat UI into the document.

### Root cause

The user made a manual edit to the file after the Task 14 section was written, and accidentally included chat log output in the paste.

### Problems involved

- **The Task 14 section is structurally broken.** The injected text appears in the middle of what should be a continuous section. Any reader of the document — including future developers, auditors, or automated tools that parse the log — will encounter the chat text as if it were part of the Task 14 documentation.
- **No code, test, or build is affected.** This is purely a documentation maintenance issue.

---

## Summary Table

| # | Issue | Classification | Status | Breaks Now? |
|---|---|---|---|---|
| 1 | Schema drift — existing tests construct payload without required fields | Test-level | Active | ✅ Yes — `ValidationError` on every run |
| 2 | Global monkeypatch — `model.encode` replaced without restore | Test-level | Active | ✅ Yes — silently contaminates other tests |
| 3 | Incomplete migration — notifier reads raw dict instead of typed fields | System inconsistency | Active | No — works by accident |
| 4 | Dead method — `_compute_embedding` mocked in tests but never called | System-level bug | Active | No — silent. Unit tests test dead code |
| 5 | `[TRACE]` logs at INFO level in production code | Operational risk | Active | No — but floods logs under load |
| 6 | NATS default queue group is a ghost consumer name | Architectural risk | Active | No — affects future workers only |
| 7 | NATS consumers are ephemeral, not durable | System-level architectural risk | Pre-existing | No — silent data loss on worker restart |
| 8 | `development_log.md` contains injected chat history | Documentation | Active | No — documentation only |

---

## Prioritised Fix Order

**Do first** — currently breaking tests:
1. Issue 1: Update `test_websocket.py` and `test_pipeline_e2e.py` to use new payload fields
2. Issue 2: Scope monkeypatch with `unittest.mock.patch`

**Do before any further test work:**
3. Issue 4: Resolve the split embedding path — either restore `_compute_embedding` as the single call site or update all test mocks to target the inline closure
4. Issue 3: Update `notifier_worker.py` to read typed fields from `payload.title` / `payload.source_domain`

**Do before production deployment:**
5. Issue 5: Remove `[TRACE]` `logger.info` calls or demote to `logger.debug`
6. Issue 6: Change `NatsEventBus.subscribe` default queue group to `None` and require explicit specification
7. Issue 7: Add durable consumer names to all worker subscriptions and plan the NATS stream reset required for deployment

**Do as documentation cleanup:**
8. Issue 8: Remove the injected chat fragment from `development_log.md`


---
### from chat window(pasted here by user) 

Edited Issues_FullSystemAssessment-Task14.md

The document is written to [Issues_FullSystemAssessment-Task14.md](file:///e:/news/docs/Issues_FullSystemAssessment-Task14.md).

Here is what's covered for each of the 8 issues, in each one explaining precisely what caused it:

---

| # | Issue | Cause |
|---|---|---|
| **1** | Schema drift — tests break on `ValidationError` | Task 14 added fields to `ArticleClusteredPayload` correctly, but the two test files that construct that payload were not updated. They were written for the old design and remain frozen there. |
| **2** | Global monkeypatch poisons other tests | Added as a quick fix for the Windows threading deadlock. Used direct attribute assignment on a module singleton instead of `unittest.mock.patch`, so the replacement is never restored. |
| **3** | Notifier reads raw dict instead of typed fields | The notifier was written in Task 11 when `title`/`source_domain` were not schema fields. Task 14 fixed the schema but only updated the publisher (`cluster_service`), not the consumer (`notifier_worker`). |
| **4** | `_compute_embedding` is mocked in tests but never called | During Task 14 debugging the embedding call site was refactored to an inline closure, bypassing the class method. The class method still exists, so all tests that mock it are mocking dead code — and the real path is untested and has no error handling or truncation. |
| **5** | `[TRACE]` INFO logs left in production code | Added during active debugging to trace where the pipeline was freezing. Never removed after the investigation ended. |
| **6** | NATS default queue group is a ghost name | Worker queue groups were fixed in Task 14, but only at the call sites. The default fallback in `NatsEventBus.subscribe()` still says `"vyomacast_workers"` — a name nothing uses anymore. |
| **7** | All NATS consumers are ephemeral | A pre-existing gap from Task 6 — `durable_name` was never passed to `js.subscribe()`. Without a named durable consumer, JetStream cannot resume delivery after a worker restart. All reliability guarantees (`ack_wait`, `max_deliver`) only hold while the consumer exists. |
| **8** | Chat text injected into `development_log.md` | Manual paste error during user editing. No code impact. |








---