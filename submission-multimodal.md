# Chronicle-Vision v0.1.0 — Multimodal application draft

Replace every PENDING field before sending. No application has been submitted.

- Track: Multimodal
- Division: Open-source Methods, subject to organizer review
- System: Chronicle-Vision
- Version: v0.1.0 + PENDING fixed Git commit
- Public repository / commit URL: PENDING
- Add: PENDING real HTTPS host + /add
- Search: PENDING real HTTPS host + /search
- Health: PENDING real HTTPS host + /health
- Authentication: Authorization: Token; dedicated Memory System Key entered privately
- Runtime/build/compute size: PENDING
- Persistent disk/restart verification: PENDING
- Supported evidence: strings or ordered text/image_url arrays; only inline
  JPEG/PNG/WebP Base64; original evidence retained and returned
- Limits:10MiB/image;30MiB decoded media/Add and Search response;64MiB JSON request;
  additional declared16Mpixel/image limit; top_k1..100; Add1/Search4 concurrency
- Models: gpt-4o-mini Add, high image detail, maximum384output tokens/call;
  no embedding; Search has zero model calls
- Model budget: up to64attempts and1200s/Add,90s timeout/attempt
- Measured sustained capacity, storage/bandwidth: PENDING
- Formal Smoke/Full/Run IDs: PENDING

## Submission explanation and running guide

Chronicle-Vision extends Chronicle-Memory with ordered original image/text
evidence, Add-time derived image descriptions, lexical/entity/time candidates
and cheap SHA256/dHash/color-histogram visual candidates. Captions are index
text only. Search restores complete source parts in rank order and respects
the decoded media prefix budget; it neither generates answers nor stores queries.

It shares the unchanged Chronicle engine with Chronicle-Code but uses a distinct
processor, database, service and key. This is an engineering method with known
appearance-retrieval limitations, not a claim of learned semantic visual embeddings.

See ATTRIBUTION.md, README.md, docs/SUBMISSION_GUIDE.md and docs/VERIFICATION.md.
No official score or real-model result is claimed. Please confirm the no-embedding
Open-source Methods configuration. All evaluation source/derived data,
snapshots/backups and payload logs must be removed within30days after completion.
