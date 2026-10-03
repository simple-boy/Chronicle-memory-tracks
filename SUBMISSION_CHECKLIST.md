# Chronicle-Code / Chronicle-Vision submission checklist

Updated 2026-10-03. Checked boxes describe code/material evidence only, never
claim completion of remote deployment or official evaluation.

## Source and rules

- [x] Consulted current official Rules, Docs, API Guide, Competition, Evaluation and public repository.
- [x] Two distinct processors share the explicitly attributed unchanged Chronicle engine.
- [x] Owner confirmed reuse authorization and selected MIT; LICENSE and ATTRIBUTION.md included.
- [x] No bundled third-party evaluation answers, private task corpora or pretrained weights.
- [ ] Create public GitHub repository and record actual URL plus complete frozen commit.
- [ ] Organizer confirms no-embedding/gpt-4o-mini configuration and division eligibility.

## API and evidence

- [x] POST /add and POST /search; GET /health; Token/Bearer/X-Api-Key authentication.
- [x] Exact ID echoes, stable memory IDs, user scope, cross-session history.
- [x] Durable synchronous transaction and committed identical retry without duplicate/model calls.
- [x] Changed retry conflicts, malformed schemas/media rejected, temporary model error does not partly write.
- [x] Code trajectories retained; no execution, repo fetching, answer generation or prose deletion.
- [x] Ordered multimodal original parts retained, validated Base64 JPEG/PNG/WebP only.
- [x] 10MiB/image and30MiB decoded Add/Search-response limits;64MiB JSON-body declared capacity.
- [x] Source/index separation; Search neither persists questions nor invokes model APIs.
- [ ] Run actual approved gpt-4o-mini Add after cost estimate; record usage and source quality.
- [ ] Measure real capacity, concurrency, p95 latency, image RAM, DB and bandwidth.

## Deployment

- [x] Pinned Pillow dependency and Linux Docker/entrypoint material included.
- [x] Runtime uses environment paths and private keys; bootstrap drops to UID/GID10001.
- [x] Separate service/DB/key per track; model/config binding stored in database.
- [x] Render Blueprint uses current plan IDs, disabled auto-deploy and independent /app/data disks.
- [ ] Build Docker image and verify Python3.11/FTS5/dependencies inside it.
- [ ] Verify mounted-volume ownership/bootstrap on actual host.
- [ ] Provision two real HTTPS services after reviewing hosting and model costs.
- [ ] Authenticated public Add→Search, unauthorized401, other-user isolation.
- [ ] Restart each deployed service then Search previously saved data without another Add.
- [ ] Freeze deployed commit/environment and record image/build identity and actual resource size.
- [ ] Ensure total request/host proxy timeouts fit declared capacity and official schedule.

## Data lifecycle and submission

- [x] Administrative scoped purge script covers source, features, FTS/entities, receipts, usage and WAL compaction.
- [ ] Maintain eval Run-to-exact-user mapping; execute deletion within30days after each completed run.
- [ ] Verify provider/host logs, exports, automatic backups and snapshots are also cleaned on time.
- [x] README, schema, costs, local tests, diagnostics and two application templates included.
- [ ] Fill templates with actual API/repository/SHA/capacity; enter keys only in private fields.
- [ ] Separately apply Coding and Multimodal, receive and distinguish Eval Key from Memory System Key.
- [ ] Pass each official Smoke, run each Full, record actual Run IDs and reviewed score.

## Publication exclusions

Never upload .env/private keys, databases/WAL/SHM, private evaluation images/queries,
payload logs, state JSON, provider credentials or private answer files.
Keep only empty .env.example. Source bundle excludes caches and synthetic local
result artifacts as well; rerun supplied scripts for your environment.

Current official result: **null**. Public URLs: **pending**.
