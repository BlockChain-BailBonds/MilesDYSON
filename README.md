# DYSON — Markdown Tuning Panels 🎚️

**918 Technologies · Public repository edition · v0.1.0**

DYSON turns **declarative tuning specifications written in Jupyter Markdown** into executable, interactive Python widget panels. Sliders are backed by a persistent, typed configuration object that the model, solver, or other code can read on subsequent cells.

**This is not decorative HTML.** `IntSlider` and `FloatSlider` widgets change saved values; `get(panel, key)` delivers those values to Python runtime code.

## What actually works

- Markdown cells contain fenced `dyson` JSON specifications; the compiler preserves the original declaration in notebook cell metadata, hides its verbose JSON in the compiled view, and inserts an interactive widget cell immediately afterward.
- Real draggable **integer/float sliders**, on/off toggles, and dropdowns via `ipywidgets`.
- Slider changes persist atomically to `.dyson/settings.json` when released; **Apply / Save** and **Reset panel** buttons are available.
- Input typing, bounds, increments, nonfinite-number rejection, duplicate-name detection, and persisted-value validation.
- Repeatable notebook compilation without duplicating widget cells.
- Integration from later code cells with `get("panel_id", "key")` and `snapshot()`.

### Platform boundaries

**Jupyter Notebook / JupyterLab**: works when the notebook frontend supports `ipywidgets` and widget managers.

**Kaggle / Colab / VS Code notebooks**: can work when their specific kernels/frontends support the installed widget protocol; platform-specific testing is still necessary.

**GitHub Markdown, plain Markdown previewers, README renderers**: show the `dyson` schema as static text/code, not live Python-bound sliders. Executing sliders directly *inside an arbitrary rendered Markdown cell on every platform* needs each platform's extension or adapter. This repository does not claim such universal support yet.

## Quick start

```bash
python -m pip install -e .
dyson-markdown validate examples/dyson_source.ipynb
dyson-markdown compile examples/dyson_source.ipynb --output examples/dyson_compiled.ipynb
```

Open `examples/dyson_compiled.ipynb` in Jupyter and **Run All**, or run each generated control cell before the consuming code. The compiled view shows the Markdown heading followed by **actual sliders**; the JSON declaration stays in the source notebook and the compiled cell metadata for lossless recompilation. Drag the sliders; the values are saved and read by the example's subsequent cells.

### Markdown declaration

Put this in a Markdown cell (including its backtick fences):

````markdown
## Qwen model tuning

```dyson
{
  "id": "inference",
  "title": "Qwen inference settings",
  "controls": [
    {"key": "temperature", "label": "Temperature", "type": "float", "min": 0, "max": 2, "step": 0.05, "default": 0.65},
    {"key": "draft_tokens", "label": "Speculative draft tokens", "type": "int", "min": 1, "max": 32, "step": 1, "default": 8},
    {"key": "speculative", "label": "Use draft model", "type": "bool", "default": true}
  ]
}
```
````

Then compile your notebook:

```bash
dyson-markdown compile original.ipynb -o dyson_enabled.ipynb
```

Code cells below can consume your actual settings:

```python
from dyson_markdown import get, snapshot

sampling_temperature = get("inference", "temperature")
draft_token_count = get("inference", "draft_tokens")
speculative_enabled = get("inference", "speculative")
print(snapshot("inference"))
```

These variables must be passed to a **compatible serving/model adapter**; merely changing a slider cannot magically reconfigure a running model instance. Live model servers may need restart or configuration reload before changes take effect.

## Run integration

| Parameter | UI control | Effect |
| --- | --- | --- |
| Floating-point range | `FloatSlider` | Validated floating-point setting |
| Integer range | `IntSlider` | Validated integer setting |
| Boolean | `ToggleButton` | On/off feature gate |
| Enumerated options | `Dropdown` | Select an available backend or mode |

For Kaggle use, add the package source to the notebook's available files (or its wheelhouse), install it in the running environment, compile the notebook locally before uploading, and use a writable path for settings. To override settings persistence:

```python
import os
os.environ["DYSON_STATE_PATH"] = "/kaggle/working/dyson_settings.json"
```

Set that environment variable **before importing or initializing** DYSON. This does not bypass Kaggle network restrictions, competition policies, GPU resource limits, model-availability requirements, or the official scoring protocol.

## Repository contents

| Location | Purpose |
| --- | --- |
| `src/dyson_markdown/core.py` | Typed schema, Markdown parser, persistent settings |
| `src/dyson_markdown/widgets.py` | Actual interactive widgets and live value observers |
| `src/dyson_markdown/notebook.py` | Safe, idempotent Markdown-to-widget notebook compilation |
| `src/dyson_markdown/cli.py` | Validate/compile command line |
| `examples/dyson_source.ipynb` | Editable Markdown declarations and consuming Python cells |
| `examples/dyson_compiled.ipynb` | Runnable notebook with interactive panels |
| `tests/` | Validation, widget wiring, persistence, and compilation tests |
| `.github/workflows/tests.yml` | Automated test workflow |

## Quality checks

```bash
python -m pip install -e '.[dev]'
ruff check src tests
pytest -q
```

There is no fabricated ARC-AGI score or benchmark attached. Software tests validate widget behavior; competition scores can only be established by executing a genuine competition run.

## Security and ownership

All user-supplied declarations are read as **JSON**, not as executable Python; no `eval` or injected JavaScript is needed. Persisted settings are validated before use. Never put secrets into Markdown panel definitions or committed notebook outputs.

**Copyright © 2026 918 Technologies. All rights reserved.** Public visibility is not permission to reuse or distribute the source code. See [NOTICE.md](NOTICE.md). No open-source license has been granted.