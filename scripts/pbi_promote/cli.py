"""Command line entry point: python -m pbi_promote <command> (run with PYTHONPATH=scripts)."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

from pbi_promote import __version__
from pbi_promote.config import ENVIRONMENTS, ConfigError, load_config
from pbi_promote.deploy import deploy
from pbi_promote.findings import Findings
from pbi_promote.ghactions import add_summary, in_actions, set_output
from pbi_promote.gitutil import GitError, changed_files
from pbi_promote.governance import check_branch, check_pull_request
from pbi_promote.plan import build_plan
from pbi_promote.units import SelectorError
from pbi_promote.validate import validate_repository

UNIT_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9-]*$")


def _report(findings: Findings) -> None:
    for f in findings.items:
        print(f.annotation() if in_actions() else f.format())


def _findings_markdown(findings: Findings) -> str:
    if not findings.items:
        return "No problems found.\n"
    return "\n".join(f"- {'❌' if f.level == 'error' else '⚠️'} {f.message}" + (f" (`{f.path}`)" if f.path else "")
                     for f in findings.items) + "\n"


def cmd_check_branch(args) -> int:
    config = load_config(args.config_root)
    findings = check_branch(args.branch, config)
    _report(findings)
    return 0 if findings.ok else 1


def cmd_validate_pr(args) -> int:
    config = load_config(args.config_root)
    repo = Path(args.repo_root)
    files = changed_files(repo, args.base, args.head, args.diff_mode)
    check = check_pull_request(files, args.branch, args.title, config)
    findings = check.findings

    teams = {t for t in check.changes.teams if t in config.teams}
    if teams:
        units = sorted(u for u in check.changes.units if (repo / config.governance.src_root / u).is_dir())
        unit_findings, _ = validate_repository(
            repo, config, units=units, teams=teams, codeowners=Path(args.codeowners) if args.codeowners else None
        )
        findings.extend(unit_findings)
    else:
        units = []

    _report(findings)
    scope = ", ".join(sorted(check.changes.teams)) or "none"
    add_summary(
        "### Pull request validation\n\n"
        f"- Work item: **{check.work_item or ('exempt branch' if check.exempt else 'missing')}**\n"
        f"- Teams changed: **{scope}**\n"
        f"- Units changed: {', '.join(f'`{u}`' for u in sorted(check.changes.units)) or 'none'}\n"
        f"- Platform files changed: {len(check.changes.platform_paths)}\n\n" + _findings_markdown(findings)
    )
    set_output("teams", json.dumps(sorted(check.changes.teams)))
    set_output("units", json.dumps(units))
    return 0 if findings.ok else 1


def cmd_validate_repo(args) -> int:
    config = load_config(args.config_root)
    repo = Path(args.repo_root)
    codeowners = Path(args.codeowners) if args.codeowners else repo / ".github" / "CODEOWNERS"
    findings, items = validate_repository(repo, config, codeowners=codeowners)
    _report(findings)
    add_summary(f"### Repository validation\n\n{len(items)} items checked.\n\n" + _findings_markdown(findings))
    return 0 if findings.ok else 1


def cmd_plan(args) -> int:
    config = load_config(args.config_root)
    plan = build_plan(
        Path(args.repo_root),
        config,
        base=args.base,
        head=args.head,
        diff_mode=args.diff_mode,
        selector=args.units,
        environments=args.env or [],
        allow_placeholder=args.allow_placeholder,
        enforce_team_limit=args.enforce_team_limit,
    )
    _report(plan.findings)
    add_summary(plan.to_markdown(args.title))
    ok = plan.findings.ok
    set_output("units", json.dumps(plan.deploy if ok else []))
    set_output("teams", json.dumps(plan.teams if ok else []))
    set_output("has_changes", "true" if ok and plan.deploy else "false")
    print(json.dumps({"deploy": plan.deploy, "removed": plan.removed, "ok": ok}, indent=2))
    return 0 if ok else 1


def cmd_deploy(args) -> int:
    config = load_config(args.config_root)
    try:
        units = json.loads(args.units_json)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"--units-json is not valid JSON: {exc}") from exc
    if not isinstance(units, list) or not all(isinstance(u, str) and UNIT_KEY_RE.match(u) for u in units):
        raise ConfigError("--units-json must be a JSON list of 'team/unit' strings.")
    if not units:
        print("Nothing to deploy.")
        return 0

    report = deploy(Path(args.repo_root), config, args.env, units, dry_run=args.dry_run, commit=args.commit or "")
    add_summary(report.to_markdown())
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(report.to_json(), encoding="utf-8")
    for r in report.results:
        if r.status == "failed":
            msg = f"Deployment of {r.team} to {args.env} failed: {r.error}"
            print(f"::error::{msg}" if in_actions() else f"ERROR: {msg}")
    return 0 if report.ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pbi_promote", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p, repo=True):
        p.add_argument("--config-root", default="config", help="Folder holding teams.json and governance.json.")
        if repo:
            p.add_argument("--repo-root", default=".", help="Repository checkout to inspect.")

    p = sub.add_parser("check-branch", help="Check a branch name against the naming rule.")
    common(p, repo=False)
    p.add_argument("--branch", required=True)
    p.set_defaults(func=cmd_check_branch)

    p = sub.add_parser("validate-pr", help="Validate a pull request: naming, scope and changed units.")
    common(p)
    p.add_argument("--base", required=True)
    p.add_argument("--head", required=True)
    p.add_argument("--branch", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--diff-mode", choices=["two-dot", "three-dot"], default="three-dot")
    p.add_argument("--codeowners", help="CODEOWNERS file to check team coverage against.")
    p.set_defaults(func=cmd_validate_pr)

    p = sub.add_parser("validate-repo", help="Validate every unit and the cross-unit rules.")
    common(p)
    p.add_argument("--codeowners")
    p.set_defaults(func=cmd_validate_repo)

    p = sub.add_parser("plan", help="Work out which units to deploy.")
    common(p)
    p.add_argument("--base")
    p.add_argument("--head")
    p.add_argument("--diff-mode", choices=["two-dot", "three-dot"], default="two-dot")
    p.add_argument("--units", help="Explicit selection, e.g. 'finance-analytics/claims-summary, csam-analytics/*'.")
    p.add_argument("--env", action="append", choices=ENVIRONMENTS, help="Environment(s) the plan will deploy to.")
    p.add_argument("--allow-placeholder", action="store_true", help="Accept placeholder workspace IDs (dry runs).")
    p.add_argument("--enforce-team-limit", action="store_true")
    p.add_argument("--title", default="Promotion plan")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("deploy", help="Deploy units to one environment.")
    common(p)
    p.add_argument("--env", required=True, choices=ENVIRONMENTS)
    p.add_argument("--units-json", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--commit")
    p.add_argument("--report", help="Write a JSON deployment report here.")
    p.set_defaults(func=cmd_deploy)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, GitError, SelectorError, ValueError) as exc:
        print(f"::error::{exc}" if in_actions() else f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
