# Dev Agent

A personal AI developer agent you control via WhatsApp. Send a message from your phone and it works on your Mac — cloning repos, writing code, running tests, pushing commits, and creating PRs.

Built with Claude (Anthropic), FastAPI, and Twilio.

## How it works

```
You (WhatsApp) → Twilio → FastAPI server → Claude with tools → your Mac
```

You send a message. Claude figures out what needs doing, uses tools to do it, and replies with a summary. No confirmation prompts — it just gets on with it.

## Tools

| Tool | What it does |
|---|---|
| `run_shell` | Run any shell command on your Mac |
| `read_file` | Read a local file |
| `write_file` | Write or edit a local file |
| `list_files` | List files in a directory |
| `search_code` | Grep/ripgrep across a codebase |
| `clone_repo` | Clone a GitHub repo locally |
| `git_commit_push` | Stage, commit, and push changes |
| `create_pull_request` | Open a PR via GitHub API |
| `git_status` | Check branch and working tree status |
| `list_repos` | List locally cloned repos |
| `list_my_github_repos` | List all your GitHub repos (no clone needed) |
| `get_github_file` | Read a file from GitHub without cloning |
| `search_github_repos` | Search your GitHub repos by name/description |

## Setup

### 1. Clone and install

```bash
git clone https://github.com/natharvey/dev-agent
cd dev-agent
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Fill in `.env`:

```env
ANTHROPIC_API_KEY=       # console.anthropic.com
TWILIO_ACCOUNT_SID=      # console.twilio.com
TWILIO_AUTH_TOKEN=       # console.twilio.com
TWILIO_WHATSAPP_FROM=    # whatsapp:+14155238886 (sandbox) or your number
ALLOWED_WHATSAPP_NUMBER= # whatsapp:+61xxxxxxxxx — only this number can talk to Dev
GITHUB_TOKEN=            # github.com → Settings → Developer settings → PAT (repo scope)
GITHUB_USERNAME=         # your GitHub username
REPOS_DIR=./repos        # where repos get cloned
```

### 3. Expose with ngrok

```bash
brew install ngrok/ngrok/ngrok
ngrok config add-authtoken YOUR_TOKEN
ngrok http 8001
```

Copy the `https://` URL and add to `.env`:
```env
WEBHOOK_URL=https://xxxx.ngrok-free.app/webhook
```

### 4. Set Twilio webhook

In the [Twilio console](https://console.twilio.com) → Messaging → Try it out → Send a WhatsApp message → Sandbox settings, set **"When a message comes in"** to your ngrok URL.

### 5. Run

```bash
uvicorn main:app --host 0.0.0.0 --port 8001
```

Join the Twilio sandbox by sending `join <your-sandbox-phrase>` to the Twilio WhatsApp number, then start messaging.

## Commands

- `/reset` — clear conversation history
- `/help` — list capabilities

Everything else goes straight to Claude.

## Example usage

```
You: list my github repos
Dev: Here are your repos: ...

You: clone https://github.com/natharvey/myapp and run the tests
Dev: Cloned to ./repos/myapp. Running tests... 3 passed, 1 failed. The failure is in test_auth.py:42 ...

You: fix it and push
Dev: Fixed the assertion in test_auth.py, committed a3f2b1c and pushed to main.
```

## Stack

- [Claude](https://anthropic.com) — claude-opus-4-6 with tool use
- [FastAPI](https://fastapi.tiangolo.com) — webhook server
- [Twilio](https://twilio.com) — WhatsApp messaging
- [GitPython](https://gitpython.readthedocs.io) — git operations
- [httpx](https://www.python-httpx.org) — GitHub API calls
