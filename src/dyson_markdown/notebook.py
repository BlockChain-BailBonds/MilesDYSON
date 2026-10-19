"""Transform Markdown declarations into adjacent, executable widget cells."""

from __future__ import annotations

import json
from pathlib import Path
import nbformat

from .core import DysonError, _PATTERN, extract_panels

_AUTO_TAG = "dyson-generated-widget"


def compile_notebook(source: str | Path, destination: str | Path) -> dict[str, Any]:
    """Preserve Markdown verbatim; insert widget cells immediately after declarations.

    Idempotent: previously injected widget cells are replaced on recompilation.
    Does not execute source notebook or its model/game code.
    """
    source_path = Path(source)
    destination_path = Path(destination)
    if source_path.resolve() == destination_path.resolve():
        raise DysonError("write to a separate output path to protect the original notebook")
    book = nbformat.read(source_path, as_version=4)
    updated = []
    seen: set[str] = set()
    declaration_count = 0
    for cell in book.cells:
        if _AUTO_TAG in cell.metadata.get("tags", []):
            continue
        updated.append(cell)
        if cell.cell_type != "markdown":
            continue
        original = cell.metadata.get("dyson", {}).get("source_markdown", cell.source)
        panels = extract_panels(original)
        if panels:
            cell.metadata["dyson"] = {"source_markdown": original}
            cell.source = _PATTERN.sub(
                "**🎚️ DYSON live sliders appear directly below this Markdown cell.**",
                original,
            )
        for spec in panels:
            if spec["id"] in seen:
                raise DysonError(f"duplicate panel id in notebook: {spec['id']}")
            seen.add(spec["id"])
            data = json.dumps(spec, separators=(",", ":"), ensure_ascii=False)
            code = ("import json as _dyson_json\n"
                    "from dyson_markdown import display_panel as _dyson_display_panel\n"
                    f"_dyson_display_panel(_dyson_json.loads({data!r}))\n")
            generated = nbformat.v4.new_code_cell(code)
            generated.metadata["tags"] = [_AUTO_TAG]
            updated.append(generated)
            declaration_count += 1
    book.cells = updated
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    nbformat.validate(book)
    nbformat.write(book, destination_path)
    return {"panels": declaration_count, "output": str(destination_path)}


def validate_notebook(path: str | Path) -> dict[str, Any]:
    book = nbformat.read(path, as_version=4)
    nbformat.validate(book)
    seen: set[str] = set()
    count = 0
    for cell in book.cells:
        if cell.cell_type != "markdown":
            continue
        original = cell.metadata.get("dyson", {}).get("source_markdown", cell.source)
        for panel in extract_panels(original):
            if panel["id"] in seen:
                raise DysonError(f"duplicate panel id: {panel['id']}")
            seen.add(panel["id"])
            count += 1
    return {"panels": count, "notebook": str(path)}
