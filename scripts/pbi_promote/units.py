"""Maps repository paths to promotion units.

A promotion unit is the folder src/<team>/<unit>/. It holds one report and its
semantic model, or a group of reports that share a model or feed one dashboard.
Units are promoted as a whole, so a report never moves without its model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

UNIT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
ITEM_SUFFIXES = (".Report", ".SemanticModel")


class SelectorError(Exception):
    pass


@dataclass
class ChangeSet:
    units: set[str] = field(default_factory=set)
    teams: set[str] = field(default_factory=set)
    platform_paths: list[str] = field(default_factory=list)
    team_level_paths: list[str] = field(default_factory=list)

    @property
    def has_team_changes(self) -> bool:
        return bool(self.teams)


def classify_paths(paths: list[str], src_root: str = "src") -> ChangeSet:
    """Split changed paths into units, teams and platform (everything outside src/)."""
    cs = ChangeSet()
    prefix = f"{src_root}/"
    for raw in paths:
        path = raw.replace("\\", "/")
        if not path.startswith(prefix):
            cs.platform_paths.append(path)
            continue
        parts = path[len(prefix):].split("/")
        if len(parts) == 1:
            # A file directly under src/ (for example src/README.md) is centrally owned.
            cs.platform_paths.append(path)
            continue
        team = parts[0]
        cs.teams.add(team)
        if len(parts) == 2:
            cs.team_level_paths.append(path)
            continue
        cs.units.add(f"{team}/{parts[1]}")
    return cs


def discover_units(repo_root: Path, src_root: str = "src") -> dict[str, Path]:
    """Return every unit folder that exists on disk, keyed by 'team/unit'."""
    base = Path(repo_root) / src_root
    units: dict[str, Path] = {}
    if not base.is_dir():
        return units
    for team_dir in sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")):
        for unit_dir in sorted(p for p in team_dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
            units[f"{team_dir.name}/{unit_dir.name}"] = unit_dir
    return units


def find_items(unit_dir: Path) -> list[Path]:
    """Return item folders (folders that contain a .platform file) inside a unit."""
    items: list[Path] = []
    stack = [unit_dir]
    while stack:
        current = stack.pop()
        if (current / ".platform").is_file() and current != unit_dir:
            items.append(current)
            continue
        stack.extend(p for p in current.iterdir() if p.is_dir() and not p.name.startswith("."))
    return sorted(items)


def parse_selector(text: str) -> list[str]:
    tokens = [t for t in re.split(r"[\s,;]+", text or "") if t]
    for token in tokens:
        if not re.match(r"^[a-z0-9][a-z0-9-]*/(\*|[a-z0-9][a-z0-9-]*)$", token):
            raise SelectorError(
                f"Invalid unit selector '{token}'. Use 'team/unit' or 'team/*' "
                "(lowercase letters, digits and hyphens), separated by commas."
            )
    return tokens


def resolve_selector(text: str, available: dict[str, Path]) -> list[str]:
    """Expand 'team/unit' and 'team/*' selectors against units on disk."""
    selected: set[str] = set()
    tokens = parse_selector(text)
    if not tokens:
        raise SelectorError("No units were selected.")
    for token in tokens:
        team, unit = token.split("/")
        if unit == "*":
            matches = {k for k in available if k.split("/")[0] == team}
            if not matches:
                raise SelectorError(f"Selector '{token}' matched no units under src/{team}/.")
            selected |= matches
        else:
            if token not in available:
                raise SelectorError(f"Unit '{token}' does not exist (expected folder src/{token}/).")
            selected.add(token)
    return sorted(selected)


def team_of(unit_key: str) -> str:
    return unit_key.split("/", 1)[0]
