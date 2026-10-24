# Deploying the Meme Bank to Azure: step by step

This guide takes the project from a folder on your PC to a bot running in your Azure subscription and answering in #general. It takes 30–45 minutes the first time, and most of that is waiting on builds.

**What you'll end up with**

```
Slack (makercrew)  ◄── outbound websocket ──  Container App "memebank" (1 copy, 0.25 vCPU / 0.5 GiB)
                                                 │      in Container Apps environment "memebank-env"
                                                 │      (+ Log Analytics workspace for its logs)
                                                 ├──►  Storage account "memebank…": Table "memes" + Blob container "memes"
                                                 └──►  Anthropic API (Claude)
Container registry (created by the build) holds the bot's image.
All of it lives in one resource group: rg-memebank.
```

---

## 1. One-time prerequisites on your PC

Open **PowerShell** (Windows PowerShell 5.1 or PowerShell 7 both work) in `C:\Users\Tony\source\MemeBank`.

| Need | Check | Install if missing |
|---|---|---|
| Azure CLI | `az --version` | `winget install -e --id Microsoft.AzureCLI`, then open a new terminal |
| Python 3.10+ | `python --version` | `winget install -e --id Python.Python.3.12` |
| An Azure subscription you can create resources in | `az login`, then `az account list -o table` | portal.azure.com |

If PowerShell refuses to run the script ("running scripts is disabled"), allow it for this window only:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

Install the bot's Python packages once. You'll need them for the local test and for the backfill and import scripts:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

---

## 2. Create the Slack app

1. Go to <https://api.slack.com/apps>, choose **Create New App**, then **From a manifest**, and pick the **makercrew** workspace.
2. Choose YAML, paste the whole of `slack-manifest.yml`, then click **Next** and **Create**.
3. In the left menu, open **Install App** and click **Install to makercrew**, then **Allow**.
   - If Slack says the app needs approval, a workspace owner or admin approves it under *Manage apps*. Kevin may be the one to ask.
   - The free plan allows up to 10 apps or integrations in a workspace.
4. Still on **Install App**, copy the **Bot User OAuth Token**. It starts with `xoxb-`.
5. In the left menu, open **Basic Information**, scroll to **App-Level Tokens**, and click **Generate Token and Scopes**.
   - Name: `socket`. Add the scope `connections:write`, then click **Generate**.
   - Copy the token. It starts with `xapp-`.
6. In Slack, go to #general and send `/invite @Meme Bank`.

If you change the scopes in the manifest later, Slack asks you to **reinstall** the app. The `xoxb-` token stays the same.

---

## 3. Get an Anthropic API key

1. Go to <https://console.anthropic.com> and open **API keys**, then **Create key**. Name it `memebank` and copy it (`sk-ant-…`).
2. Recommended: under **Billing** or **Limits**, set a monthly spend limit (for example $10). That caps the cost if anything misbehaves.

---

## 4. Fill in settings

```powershell
Copy-Item .env.example .env
notepad .env
```

Paste in the three secrets: `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` and `ANTHROPIC_API_KEY`. The rest is already set for makercrew:

- `MEME_CHANNEL_IDS=C6UTZF23Z` is #general.
- `ADMIN_USER_IDS=U01D6UG5KFH` is you. You can withdraw any meme; everyone else can only withdraw their own deposits.
- `ACK_MODE=thread` replies with the tags. Set it to `reaction` if you only want the :bank: reaction.

`.env` holds secrets. It's listed in `.gitignore` and `.dockerignore`, so it never goes into Git or the container image.

---

## 5. Optional, recommended: a 5-minute local test

This tests the Slack and Claude side before Azure is involved.

```powershell
.venv\Scripts\Activate.ps1
python -m memebank
```

You should see `Meme Bank open for business (local storage, 0 memes)`. Then, in #general:

1. Post any image with "a meme for the meme bank". It should react :bank: and reply "Banked as #1 …" in a thread.
2. Send `hey meme bank, stats`.

Press **Ctrl+C** to stop it. If you leave it running, Slack splits incoming events between the local copy and the Azure copy, so some memes would end up in the wrong bank. The local test data in `.\memebank-data` doesn't carry over to Azure. Delete that folder whenever you like.

---

## 6. Sign in to Azure and pick the subscription

```powershell
az login
az account list -o table
az account set --subscription "<name or id from the list>"
az account show -o table
```

The deploy script uses whichever subscription `az account show` reports.

---

## 7. Run the deploy script

```powershell
.\deploy\deploy.ps1
```

Optional parameters: `-Location westus3` (default `westus2`), `-ResourceGroup rg-memebank`, `-AppName memebank`, `-StorageAccount <name>`.

What it does, in order:

| Step | What happens | Time |
|---|---|---|
| Checks | Reads `.env` and stops if a secret is missing. Shows the signed-in subscription. Adds the `containerapp` CLI extension and registers `Microsoft.App` and `Microsoft.OperationalInsights` on the subscription. | ~1 min |
| Resource group | Creates `rg-memebank` in the region. | seconds |
| Storage | Creates a Standard LRS StorageV2 account named `memebank` plus 8 random characters, with TLS 1.2 minimum and no public blob access. Reads its connection string. | ~30 s |
| Protection | Turns on 30-day soft delete for blobs and containers, and puts a `CanNotDelete` lock (`keep-the-memes`) on the storage account, so the memes can't be deleted along with the resource group by accident. | seconds |
| Build and deploy | `az containerapp up --source .` uploads the folder, builds the Dockerfile in the cloud, pushes the image to a new container registry, and creates the `memebank-env` environment (with a Log Analytics workspace) and the `memebank` app. | 5–10 min |
| Ingress | Removes the public web endpoint if the build added one. The bot only makes outbound connections. | seconds |
| Secrets | Stores the two Slack tokens, the Anthropic key and the storage connection string as Container App secrets. | seconds |
| Settings | Points the environment variables at those secrets, sets `STORAGE_BACKEND=azure` and your channel and admin settings, sizes the app at 0.25 vCPU / 0.5 GiB, and pins it at exactly 1 replica. | ~1 min |
| `.env.azure` | Writes your settings plus the storage connection string to `.env.azure`, for the backfill and import scripts. It's git-ignored and contains secrets. | — |

During the build step the app may start before its secrets exist, crash, and restart. That's expected. The settings step fixes it.

**Check it's running:**

```powershell
az containerapp logs show -n memebank -g rg-memebank --follow
```

Look for `Meme Bank open for business (azure storage, 0 memes)`. Press Ctrl+C to stop following the logs; the bot keeps running. Then send `hey meme bank, stats` in #general.

---

## 8. Bank the existing posts and the two rescued memes

Do this soon. On the free plan the bot can only read the last 90 days of the channel, and the 27 July post ages out on 25 October.

```powershell
.venv\Scripts\Activate.ps1
python -m scripts.backfill --env-file .env.azure --channel C6UTZF23Z --dry-run
python -m scripts.backfill --env-file .env.azure --channel C6UTZF23Z
```

The dry run lists what it found. The real run banks the posts oldest first, so the numbering follows posting order. It doesn't react or reply on the old posts. Add `--ack` if you want it to.

Then import the two Thomas Sowell memes from Downloads, adjusting the file names to whatever they're called there:

```powershell
python -m scripts.import_local --env-file .env.azure "$env:USERPROFILE\Downloads\image.png" --date 2026-07-27 --posted-by U01D6UG5KFH --caption "This meme needs to be added again to the meme bank"
python -m scripts.import_local --env-file .env.azure "$env:USERPROFILE\Downloads\image (1).png" --date 2026-08-01 --posted-by U01D6UG5KFH --caption "A Sowell meme for the meme bank that is 100% confirmed by my Visual Studio career"
```

If the backfill already banked a post, its import is recognised as a duplicate and skipped.

The bot picks up memes added by these scripts within a minute.

---

## 9. Day-to-day

- **Ship a code change:** edit the code, then re-run `.\deploy\deploy.ps1`. It rebuilds and redeploys into the same resources.
- **Change a setting** (channels, ACK_MODE, models): edit `.env`, then re-run the script.
- **Rotate a key:** put the new value in `.env` and re-run the script.
- **Restart:** `az containerapp revision restart -n memebank -g rg-memebank --revision (az containerapp revision list -n memebank -g rg-memebank --query "[0].name" -o tsv)`
- **See the app in the portal:** portal.azure.com, then **Resource groups**, then **rg-memebank**, then **memebank**. *Log stream* shows live logs, and *Revisions and replicas* shows health.
- **Look at the stored memes:** open the storage account and use *Storage browser*. Tables → `memes` is the catalogue, and Blob containers → `memes` holds the images.
- **Back up the memes:** `python -m scripts.export --env-file .env.azure` copies the catalogue (`memes.json`) and every image to `.\memebank-backup`. Re-running it only downloads new images. Do this every so often, and always before removing anything.
- **Undo a `forget`:** `hey meme bank, restore #12`. A forgotten meme is only hidden, so it keeps its number and image. Deleted or overwritten images are also kept for 30 days by blob soft delete, which you can see under the storage account's *Data protection* settings.

---

## 10. Keep an eye on cost

What runs continuously, and how it's billed:

- **Container App, 1 replica, 0.25 vCPU / 0.5 GiB.** On the Consumption plan, a replica sitting at its minimum count with almost no CPU or network use is billed at the lower *idle* rate. A monthly free grant (180,000 vCPU-seconds and 360,000 GiB-seconds per subscription) covers part of it.
- **Container registry** created by the build. A Basic registry is a few dollars a month.
- **Log Analytics** ingestion for the bot's logs. Small at this volume.
- **Storage:** a few MB of tables and images, so pennies.
- **Claude:** roughly a cent or two per deposit or search at the default model. `TAG_MODEL` / `SEARCH_MODEL` set to `claude-haiku-5-5` is cheaper.

My estimate is a few dollars to about $20 a month in total. Check the real figure after the first week.

**Set a budget alert:** in the portal, go to **Cost Management**, then **Budgets**, then **Add**. Scope it to `rg-memebank`, set a monthly amount (say $20), and add an alert to tony@schnauzergroup.com at 80%.

---

## 11. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Bot ignores everything | It isn't in the channel: run `/invite @Meme Bank`. Or check the logs for a startup error (missing setting, bad token). |
| `invalid_auth` / `not_authed` in logs | Wrong or revoked `xoxb-` token. Fix it in `.env` and re-run the deploy. |
| Startup error about Socket Mode / `xapp` | The app-level token is missing `connections:write`, or Socket Mode is off in the Slack app settings. |
| "Couldn't bank that one: Got a web page instead of an image" | The `files:read` scope is missing. Reinstall the app from the manifest. |
| Memes posted back as links instead of images | The `files:write` scope is missing. Reinstall the app. |
| `missing_scope` in logs | You changed scopes without reinstalling. Go to **Install App**, then **Reinstall**. |
| Claude errors 401 / 403 | Bad Anthropic key. Fix it in `.env` and re-run the deploy. |
| Claude errors 429 / overloaded | Temporary. Searches fall back to keyword matching. A failed deposit can be retried by removing and re-adding the :bank: reaction. |
| Some memes missing from the bank | A second copy, such as the local test, was running at the same time. Stop it. |
| Deploy fails on provider registration | Registration can take a few minutes the first time. Wait and re-run. |
| Deploy fails: storage name taken | Pass `-StorageAccount memebank<something unique>`. |
| Deploy fails with `AADSTS50076` … "you must use multi-factor authentication" | Azure requires MFA to create or change resources, while reads still work without it. With a personal Microsoft account, turn on two-step verification at account.microsoft.com/security. Then make the CLI sign in through the browser instead of Windows' silent sign-in: `az config set core.enable_broker_on_windows=false`, `az account clear`, `az login --tenant <tenant id>`. The browser sign-in has to ask for your second factor. |
| `UnicodeEncodeError: 'charmap' codec can't encode…` from `az` | The CLI on Windows can't print some characters when its output is piped or redirected, for example the ⚡ in the bot's startup log. Run the command directly in a terminal, or use *Log stream* in the portal. The Dockerfile turns off pip's progress bar so the cloud-build log stays plain ASCII for the same reason. |
| Replica keeps restarting | Run `az containerapp logs show -n memebank -g rg-memebank --type system`. Usually a missing setting. |

---

## 12. Removing it

This deletes everything, **including the stored memes**, so back them up first. The storage account has a delete lock (`keep-the-memes`), so `az group delete` fails until you remove it on purpose:

```powershell
python -m scripts.export --env-file .env.azure --out memebank-backup   # the catalogue (memes.json) plus every image
$sa = az storage account list -g rg-memebank --query "[0].name" -o tsv
az lock delete --name keep-the-memes -g rg-memebank --resource $sa --resource-type Microsoft.Storage/storageAccounts
az group delete -n rg-memebank
```

Afterwards, delete the Slack app at api.slack.com/apps and revoke the Anthropic key.
