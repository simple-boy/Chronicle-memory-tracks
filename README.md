# Chronicle-Memory Tracks

**Chronicle-Code** and **Chronicle-Vision** are two targeted continuations of
[Chronicle-Memory](https://github.com/simple-boy/Chronicle-Memory) for the second
Agent Memory Challenge's Coding and Multimodal tracks.
Each is an independent Add/Search service with its own database, key and configuration.

Status: runnable participant-side implementation; local tests and synthetic
diagnostics only. No official Smoke/Full, real GPT inference, Docker build or
new public deployment is claimed.

## Overview and motivation

Text memory alone loses code punctuation and visual evidence. Chronicle-Code
keeps the supplied engineering history and finds paths, symbols and errors.
Chronicle-Vision keeps ordered original text/images, adds searchable descriptions
during Add, and combines textual retrieval with cheap visual comparisons.
Both return source evidence for AML's Answer/Eval stages.

## Method and architecture

~~~text
WRITE
Supplied messages + exact scope + source time
  -> Validate every message and decoded media
  -> Code: continuous source chunks + identifier/path/error features
     Vision: ordered source parts + image features + optional GPT descriptions
  -> Atomic SQLite transaction:
     raw_messages | source_records | derived text/FTS/entity/time indexes
     request receipt | model usage | frozen track configuration
  -> durable commit -> exact Add acknowledgement

READ
Query + exact user_id + optional answer options
  -> Local query parsing (no LLM, no memory write)
  -> Chronicle lexical/entity/time candidates + wider FTS pool
     Vision image query: additional scope-wide visual feature candidates
  -> Query-conditioned ranking
  -> Restore original evidence; respect top_k and 30 MiB image prefix
  -> data[] -> AML Answer -> AML Eval
~~~

### Memory writing

- Code chunks are at most 8,000 characters; contiguous, with no discarded
  whitespace or overlapping copies. Raw messages remain separately recoverable.
- Code imperatives such as "Remove obsolete cache" are stored as engineering
  evidence. The old prose forgetting/supersession path is bypassed.
- Multimodal records preserve the entire original message and its part order.
  Only inline JPEG/PNG/WebP Data URIs are accepted.
- gpt-4o-mini indexes code/text or describes each image at Add time when enabled.
  Descriptions are derived index data and never replace returned source evidence.
- Every Add is prepared before its transaction. No partial batch is acknowledged.
  A committed identical retry invokes no model and adds no duplicate records.
  Failed, uncommitted Add retries can still repeat external model charges.

### Memory retrieval

Chronicle's unchanged engine supplies lexical/entity/time candidates. An extra
FTS pool of up to 800 candidates permits feature reranking beyond the output
Top-100. Code scoring combines query term coverage, exact symbols/errors/paths,
language aliases and a small discount for query-weak repeated/progress noise.
Failure traces remain searchable.

For image queries, Vision scans scoped feature metadata, then unions up to
1,000 visual candidates with textual candidates. Image similarity uses exact
file SHA256, dHash and RGB histogram intersection. These are appearance
heuristics, **not semantic embeddings or calibrated probabilities**.
Text questions retrieve Add captions through FTS. Search uses no GPT call.

## Installation

Publish this directory as the root of your new repository. Replace YOUR_REPO
with its real public URL; no repository has been created automatically.

~~~bash
git clone YOUR_REPO Chronicle-Memory-Tracks
cd Chronicle-Memory-Tracks
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -B -m unittest discover -s tests -v
~~~

Python 3.10+; Docker targets 3.11. Local tests have used Python 3.10.21 and 3.14.6
with Pillow 12.3.0. SQLite needs FTS5 for the primary lexical path; the inherited
core has a fallback, but formal builds should check FTS5 explicitly.
No GPU, FAISS, numpy, model weights or OpenAI SDK is required.

## Running locally, no model fees

In Linux/WSL, start Coding:

~~~bash
export MEMORY_TRACK=coding MODEL_PROVIDER=none HOST=127.0.0.1 PORT=8001
export MEMORY_DB_PATH=data/coding-local.sqlite3
export MEMORY_API_KEY=local-test-only
export PRODUCTION=0
python -B app.py
~~~

In a separate terminal:

~~~bash
export MEMORY_API_KEY=local-test-only
python -B scripts/smoke.py --base-url http://127.0.0.1:8001 --track coding --state state-coding.json --check-auth
~~~

For Vision use MEMORY_TRACK=multimodal, PORT=8002 and a separate DB; pass
--track multimodal to smoke.py. Restart each service with the same configuration
and DB, then run --phase read with its same state file, without another Add.
.env.example is a template; app.py does not load dotenv files automatically.

## API

GET /health is public. POST /add and POST /search accept Authorization: Token,
Authorization: Bearer, or X-Api-Key using MEMORY_API_KEY. Production requires
a nonempty key and MODEL_PROVIDER=openai. JSON Schemas are in [config](config/).

~~~json
{"request_id":"example-1","user_id":"example-user","session_id":"session-1",
 "messages":[{"role":"user","content":"src/config.py: parseConfig raised ConfigError.",
              "timestamp":1790985600000}]}
~~~

Add response:

~~~json
{"success":true,"request_id":"example-1","user_id":"example-user","session_id":"session-1"}
~~~

Search request:

~~~json
{"query":"Which parseConfig issue occurred?","user_id":"example-user","top_k":100}
~~~

Search response contains data with stable id, original content, numeric ranking
score and ISO created_at. Empty result is {"data":[]}. Multimodal content/query
can instead be ordered arrays of text and image_url parts.
The caller's timestamp is Unix milliseconds; ingestion time is never substituted
for a missing source timestamp in returned provenance.

422: invalid payload; 409: reused request_id with changed content/session;
413: capacity/media limit; 429: active request slots full; 503: index model failure
or budget exhaustion; 408: request read timeout. No Add is acknowledged before commit.

## Deployment and model budget

Render is optional: Docker can run on a Linux VPS with a HTTPS reverse proxy too.
render.yaml defines two **paid** services and independent persistent disks.
Its initial 0.5c-512mb/1GB settings are examples, not validated Full capacity.
Bootstrap limits directory ownership changes to /app/data and drops privileges
to UID/GID 10001 before starting the service.

Deployment variables: MEMORY_TRACK, MEMORY_DB_PATH, MEMORY_API_KEY,
MODEL_PROVIDER=openai, OPENAI_API_KEY, ADD_MODEL=gpt-4o-mini, PRODUCTION=1.
Keep keys in your host's private environment settings. Different model, track,
processor or retrieval configuration requires a new DB and a declared version.
Freeze the Git commit and disable auto-deployment during formal evaluation.

## Evaluation and ablation

~~~bash
python -B -m unittest discover -s tests -v
python -B evaluation/coding_diagnostic.py --output artifacts/private/coding-diagnostic.json
python -B scripts/estimate_cost.py --code-chunks 1000 --images 1000 --width 1024 --height 1024 --detail high
~~~


## License and acknowledgement

MIT, under the repository owner's confirmed reuse authorization.
See [LICENSE](LICENSE) and [ATTRIBUTION](ATTRIBUTION.md).
Chronicle's original authors, AML's published contract, and Pillow are acknowledged.
