"""Stages the selected units for each team and publishes them with fabric-cicd.

Each team is deployed on its own: only the selected units are copied into a
temporary folder, and fabric-cicd publishes that folder to the team's workspace
for the target environment. Nothing outside the selection is touched, and
orphan items are never deleted (team workspaces may hold content that is not
in Git).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from pbi_promote.config import Config, ConfigError
from pbi_promote.units import discover_units, find_items, team_of

log = logging.getLogger("pbi_promote")

OIDC_AUDIENCE = "api://AzureADTokenExchange"
STAGING_IGNORE = shutil.ignore_patterns(".pbi", "*.pbix", "cache.abf", "localSettings.json")


class Publisher(Protocol):
    def __call__(
        self,
        *,
        workspace_id: str,
        environment: str,
        repository_directory: Path,
        item_types: list[str],
        parameter_file: Path | None,
        folder_mode: str,
    ) -> None: ...


@dataclass
class TeamResult:
    team: str
    workspace_id: str
    units: list[str]
    items: list[str] = field(default_factory=list)
    status: str = "pending"
    error: str = ""
    seconds: float = 0.0


@dataclass
class DeployReport:
    environment: str
    dry_run: bool
    commit: str
    results: list[TeamResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.status in ("succeeded", "dry-run") for r in self.results)

    def to_json(self) -> str:
        return json.dumps(
            {"environment": self.environment, "dryRun": self.dry_run, "commit": self.commit,
             "results": [asdict(r) for r in self.results]},
            indent=2,
        )

    def to_markdown(self) -> str:
        mode = " (dry run)" if self.dry_run else ""
        lines = [f"### Deployment to `{self.environment}`{mode}", "",
                 "| Team | Workspace | Units | Items | Status |", "| --- | --- | --- | --- | --- |"]
        for r in self.results:
            status = r.status if not r.error else f"{r.status}: {r.error[:200]}"
            lines.append(
                f"| {r.team} | `{r.workspace_id}` | {'<br>'.join(r.units)} | {'<br>'.join(r.items)} | {status} |"
            )
        return "\n".join(lines) + "\n"


def stage_units(repo_root: Path, units: list[str], dest: Path, src_root: str, folder_mode: str) -> list[str]:
    """Copy the item folders of the given units into dest. Returns 'Type: name' labels."""
    available = discover_units(repo_root, src_root)
    labels: list[str] = []
    for unit in units:
        if unit not in available:
            raise ConfigError(f"Unit '{unit}' does not exist in this commit.")
        target_parent = dest / unit.split("/", 1)[1] if folder_mode == "unit" else dest
        target_parent.mkdir(parents=True, exist_ok=True)
        for item in find_items(available[unit]):
            target = target_parent / item.name
            if target.exists():
                raise ConfigError(
                    f"Two selected units contain an item folder named '{item.name}'. Item names must be "
                    "unique within a team workspace."
                )
            shutil.copytree(item, target, ignore=STAGING_IGNORE)
            kind = item.name.rsplit(".", 1)[-1]
            labels.append(f"{kind}: {item.name.rsplit('.', 1)[0]}")
    return labels


def github_oidc_assertion() -> str:
    """Fetch a GitHub Actions OIDC token for Entra ID workload identity federation."""
    url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
    sep = "&" if "?" in url else "?"
    request = urllib.request.Request(
        f"{url}{sep}audience={urllib.parse.quote(OIDC_AUDIENCE, safe='')}",
        headers={"Authorization": f"Bearer {os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - URL comes from the runner
        return json.load(response)["value"]


def _credential():
    from azure.identity import AzureCliCredential, ClientAssertionCredential, ClientSecretCredential

    tenant = os.environ.get("AZURE_TENANT_ID", "")
    client = os.environ.get("AZURE_CLIENT_ID", "")
    secret = os.environ.get("AZURE_CLIENT_SECRET")
    if secret:
        if not (tenant and client):
            raise ConfigError("AZURE_CLIENT_SECRET is set but AZURE_TENANT_ID or AZURE_CLIENT_ID is missing.")
        return ClientSecretCredential(tenant_id=tenant, client_id=client, client_secret=secret)
    if tenant and client and os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL"):
        # A fresh GitHub token is requested whenever Entra needs a new assertion, so long runs keep working.
        return ClientAssertionCredential(tenant_id=tenant, client_id=client, func=github_oidc_assertion)
    # Local use: reuse the signed-in Azure CLI session (az login).
    return AzureCliCredential()


def fabric_cicd_publisher(
    *,
    workspace_id: str,
    environment: str,
    repository_directory: Path,
    item_types: list[str],
    parameter_file: Path | None,
    folder_mode: str,
) -> None:
    import fabric_cicd

    flag = "disable_workspace_folder_publish"
    if folder_mode == "preserve":
        # Leave items in whatever workspace folder an admin placed them; new items land at the root.
        fabric_cicd.append_feature_flag(flag)
    else:
        fabric_cicd.remove_feature_flag(flag)

    kwargs = {}
    if parameter_file is not None:
        kwargs["parameter_file_path"] = str(parameter_file)
    workspace = fabric_cicd.FabricWorkspace(
        workspace_id=workspace_id,
        environment=environment,
        repository_directory=str(repository_directory),
        item_type_in_scope=list(item_types),
        token_credential=_credential(),
        **kwargs,
    )
    fabric_cicd.publish_all_items(workspace)


def dry_run_publisher(
    *,
    workspace_id: str,
    environment: str,
    repository_directory: Path,
    item_types: list[str],
    parameter_file: Path | None,
    folder_mode: str,
) -> None:
    """Checks that fabric-cicd accepts the staged folder and parameter file, without calling Fabric."""
    if parameter_file is None:
        return
    try:
        from fabric_cicd._parameter._utils import validate_parameter_file
    except ImportError:
        log.warning("fabric-cicd is not installed; skipping parameter.yml validation in dry run.")
        return
    if not validate_parameter_file(
        repository_directory=str(repository_directory),
        item_type_in_scope=list(item_types),
        environment=environment,
        parameter_file_path=str(parameter_file),
    ):
        raise ConfigError(f"fabric-cicd rejected {parameter_file.name} for environment '{environment}'.")


def deploy(
    repo_root: Path,
    config: Config,
    environment: str,
    units: list[str],
    *,
    dry_run: bool = False,
    commit: str = "",
    publisher: Callable[..., None] | None = None,
    parameter_file: Path | None = None,
) -> DeployReport:
    repo_root = Path(repo_root).resolve()
    publish = publisher or (dry_run_publisher if dry_run else fabric_cicd_publisher)
    if parameter_file is None:
        candidate = config.root / "parameter.yml"
        parameter_file = candidate if candidate.is_file() else None
    if parameter_file is not None:
        parameter_file = Path(parameter_file).resolve()
        if not parameter_file.is_file():
            raise ConfigError(f"Parameter file {parameter_file} does not exist.")

    report = DeployReport(environment=environment, dry_run=dry_run, commit=commit)
    by_team: dict[str, list[str]] = {}
    for unit in sorted(set(units)):
        by_team.setdefault(team_of(unit), []).append(unit)

    for team_key, team_units in by_team.items():
        result = TeamResult(team=team_key, workspace_id="", units=team_units)
        report.results.append(result)
        started = time.monotonic()
        try:
            team = config.team(team_key)
            result.workspace_id = team.workspace_id(environment, allow_placeholder=dry_run)
            with tempfile.TemporaryDirectory(prefix=f"pbi-{team_key}-") as tmp:
                staging = Path(tmp)
                result.items = stage_units(
                    repo_root, team_units, staging, config.governance.src_root, config.governance.workspace_folder_mode
                )
                log.info("Deploying %s to %s workspace %s: %s", team_key, environment, result.workspace_id, result.items)
                publish(
                    workspace_id=result.workspace_id,
                    environment=environment,
                    repository_directory=staging,
                    item_types=list(config.governance.item_types),
                    parameter_file=parameter_file,
                    folder_mode=config.governance.workspace_folder_mode,
                )
            result.status = "dry-run" if dry_run else "succeeded"
        except Exception as exc:  # one team's failure must not stop the others
            log.exception("Deployment for team %s failed", team_key)
            result.status = "failed"
            result.error = f"{type(exc).__name__}: {exc}"
        finally:
            result.seconds = round(time.monotonic() - started, 1)
    return report
