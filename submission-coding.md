# Chronicle-Code v0.1.0 — Coding application draft

Replace every PENDING field before sending. No application has been submitted.

- Track: Coding
- Division: Open-source Methods, subject to organizer review
- System: Chronicle-Code
- Version: v0.1.0 + PENDING fixed Git commit
- Public repository / commit URL: PENDING
- Add: PENDING real HTTPS host + /add
- Search: PENDING real HTTPS host + /search
- Health: PENDING real HTTPS host + /health
- Authentication: Authorization: Token; Memory System Key entered privately
- Runtime: Python / SQLite / Pillow; actual build and compute size PENDING
- Persistent disk and restart verification: PENDING
- Local implementation limits: request JSON 64MiB; top_k 1..100; chunk 8000 chars;
  Add concurrency1/Search4; model timeout90s, up to64attempts and1200s/Add
- Models: Add gpt-4o-mini, maximum384output tokens/call; no embedding;
  Search local lexical/entity/time plus code feature scoring, no model calls
- Capacity: empirical latency, sustainable storage, add size and bandwidth PENDING
- Formal Smoke / Full / Run IDs: PENDING

## Submission explanation and running guide

Chronicle-Code extends Chronicle-Memory's existing SQLite/FTS5 and entity/time
retrieval foundation. It preserves raw engineering trajectories and contiguous
source chunks, expands code identifiers/path/error terms, and ranks query-relevant
evidence while discounting weak progress/repetition noise. Failed attempts and
stack traces are retained. Add-time model hints are indexes only; Search returns
source text, not a generated repair or final answer.

Source attribution, MIT authorization and exact engine hash: ATTRIBUTION.md.
Changes and models: README.md, docs/RULES_AND_REUSE.md.
Reproduction/deployment: docs/SUBMISSION_GUIDE.md.
Actual deployment verification: docs/VERIFICATION.md plus the PENDING public record.
No official accuracy is claimed. Request confirmation of Open-source Methods
eligibility for a gpt-4o-mini Add method without embedding.

No third-party evaluation answers or private datasets are bundled. Evaluation
scope and all derived copies will be purged within30days of run completion;
backups and snapshots are included in the deployment cleanup procedure.
