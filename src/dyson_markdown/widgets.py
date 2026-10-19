"""ipywidgets slider rendering; controls update StateStore on real interactions."""

from __future__ import annotations

from html import escape
from typing import Any

from .core import StateStore, validate_panel

_DEFAULT_STORE: StateStore | None = None


def store() -> StateStore:
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = StateStore()
    return _DEFAULT_STORE


def get(panel_id: str, key: str) -> Any:
    return store().get(panel_id, key)


def snapshot(panel_id: str | None = None) -> dict[str, Any]:
    return store().snapshot(panel_id)


def render(spec: dict[str, Any], state: StateStore | None = None) -> Any:
    """Return live slider widgets; display with IPython.display.display(render(spec))."""
    try:
        import ipywidgets as widgets
    except ImportError as exc:
        raise RuntimeError("Install ipywidgets>=8 to render DYSON interactive controls") from exc
    state = state or store()
    schema = validate_panel(spec)
    current = state.register(schema)
    rows = []
    controls = {}
    feedback = widgets.HTML(value="<small>Slider changes are saved when released.</small>")
    syncing = {"active": False}

    def repaint() -> None:
        syncing["active"] = True
        try:
            for key, control in controls.items():
                expected = state.get(schema["id"], key)
                if control.value != expected:
                    control.value = expected
                if isinstance(control, widgets.ToggleButton):
                    control.description = "ON" if control.value else "OFF"
                    control.button_style = "success" if control.value else ""
        finally:
            syncing["active"] = False

    for item in schema["controls"]:
        key, kind = item["key"], item["type"]
        desc = widgets.HTML(value="<b>" + escape(item["label"]) + "</b>",
                            layout=widgets.Layout(width="220px"))
        if kind == "int":
            control = widgets.IntSlider(value=current[key], min=item["min"], max=item["max"],
                                        step=item["step"], continuous_update=False,
                                        layout=widgets.Layout(width="420px"))
        elif kind == "float":
            control = widgets.FloatSlider(value=current[key], min=item["min"], max=item["max"],
                                          step=item["step"], readout_format=".3f",
                                          continuous_update=False, layout=widgets.Layout(width="420px"))
        elif kind == "bool":
            control = widgets.ToggleButton(value=current[key], description="ON" if current[key] else "OFF",
                                           button_style="success" if current[key] else "",
                                           layout=widgets.Layout(width="95px"))
        elif kind == "choice":
            control = widgets.Dropdown(options=item["options"], value=current[key],
                                       layout=widgets.Layout(width="420px"))
        else:
            raise AssertionError(f"validated schema contained unsupported type {kind}")
        controls[key] = control
        rows.append(widgets.HBox([desc, control], layout=widgets.Layout(align_items="center")))

        def on_change(change: dict[str, Any], field: str = key) -> None:
            if syncing["active"] or change.get("name") != "value":
                return
            try:
                state.set(schema["id"], field, change["new"])
                feedback.value = "<small>✓ Saved " + escape(field) + "</small>"
            except (ValueError, OSError) as exc:
                feedback.value = "<small>Rejected: " + escape(str(exc)) + "</small>"
            finally:
                repaint()
        control.observe(on_change, names="value")

    reset = widgets.Button(description="Reset panel", icon="refresh")
    apply_button = widgets.Button(description="Apply / Save", icon="check", button_style="info")

    def on_reset(_button: Any) -> None:
        try:
            state.reset(schema["id"])
            feedback.value = "<small>✓ Defaults restored and saved</small>"
        except (ValueError, OSError) as exc:
            feedback.value = "<small>Reset failed: " + escape(str(exc)) + "</small>"
        finally:
            repaint()

    def on_save(_button: Any) -> None:
        try:
            state.save()
            feedback.value = "<small>✓ Configuration written to " + escape(str(state.path)) + "</small>"
        except OSError as exc:
            feedback.value = "<small>Save failed: " + escape(str(exc)) + "</small>"

    reset.on_click(on_reset)
    apply_button.on_click(on_save)
    view = widgets.VBox([widgets.HTML(value="<h3>🎚️ " + escape(schema["title"]) + "</h3>"),
                         *rows, widgets.HBox([apply_button, reset]), feedback])
    view._dyson_controls = controls
    return view


def display_panel(spec: dict[str, Any], state: StateStore | None = None) -> Any:
    """Register panel and display its widgets in the current notebook output."""
    from IPython.display import display
    view = render(spec, state=state)
    display(view)
    return view
