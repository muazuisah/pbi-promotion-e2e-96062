"""Branch, pull request title and change-scope rules."""

from __future__ import annotations

from dataclasses import dataclass

from pbi_promote.config import Config
from pbi_promote.findings import Findings
from pbi_promote.units import ChangeSet, classify_paths


@dataclass
class PrCheck:
    findings: Findings
    changes: ChangeSet
    work_item: str | None
    exempt: bool


def is_exempt_branch(branch: str, config: Config) -> bool:
    return any(p.search(branch) for p in config.governance.exempt_branch_patterns)


def work_item_from_branch(branch: str, config: Config) -> str | None:
    m = config.governance.branch_pattern.fullmatch(branch)
    return m.group("workItem") if m else None


def check_branch(branch: str, config: Config) -> Findings:
    findings = Findings()
    if is_exempt_branch(branch, config):
        return findings
    if work_item_from_branch(branch, config) is None:
        findings.error(
            f"Branch '{branch}' does not follow the naming rule "
            f"'{config.governance.branch_pattern.pattern}'. "
            "Start the branch name with the work item ID, for example STRY0012345-add-claims-page."
        )
    return findings


def check_pull_request(changed_paths: list[str], branch: str, title: str, config: Config) -> PrCheck:
    gov = config.governance
    findings = Findings()
    exempt = is_exempt_branch(branch, config)
    work_item: str | None = None

    if not exempt:
        findings.extend(check_branch(branch, config))
        work_item = work_item_from_branch(branch, config)
        tm = gov.title_pattern.match(title or "")
        if tm is None:
            findings.error(
                f"Pull request title '{title}' must start with the work item ID "
                f"(pattern '{gov.title_pattern.pattern}'), for example 'STRY0012345: Add claims page'."
            )
        elif work_item and tm.group("workItem") != work_item:
            findings.error(
                f"Pull request title references {tm.group('workItem')} but the branch references {work_item}. "
                "They must match so the change can be traced to one work item."
            )

    changes = classify_paths(changed_paths, gov.src_root)

    unknown = sorted(t for t in changes.teams if t not in config.teams)
    for team in unknown:
        findings.error(
            f"Folder {gov.src_root}/{team}/ is not a registered team. "
            "Ask the platform team to add it to config/teams.json and CODEOWNERS first."
        )

    if len(changes.teams) > gov.max_teams_per_pull_request:
        findings.error(
            f"This pull request changes {len(changes.teams)} teams ({', '.join(sorted(changes.teams))}). "
            f"At most {gov.max_teams_per_pull_request} team(s) may change per pull request so each team "
            "can release on its own schedule. Split the work into separate branches."
        )

    if changes.platform_paths and changes.teams and not gov.allow_mixed_platform_and_team_changes:
        sample = ", ".join(changes.platform_paths[:5])
        findings.error(
            "This pull request mixes platform files and report files. Platform changes "
            f"({sample}{'...' if len(changes.platform_paths) > 5 else ''}) must go in their own pull request, "
            "reviewed by the platform team."
        )

    if not changed_paths:
        findings.warning("No files changed between the base and head commits.")

    return PrCheck(findings=findings, changes=changes, work_item=work_item, exempt=exempt)
