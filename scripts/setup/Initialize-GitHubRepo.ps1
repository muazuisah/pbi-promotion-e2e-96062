<#
.SYNOPSIS
    Configures a GitHub repository for Power BI selective promotion.

.DESCRIPTION
    Idempotent. Creates or updates:
      * environments dev, test and prod, with deployment branch policies
        (dev: STRY*/RITM*/INC* branches, test and prod: main only);
      * required reviewers on prod (and optionally test);
      * environment variables AZURE_TENANT_ID and AZURE_CLIENT_ID (one service principal per environment);
      * repository variables DEPLOY_DRY_RUN and DEV_AUTO_DEPLOY;
      * the rulesets in .github/rulesets (protect main, work-item branch names);
      * squash-only merges and automatic deletion of merged branches.

    Requires the GitHub CLI (gh), signed in with admin rights on the repository.
    Environments with required reviewers and rulesets on private repositories need
    GitHub Team or Enterprise.

.EXAMPLE
    ./scripts/setup/Initialize-GitHubRepo.ps1 -Repository abcbs/powerbi-reports `
        -TenantId <tenant-guid> -DevClientId <guid> -TestClientId <guid> -ProdClientId <guid> `
        -ProdReviewers @('team:abcbs/powerbi-release-approvers') -DryRun

.EXAMPLE
    ./scripts/setup/Initialize-GitHubRepo.ps1 -Repository abcbs/powerbi-reports -WhatIf
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^[\w.-]+/[\w.-]+$')]
    [string]$Repository,

    [string]$TenantId,
    [string]$DevClientId,
    [string]$TestClientId,
    [string]$ProdClientId,

    # Users ('octocat') or teams ('team:org/slug') who approve production deployments.
    [string[]]$ProdReviewers = @(),

    # Optional reviewers for the test (UAT) environment.
    [string[]]$TestReviewers = @(),

    # Keep the pipelines in dry-run mode (nothing is published to Fabric) until the workspaces and
    # service principals are ready.
    [switch]$DryRun,

    # Deploy work branches to dev from GitHub Actions (otherwise developers publish to dev from Desktop).
    [switch]$EnableDevAutoDeploy,

    # Let the person who triggered a deployment approve it (not recommended outside testing).
    [switch]$AllowSelfReview,

    [switch]$SkipRulesets
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$templateRoot = Resolve-Path (Join-Path $PSScriptRoot '..\..')

function Invoke-Gh {
    param([string[]]$Arguments, [string]$Body)
    if ($Body) {
        $tmp = New-TemporaryFile
        try {
            [IO.File]::WriteAllText($tmp, $Body)
            $out = & gh @Arguments --input $tmp 2>&1
        } finally {
            Remove-Item $tmp -Force
        }
    } else {
        $out = & gh @Arguments 2>&1
    }
    if ($LASTEXITCODE -ne 0) { throw "gh $($Arguments -join ' ') failed:`n$out" }
    return ($out -join "`n")
}

function Resolve-Reviewer {
    param([string]$Reviewer)
    if ($Reviewer -match '^team:(?<org>[^/]+)/(?<slug>.+)$') {
        $id = Invoke-Gh @('api', "orgs/$($Matches.org)/teams/$($Matches.slug)", '--jq', '.id')
        return @{ type = 'Team'; id = [int64]$id }
    }
    $id = Invoke-Gh @('api', "users/$Reviewer", '--jq', '.id')
    return @{ type = 'User'; id = [int64]$id }
}

function Set-Environment {
    param([string]$Name, [string[]]$Reviewers, [string[]]$BranchPatterns, [string]$ClientId)

    if (-not $PSCmdlet.ShouldProcess("$Repository environment '$Name'", 'Create or update')) { return }

    $body = @{
        deployment_branch_policy = @{ protected_branches = $false; custom_branch_policies = $true }
    }
    if ($Reviewers.Count -gt 0) {
        $body.reviewers = @($Reviewers | ForEach-Object { Resolve-Reviewer $_ })
        $body.prevent_self_review = -not $AllowSelfReview.IsPresent
    }
    try {
        Invoke-Gh @('api', '--method', 'PUT', "repos/$Repository/environments/$Name") ($body | ConvertTo-Json -Depth 5) | Out-Null
    } catch {
        if ($_.Exception.Message -match 'billing plan') {
            throw "Environment '$Name': required reviewers on a private repository need GitHub Team or Enterprise. Upgrade the plan, or rerun without reviewers for this environment (deployments would then not wait for approval).`n$($_.Exception.Message)"
        }
        throw
    }

    $existing = Invoke-Gh @('api', "repos/$Repository/environments/$Name/deployment-branch-policies", '--jq', '.branch_policies[].name')
    $existingNames = @($existing -split "`n" | Where-Object { $_ })
    foreach ($pattern in $BranchPatterns) {
        if ($existingNames -notcontains $pattern) {
            Invoke-Gh @('api', '--method', 'POST', "repos/$Repository/environments/$Name/deployment-branch-policies") (@{ name = $pattern; type = 'branch' } | ConvertTo-Json) | Out-Null
        }
    }
    foreach ($stale in $existingNames | Where-Object { $BranchPatterns -notcontains $_ }) {
        Write-Warning "Environment '$Name' also allows branch pattern '$stale' (left unchanged)."
    }

    if ($TenantId) { Set-RepoVariable -Environment $Name -Name 'AZURE_TENANT_ID' -Value $TenantId }
    if ($ClientId) { Set-RepoVariable -Environment $Name -Name 'AZURE_CLIENT_ID' -Value $ClientId }
    Write-Host "Environment '$Name' configured (branches: $($BranchPatterns -join ', '); reviewers: $(if ($Reviewers) { $Reviewers -join ', ' } else { 'none' }))."
}

function Set-RepoVariable {
    param([string]$Name, [string]$Value, [string]$Environment)
    $ghArgs = @('variable', 'set', $Name, '--repo', $Repository, '--body', $Value)
    if ($Environment) { $ghArgs += @('--env', $Environment) }
    Invoke-Gh $ghArgs | Out-Null
}

function Set-Ruleset {
    param([string]$Path)
    $definition = Get-Content $Path -Raw | ConvertFrom-Json
    if (-not $PSCmdlet.ShouldProcess("$Repository ruleset '$($definition.name)'", 'Create or update')) { return }
    $json = Get-Content $Path -Raw
    $id = Invoke-Gh @('api', "repos/$Repository/rulesets", '--jq', ".[] | select(.name == `"$($definition.name)`") | .id")
    if ($id) {
        Invoke-Gh @('api', '--method', 'PUT', "repos/$Repository/rulesets/$id") $json | Out-Null
        Write-Host "Ruleset '$($definition.name)' updated."
    } else {
        Invoke-Gh @('api', '--method', 'POST', "repos/$Repository/rulesets") $json | Out-Null
        Write-Host "Ruleset '$($definition.name)' created."
    }
}

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { throw 'The GitHub CLI (gh) is required: https://cli.github.com' }
Invoke-Gh @('auth', 'status') | Out-Null
if ($ProdReviewers.Count -eq 0) { Write-Warning 'No -ProdReviewers given: production deployments will not wait for approval.' }

if ($PSCmdlet.ShouldProcess($Repository, 'Set merge settings (squash only, delete merged branches)')) {
    Invoke-Gh @('api', '--method', 'PATCH', "repos/$Repository") (@{
        allow_squash_merge = $true; allow_merge_commit = $false; allow_rebase_merge = $false
        delete_branch_on_merge = $true; squash_merge_commit_title = 'PR_TITLE'
    } | ConvertTo-Json) | Out-Null
}

Set-Environment -Name 'dev'  -Reviewers @() -BranchPatterns @('STRY*', 'RITM*', 'INC*') -ClientId $DevClientId
Set-Environment -Name 'test' -Reviewers $TestReviewers -BranchPatterns @('main') -ClientId $TestClientId
Set-Environment -Name 'prod' -Reviewers $ProdReviewers -BranchPatterns @('main') -ClientId $ProdClientId

if ($PSCmdlet.ShouldProcess($Repository, 'Set repository variables')) {
    Set-RepoVariable -Name 'DEPLOY_DRY_RUN' -Value $(if ($DryRun) { 'true' } else { 'false' })
    Set-RepoVariable -Name 'DEV_AUTO_DEPLOY' -Value $(if ($EnableDevAutoDeploy) { 'true' } else { 'false' })
}

if (-not $SkipRulesets) {
    Get-ChildItem (Join-Path $templateRoot '.github\rulesets') -Filter *.json | ForEach-Object { Set-Ruleset $_.FullName }
}

Write-Host "`nDone. Next: create the federated credentials (New-FederatedCredential.ps1) and add team reviewers to .github/CODEOWNERS."
