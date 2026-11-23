#Requires -Version 7
<#
.SYNOPSIS
    Rotates MemeBank's three secrets: the Slack bot token, the Slack app-level token and the
    Anthropic API key. Updates the Azure Container App and the local .env / .env.azure.

.DESCRIPTION
    Neither Slack nor Anthropic lets a script mint these credentials, so some steps are clicks
    you make in a browser. Each one says WHERE to go, what to click, and exactly what to COPY.
    The script does the rest and checks every value before it relies on it.

      0. Checks. Whether your az sign-in can change Azure (Azure demands MFA for that). If it
         can't, the Azure changes are done by you in the Portal, which asks for MFA itself.
      1. Anthropic: you create a new key; the script checks it works.
      2. Slack app-level token: you generate a new one; the script checks it can connect.
         The two new values are saved to .env at once, so a later failure loses nothing.
      3. Azure, before anything goes offline: the new Anthropic key and app-level token are
         stored as Container App secrets. The running bot is not affected yet.
      4. Slack bot token: the script revokes the old one, you reinstall the app and copy the
         new one. THE BOT IS OFFLINE FROM HERE until step 5 restarts it.
      5. Azure: the new bot token is stored, all three secrets are checked, then the revision
         is restarted. The script watches only the new copy of the bot and stops early,
         naming the secret, if it fails to start.
      6. Slack channels: reinstalling the app removes the bot from every channel, so you
         /invite it back, then send it one test message.
      7. Old credentials: you revoke the old app-level token and delete the old Anthropic key;
         the script checks both are dead.

    If it stops partway, run it again: it finds .env.rotation, says where it got to, and
    carries on from there. .env.rotation holds the OLD values only (needed to revoke them),
    is ignored by git and Docker, and is deleted when the rotation finishes.

    Nothing secret is printed. Values are pasted into hidden prompts (Read-Host -AsSecureString).

.PARAMETER Portal
    Make the Azure changes in the Portal even if your az sign-in could make them.

.PARAMETER AdminApiKeyPrompt
    At step 7, deactivate the old Anthropic key with an Anthropic Admin API key
    (sk-ant-admin...) instead of deleting it in the Console.

.PARAMETER Subscription
    Azure subscription that holds the app. Found automatically when left out. The script never
    changes your az default subscription.

.EXAMPLE
    cd C:\Users\Tony\source\MemeBank
    .\deploy\Rotate-Secrets.ps1
#>
[CmdletBinding()]
param(
    [string]$AppName = 'memebank',
    [string]$ResourceGroup = 'rg-memebank',
    [string]$Subscription,
    [string]$EnvFile = '.env',
    [string]$AzureEnvFile = '.env.azure',
    [string]$StateFile = '.env.rotation',
    [switch]$Portal,
    [switch]$AdminApiKeyPrompt
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$env:PYTHONIOENCODING = 'utf-8'   # az crashes on non-ASCII log lines under the Windows console code page

# Container App secret name <-> .env variable
$SecretMap = [ordered]@{
    'anthropic-api-key' = 'ANTHROPIC_API_KEY'
    'slack-app-token'   = 'SLACK_APP_TOKEN'
    'slack-bot-token'   = 'SLACK_BOT_TOKEN'
}
$Stages = @('collected', 'staged', 'revoked', 'azure-done', 'invited')   # progress, in order

# ================================================================ helpers
# Verb-Noun names on purpose: a short name such as H can be a built-in alias and run instead.

function Write-Step([string]$Text) { Write-Host ''; Write-Host "== $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text) { Write-Host "  OK  $Text" -ForegroundColor Green }
function Write-Warn([string]$Text) { Write-Host "  !!  $Text" -ForegroundColor Yellow }
function Write-Note([string]$Text) { Write-Host "      $Text" }

function Show-ManualStep([string]$Where, [string[]]$Do, [string]$Copy) {
    Write-Host ''
    Write-Host '  ---- YOUR TURN (browser) ----------------------------------------------' -ForegroundColor Yellow
    Write-Host "  WHERE: $Where" -ForegroundColor Yellow
    $n = 1
    foreach ($d in $Do) {
        if ($d.StartsWith('   ')) { Write-Host "     $($d.Trim())" }    # continuation of the step above
        else { Write-Host "  $n. $d"; $n++ }
    }
    if ($Copy) { Write-Host "  COPY:  $Copy" -ForegroundColor Yellow }
    Write-Host '  ------------------------------------------------------------------------' -ForegroundColor Yellow
}

function Show-Banner([string]$Text, [string]$Color) {
    Write-Host ''
    Write-Host ('  ' + ('#' * 72)) -ForegroundColor $Color
    Write-Host "  #  $Text" -ForegroundColor $Color
    Write-Host ('  ' + ('#' * 72)) -ForegroundColor $Color
}

function Confirm-Continue([string]$Question) {
    $answer = Read-Host "  $Question [y/N]"
    if ($answer -notmatch '^(y|yes)$') { throw "You stopped at: $Question" }
}

function Read-SecretValue([string]$Prompt, [string]$MustStartWith) {
    while ($true) {
        $secure = Read-Host "  $Prompt (paste, nothing shows, then Enter)" -AsSecureString
        $plain = [Net.NetworkCredential]::new('', $secure).Password.Trim()
        if ($plain.StartsWith($MustStartWith)) { return $plain }
        Write-Warn "That doesn't start with '$MustStartWith'. Copy it again and paste."
    }
}

# Reads a property that may be missing (Set-StrictMode throws on a plain .Name for those).
function Get-Field($Object, [string]$Name) {
    if ($null -eq $Object) { return $null }
    $p = $Object.PSObject.Properties[$Name]
    if ($p) { $p.Value } else { $null }
}

function Read-EnvValues([string]$Path) {
    $values = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $values }
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^\s*([A-Z0-9_]+)\s*=\s*(.*)$') { $values[$Matches[1]] = $Matches[2].Trim().Trim('"') }
    }
    $values
}

# Replaces (or appends) KEY=value lines, keeping everything else. UTF-8, no BOM.
function Set-EnvValues([string]$Path, [hashtable]$NewValues, [switch]$Create) {
    if (-not (Test-Path -LiteralPath $Path)) {
        if (-not $Create) { Write-Note "$Path not found - skipped."; return }
        [IO.File]::WriteAllLines((Join-Path (Get-Location) $Path), [string[]]@())
    }
    $full = (Resolve-Path -LiteralPath $Path).Path
    $lines = [System.Collections.Generic.List[string]]::new()
    $seen = @{}
    foreach ($line in [IO.File]::ReadAllLines($full)) {
        if ($line -match '^\s*([A-Z0-9_]+)\s*=' -and $NewValues.ContainsKey($Matches[1])) {
            $lines.Add("$($Matches[1])=$($NewValues[$Matches[1]])"); $seen[$Matches[1]] = $true
        } else { $lines.Add($line) }
    }
    foreach ($k in $NewValues.Keys) { if (-not $seen.ContainsKey($k)) { $lines.Add("$k=$($NewValues[$k])") } }
    [IO.File]::WriteAllLines($full, [string[]]$lines)
}

function Save-NewValues([hashtable]$Values) {
    Set-EnvValues $EnvFile $Values
    Set-EnvValues $AzureEnvFile $Values
}

function Set-Stage([string]$Stage) { Set-EnvValues $StateFile @{ STAGE = $Stage } -Create; $script:Stage = $Stage }
function Test-StageReached([string]$Stage) { $Stages.IndexOf($script:Stage) -ge $Stages.IndexOf($Stage) }

# Calls a Slack Web API method with a token and returns the parsed body. Never echoes the token.
function Invoke-SlackMethod([string]$Method, [string]$Token, [hashtable]$Body = @{}) {
    try {
        Invoke-RestMethod -Method Post -Uri "https://slack.com/api/$Method" `
            -Headers @{ Authorization = "Bearer $Token" } -Body $Body -ContentType 'application/x-www-form-urlencoded'
    } catch {
        [pscustomobject]@{ ok = $false; error = "HTTP failure calling $Method ($($_.Exception.GetType().Name))" }
    }
}

function Test-AnthropicKey([string]$Key) {
    try {
        $r = Invoke-WebRequest -Method Get -Uri 'https://api.anthropic.com/v1/models?limit=1' `
            -Headers @{ 'x-api-key' = $Key; 'anthropic-version' = '2023-06-01' } -SkipHttpErrorCheck
        return [int]$r.StatusCode
    } catch { return -1 }
}

function Test-AppToken([string]$Token) { [bool](Get-Field (Invoke-SlackMethod 'apps.connections.open' $Token) 'ok') }

# Runs az; on failure shows az's own error with any secret value replaced by ***.
function Invoke-AzQuiet([string[]]$Arguments, [string[]]$Redact = @()) {
    $errFile = [IO.Path]::GetTempFileName()
    try {
        $out = & az @Arguments 2>$errFile
        if ($LASTEXITCODE -ne 0) {
            $msg = (Get-Content -LiteralPath $errFile -Raw) + ''
            foreach ($r in $Redact) { if ($r) { $msg = $msg.Replace($r, '***') } }
            if ($msg -match 'MFA') { $msg += "`n  Azure wants an MFA sign-in for changes. Run this script again with -Portal." }
            throw "az $($Arguments[0..2] -join ' ') ... failed (exit $LASTEXITCODE):`n  $($msg.Trim())"
        }
        $out
    } finally { Remove-Item -LiteralPath $errFile -ErrorAction SilentlyContinue }
}

# True when the az sign-in for Azure Resource Manager was made with MFA. Azure refuses resource
# changes without it. A personal Microsoft account, or a silent Windows sign-in, often has no MFA
# claim even after az login. The token is decoded locally and never printed.
function Test-AzMfaSignIn([string]$SubscriptionId) {
    try {
        $tok = (& az account get-access-token --subscription $SubscriptionId --query accessToken -o tsv 2>$null)
        if (-not $tok) { return $false }
        $part = $tok.Split('.')[1].Replace('-', '+').Replace('_', '/')
        $part += '=' * ((4 - $part.Length % 4) % 4)
        $claims = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($part)) | ConvertFrom-Json
        return [bool](@(Get-Field $claims 'amr') -contains 'mfa')
    } catch { return $false } finally { $tok = $null }
}

# Compares each Azure secret with the value we hold, by SHA-256, printing neither.
function Test-AzureSecrets([string[]]$Names) {
    $sha = [Security.Cryptography.SHA256]::Create()
    $bad = @()
    try {
        foreach ($s in $Names) {
            $stored = (& az containerapp secret show @AzScope --secret-name $s --query value -o tsv 2>$null | Out-String).Trim()
            $want = $new[$SecretMap[$s]]
            $same = $stored -and ([Convert]::ToBase64String($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($stored))) -eq
                                  [Convert]::ToBase64String($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($want))))
            $stored = $null
            if ($same) { Write-Ok "Azure secret $s holds the new value" } else { Write-Warn "Azure secret $s does NOT hold the new value"; $bad += $s }
        }
    } finally { $sha.Dispose() }
    , $bad
}

# Stores secrets: with az when it can, otherwise walks you through the Portal and checks.
function Set-AzureSecrets([string[]]$Names) {
    if ($AzureMode -eq 'cli') {
        $pairs = foreach ($s in $Names) { "$s=$($new[$SecretMap[$s]])" }
        Invoke-AzQuiet (@('containerapp', 'secret', 'set') + $AzScope + @('--secrets') + $pairs + @('-o', 'none')) @($new.Values) | Out-Null
        $pairs = $null
        $bad = Test-AzureSecrets $Names
        if ($bad.Count) { throw "Azure didn't keep: $($bad -join ', ')." }
        return
    }
    while ($true) {
        $steps = @('Left menu: Security -> Secrets.')
        foreach ($s in $Names) {
            $what = switch ($s) { 'anthropic-api-key' { 'the NEW Anthropic key (sk-ant-)' } 'slack-app-token' { 'the NEW app-level token (xapp-)' } 'slack-bot-token' { 'the NEW bot token (xoxb-)' } }
            $steps += "$s : click its pencil (Edit) icon -> paste $what -> Save, and wait for 'updated'."
        }
        $steps += 'Do NOT restart anything yet - the script checks the secrets first.'
        Show-ManualStep "Azure Portal (https://portal.azure.com) -> Resource groups -> $ResourceGroup -> $AppName (type: Container App)" $steps 'the same values you gave this script (Win+V lists recent copies)'
        Confirm-Continue 'Saved them all?'
        $bad = Test-AzureSecrets $Names
        if (-not $bad.Count) { return }
        Write-Note "Paste $($bad -join ', ') again, Save, then answer y to re-check."
    }
}

# Restarts the revision (az, or you in the Portal) and returns the UTC time it was restarted.
function Restart-AzureRevision {
    $revision = Invoke-AzQuiet (@('containerapp', 'show') + $AzScope + @('--query', 'properties.latestRevisionName', '-o', 'tsv'))
    $at = [DateTime]::UtcNow.AddSeconds(-30)
    if ($AzureMode -eq 'cli') {
        Invoke-AzQuiet (@('containerapp', 'revision', 'restart', '--revision', $revision, '--resource-group', $ResourceGroup,
                          '--name', $AppName, '--subscription', $Subscription, '-o', 'none')) | Out-Null
        Write-Ok "restarted revision $revision"
    } else {
        Show-ManualStep "Azure Portal -> $AppName (Container App)" @(
            'Left menu: App -> Revisions and replicas.',
            "Click the active revision ($revision). A pane opens on the right.",
            'Click Restart at the top of that pane, and confirm.'
        ) ''
        Confirm-Continue 'Restarted, and saw "Successfully restarted revision"?'
    }
    $at
}

# Watches only bot copies started after $Since: success on "open for business", early stop on
# an auth failure (naming the likely secret). Returns $true when the bot is up.
function Wait-BotOnline([datetime]$Since) {
    $cut = $Since.ToString('yyyy-MM-ddTHH:mm:ss')
    $deadline = (Get-Date).AddMinutes(8)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 15
        $lines = @(& az containerapp logs show @AzScope --type console --tail 300 --format text 2>$null |
                   Where-Object { $_ -match '^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)' -and $Matches[1] -ge $cut })
        $text = $lines -join "`n"
        if ($text -match 'open for business') { return $true }
        if ($text -match 'token_revoked|invalid_auth|not_authed|account_inactive|BoltError') {
            $culprit = if ($text -match 'apps\.connections\.open|SocketMode|xapp') { 'slack-app-token' } else { 'slack-bot-token' }
            Write-Warn "the new bot copy failed to sign in to Slack - most likely $culprit is wrong in Azure."
            return $false
        }
        $replicas = @(& az containerapp replica list @AzScope --query "[].{c:properties.createdTime, s:properties.runningState}" -o json 2>$null | ConvertFrom-Json)
        $fresh = @($replicas | Where-Object { $c = Get-Field $_ 'c'; $c -and [datetime]::Parse($c).ToUniversalTime() -ge $Since })
        $state = if ($fresh) { ($fresh | ForEach-Object { Get-Field $_ 's' }) -join ', ' } else { 'not started yet' }
        Write-Note "waiting for the bot ... new copy: $state"
    }
    Write-Warn 'no "open for business" within 8 minutes.'
    $false
}

# Printed whenever the script stops early: what is live now, and how to carry on.
function Show-WhereYouAre([string]$Why) {
    Write-Host ''
    Write-Host '  ======================== WHERE YOU ARE NOW ========================' -ForegroundColor Magenta
    Write-Host "  Stopped because: $Why" -ForegroundColor Magenta
    $botOffline = (Test-StageReached 'revoked') -and -not (Test-StageReached 'azure-done')
    Write-Host ("  Bot: " + $(if ($botOffline) { 'OFFLINE (old bot token revoked, Azure not finished)' } elseif (Test-StageReached 'azure-done') { 'online with the new secrets' } else { 'online with the OLD secrets (nothing live changed)' })) -ForegroundColor Magenta
    Write-Host "  Done: $(if ($script:Stage) { "up to '$($script:Stage)'" } else { 'nothing yet' })" -ForegroundColor Magenta
    if ($script:Stage) {
        Write-Host '  New values collected so far are saved in .env; the old ones in .env.rotation.' -ForegroundColor Magenta
        Write-Host '  To carry on, run the same command again:  .\deploy\Rotate-Secrets.ps1' -ForegroundColor Magenta
        if ($Why -match 'MFA') { Write-Host '  ... with -Portal added, to make the Azure changes in the Portal.' -ForegroundColor Magenta }
    } else {
        Write-Host '  Nothing was changed. Run the script again whenever you are ready.' -ForegroundColor Magenta
    }
    Write-Host '  =====================================================================' -ForegroundColor Magenta
}

# ================================================================ main
$script:Stage = $null
try {
    # ------------------------------------------------------------ 0. checks
    Write-Step 'Checks'
    if (-not (Test-Path -LiteralPath $EnvFile)) { throw "No $EnvFile here. Run this from the MemeBank folder." }
    if (-not (Get-Command az -ErrorAction SilentlyContinue)) { throw 'Azure CLI (az) is not installed.' }
    & az account show -o none 2>$null; if ($LASTEXITCODE -ne 0) { throw 'Not signed in to Azure. Run: az login' }
    $startDefault = (& az account show --query name -o tsv 2>$null)

    if (-not $Subscription) {
        foreach ($id in (& az account list --query '[].id' -o tsv 2>$null)) {
            & az containerapp show --name $AppName --resource-group $ResourceGroup --subscription $id -o none 2>$null
            if ($LASTEXITCODE -eq 0) { $Subscription = $id; break }
        }
        if (-not $Subscription) { throw "Couldn't find Container App '$AppName' in '$ResourceGroup' in any subscription you can see." }
    }
    $AzScope = @('--name', $AppName, '--resource-group', $ResourceGroup, '--subscription', $Subscription)
    $status = Invoke-AzQuiet (@('containerapp', 'show') + $AzScope + @('--query', 'properties.runningStatus', '-o', 'tsv'))
    Write-Ok "found $AppName in $ResourceGroup (subscription $Subscription), status $status"

    # How Azure gets changed. Decided BEFORE anything is revoked: the first real run revoked the bot
    # token and only then found az couldn't write (no MFA), leaving the bot offline.
    if ($Portal) { $AzureMode = 'portal'; Write-Ok 'Azure changes: you, in the Portal (-Portal)' }
    elseif (Test-AzMfaSignIn $Subscription) { $AzureMode = 'cli'; Write-Ok 'Azure changes: by the script (your az sign-in has MFA)' }
    else {
        Write-Warn "your az sign-in has no MFA, so Azure won't let the script change the app."
        Write-Note 'You can make those changes in the Azure Portal instead (it asks for MFA itself); the script'
        Write-Note 'tells you exactly what to click and checks your edits. Or stop, and fix az MFA first.'
        Confirm-Continue 'Make the Azure changes in the Portal?'
        $AzureMode = 'portal'
    }

    $envNow = Read-EnvValues $EnvFile
    $channels = @(($envNow['MEME_CHANNEL_IDS'] -split '[,\s]+') | Where-Object { $_ })
    $new = @{}

    if (Test-Path -LiteralPath $StateFile) {
        # ---- carrying on from an earlier run
        $st = Read-EnvValues $StateFile
        $script:Stage = $st['STAGE']
        $old = @{}; foreach ($v in $SecretMap.Values) { $old[$v] = $st["OLD_$v"] }
        $oldBotTeam = $st['OLD_BOT_TEAM_ID']; $oldBotUser = $st['OLD_BOT_USER_ID']
        Write-Ok "carrying on from an earlier run (it got to '$($script:Stage)')"
        $new['ANTHROPIC_API_KEY'] = $envNow['ANTHROPIC_API_KEY']
        $new['SLACK_APP_TOKEN'] = $envNow['SLACK_APP_TOKEN']
        if ($envNow['SLACK_BOT_TOKEN'] -ne $old['SLACK_BOT_TOKEN']) { $new['SLACK_BOT_TOKEN'] = $envNow['SLACK_BOT_TOKEN'] }
    } else {
        # ---- fresh run
        $old = @{}; foreach ($v in $SecretMap.Values) { $old[$v] = $envNow[$v]; if (-not $old[$v]) { throw "$v is missing from $EnvFile." } }
        $oldBot = Invoke-SlackMethod 'auth.test' $old['SLACK_BOT_TOKEN']
        if (-not (Get-Field $oldBot 'ok')) { throw "The bot token in $EnvFile doesn't work ($(Get-Field $oldBot 'error')). If a run without .env.rotation already revoked it, rotate the bot token by hand." }
        $oldBotTeam = Get-Field $oldBot 'team_id'; $oldBotUser = Get-Field $oldBot 'user_id'
        Write-Ok "current bot token is bot '$(Get-Field $oldBot 'user')' in workspace '$(Get-Field $oldBot 'team')'"
    }

    Write-Host ''
    Write-Host '  Before you start:' -ForegroundColor Yellow
    Write-Note '1. Open and sign in to:  Slack app settings  https://api.slack.com/apps'
    Write-Note '                         Anthropic Console   https://platform.claude.com/settings/keys'
    if ($AzureMode -eq 'portal') { Write-Note '                         Azure Portal        https://portal.azure.com' }
    Write-Note '2. Press Win+V and turn on clipboard history: you paste some values twice (here and in the Portal).'
    Write-Note '3. Pasting into this window shows nothing (input is hidden). Paste once, then press Enter.'
    Write-Note '4. You never need to quit or sign out of the Slack app. Only the bot goes offline, from step 4'
    Write-Note '   until the restart in step 5. Steps 1-3 change nothing the bot is using.'
    Confirm-Continue 'Ready?'

    # ------------------------------------------------------------ 1. Anthropic key
    if ($new['ANTHROPIC_API_KEY'] -and $new['ANTHROPIC_API_KEY'] -ne $old['ANTHROPIC_API_KEY'] -and (Test-AnthropicKey $new['ANTHROPIC_API_KEY']) -eq 200) {
        Write-Step '1/7  New Anthropic API key'; Write-Ok 'already collected (in .env) and still works'
    } else {
        Write-Step '1/7  New Anthropic API key   (nothing live changes)'
        Show-ManualStep 'Anthropic Console -> Settings -> API keys  (https://platform.claude.com/settings/keys)' @(
            'Click "Create key". When asked "Where does your code run?", pick "Somewhere else".',
            'Name: memebank-<year>-<month>, e.g. memebank-2026-11.',
            'Expires: CHANGE the 30-day default to a long one (the bot stops working when the key expires).',
            'Linked account and Scope: the same as the old MemeBank key (normally you, and Default workspace).',
            'Click "Create key". The key is shown ONCE - copy it now. Do NOT delete the old key yet (step 7).'
        ) 'the whole new key, starting  sk-ant-  (one long line)'
        while ($true) {
            $k = Read-SecretValue 'Paste the NEW Anthropic key' 'sk-ant-'
            if ($k -eq $old['ANTHROPIC_API_KEY']) { Write-Warn 'That is the old key. Create a new one.'; continue }
            $code = Test-AnthropicKey $k
            if ($code -eq 200) { $new['ANTHROPIC_API_KEY'] = $k; Write-Ok 'new key works (models list returned 200; nothing billed)'; break }
            Write-Warn "The API answered $code for that key. Copy it again - all of it."
        }
    }

    # ------------------------------------------------------------ 2. Slack app-level token
    if ($new['SLACK_APP_TOKEN'] -and $new['SLACK_APP_TOKEN'] -ne $old['SLACK_APP_TOKEN'] -and (Test-AppToken $new['SLACK_APP_TOKEN'])) {
        Write-Step '2/7  New Slack app-level token'; Write-Ok 'already collected (in .env) and still works'
    } else {
        Write-Step '2/7  New Slack app-level token   (nothing live changes)'
        Show-ManualStep 'https://api.slack.com/apps' @(
            'On "Your Apps", click the app NAME "Meme Bank". (Not the "Generate Token" button under',
            '   "Your App Configuration Tokens" - that is a different kind of token.)',
            'You land on Basic Information. Scroll down to the "App-Level Tokens" section.',
            'Click "Generate Token and Scopes". Token Name: e.g. memebank-2026-11.',
            'Click "Add Scope", pick  connections:write , click "Generate", then "Copy".',
            'Click "Done". Leave the OLD token in the list for now (step 7).'
        ) 'the new App-Level Token, starting  xapp-'
        while ($true) {
            $t = Read-SecretValue 'Paste the NEW app-level token' 'xapp-'
            if ($t -eq $old['SLACK_APP_TOKEN']) { Write-Warn 'That is the old token.'; continue }
            if (Test-AppToken $t) { $new['SLACK_APP_TOKEN'] = $t; Write-Ok 'new app-level token can open a Socket Mode connection'; break }
            Write-Warn 'Slack refused it. It needs the connections:write scope; copy it again.'
        }
    }

    # Save now, so a failure later loses nothing (the first real run lost all three new values).
    if (-not $script:Stage) {
        $seed = @{ OLD_BOT_TEAM_ID = $oldBotTeam; OLD_BOT_USER_ID = $oldBotUser }
        foreach ($v in $SecretMap.Values) { $seed["OLD_$v"] = $old[$v] }
        Set-EnvValues $StateFile $seed -Create
        Set-Stage 'collected'
    }
    Save-NewValues @{ ANTHROPIC_API_KEY = $new['ANTHROPIC_API_KEY']; SLACK_APP_TOKEN = $new['SLACK_APP_TOKEN'] }
    Write-Ok "saved the two new values to $EnvFile and $AzureEnvFile (old values kept in $StateFile)"

    # ------------------------------------------------------------ 3. Azure, while the bot is still up
    Write-Step '3/7  Azure: store the new Anthropic key and app-level token   (the bot keeps running)'
    if (Test-StageReached 'staged') { Write-Ok 'already done' }
    else {
        Write-Note 'The running bot keeps its current values until it restarts in step 5.'
        Set-AzureSecrets @('anthropic-api-key', 'slack-app-token')
        Set-Stage 'staged'
    }

    # ------------------------------------------------------------ 4. Slack bot token
    if (-not (Test-StageReached 'revoked')) {
        Write-Step '4/7  New Slack bot token   (THE BOT GOES OFFLINE HERE)'
        Write-Note 'Slack only issues a new bot token after the old one is revoked. Revoking it also'
        Write-Note "uninstalls the app, which removes the bot from every channel (you re-add it in step 6)."
        Write-Note 'You do not need to stop anything in Slack or Azure first.'
        Confirm-Continue 'Revoke the current bot token now?'
        $rev = Invoke-SlackMethod 'auth.revoke' $old['SLACK_BOT_TOKEN']
        if (-not (Get-Field $rev 'ok')) { throw "Slack refused the revoke ($(Get-Field $rev 'error')). The bot is still online with its old token." }
        Set-Stage 'revoked'
        Show-Banner "MEMEBANK BOT IS NOW OFFLINE - old bot token revoked ($(Get-Date -Format 'HH:mm:ss'))" 'Red'
    } else {
        Write-Step '4/7  New Slack bot token   (the old one is already revoked - the bot is offline)'
    }
    if ($new['SLACK_BOT_TOKEN'] -and (Get-Field (Invoke-SlackMethod 'auth.test' $new['SLACK_BOT_TOKEN']) 'ok')) {
        Write-Ok 'already collected (in .env) and still works'
    } else {
        Show-ManualStep 'https://api.slack.com/apps -> Meme Bank -> OAuth & Permissions (left menu)' @(
            'Under "OAuth Tokens": if there is no "Bot User OAuth Token", click "Install to <workspace>"',
            '   (or "Reinstall to ...") and then "Allow". If there already is one, do NOT reinstall.',
            'Click "Copy" next to "Bot User OAuth Token". Ignore any "User OAuth Token" (xoxp-).'
        ) 'the Bot User OAuth Token, starting  xoxb-'
        while ($true) {
            $b = Read-SecretValue 'Paste the NEW bot token' 'xoxb-'
            if ($b -eq $old['SLACK_BOT_TOKEN']) { Write-Warn 'That is the old (revoked) token.'; continue }
            $nb = Invoke-SlackMethod 'auth.test' $b
            if (-not (Get-Field $nb 'ok')) { Write-Warn "Slack said: $(Get-Field $nb 'error'). Copy it again."; continue }
            if ($oldBotTeam -and ((Get-Field $nb 'team_id') -ne $oldBotTeam -or (Get-Field $nb 'user_id') -ne $oldBotUser)) {
                Write-Warn "That token is for '$(Get-Field $nb 'user')' in '$(Get-Field $nb 'team')', not the Meme Bank bot."; continue
            }
            $new['SLACK_BOT_TOKEN'] = $b
            Write-Ok "new bot token is '$(Get-Field $nb 'user')' in '$(Get-Field $nb 'team')'"; break
        }
    }
    Save-NewValues @{ SLACK_BOT_TOKEN = $new['SLACK_BOT_TOKEN'] }
    Write-Ok "saved the new bot token to $EnvFile and $AzureEnvFile"

    # ------------------------------------------------------------ 5. Azure: bot token, check, restart
    if (-not (Test-StageReached 'azure-done')) {
        Write-Step '5/7  Azure: store the new bot token, check all three, then restart'
        Set-AzureSecrets @('slack-bot-token')
        $bad = Test-AzureSecrets @($SecretMap.Keys)     # all three, before restarting (the first run restarted too early)
        if ($bad.Count) { Set-AzureSecrets $bad }
        while ($true) {
            $since = Restart-AzureRevision
            if (Wait-BotOnline $since) { break }
            Write-Note 'Fix the secret named above (paste it again), and the script restarts the bot again.'
            Confirm-Continue 'Try again?'
            Set-AzureSecrets @($SecretMap.Keys)
        }
        Set-Stage 'azure-done'
        Show-Banner "MEMEBANK BOT IS BACK ONLINE with the new secrets ($(Get-Date -Format 'HH:mm:ss'))" 'Green'
    } else {
        Write-Step '5/7  Azure'; Write-Ok 'already done - the bot is running with the new secrets'
    }

    # ------------------------------------------------------------ 6. back into the channels, and a test
    if (-not (Test-StageReached 'invited')) {
        Write-Step '6/7  Put the bot back in its channels, and test it'
        Write-Note 'The app cannot list channels (no channels:read scope), so the script cannot check this.'
        Show-ManualStep 'Slack (the normal Slack app)' @(
            "In each meme channel (MEME_CHANNEL_IDS: $($channels -join ', ') - the ID is at the bottom of a channel's About tab),",
            '   type:  /invite @Meme Bank   (if Slack says it is already there, fine).',
            'Test it, without banking anything: in that channel type   hey meme bank, find me a France meme',
            '   A reply proves the Slack tokens AND the Anthropic key (Claude does the search).'
        ) ''
        Confirm-Continue 'Invited it, and it answered the test?'
        Set-Stage 'invited'
    }

    # ------------------------------------------------------------ 7. retire the old credentials
    Write-Step '7/7  Retire the old credentials   (the bot stays online)'
    if (Test-AppToken $old['SLACK_APP_TOKEN']) {
        Show-ManualStep 'https://api.slack.com/apps -> Meme Bank -> Basic Information -> "App-Level Tokens"' @(
            'Click every token EXCEPT the one you made in step 2 (check the names).',
            'Click "Revoke" and confirm.'
        ) ''
        Confirm-Continue 'Revoked the old app-level token(s)?'
    }
    if (Test-AppToken $old['SLACK_APP_TOKEN']) { Write-Warn 'The old app-level token still works - it was not revoked. Revoke it in Slack.' }
    else { Write-Ok 'old app-level token no longer works' }

    $tail = $old['ANTHROPIC_API_KEY'].Substring($old['ANTHROPIC_API_KEY'].Length - 4)
    $deactivated = $false
    if ($AdminApiKeyPrompt -and (Test-AnthropicKey $old['ANTHROPIC_API_KEY']) -ne 401) {
        $admin = Read-SecretValue 'Paste an Anthropic ADMIN key (sk-ant-admin...)' 'sk-ant-admin'
        try {
            $hdr = @{ 'x-api-key' = $admin; 'anthropic-version' = '2023-06-01' }
            $keys = @(Get-Field (Invoke-RestMethod -Uri 'https://api.anthropic.com/v1/organizations/api_keys?status=active&limit=100' -Headers $hdr) 'data')
            $match = @($keys | Where-Object { "$(Get-Field $_ 'partial_key_hint')".EndsWith($tail) })
            if ($match.Count -eq 1) {
                Confirm-Continue "Deactivate Anthropic key '$(Get-Field $match[0] 'name')' (hint $(Get-Field $match[0] 'partial_key_hint'))?"
                Invoke-RestMethod -Method Post -Uri "https://api.anthropic.com/v1/organizations/api_keys/$(Get-Field $match[0] 'id')" -Headers $hdr `
                    -ContentType 'application/json' -Body '{"status":"inactive"}' | Out-Null
                $deactivated = $true; Write-Ok 'old Anthropic key deactivated'
            } else { Write-Note "Couldn't pick out the old key automatically ($($match.Count) matches) - use the Console." }
        } finally { $admin = $null; $hdr = $null }
    }
    if (-not $deactivated -and (Test-AnthropicKey $old['ANTHROPIC_API_KEY']) -ne 401) {
        Show-ManualStep 'Anthropic Console -> Settings -> API keys  (https://platform.claude.com/settings/keys)' @(
            "Delete the OLD MemeBank key: its hint ends in ...$tail (NOT the key you made in step 1).",
            'Also delete any other MemeBank key from an earlier, unfinished attempt (e.g. memebank-2026-11 if you are on -11b).',
            'Use the key''s "..." menu -> Delete (or Disable), and confirm.'
        ) ''
        Confirm-Continue 'Deleted the old Anthropic key(s)?'
    }
    if ((Test-AnthropicKey $old['ANTHROPIC_API_KEY']) -eq 401) { Write-Ok 'old Anthropic key no longer works' }
    else { Write-Warn 'The old Anthropic key still answers - deletion can take a minute; check the Console.' }

    Remove-Item -LiteralPath $StateFile -ErrorAction SilentlyContinue
    $old = $null; $new = $null
    Write-Host ''
    Write-Host '  Rotation finished. The old values are dead, and .env.rotation is deleted.' -ForegroundColor Green
    # The script never changes the az default, but signing in again mid-run (az login) does.
    $nowDefault = (& az account show --query name -o tsv 2>$null)
    if ($nowDefault -ne $startDefault) {
        Write-Note "Your az default subscription changed from '$startDefault' to '$nowDefault'. To put it back:"
        Write-Note "    az account set --subscription `"$startDefault`""
    } else {
        Write-Note "Your az default subscription is '$nowDefault'. If an az login before this run changed it, set it back with"
        Write-Note '    az account set --subscription "<the one you normally use>"'
    }
} catch {
    Show-WhereYouAre $_.Exception.Message
    $old = $null; $new = $null
    exit 1
}
