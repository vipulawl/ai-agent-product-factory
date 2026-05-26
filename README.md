# AI Agent Product Factory

An autonomous multi-agent pipeline that discovers developer pain points in the AI/coding-agent space, prioritizes them, builds products, and ships to GitHub — with Telegram for approvals and daily digests.

## What It Does

```
Discovery Agent (every 6h)
  └─▶ scrapes Reddit, HN, GitHub Issues
  └─▶ GPT-4o extracts and clusters pain points into themes
  └─▶ writes to SQLite backlog

Priority Agent (after each discovery run)
  └─▶ scores each theme: frequency × novelty × feasibility × recency × market
  └─▶ ranks backlog

Builder Agent (daily 9am)
  └─▶ picks top-ranked unbuilt item
  └─▶ sends Telegram: "Ready to build X — reply YES <token> to proceed"
  └─▶ waits for your approval (up to 24h)
  └─▶ GPT-4o generates full project (code, README, tests, .gitignore)
  └─▶ pushes to github.com/vipulawl/<repo-name>
  └─▶ sends Telegram build-complete notification

Refiner Agent (every Monday)
  └─▶ reviews built products, opens GitHub enhancement issues
  └─▶ generates outreach messages for original Reddit/HN threads
  └─▶ sends outreach drafts to you via Telegram

Daily Digest (8am)
  └─▶ Telegram summary: posts scraped, themes added, top backlog items
```

## Setup

### 1. Clone and create venv

```bash
git clone https://github.com/vipulawl/ai-agent-product-factory
cd ai-agent-product-factory
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Fill in all values — see table below
```

### 3. Telegram bot

1. Message [@BotFather](https://t.me/BotFather) → `/newbot`
2. Copy the token → `TELEGRAM_BOT_TOKEN` in `.env`
3. Send any message to your new bot
4. Run `python scripts/get_chat_id.py` → paste result into `TELEGRAM_CHAT_ID`

### 4. Reddit API

1. Go to https://www.reddit.com/prefs/apps → "create another app"
2. Type: **script**, redirect URI: `http://localhost:8080`
3. Copy Client ID and Secret → `.env`

### 5. GitHub token (vipulawl account)

Generate a PAT at https://github.com/settings/tokens with `repo` scope on the `vipulawl` account → `GITHUB_TOKEN` in `.env`

### 6. Run

```bash
# Test a full run immediately
python orchestrator.py --run-now

# Start the scheduler (runs continuously)
python orchestrator.py

# Run individual agents
python orchestrator.py --discovery
python orchestrator.py --priority
python orchestrator.py --builder
python orchestrator.py --refiner
python orchestrator.py --digest

# Check pipeline status
python scripts/status.py
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `OPENAI_API_KEY` | ✅ | GPT-4o for all agent reasoning |
| `TELEGRAM_BOT_TOKEN` | ✅ | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | ✅ | Your personal chat ID (run `scripts/get_chat_id.py`) |
| `GITHUB_TOKEN` | ✅ | vipulawl PAT with `repo` scope |
| `REDDIT_CLIENT_ID` | ✅ | Reddit app client ID |
| `REDDIT_CLIENT_SECRET` | ✅ | Reddit app secret |
| `REDDIT_USER_AGENT` | ✅ | e.g. `ai-factory:v1.0 (by /u/yourname)` |

## Approval Flow

When the builder picks an item, you get a Telegram message like:

```
🔨 Ready to Build

Agent Debugging Replay Tool
Developers can't replay failed agent runs step-by-step...

Priority score: 7.8/10

Reply YES A3F9B2C1 to proceed.
Reply /reject A3F9B2C1 to skip.
```

Reply `YES A3F9B2C1` (or `APPROVE A3F9B2C1`) in Telegram. The builder polls every 60 seconds.

## File Structure

```
ai-agent-product-factory/
├── orchestrator.py          # Main scheduler and coordinator
├── agents/
│   ├── base.py              # OpenAI client + Telegram send/poll helpers
│   ├── discovery_agent.py   # Scrapes Reddit/HN/GitHub, extracts themes
│   ├── priority_agent.py    # Scores and ranks backlog items
│   ├── builder_agent.py     # Builds products, approval gate, GitHub push
│   └── refiner_agent.py     # Improves products, generates outreach
├── storage/
│   └── backlog.py           # SQLite schema + CRUD (data/backlog.db)
├── config/
│   ├── sources.yaml         # Subreddits, HN searches, GitHub repos to monitor
│   └── priority_rules.yaml  # Scoring weights and thresholds
└── scripts/
    ├── status.py            # Print pipeline status
    └── get_chat_id.py       # Find your Telegram chat ID
```

## Costs

- GPT-4o: ~$0.05–0.15 per discovery run, ~$0.20–0.50 per build
- Reddit/HN/GitHub APIs: free
- Telegram Bot API: free

Estimated daily spend: ~$0.30–0.80
