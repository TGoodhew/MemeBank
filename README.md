# The Meme Bank

[![Tests](https://github.com/TGoodhew/MemeBank/actions/workflows/test.yml/badge.svg)](https://github.com/TGoodhew/MemeBank/actions/workflows/test.yml)

A Slack bot for your meme channel that takes the running gag literally. When someone posts "a meme for the meme bank", it keeps a copy, has Claude look at the picture and tag it, and files it. Later anyone can ask for it back:

> **hey meme bank, could you find me the France meme**
> **hey meme bank, what have you got on socialism?**

It reposts the best match with its number, and lists any runners-up.

Because the bank keeps its own copy of every image, a meme survives after Slack's free plan hides posts older than 90 days. You can take off and nuke the site from orbit and still not lose the memes.

## Using it in the channel

| You post | The bank does |
|---|---|
| an image saying it's for the bank: "a meme for the meme bank", "store this in the meme bank", "add it to the meme bank", or :bank: in the text | banks it, reacts :bank:, and replies in a thread with its number and tags. It also DMs whoever banked it the full tag list: reply `tag it …` there (or just the words, in that DM's thread) to add more |
| a link whose preview has a picture (e.g. an x.com post), saying it's for the meme bank | banks the preview picture once Slack unfurls the link |
| a :bank: reaction on any image post | banks that post |
| an image sent to the bot in a DM, or posted with an @mention of it | banks it |
| an image that only *mentions* the meme bank ("this is why the meme bank is important"), or asks about it ("did this go in the meme bank?") | doesn't bank it: DMs the poster "Want me to bank it?". Reply `yes` or `no` in that DM (or react :bank: on the post) |
| a meme that's already in the bank | replies "Already in the vault as #7" and doesn't bank it twice (it catches re-screenshots and resized copies too, while a different meme on the same template, with different text, is banked as new) |
| `hey meme bank, find me the France meme` | searches by meaning, so a Macron or Eiffel Tower meme still counts as "France" |
| `hey meme bank, #12` | posts meme 12 |
| `hey meme bank, random` | dealer's choice |
| `hey meme bank, stats` | how many memes it has, plus the most common tags |
| `hey meme bank, forget #12` | from a bank manager (ADMIN_USER_IDS): takes it out of the vault (hidden, not deleted). From anyone else: asks the bank managers by DM, and they decide |
| `hey meme bank, restore #12` | puts a forgotten meme back, with the same number (bank managers only) |
| `hey meme bank, tag #12 ilhan omar, cabin trip` | adds tags (anyone can). Use it for names and in-jokes: Claude only names people whose names are written in the image or the caption |
| `hey meme bank, untag #12 cabin trip` | removes a tag (the person who deposited it, or an admin) |
| `hey meme bank, retag #12` | has Claude tag it again. Tags people added are kept |
| `hey meme bank` or `hey meme bank, help` | shows the help |

It also responds to "yo/hi/ok meme bank …", "Meme bank: …", an @mention, or a DM, where everything you type without an image is treated as a question. Plain chat that happens to mention the meme bank, like "this is why the meme bank is important", gets ignored. If it has an image, the poster gets a DM asking whether to bank it, and nothing is banked unless they say yes. Every meme the bank posts ends with its number and how to tag it or ask for its removal.

## How it works

```
Slack ──(Socket Mode, outbound websocket)──► bot (Azure Container Apps, 1 replica)
                                               ├─ Claude: tags each image; picks the best matches for a question
                                               ├─ Azure Table Storage: the catalogue (number, title, tags, text, who/when)
                                               └─ Azure Blob Storage: the images themselves
```

Socket Mode lets the bot connect out to Slack, so it needs no public URL, web server, or certificate. Search runs in two steps. Claude reads the whole catalogue and picks the matches by meaning. If Claude can't be reached, a keyword match over the tags, title, and image text takes over.

## Setup (about 20 minutes)

You need a Slack workspace where you can add apps, an Anthropic API key, an Azure subscription, and PowerShell (Windows, or PowerShell 7 on macOS/Linux) with the Azure CLI. To have code changes deploy themselves, use your own fork or copy of this repo. For a full walk-through of every step, including what the deploy script creates, cost, and troubleshooting, see **[docs/DEPLOY-AZURE.md](docs/DEPLOY-AZURE.md)**.

**1. Create the Slack app**
- Go to https://api.slack.com/apps, choose **Create New App**, then **From a manifest**, and pick your workspace.
- Paste in the contents of `slack-manifest.yml` and create the app.
- Go to **Install App** and install it to the workspace. If the workspace requires approval for apps, a workspace owner has to approve it.
- Copy the **Bot User OAuth Token** (`xoxb-…`).
- Go to **Basic Information**, then **App-Level Tokens**, and choose **Generate Token and Scopes**. Name it `socket` and add the scope `connections:write`. Copy the token (`xapp-…`).
- In your meme channel, type `/invite @Meme Bank`.

**2. Get an Anthropic API key** at https://console.anthropic.com, under API keys.

**3. Fill in settings.** Copy `.env.example` to `.env` and paste in the three tokens. Set `MEME_CHANNEL_IDS` to the channel's ID (at the bottom of the channel's *About* tab) and `ADMIN_USER_IDS` to your Slack member ID (your profile, **⋮**, **Copy member ID**), which makes you the bank manager.

**4. Optional: try it on your PC first**
```powershell
python -m venv .venv; .venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m memebank          # STORAGE_BACKEND=local keeps everything in .\memebank-data
```
Post a test meme. Press Ctrl+C to stop the bot before you deploy, so only one copy is answering.

**5. Deploy to Azure**
```powershell
az login
.\deploy\deploy.ps1          # optional: -Location westus3 -ResourceGroup rg-memebank
```
The script creates the resource group, the storage account, and the container app, which it builds in the cloud from the Dockerfile. It stores the tokens as Container App secrets and pins the app to exactly one running copy. It also writes `.env.azure` for the scripts below. Re-run it to change settings or secrets.

**6. Let GitHub deploy code changes**
```powershell
gh auth login
.\deploy\setup-github-deploy.ps1
```
After this one-time setup, every merge to `main` that passes the tests deploys itself (Actions → Deploy).

**7. Bank what's already in the channel**
```powershell
python -m scripts.backfill --env-file .env.azure --channel C0123456789 --dry-run   # list what it found
python -m scripts.backfill --env-file .env.azure --channel C0123456789             # bank them, without posting in the channel
```
The backfill can only see posts Slack still shows, which on the free plan means the last 90 days, so run it soon after setup. Running it again is harmless: posts already banked are skipped.

To bank image files from your PC (for example, memes that have aged out of Slack), use `scripts.import_local`:
```powershell
python -m scripts.import_local --env-file .env.azure "$env:USERPROFILE\Downloads\image.png" --date 2026-07-27 --posted-by U0123456789 --caption "what was said when it was posted"
```
A file that's already in the bank is recognised as a duplicate and skipped.

## Running it

- **Ship a code change:** merge it to `main`. The Tests workflow runs, then the Deploy workflow builds the image and moves the bot onto it. Watch it under the repo's *Actions* tab.
- **Logs:** `az containerapp logs show -n memebank -g rg-memebank --follow`
- **Alerts:** set `ALERT_EMAIL` in `.env` and the deploy adds email alerts for a crash loop, logged errors, and the bot going quiet (no connected heartbeat for 2 hours). See [docs/DEPLOY-AZURE.md](docs/DEPLOY-AZURE.md#9-day-to-day).
- **Settings:** edit `.env` and re-run `deploy.ps1` from an up-to-date `main` (it rebuilds the image from your folder too). `ACK_MODE=reaction` makes it react with :bank: without replying in a thread. `DM_TAGS=off` stops the tag DM after each deposit. `MEME_CHANNEL_IDS` takes a comma-separated list if you want more channels.
- **Models:** `TAG_MODEL` and `SEARCH_MODEL` default to `claude-sonnet-5-5`. Set either to `claude-haiku-5-5` for cheaper tagging or searching.
- **Cost:** a container app with one small replica running all the time, a storage account holding a few MB, and possibly a Basic container registry created by the cloud build. Expect a few dollars to roughly $20 a month on the Azure side, so check Cost Management after the first week. Claude usage at a channel's meme rate is small: roughly a cent or two per deposit or search.
- **Back up:** `python -m scripts.export --env-file .env.azure` copies the catalogue and every image to `.\memebank-backup`. The scripts reach storage as you, with no key, so be signed in with `az login`.
- **Remove everything:** export first. The storage account has a delete lock, so see [docs/DEPLOY-AZURE.md](docs/DEPLOY-AZURE.md#12-removing-it) for removing the lock, then `az group delete -n rg-memebank`. This deletes the stored memes too.

## Code

```
memebank/
  __main__.py   python -m memebank: settings, storage, Socket Mode, hourly heartbeat log line
  config.py     settings from environment variables / .env
  triggers.py   decides deposit vs ask-first vs question, parses commands
  core.py       deposits (download, dedupe, tag, store, acknowledge), "want me to bank it?" DMs, and answers
  tagger.py     Claude prompts: image -> title/description/text/tags; question -> best matches
  search.py     Claude pick with keyword fallback
  store.py      AzureStore (Table + Blob) and LocalStore (SQLite + folder)
  images.py     download, resize for Claude, perceptual hash for reposts
  slack_app.py  Bolt event wiring (messages, link-preview edits, :bank: reactions, yes/no answers, DM tagging)
scripts/        backfill.py, import_local.py, export.py (backup to a folder)
tests/          pytest; test_azure_store.py runs against Azurite with AZURITE=1
deploy/         deploy.ps1 (Azure resources, settings, secrets), setup-github-deploy.ps1 (one-time Actions sign-in)
.github/        workflows: test.yml (every push to main and pull request), deploy.yml (after tests pass on main)
```
Run the tests with `pip install pytest` and then `pytest`. The `AzureStore` tests are skipped unless the Azurite storage emulator is running: start it with `npx azurite --inMemoryPersistence --skipApiVersionCheck --loose` and, in another terminal, run `$env:AZURITE=1; pytest tests/test_azure_store.py`. GitHub Actions runs both on every push to `main` and every pull request.

## License

[MIT](LICENSE)
