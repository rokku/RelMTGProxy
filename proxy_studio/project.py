"""Project state — the JSON file that's the source of truth between sessions.

See spec §5 for the schema. Atomic writes so a crash mid-save can't corrupt.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECTS_DIR = Path("projects")


@dataclass
class SelectedPrint:
    scryfall_id: str
    set: str
    collector_number: str


@dataclass
class Entry:
    quantity: int
    name: str
    oracle_id: str
    selected_print: SelectedPrint
    layout: str = "normal"
    # "standard" → use bundled MTG back;
    # "face" → use card_faces[1] image of the selected printing.
    back: str = "standard"
    # If set, the front image comes from a user-uploaded file (path relative
    # to the project root) rather than from Scryfall. `selected_print` and
    # `oracle_id` may be empty for these entries.
    custom_image_path: str | None = None


@dataclass
class PageSettings:
    paper: str = "A4"
    grid: tuple[int, int] = (3, 3)
    cut_line_mode: str = "full-bleed-extensions"


@dataclass
class Project:
    name: str
    created: str = field(default_factory=lambda: datetime.now(timezone.utc)
                          .strftime("%Y-%m-%dT%H:%M:%SZ"))
    page_settings: PageSettings = field(default_factory=PageSettings)
    entries: list[Entry] = field(default_factory=list)

    # --- I/O ---------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # `grid` is a tuple → JSON needs a list.
        d["page_settings"]["grid"] = list(self.page_settings.grid)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Project":
        ps_d = d.get("page_settings", {})
        ps = PageSettings(
            paper=ps_d.get("paper", "A4"),
            grid=tuple(ps_d.get("grid", (3, 3))),  # type: ignore[arg-type]
            cut_line_mode=ps_d.get("cut_line_mode", "full-bleed-extensions"),
        )
        entries = [
            Entry(
                quantity=e["quantity"],
                name=e["name"],
                oracle_id=e.get("oracle_id", ""),
                selected_print=SelectedPrint(**e["selected_print"]),
                layout=e.get("layout", "normal"),
                back=e.get("back", "standard"),
                custom_image_path=e.get("custom_image_path"),
            )
            for e in d.get("entries", [])
        ]
        return cls(name=d["name"], created=d.get("created", ""),
                   page_settings=ps, entries=entries)

    def save(self, projects_dir: str | Path = PROJECTS_DIR) -> Path:
        dir_ = Path(projects_dir)
        dir_.mkdir(parents=True, exist_ok=True)
        path = dir_ / f"{self.name}.json"
        _atomic_write_text(path, json.dumps(self.to_dict(), indent=2))
        return path

    @classmethod
    def load(cls, name: str, projects_dir: str | Path = PROJECTS_DIR) -> "Project":
        path = Path(projects_dir) / f"{name}.json"
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    # --- Mutation helpers --------------------------------------------------
    def add_entry(self, entry: Entry) -> None:
        self.entries.append(entry)

    def set_selection(self, index: int, sel: SelectedPrint, *,
                      layout: str | None = None, back: str | None = None) -> None:
        e = self.entries[index]
        e.selected_print = sel
        if layout is not None:
            e.layout = layout
        if back is not None:
            e.back = back


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
