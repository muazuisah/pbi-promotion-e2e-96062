"""Works out which promotion units to deploy, from a git diff or an explicit selection."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pbi_promote.config import Config, ConfigError
from pbi_promote.findings import Findings
from pbi_promote.gitutil import changed_files
from pbi_promote.units import classify_paths, discover_units, resolve_selector, team_of
from pbi_promote.validate import validate_repository


@dataclass
class Plan:
    source: str
    deploy: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    findings: Findings = field(default_factory=Findings)

    @property
    def teams(self) -> list[str]:
        return sorted({team_of(u) for u in self.deploy})

    def to_markdown(self, title: str) -> str:
        lines = [f"### {title}", "", f"Source: **{self.source}**", ""]
        if self.deploy:
            lines += ["| Team | Unit |", "| --- | --- |"]
            lines += [f"| {team_of(u)} | {u.split('/', 1)[1]} |" for u in self.deploy]
        else:
            lines.append("No promotion units to deploy.")
        if self.removed:
            lines += ["", "Units deleted from Git (not removed from workspaces, retire them manually):"]
            lines += [f"- `{u}`" for u in self.removed]
        if self.findings.items:
            lines += ["", "Findings:"]
            lines += [f"- {f.format()}" for f in self.findings.items]
        return "\n".join(lines) + "\n"


def build_plan(
    repo_root: Path,
    config: Config,
    *,
    base: str | None = None,
    head: str | None = None,
    diff_mode: str = "two-dot",
    selector: str | None = None,
    environments: list[str] | None = None,
    allow_placeholder: bool = False,
    enforce_team_limit: bool = False,
) -> Plan:
    repo_root = Path(repo_root)
    gov = config.governance
    available = discover_units(repo_root, gov.src_root)

    if selector:
        plan = Plan(source=f"manual selection '{selector}'")
        plan.deploy = resolve_selector(selector, available)
    else:
        if not base or not head:
            raise ValueError("Either a unit selector or both base and head must be given.")
        plan = Plan(source=f"git diff {base[:12]}{'...' if diff_mode == 'three-dot' else '..'}{head[:12]}")
        plan.changed_files = changed_files(repo_root, base, head, diff_mode)
        changes = classify_paths(plan.changed_files, gov.src_root)
        for unit in sorted(changes.units):
            (plan.deploy if unit in available else plan.removed).append(unit)
        for unit in plan.removed:
            plan.findings.warning(
                f"Unit {unit} was deleted in Git. Workspace items are never deleted automatically; "
                "follow the 'Retire a report' procedure in the runbook."
            )
        if changes.team_level_paths:
            plan.findings.warning(
                "Files directly under a team folder changed; they are not part of any unit and are not deployed: "
                + ", ".join(changes.team_level_paths[:5])
            )

    for team in sorted({team_of(u) for u in plan.deploy}):
        if team not in config.teams:
            plan.findings.error(f"Team folder {gov.src_root}/{team}/ is not registered in config/teams.json.")
            continue
        for env in environments or []:
            try:
                config.team(team).workspace_id(env, allow_placeholder=allow_placeholder)
            except ConfigError as exc:
                plan.findings.error(str(exc))

    if enforce_team_limit and len(plan.teams) > gov.max_teams_per_pull_request:
        plan.findings.error(
            f"This change touches {len(plan.teams)} teams ({', '.join(plan.teams)}); "
            f"the limit is {gov.max_teams_per_pull_request}."
        )

    if plan.deploy:
        teams_in_scope = {t for t in plan.teams if t in config.teams}
        findings, _ = validate_repository(repo_root, config, units=plan.deploy, teams=teams_in_scope)
        plan.findings.extend(findings)

    return plan
