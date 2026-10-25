<#
.SYNOPSIS
  One-time setup so GitHub Actions can deploy the Meme Bank (.github/workflows/deploy.yml).

.DESCRIPTION
  Creates (if missing) an Azure app registration "memebank-github-deploy" with a federated credential that only
  the main branch of this GitHub repo can use (OIDC: no password or key is stored anywhere), gives it Contributor
  on the resource group only, and stores the IDs it needs with gh: the Azure IDs and registry name as repository
  secrets (they aren't credentials, but secrets are masked in the public Actions logs), the resource group as a
  repository variable.

  Run it once after deploy.ps1, signed in with az login (MFA) and gh auth login. Safe to re-run.

.EXAMPLE
  .\deploy\setup-github-deploy.ps1
#>
param(
    [string]$ResourceGroup = "rg-memebank",  # as given to deploy.ps1 (-ResourceGroup)
    [string]$ContainerApp = "memebank",      # deploy.ps1's -AppName: the container app the workflow updates
    [string]$Repo = "",            # owner/name; taken from gh if blank
    [string]$AppName = "memebank-github-deploy"
)
$ErrorActionPreference = "Stop"

function Invoke-Az {
    $out = & az @args
    if ($LASTEXITCODE -ne 0) { throw "az $($args -join ' ') failed (exit $LASTEXITCODE)" }
    return $out
}

# The repo this script sits in (it needs no other files), or the current folder's, if -Repo isn't given.
if (-not $Repo) {
    foreach ($dir in @((Split-Path $PSScriptRoot -Parent), (Get-Location).Path)) {
        Push-Location $dir; try { $Repo = (& gh repo view --json nameWithOwner --jq .nameWithOwner 2>$null) } finally { Pop-Location }
        if ($Repo) { break }
    }
}
if (-not $Repo) { throw "Couldn't tell which GitHub repo this is; pass -Repo owner/name" }
$tenant = Invoke-Az account show --query tenantId -o tsv
$subscription = Invoke-Az account show --query id -o tsv
$groupId = Invoke-Az group show --name $ResourceGroup --query id -o tsv
$registry = Invoke-Az acr list --resource-group $ResourceGroup --query "[0].name" -o tsv
if (-not $registry) { throw "No container registry in $ResourceGroup yet; run deploy.ps1 first" }

Write-Host "== App registration $AppName"
$appId = Invoke-Az ad app list --display-name $AppName --query "[0].appId" -o tsv
if (-not $appId) { $appId = Invoke-Az ad app create --display-name $AppName --query appId -o tsv }
$principal = Invoke-Az ad sp list --filter "appId eq '$appId'" --query "[0].id" -o tsv
if (-not $principal) { $principal = Invoke-Az ad sp create --id $appId --query id -o tsv }

Write-Host "== Federated credentials: only $Repo's main branch"
# GitHub's token subject names the repo either by name ("repo:owner/name") or, with immutable subjects (the default
# for newer repos), by name plus numeric IDs ("repo:owner@123/name@456"), which survive renames. Ask GitHub which
# prefix it uses, and register both forms so a change to that setting doesn't break deploys.
$prefix = (& gh api "repos/$Repo/actions/oidc/customization/sub" --jq .sub_claim_prefix)
if ($LASTEXITCODE -ne 0) { throw "Couldn't ask GitHub which token subject $Repo uses (gh api failed)" }
$subjects = @("repo:${Repo}:ref:refs/heads/main")
if ($prefix -and "${prefix}:ref:refs/heads/main" -notin $subjects) { $subjects = @("${prefix}:ref:refs/heads/main") + $subjects }
foreach ($subject in $subjects) {
    $existing = Invoke-Az ad app federated-credential list --id $appId --query "[?subject=='$subject'].name" -o tsv
    if (-not $existing) {
        $name = if ($subject -match "@") { "github-main-ids" } else { "github-main" }
        $spec = @{ name = $name; issuer = "https://token.actions.githubusercontent.com"; subject = $subject
                   audiences = @("api://AzureADTokenExchange"); description = "GitHub Actions deploys from main" }
        $file = New-TemporaryFile
        $spec | ConvertTo-Json | Set-Content -Path $file -Encoding utf8
        try { Invoke-Az ad app federated-credential create --id $appId --parameters "@$file" | Out-Null } finally { Remove-Item $file }
    }
    Write-Host "   $subject"
}

Write-Host "== Contributor on $ResourceGroup only"
$has = Invoke-Az role assignment list --assignee $principal --role Contributor --scope $groupId --query "[].id" -o tsv
if (-not $has) {
    Invoke-Az role assignment create --assignee-object-id $principal --assignee-principal-type ServicePrincipal `
        --role Contributor --scope $groupId --only-show-errors | Out-Null
}

Write-Host "== GitHub repository secrets (masked in the Actions logs) and variables"
$oldVars = @(& gh variable list --repo $Repo --json name --jq ".[].name")
if ($LASTEXITCODE -ne 0) { throw "gh variable list failed" }
foreach ($kv in @(@("AZURE_CLIENT_ID", $appId), @("AZURE_TENANT_ID", $tenant), @("AZURE_SUBSCRIPTION_ID", $subscription),
                  @("ACR_NAME", $registry))) {
    & gh secret set $kv[0] --repo $Repo --body $kv[1]
    if ($LASTEXITCODE -ne 0) { throw "gh secret set $($kv[0]) failed" }
    if ($kv[0] -in $oldVars) {  # older versions of this script stored them as plain variables
        & gh variable delete $kv[0] --repo $Repo
        if ($LASTEXITCODE -ne 0) { throw "gh variable delete $($kv[0]) failed" }
    }
}
foreach ($kv in @(@("RESOURCE_GROUP", $ResourceGroup), @("APP_NAME", $ContainerApp))) {  # what the workflow deploys to
    & gh variable set $kv[0] --repo $Repo --body $kv[1]
    if ($LASTEXITCODE -ne 0) { throw "gh variable set $($kv[0]) failed" }
}

Write-Host ""
Write-Host "Done. Pushes to main that pass the tests now deploy themselves (Actions > Deploy)."
Write-Host "  Deploy by hand:   gh workflow run deploy.yml"
