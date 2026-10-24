<#
.SYNOPSIS
  Deploys (or updates) the Meme Bank bot on Azure Container Apps.

.DESCRIPTION
  Creates, if missing: a resource group, a storage account (Table + Blob for the memes, with 30-day soft delete
  and a delete lock), a Container Apps environment and the app itself, built in the cloud from this folder's
  Dockerfile. Secrets come from .env and are stored as Container App secrets.

  Re-run it to change settings or secrets. Code changes normally deploy from GitHub Actions once merged to main
  (set up once with setup-github-deploy.ps1). This script also rebuilds the image from this folder, so run it from
  an up-to-date main checkout, or it replaces what Actions deployed with whatever code is here.

  Storage is reached by identity, not an account key: the app's managed identity, and you (your az login) for
  the scripts. The script grants both the Table and Blob data roles and then turns account-key access off.

  Also writes .env.azure (your .env plus the storage account name, no key) so the backfill, import and export
  scripts can be run from your PC against the cloud bank while you're signed in with az login.

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
$storageId = Invoke-Az storage account show --name $StorageAccount --resource-group $ResourceGroup --query id -o tsv

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

Write-Host "== Storage access by identity: the app's managed identity, and you (for the scripts on this PC)"
$appPrincipal = Invoke-Az containerapp identity assign --name $AppName --resource-group $ResourceGroup --system-assigned `
    --query principalId -o tsv
$me = Invoke-Az ad signed-in-user show --query id -o tsv
foreach ($who in @(@($appPrincipal, "ServicePrincipal"), @($me, "User"))) {
    foreach ($role in "Storage Table Data Contributor", "Storage Blob Data Contributor") {
        # (No "length(@)" here: through az.cmd the parentheses break the command line.)
        $has = Invoke-Az role assignment list --assignee $who[0] --role $role --scope $storageId --query "[].id" -o tsv
        if (-not $has) {
            Invoke-Az role assignment create --assignee-object-id $who[0] --assignee-principal-type $who[1] --role $role `
                --scope $storageId --only-show-errors | Out-Null
        }
    }
}
# New role assignments take a few minutes to work. Wait until signing in can actually read a meme row as you (the
# scripts need it; the app itself is checked after it's switched over, below). Reading a row needs the data role:
# listing tables would pass for an Owner without it. Creating the table is retried too, since on a new account it
# fails until the role works.
foreach ($try in 1..30) {
    & az storage table create --name memes --account-name $StorageAccount --auth-mode login --only-show-errors -o none 2>$null
    & az storage entity query --table-name memes --account-name $StorageAccount --auth-mode login --num-results 1 `
        --only-show-errors -o none 2>$null
    if ($LASTEXITCODE -eq 0) { break }
    if ($try -eq 30) { throw "Storage still refuses sign-in access after 10 minutes; check the role assignments on $StorageAccount" }
    Start-Sleep -Seconds 20
}

Write-Host "== Storing secrets"
Invoke-Az containerapp secret set --name $AppName --resource-group $ResourceGroup --secrets `
    "slack-bot-token=$($cfg['SLACK_BOT_TOKEN'])" `
    "slack-app-token=$($cfg['SLACK_APP_TOKEN'])" `
    "anthropic-api-key=$($cfg['ANTHROPIC_API_KEY'])" | Out-Null

Write-Host "== Settings, size, and exactly one copy running"
$envVars = @(
    "SLACK_BOT_TOKEN=secretref:slack-bot-token",
    "SLACK_APP_TOKEN=secretref:slack-app-token",
    "ANTHROPIC_API_KEY=secretref:anthropic-api-key",
    "AZURE_STORAGE_ACCOUNT=$StorageAccount",
    "STORAGE_BACKEND=azure"
)
foreach ($k in "MEME_CHANNEL_IDS", "ADMIN_USER_IDS", "ACK_MODE", "BANK_REACTIONS", "DM_TAGS", "TAG_MODEL", "SEARCH_MODEL") {
    if ($cfg[$k]) { $envVars += "$k=$($cfg[$k])" }
}
# AZURE_STORAGE_ACCOUNT makes the bot sign in as its identity. Any old account-key setting stays in place until the
# new revision has shown it can reach storage that way.
Invoke-Az containerapp update --name $AppName --resource-group $ResourceGroup `
    --min-replicas 1 --max-replicas 1 --cpu 0.25 --memory 0.5Gi `
    --set-env-vars @envVars --only-show-errors | Out-Null

Write-Host "== Checking the bot reaches storage as its identity (before any key is taken away)"
# At startup the bot creates its blob container and reads the catalogue, then logs "open for business": that line from
# the new revision proves Table and Blob access by identity. Logs reach Log Analytics a few minutes late.
Invoke-Az extension add --name log-analytics --upgrade --only-show-errors | Out-Null
$customerId = Invoke-Az containerapp env show --name "$AppName-env" --resource-group $ResourceGroup `
    --query "properties.appLogsConfiguration.logAnalyticsConfiguration.customerId" -o tsv
$revision = Invoke-Az containerapp show --name $AppName --resource-group $ResourceGroup --query properties.latestRevisionName -o tsv
$started = "ContainerAppConsoleLogs_CL | where RevisionName_s == '$revision' and Log_s has 'open for business (azure storage' | count"
$ok = $false
foreach ($try in 1..20) {
    $n = & az monitor log-analytics query -w $customerId --analytics-query $started --query "[0].Count" -o tsv 2>$null
    if ($LASTEXITCODE -eq 0 -and [int]$n -gt 0) { $ok = $true; break }
    Start-Sleep -Seconds 30
}
$old = Invoke-Az containerapp show --name $AppName --resource-group $ResourceGroup `
    --query "properties.template.containers[0].env[?name=='AZURE_STORAGE_CONNECTION_STRING'].name | [0]" -o tsv
if (-not $ok) {
    if ($old) {  # back to the account key, so the bot keeps working while the cause is found
        Invoke-Az containerapp update --name $AppName --resource-group $ResourceGroup --remove-env-vars AZURE_STORAGE_ACCOUNT `
            --only-show-errors | Out-Null
    }
    throw "Revision $revision didn't start on identity access within 10 minutes$(if ($old) { ' (put back on the account key)' }). " +
          "Check: az containerapp logs show -n $AppName -g $ResourceGroup. Nothing was turned off."
}

Write-Host "== Turning off account-key access to storage (identity only from now on)"
# The account-key connection string from before identity access is no longer used by anything.
if ($old) {
    Invoke-Az containerapp update --name $AppName --resource-group $ResourceGroup --remove-env-vars AZURE_STORAGE_CONNECTION_STRING `
        --only-show-errors | Out-Null
}
if (Invoke-Az containerapp secret list --name $AppName --resource-group $ResourceGroup --query "[?name=='storage-connection'].name" -o tsv) {
    Invoke-Az containerapp secret remove --name $AppName --resource-group $ResourceGroup --secret-names storage-connection `
        --only-show-errors | Out-Null
}
Invoke-Az storage account update --name $StorageAccount --resource-group $ResourceGroup --allow-shared-key-access false `
    --only-show-errors | Out-Null

if ($cfg['ALERT_EMAIL']) {
    Write-Host "== Alerts to $($cfg['ALERT_EMAIL']) (crash loop, errors, gone quiet)"
    Invoke-Az extension add --name scheduled-query --upgrade --only-show-errors | Out-Null
    # The workspace the app's environment actually sends its logs to ($customerId, from the storage check above).
    $workspace = Invoke-Az monitor log-analytics workspace list --query "[?customerId=='$customerId'].id | [0]" -o tsv
    if (-not $workspace) { throw "Couldn't find the Log Analytics workspace ($customerId) for $AppName-env" }
    $group = Invoke-Az monitor action-group create --name "$AppName-alerts" --resource-group $ResourceGroup `
        --short-name $AppName.Substring(0, [Math]::Min(12, $AppName.Length)) `
        --action email owner $cfg['ALERT_EMAIL'] --only-show-errors --query id -o tsv
    # The first version used the RestartCount metric, which is a lifetime total per replica; replaced below.
    if (Invoke-Az monitor metrics alert list --resource-group $ResourceGroup --query "[?name=='$AppName-restarts'].name" -o tsv) {
        Invoke-Az monitor metrics alert delete --name "$AppName-restarts" --resource-group $ResourceGroup | Out-Null
    }
    # A crash loop (a bad or revoked token, a missing setting): one revision's container terminated again and again.
    # Deploys don't log ContainerTerminated for the old revision, so this only counts real exits.
    $system = "ContainerAppSystemLogs_CL | where ContainerAppName_s == '$AppName' and Reason_s == 'ContainerTerminated'"
    Invoke-Az monitor scheduled-query create --name "$AppName-crashloop" --resource-group $ResourceGroup --scopes $workspace `
        --condition "count 'CrashLoops' > 0" --condition-query "CrashLoops=$system | summarize n=count() by RevisionName_s | where n >= 3" `
        --window-size 1h --evaluation-frequency 15m --severity 2 --action-groups $group `
        --description "The Meme Bank container keeps exiting. Check: az containerapp logs show -n $AppName -g $ResourceGroup --type system" `
        --only-show-errors | Out-Null
    $logs = "ContainerAppConsoleLogs_CL | where ContainerAppName_s == '$AppName'"
    # A deposit, search or upload failed: Claude unreachable, Slack errors, storage errors. Anchored on the log
    # format's level field and the start of a traceback, so a meme title can't set it off.
    $errorLines = "Log_s matches regex @'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+ (ERROR|CRITICAL) ' or Log_s startswith 'Traceback (most recent call last)'"
    Invoke-Az monitor scheduled-query create --name "$AppName-errors" --resource-group $ResourceGroup --scopes $workspace `
        --condition "count 'Errors' > 0" --condition-query "Errors=$logs | where $errorLines" `
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

# .env.azure: same settings, pointed at the cloud storage, for running backfill/import/export from this PC. No
# storage key in it any more: the scripts sign in as you (az login), which the role assignments above allow.
$skip = "STORAGE_BACKEND", "AZURE_STORAGE_CONNECTION_STRING", "AZURE_STORAGE_ACCOUNT"
$lines = @(foreach ($k in $cfg.Keys) { if ($k -notin $skip) { "$k=$($cfg[$k])" } })
$lines += "STORAGE_BACKEND=azure", "AZURE_STORAGE_ACCOUNT=$StorageAccount"
[System.IO.File]::WriteAllLines((Join-Path (Get-Location) ".env.azure"), [string[]]$lines)  # UTF-8, no BOM

Write-Host ""
Write-Host "Done. The Meme Bank is starting up in Azure."
Write-Host "  Watch the logs:   az containerapp logs show -n $AppName -g $ResourceGroup --follow"
Write-Host "  Backfill (dry):   python -m scripts.backfill --env-file .env.azure --channel C0123456789 --dry-run"
Write-Host "  Back up memes:    python -m scripts.export --env-file .env.azure"
Write-Host "  GitHub deploys:   .\deploy\setup-github-deploy.ps1 (once), then merging to main ships code changes"
Write-Host "  Remove it all:    export first, then remove the lock (docs/DEPLOY-AZURE.md, section 12) and run az group delete -n $ResourceGroup"
