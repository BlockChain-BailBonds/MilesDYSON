"""Execute real observer callbacks with a faithful fake widget protocol offline."""

import json
import sys
import types
from pathlib import Path

from dyson_markdown import StateStore


class _Widget:
    def __init__(self, *args, **kwargs):
        self._value = kwargs.pop("value", None)
        self.children = args[0] if args else []
        self._callbacks = []
        self._clicks = []
        for key, value in kwargs.items():
            setattr(self, key, value)

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, new):
        old = self._value
        self._value = new
        if old != new:
            for callback in list(self._callbacks):
                callback({"name": "value", "old": old, "new": new})

    def observe(self, fn, names):
        assert names == "value"
        self._callbacks.append(fn)

    def on_click(self, fn):
        self._clicks.append(fn)

    def click(self):
        for fn in self._clicks:
            fn(self)


def test_widgets_are_real_slider_types_and_observe_changes(tmp_path: Path, monkeypatch):
    widget_module = types.ModuleType("ipywidgets")
    for name in ("Layout", "HTML", "IntSlider", "FloatSlider", "ToggleButton",
                 "Dropdown", "Button", "HBox", "VBox"):
        setattr(widget_module, name, type(name, (_Widget,), {}))
    monkeypatch.setitem(sys.modules, "ipywidgets", widget_module)
    from dyson_markdown.widgets import render

    spec = {
        "id": "model",
        "title": "Model controls",
        "controls": [
            {"key": "temp", "type": "float", "min": 0, "max": 2, "step": 0.1, "default": 0.5},
            {"key": "draft", "type": "int", "min": 1, "max": 16, "step": 1, "default": 4},
            {"key": "active", "type": "bool", "default": True},
            {"key": "mode", "type": "choice", "options": ["x", "y"], "default": "x"},
        ],
    }
    state = StateStore(tmp_path / "state.json")
    view = render(spec, state=state)
    controls = view._dyson_controls
    assert isinstance(controls["temp"], widget_module.FloatSlider)
    assert isinstance(controls["draft"], widget_module.IntSlider)
    assert isinstance(controls["active"], widget_module.ToggleButton)
    assert isinstance(controls["mode"], widget_module.Dropdown)
    controls["temp"].value = 1.2
    controls["draft"].value = 12
    controls["active"].value = False
    controls["mode"].value = "y"
    assert state.snapshot("model") == {"temp": 1.2, "draft": 12, "active": False, "mode": "y"}
    assert controls["active"].description == "OFF"
    assert json.loads(state.path.read_text())["values"]["model"]["draft"] == 12

    controls["temp"].value = 1.25
    assert controls["temp"].value == 1.2
    assert state.get("model", "temp") == 1.2
    buttons = view.children[-2].children
    buttons[1].click()
    assert state.get("model", "temp") == 0.5
    assert controls["temp"].value == 0.5
