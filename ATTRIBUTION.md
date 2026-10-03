# Attribution and reuse

Owner authorization: on 2026-10-03 the user confirmed ownership/authorization
for reused Chronicle-Memory code and selected MIT for this derivative.
This package's LICENSE applies to this authorized release; it does not assert
that every historical upstream version previously carried MIT.

## Source chain

- Original method: [simple-boy/Chronicle-Memory](https://github.com/simple-boy/Chronicle-Memory).
- Prior V2 audit referred to upstream snapshot 00d0862560aba0213ca122ef62a1bba37cf5e431.
  That snapshot is a historical source reference, not the new submission commit.
- The locally inspected updated upstream checkout reported HEAD
  26bd3aca10e4d6cf4262c5079f2e77e6c7ff9c94. Its engine matched the local text V2
  engine. A live remote HEAD and the original Phase 1 evaluated commit were
  not re-established in this run.
- This package copies memory_core.py unchanged from the local text V2.
  SHA256: E2DCB79212FAF63EF7EEC3CE21664B761261F2A147FB26EB8BB150C4DD1A1B3F.

## Reused and changed components

| Component | Origin / change |
|---|---|
| memory_core.py | Unchanged Chronicle V2 SQLite/WAL, FTS5, scope, entity/time helpers and candidate retrieval |
| store.py | New subclass; atomic raw/source/index writes, frozen track config, receipts, multimodal source restoration, wider lexical pool and visual candidates |
| coding.py | New source-preserving chunks, identifier/path/error expansion, language aliases, query-conditioned scoring |
| multimodal.py | New ordered text/image parts, inline image validation, original source retention, local image features and Add captions |
| models.py | New optional gpt-4o-mini Add indexing; no Search LLM calls |
| app.py | Reuses the standard-library HTTP approach; new track factory, media schema validation, budgets and error handling |
| Deployment / scripts / tests | New deployment, cleanup, estimates and diagnostic material |

These are competition engineering adaptations. We do not claim that SQLite,
hybrid retrieval, image captioning, or their combination is a novel research
contribution. No external benchmark answers, task-specific oracle labels,
training corpus, model weights, or third-party repository snapshots are bundled.
The synthetic diagnostic's labels are used by its evaluator only.

Pillow is a dependency under its own [license](https://github.com/python-pillow/Pillow/blob/main/LICENSE).
GPT-4o-mini is an external model service, not code redistributed under this project's MIT license.
The official [AML repository](https://github.com/AML-memory/agent-memory-leaderboard)
and [website](https://agentmemories.ai/rules) define the evaluation contract;
this package is not an official AML adapter or evaluator.
