<#
.SYNOPSIS
    Lets a GitHub environment sign in as an existing service principal with OIDC (no client secret).

.DESCRIPTION
    Adds a federated identity credential to the Entra ID app registration of the service principal
    used for one environment. The subject is repo:<org>/<repo>:environment:<env>, so only jobs that
    run in that GitHub environment (and pass its protection rules) can get a token.

    Idempotent: an existing credential with the same subject is left unchanged.
    Requires the Azure CLI, signed in as an owner of the app registration (or Application Administrator).

.EXAMPLE
    ./scripts/setup/New-FederatedCredential.ps1 -Repository abcbs/powerbi-reports -Environment prod -ClientId <prod-sp-app-id>
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^[\w.-]+/[\w.-]+$')]
    [string]$Repository,

    [Parameter(Mandatory)]
    [ValidateSet('dev', 'test', 'prod')]
    [string]$Environment,

    # Application (client) ID of the service principal for this environment.
    [Parameter(Mandatory)]
    [ValidatePattern('^[0-9a-fA-F-]{36}$')]
    [string]$ClientId,

    [string]$Issuer = 'https://token.actions.githubusercontent.com'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not (Get-Command az -ErrorAction SilentlyContinue)) { throw 'The Azure CLI (az) is required.' }

$subject = "repo:${Repository}:environment:$Environment"
$name = ("github-" + ($Repository -replace '[^A-Za-z0-9-]', '-') + "-$Environment").ToLowerInvariant()
if ($name.Length -gt 120) { $name = $name.Substring(0, 120) }

$existing = az ad app federated-credential list --id $ClientId --query "[?subject=='$subject'].name" -o tsv
if ($LASTEXITCODE -ne 0) { throw "Could not read the app registration $ClientId. Check that you are signed in (az login) and own the app." }
if ($existing) {
    Write-Host "Federated credential '$existing' already trusts $subject."
    return
}

$definition = @{
    name        = $name
    issuer      = $Issuer
    subject     = $subject
    description = "GitHub Actions deployments from $Repository to the $Environment environment"
    audiences   = @('api://AzureADTokenExchange')
} | ConvertTo-Json

if ($PSCmdlet.ShouldProcess($ClientId, "Add federated credential for $subject")) {
    $file = New-TemporaryFile
    try {
        [IO.File]::WriteAllText($file, $definition)
        az ad app federated-credential create --id $ClientId --parameters "@$file" -o none
        if ($LASTEXITCODE -ne 0) { throw 'az ad app federated-credential create failed.' }
    } finally {
        Remove-Item $file -Force
    }
    Write-Host "Added federated credential '$name' ($subject) to app $ClientId."
}
