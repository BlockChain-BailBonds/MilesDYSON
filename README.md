# DYSON — Standalone Model Control

**918 Technologies · v0.2.0**

DYSON runs as an independent local application. Its browser dashboard, authenticated API,
CLI, configuration store, telemetry, and adaptive inference controller work without
Kaggle, ARC, Jupyter, or the SuperDuper harness. Notebook panels remain an optional integration.

The standalone core uses Python's standard library and has no required third-party
runtime dependencies. An actual local model backend is required for inference.
DYSON does not include model weights or a llama.cpp binary.

## Install and launch

Requires Python 3.10 or newer. From this repository:

```bash
python -m pip install .
dyson serve
```

Open **http://127.0.0.1:9180**. The dashboard starts independently of the model backend.
It will report a backend error until a compatible local model server is available.

The distribution retains its existing package name,
`dyson-markdown-tuning-panels`, for compatibility. The standalone command is `dyson`.

### Connect an existing local backend

Use a local server implementing `GET /v1/models` and
`POST /v1/chat/completions`, such as llama-server. Stop DYSON before configuring
from the CLI, or apply changes through the dashboard while it is running.

```bash
dyson config --set '{"endpoint":"http://127.0.0.1:8080/v1","model":"my-local-model"}'
dyson serve
```

Endpoint URLs must use loopback HTTP with an explicit port and `/v1`.
The backend must be configured without API authentication. DYSON's own API has
separate token authentication.

### Let DYSON start and own llama-server

Supply an installed executable and your actual local GGUF model:

```bash
dyson serve --llama-server /opt/llama.cpp/build/bin/llama-server --model-path /models/model.gguf --gpu-layers 0
```

For a multimodal model, add `--mmproj /models/mmproj.gguf`.
For a compatible CUDA build and available GPU memory, set the desired
`--gpu-layers`. CPU is the default. Model compatibility, required RAM/VRAM, and
context capacity depend on your model and llama.cpp build.

The model process binds to 127.0.0.1:8080 with alias `dyson-local`.
Use `--backend-port` to change that port and `--context-size` to change the
startup context allocation. Split GGUFs must have all shards together; pass the
first shard. Nothing is downloaded automatically.

DYSON waits for model readiness and stops its owned model process on shutdown.
A model process you started separately stays under your control.
Startup output is written to `llama-server.log` in the state directory.
Backend startup options require restarting the owned model process;
sampling controls apply on the next request.

## What the equalizer does

The standalone controller preserves the notebook v10 policy's four modes.
Each session keeps separate observed outcomes and measured request latencies.

| Mode | Evidence | Effect |
| --- | --- | --- |
| BASELINE | Insufficient or inconclusive evidence | Original configured request parameters |
| RECOVERY | Consecutive observed no-ops and low progress rate | Restore the full configured output budget |
| MOMENTUM | At least 70% recent observed progress and at least three positive outcomes | Reduce output budget using progress multiplier |
| ECONOMY | At least three measured slow requests and mixed, insufficient progress | Reduce output budget using economy multiplier |

The default controller needs four known outcomes and enforces a three-request
cooldown between mode changes. It considers a rolling window of twelve outcomes.
Unknown outcomes are counted separately and never treated as no-ops.
Explicit level changes clear old-level evidence and restore BASELINE.
Disabling adaptation preserves configured parameters.

Only the output-token budget changes automatically in this version.
Temperature, top-p, seed, and the maximum budget remain explicit user settings.
The controller does not mutate model weights, impose gameplay action limits,
or infer task success from fluent text. Reduced tokens are an efficiency heuristic;
improved accuracy or score is not established by the implementation.

Use the dashboard's **Observed progress**, **Observed no change**, or
**Outcome unknown** buttons after inspecting an actual outcome. Application
adapters can report those outcomes through the API. Caller-reported observations
are labeled as such; they are not official scores.

The generic standalone controller does not include the ARC harness's prompt
injection hooks or reasoning/deadline overrides. Those belong in an ARC adapter,
which may use the API without making ARC a dependency.

## CLI

```bash
dyson --help
dyson config
dyson config --set '{"temperature":0.25,"max_tokens":2048}'
dyson serve --port 9180
dyson doctor
dyson chat "Explain the difference between evidence and a prediction."
dyson status
```

Use `--state-dir /path/to/state` **before** the subcommand or set `DYSON_HOME`.
The default is `~/.dyson`. Client commands accept `--port` after their subcommand.
The same state directory and API port must be used by service and clients.

Only one process may own a state directory. While the service is running,
update configuration through the dashboard, Python client, or HTTP API.
CLI errors return exit status 2.

## Python / notebook / ARC adapter

Start DYSON once, then connect from any Python application:

```python
from pathlib import Path
from dyson.client import DysonClient

client = DysonClient(token_file=Path.home() / ".dyson" / "api-token")
result = client.chat(
    [{"role": "user", "content": "Inspect the current task."}],
    session_id="my-agent",
)
print(result["message"])
print(result["parameters"], result["settings_revision"])

# Only report a change after your application observes a real outcome.
observed_change = None
client.observe(result["request_id"], changed=observed_change, level="level-1")
```

Text, multimodal image messages, and tool definitions can be forwarded to a
capable backend. Tool calls are returned to the caller; DYSON never executes
model-requested tools. ARC actions and official scoring remain the adapter's job.

Feedback is accepted once per successful request and must reference its returned
`request_id`. At most 256 recent requests can await feedback. Newer feedback in
a session supersedes older outstanding feedback from that session, preventing
delayed observations from reversing level order. Reconfiguring DYSON clears
pending feedback and adaptive evidence. Settings persist across service restarts;
adaptive evidence intentionally starts fresh.

## Local HTTP API

All API requests require `Authorization: Bearer TOKEN` or
`X-Dyson-Token: TOKEN`. Read the private token from the state directory's
`api-token` file. The token changes on service restart. The dashboard supplies it
automatically.

| Method | Route | Body / result |
| --- | --- | --- |
| GET | /api/status | Settings revision, session modes, recent telemetry |
| GET | /api/health | Real backend model-list probe |
| POST | /api/config | `{"revision":0,"values":{"temperature":0.25}}` |
| POST | /api/chat | `{"session_id":"agent","messages":[{"role":"user","content":"Hello"}]}` |
| POST | /api/feedback | `{"request_id":"returned-id","changed":true,"completed":false,"level":"level-1"}` |

A stale configuration revision, duplicate feedback, or busy inference request
returns HTTP 409. Invalid input returns 400; missing authentication returns 401;
invalid Host or cross-origin access returns 403; backend failures return 502.
Only one model inference runs at a time. API request bodies are limited to 2 MiB.
Backend responses are limited to 8 MiB. Session identifiers are bounded to 128
ASCII letters, digits, underscores, periods, colons, and hyphens.

The dashboard binds exclusively to 127.0.0.1 and is intended for personal local
use. Token authentication, origin/Host checks, a content security policy, bounded
connections, and disabled backend redirects protect the local control interface.
The root dashboard page is available without an API token to local browser
users; this is not a multi-user or remotely exposed service.

## Persistence and telemetry

Settings are validated before an atomic replacement and written with private
file permissions. Revisions protect against stale UI writes. A process lock
prevents concurrent writers to the same state directory.

`events.jsonl` records actual request latency, effective parameters, mode,
backend-provided token usage, revisions, and caller-reported outcomes. Prompts,
responses, and bearer tokens are excluded. Missing token usage remains unknown.
The event log rotates at roughly 5 MiB, retaining one previous file.
The dashboard shows up to 100 recent process events.

The managed backend's own log is separate and follows the installed backend's
logging behavior. Do not share it without checking its contents.

## Optional Markdown notebook panels

```bash
python -m pip install '.[notebook]'
dyson-markdown validate examples/dyson_source.ipynb
dyson-markdown compile examples/dyson_source.ipynb -o examples/dyson_compiled.ipynb
```

Notebook controls require a frontend supporting ipywidgets. Static Markdown
renderers remain static. Existing `dyson_markdown` APIs are preserved.
The standalone browser dashboard needs no notebook renderer.

## Verification and packaged downloads

```bash
python -m pip install -e '.[dev]'
ruff check src tests
python -m pytest -q
python -m build
```

For actual browser interaction tests:

```bash
python -m pip install -e '.[dev,ui]'
python -m playwright install chromium
python -m pytest tests/test_dashboard_browser.py -q
```

GitHub Actions runs Python 3.10/3.12 tests, HTTP backend contract tests,
notebook compatibility checks, an isolated wheel install without notebook
dependencies, and a Chromium slider-to-request persistence test. Successful
Python 3.12 builds attach wheel and source distributions to the workflow run.

Contract tests use a clearly labeled test backend. They establish wiring,
persistence, validation, and controller behavior. Real GGUF inference, GPU
compatibility, model accuracy, and official competition scores require a run
with your model and hardware. No benchmark score is bundled or fabricated.

**Copyright © 2026 918 Technologies. All rights reserved.**
See [NOTICE.md](NOTICE.md). Existing ownership terms are unchanged.
