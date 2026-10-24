<#
.SYNOPSIS
  Deploys (or updates) the Meme Bank bot on Azure Container Apps.

.DESCRIPTION
  Creates, if missing: a resource group, a storage account (Table + Blob for the memes, with 30-day soft delete
  and a delete lock), a Container Apps environment and the app itself, built in the cloud from this folder's
  Dockerfile. Secrets come from .env
  and are stored as Container App secrets. Re-run it any time to ship code changes.

  Also writes .env.azure (your .env plus the storage connection string) so the backfill and import scripts
  can be run from your PC against the cloud bank.

.EXAMPLE
  az login
  .\deploy\deploy.ps1
#>
param(
    [string]$ResourceGroup = "rg-memebank",
    [string]$Location = "westus2",
    [string]$AppName = "memebank",
    [string]$StorageAccount = "",  # 3-24 lowercase letters/digits, globally unique; picked for you if blank
    [string]$EnvFile = ".env"
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

function Invoke-Az {
    # Runs az and stops on failure (PowerShell doesn't do that for external programs on its own).
    $out = & az @args
    if ($LASTEXITCODE -ne 0) { throw "az $($args -join ' ') failed (exit $LASTEXITCODE)" }
    return $out
}

function Read-DotEnv([string]$Path) {
    $values = [ordered]@{}
    if (-not (Test-Path $Path)) { throw "$Path not found. Copy .env.example to .env and fill it in." }
    foreach ($line in Get-Content $Path) {
        if ($line -match '^\s*#' -or $line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') { continue }
        $values[$Matches[1]] = $Matches[2].Trim().Trim('"').Trim("'")
    }
    return $values
}

$cfg = Read-DotEnv $EnvFile
foreach ($k in "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "ANTHROPIC_API_KEY") {
    if (-not $cfg[$k] -or $cfg[$k].EndsWith("...")) { throw "$k is not filled in in $EnvFile" }
}

Write-Host "== Checking Azure CLI login and extensions"
$account = Invoke-Az account show --query "{name:name, user:user.name}" -o tsv
Write-Host "   Subscription/user: $account"
Invoke-Az extension add --name containerapp --upgrade --only-show-errors | Out-Null
Invoke-Az provider register --namespace Microsoft.App --only-show-errors | Out-Null
Invoke-Az provider register --namespace Microsoft.OperationalInsights --only-show-errors | Out-Null

Write-Host "== Resource group $ResourceGroup ($Location)"
Invoke-Az group create --name $ResourceGroup --location $Location --only-show-errors | Out-Null

if (-not $StorageAccount) {
    $StorageAccount = Invoke-Az storage account list --resource-group $ResourceGroup --query "[?starts_with(name,'memebank')].name | [0]" -o tsv
    if (-not $StorageAccount) {
        $StorageAccount = "memebank" + (-join ((48..57) + (97..122) | Get-Random -Count 8 | ForEach-Object { [char]$_ }))
    }
}
Write-Host "== Storage account $StorageAccount"
Invoke-Az storage account create --name $StorageAccount --resource-group $ResourceGroup --location $Location `
    --sku Standard_LRS --kind StorageV2 --min-tls-version TLS1_2 --allow-blob-public-access false --only-show-errors | Out-Null
$conn = Invoke-Az storage account show-connection-string --name $StorageAccount --resource-group $ResourceGroup --query connectionString -o tsv

Write-Host "== Protecting the memes (30-day soft delete, delete lock)"
# Deleted or overwritten images stay recoverable for 30 days.
Invoke-Az storage account blob-service-properties update --account-name $StorageAccount --resource-group $ResourceGroup `
    --enable-delete-retention true --delete-retention-days 30 `
    --enable-container-delete-retention true --container-delete-retention-days 30 --only-show-errors | Out-Null
# The account (and so the resource group) can't be deleted until this lock is removed on purpose.
Invoke-Az lock create --name keep-the-memes --lock-type CanNotDelete --resource-group $ResourceGroup `
    --resource $StorageAccount --resource-type Microsoft.Storage/storageAccounts `
    --notes "Holds the memes. Export them (python -m scripts.export) before removing this lock." --only-show-errors | Out-Null

Write-Host "== Building and deploying the container (cloud build, takes a few minutes)"
Invoke-Az containerapp up --name $AppName --resource-group $ResourceGroup --location $Location `
    --environment "$AppName-env" --source . | Out-Host

# The bot only makes outbound connections; make sure no web endpoint (and its health probe) is configured.
$ingress = Invoke-Az containerapp show --name $AppName --resource-group $ResourceGroup --query "properties.configuration.ingress.fqdn" -o tsv
if ($ingress) { Invoke-Az containerapp ingress disable --name $AppName --resource-group $ResourceGroup --only-show-errors | Out-Null }

Write-Host "== Storing secrets"
Invoke-Az containerapp secret set --name $AppName --resource-group $ResourceGroup --secrets `
    "slack-bot-token=$($cfg['SLACK_BOT_TOKEN'])" `
    "slack-app-token=$($cfg['SLACK_APP_TOKEN'])" `
    "anthropic-api-key=$($cfg['ANTHROPIC_API_KEY'])" `
    "storage-connection=$conn" | Out-Null

Write-Host "== Settings, size, and exactly one copy running"
$envVars = @(
    "SLACK_BOT_TOKEN=secretref:slack-bot-token",
    "SLACK_APP_TOKEN=secretref:slack-app-token",
    "ANTHROPIC_API_KEY=secretref:anthropic-api-key",
    "AZURE_STORAGE_CONNECTION_STRING=secretref:storage-connection",
    "STORAGE_BACKEND=azure"
)
foreach ($k in "MEME_CHANNEL_IDS", "ADMIN_USER_IDS", "ACK_MODE", "BANK_REACTIONS", "DM_TAGS", "TAG_MODEL", "SEARCH_MODEL") {
    if ($cfg[$k]) { $envVars += "$k=$($cfg[$k])" }
}
Invoke-Az containerapp update --name $AppName --resource-group $ResourceGroup `
    --min-replicas 1 --max-replicas 1 --cpu 0.25 --memory 0.5Gi `
    --set-env-vars @envVars --only-show-errors | Out-Null

if ($cfg['ALERT_EMAIL']) {
    Write-Host "== Alerts to $($cfg['ALERT_EMAIL']) (restarts, errors, gone quiet)"
    Invoke-Az extension add --name scheduled-query --upgrade --only-show-errors | Out-Null
    $appId = Invoke-Az containerapp show --name $AppName --resource-group $ResourceGroup --query id -o tsv
    $workspace = Invoke-Az monitor log-analytics workspace list --resource-group $ResourceGroup --query "[0].id" -o tsv
    $group = Invoke-Az monitor action-group create --name "$AppName-alerts" --resource-group $ResourceGroup `
        --short-name $AppName.Substring(0, [Math]::Min(12, $AppName.Length)) `
        --action email owner $cfg['ALERT_EMAIL'] --only-show-errors --query id -o tsv
    # A crash loop: a bad or revoked token, a missing setting. Normal deploys stay at 0-1 restarts.
    Invoke-Az monitor metrics alert create --name "$AppName-restarts" --resource-group $ResourceGroup --scopes $appId `
        --condition "max RestartCount > 3" --window-size 1h --evaluation-frequency 15m --severity 2 --action $group `
        --description "The Meme Bank container keeps restarting. Check: az containerapp logs show -n $AppName -g $ResourceGroup --type system" `
        --only-show-errors | Out-Null
    $logs = "ContainerAppConsoleLogs_CL | where ContainerAppName_s == '$AppName'"
    # A deposit, search or upload failed: Claude unreachable, Slack errors, storage errors.
    Invoke-Az monitor scheduled-query create --name "$AppName-errors" --resource-group $ResourceGroup --scopes $workspace `
        --condition "count 'Errors' > 0" --condition-query "Errors=$logs | where Log_s has ' ERROR ' or Log_s has 'Traceback'" `
        --window-size 15m --evaluation-frequency 15m --severity 2 --action-groups $group `
        --description "The Meme Bank logged an error. Look in Log stream or Log Analytics for ERROR lines." `
        --only-show-errors | Out-Null
    # Hung, or connected to nothing: the bot logs "Heartbeat: connected=True" every hour while Slack is reachable.
    Invoke-Az monitor scheduled-query create --name "$AppName-quiet" --resource-group $ResourceGroup --scopes $workspace `
        --condition "count 'Heartbeats' < 1" --condition-query "Heartbeats=$logs | where Log_s has 'Heartbeat: connected=True'" `
        --window-size 2h --evaluation-frequency 1h --severity 1 --action-groups $group `
        --description "No connected heartbeat from the Meme Bank for 2 hours: it may be down or disconnected from Slack." `
        --only-show-errors | Out-Null
} else {
    Write-Host "== Alerts skipped (set ALERT_EMAIL in $EnvFile to get them)"
}

# .env.azure: same settings, pointed at the cloud storage, for running backfill/import from this PC
$lines = @(foreach ($k in $cfg.Keys) { if ($k -notin "STORAGE_BACKEND", "AZURE_STORAGE_CONNECTION_STRING") { "$k=$($cfg[$k])" } })
$lines += "STORAGE_BACKEND=azure", "AZURE_STORAGE_CONNECTION_STRING=$conn"
[System.IO.File]::WriteAllLines((Join-Path (Get-Location) ".env.azure"), [string[]]$lines)  # UTF-8, no BOM

Write-Host ""
Write-Host "Done. The Meme Bank is starting up in Azure."
Write-Host "  Watch the logs:   az containerapp logs show -n $AppName -g $ResourceGroup --follow"
Write-Host "  Backfill (dry):   python -m scripts.backfill --env-file .env.azure --channel C6UTZF23Z --dry-run"
Write-Host "  Back up memes:    python -m scripts.export --env-file .env.azure"
Write-Host "  Remove it all:    export first, then remove the lock (docs/DEPLOY-AZURE.md, section 12) and run az group delete -n $ResourceGroup"
