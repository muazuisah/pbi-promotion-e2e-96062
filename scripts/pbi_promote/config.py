"""Loads and validates config/teams.json and config/governance.json."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

ENVIRONMENTS = ("dev", "test", "prod")
PLACEHOLDER_GUID = "00000000-0000-0000-0000-000000000000"
GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
TEAM_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
FOLDER_MODES = ("preserve", "unit")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Team:
    key: str
    display_name: str
    reviewers: str
    workspaces: dict[str, str]

    def workspace_id(self, env: str, *, allow_placeholder: bool = False) -> str:
        if env not in self.workspaces:
            raise ConfigError(f"Team '{self.key}' has no workspace configured for environment '{env}'.")
        ws = self.workspaces[env]
        if ws == PLACEHOLDER_GUID and not allow_placeholder:
            raise ConfigError(
                f"Team '{self.key}' still uses the placeholder workspace ID for '{env}'. "
                "Set the real workspace GUID in config/teams.json before deploying."
            )
        return ws


@dataclass(frozen=True)
class Governance:
    branch_pattern: re.Pattern
    title_pattern: re.Pattern
    exempt_branch_patterns: tuple[re.Pattern, ...]
    max_teams_per_pull_request: int
    allow_mixed_platform_and_team_changes: bool
    src_root: str
    workspace_folder_mode: str
    item_types: tuple[str, ...]
    forbidden_file_patterns: tuple[re.Pattern, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Config:
    teams: dict[str, Team]
    governance: Governance
    root: Path

    def team(self, key: str) -> Team:
        if key not in self.teams:
            raise ConfigError(f"Team '{key}' is not defined in config/teams.json.")
        return self.teams[key]


def _read_json(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8-sig") as fh:
            return json.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"Missing configuration file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc


def _compile(pattern: str, name: str, require_group: bool = False) -> re.Pattern:
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        raise ConfigError(f"governance.json '{name}' is not a valid regex: {exc}") from exc
    if require_group and "workItem" not in rx.groupindex:
        raise ConfigError(f"governance.json '{name}' must contain a named group (?P<workItem>...).")
    return rx


def load_teams(path: Path) -> dict[str, Team]:
    data = _read_json(path)
    raw = data.get("teams")
    if not isinstance(raw, dict) or not raw:
        raise ConfigError(f"{path} must contain a non-empty 'teams' object.")
    teams: dict[str, Team] = {}
    for key, value in raw.items():
        if not TEAM_KEY_RE.match(key):
            raise ConfigError(
                f"Team key '{key}' must be lowercase letters, digits and hyphens (it must equal the folder under src/)."
            )
        if not isinstance(value, dict):
            raise ConfigError(f"Team '{key}' must be an object.")
        workspaces = value.get("workspaces") or {}
        for env in ENVIRONMENTS:
            ws = workspaces.get(env)
            if not isinstance(ws, str) or not GUID_RE.match(ws):
                raise ConfigError(f"Team '{key}' workspaces.{env} must be a workspace GUID (got {ws!r}).")
        reviewers = value.get("reviewers", "")
        if not isinstance(reviewers, str) or not re.match(r"^@[\w.-]+(/[\w.-]+)?$", reviewers):
            raise ConfigError(f"Team '{key}' reviewers must be a GitHub user or team handle such as @org/team.")
        teams[key] = Team(
            key=key,
            display_name=str(value.get("displayName") or key),
            reviewers=reviewers,
            workspaces={env: workspaces[env].lower() for env in ENVIRONMENTS},
        )
    return teams


def load_governance(path: Path) -> Governance:
    data = _read_json(path)
    max_teams = data.get("maxTeamsPerPullRequest", 1)
    if not isinstance(max_teams, int) or max_teams < 1:
        raise ConfigError("governance.json 'maxTeamsPerPullRequest' must be an integer >= 1.")
    src_root = str(data.get("srcRoot", "src")).strip("/")
    if not src_root or "/" in src_root:
        raise ConfigError("governance.json 'srcRoot' must be a single top-level folder name.")
    folder_mode = data.get("workspaceFolderMode", "preserve")
    if folder_mode not in FOLDER_MODES:
        raise ConfigError(f"governance.json 'workspaceFolderMode' must be one of {FOLDER_MODES}.")
    item_types = tuple(data.get("itemTypes", ["SemanticModel", "Report"]))
    if not item_types or any(t not in ("SemanticModel", "Report") for t in item_types):
        raise ConfigError("governance.json 'itemTypes' may only contain 'SemanticModel' and 'Report'.")
    return Governance(
        branch_pattern=_compile(data["branchNamePattern"], "branchNamePattern", True),
        title_pattern=_compile(data["pullRequestTitlePattern"], "pullRequestTitlePattern", True),
        exempt_branch_patterns=tuple(
            _compile(p, "exemptBranchPatterns") for p in data.get("exemptBranchPatterns", [])
        ),
        max_teams_per_pull_request=max_teams,
        allow_mixed_platform_and_team_changes=bool(data.get("allowMixedPlatformAndTeamChanges", False)),
        src_root=src_root,
        workspace_folder_mode=folder_mode,
        item_types=item_types,
        forbidden_file_patterns=tuple(
            _compile(p, "forbiddenFilePatterns") for p in data.get("forbiddenFilePatterns", [])
        ),
    )


def load_config(config_root: Path) -> Config:
    # Absolute, because fabric-cicd resolves relative parameter paths against its staging folder.
    config_root = Path(config_root).resolve()
    try:
        governance = load_governance(config_root / "governance.json")
    except KeyError as exc:
        raise ConfigError(f"governance.json is missing required key {exc}.") from exc
    return Config(teams=load_teams(config_root / "teams.json"), governance=governance, root=config_root)
