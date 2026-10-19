import json
from pathlib import Path

import nbformat
import pytest

from dyson_markdown import (
    DysonError,
    StateStore,
    compile_notebook,
    extract_panels,
    validate_notebook,
    validate_panel,
)

PANEL = {
    "id": "inference",
    "title": "Qwen target/draft control",
    "controls": [
        {"key": "temperature", "label": "Temperature", "type": "float", "min": 0.0,
         "max": 2.0, "step": 0.05, "default": 0.65},
        {"key": "draft_tokens", "label": "Draft tokens", "type": "int", "min": 1,
         "max": 32, "step": 1, "default": 8},
        {"key": "enabled", "label": "Speculative decoding", "type": "bool", "default": True},
        {"key": "backend", "label": "Backend", "type": "choice",
         "options": ["sglang", "vllm"], "default": "sglang"},
    ],
}


def test_extract_and_validation():
    doc = "# Heading\n\n```dyson\n" + json.dumps(PANEL) + "\n```\n\nOther content"
    results = extract_panels(doc)
    assert len(results) == 1
    assert results[0]["id"] == "inference"
    assert not extract_panels("```python\npass\n```")


def test_reject_invalid_schema():
    wrong = dict(PANEL)
    wrong["id"] = "../../bad"
    with pytest.raises(DysonError):
        validate_panel(wrong)
    repeated = {"id": "a", "controls": [PANEL["controls"][0], PANEL["controls"][0]]}
    with pytest.raises(DysonError, match="duplicate"):
        validate_panel(repeated)
    with pytest.raises(DysonError):
        extract_panels("```dyson\n{bad JSON}\n```")


def test_store_persists_across_process_reinstantiation(tmp_path: Path):
    path = tmp_path / "deep" / "dyson.json"
    st = StateStore(path)
    st.register(PANEL)
    st.set("inference", "temperature", 1.2)
    st.set("inference", "enabled", False)
    st.set("inference", "backend", "vllm")
    assert st.get("inference", "temperature") == 1.2
    raw = json.loads(path.read_text())
    assert raw["version"] == 1
    assert raw["values"]["inference"]["enabled"] is False
    second = StateStore(path)
    assert second.register(PANEL)["backend"] == "vllm"
    assert second.get("inference", "temperature") == 1.2
    second.reset("inference")
    assert second.get("inference", "temperature") == 0.65


def test_invalid_slider_values_leave_state_unchanged(tmp_path: Path):
    state = StateStore(tmp_path / "state.json")
    state.register(PANEL)
    for key, value in [("temperature", 9.0), ("temperature", float("nan")),
                       ("temperature", 0.68), ("draft_tokens", 1.5),
                       ("enabled", 1), ("backend", "unknown")]:
        with pytest.raises(DysonError):
            state.set("inference", key, value)
    assert state.snapshot("inference")["temperature"] == 0.65


def test_compile_idempotent_and_keeps_markdown(tmp_path: Path):
    src, compiled, second = (tmp_path / f for f in ("src.ipynb", "out.ipynb", "out2.ipynb"))
    markdown = "# DYSON\n```dyson\n" + json.dumps(PANEL) + "\n```"
    book = nbformat.v4.new_notebook(cells=[nbformat.v4.new_markdown_cell(markdown),
                                            nbformat.v4.new_code_cell("from dyson_markdown import get\nprint(get('inference', 'draft_tokens'))")])
    nbformat.write(book, src)
    assert validate_notebook(src)["panels"] == 1
    assert compile_notebook(src, compiled)["panels"] == 1
    c = nbformat.read(compiled, as_version=4)
    assert len(c.cells) == 3
    assert "```dyson" not in c.cells[0].source
    assert c.cells[0].metadata["dyson"]["source_markdown"] == markdown
    assert "_dyson_display_panel" in c.cells[1].source
    assert c.cells[2].source == book.cells[1].source
    assert compile_notebook(compiled, second)["panels"] == 1
    assert len(nbformat.read(second, as_version=4).cells) == 3
    with pytest.raises(DysonError):
        compile_notebook(src, src)


def test_duplicate_panel_id_rejected(tmp_path: Path):
    markdown = "```dyson\n" + json.dumps(PANEL) + "\n```"
    book = nbformat.v4.new_notebook(cells=[nbformat.v4.new_markdown_cell(markdown),
                                            nbformat.v4.new_markdown_cell(markdown)])
    src = tmp_path / "dup.ipynb"
    nbformat.write(book, src)
    with pytest.raises(DysonError, match="duplicate"):
        validate_notebook(src)


def test_widget_types_and_live_persistence(tmp_path: Path):
    widgets = pytest.importorskip("ipywidgets")
    from dyson_markdown import render
    state = StateStore(tmp_path / "saved.json")
    view = render(PANEL, state=state)
    controls = view._dyson_controls
    assert isinstance(controls["temperature"], widgets.FloatSlider)
    assert isinstance(controls["draft_tokens"], widgets.IntSlider)
    assert isinstance(controls["enabled"], widgets.ToggleButton)
    assert isinstance(controls["backend"], widgets.Dropdown)
    controls["temperature"].value = 1.2
    assert state.get("inference", "temperature") == 1.2
    assert json.loads(state.path.read_text())["values"]["inference"]["temperature"] == 1.2
    controls["enabled"].value = False
    assert controls["enabled"].description == "OFF"
    assert state.get("inference", "enabled") is False
