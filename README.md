# AI Agent Product Factory

An autonomous multi-agent pipeline that discovers developer pain points in the AI/coding-agent space, prioritizes them, builds products, and ships to GitHub — with Telegram for approvals and daily digests.

## What It Does

```
Discovery Agent (every 6h)
  └─▶ scrapes Reddit, HN, GitHub Discussions
  └─▶ GPT-4o extracts and clusters pain points into themes
  └─▶ writes to SQLite backlog

Priority Agent (after each discovery run)
  └─▶ scores each theme: frequency × novelty × feasibility × recency × market
  └─▶ ranks backlog

Builder Agent (daily)
  └─▶ picks top-ranked unbuilt item
  └─▶ sends Telegram: "Ready to build X — reply YES to proceed"
  └─▶ waits for your approval (up to 24h)
  └─▶ GPT-4o generates full project (code, README, tests, CI)
  └─▶ pushes to github.com/vipulawl/<repo-name>
  └─▶ sends Telegram build complete notification

Refiner Agent (weekly)
  └─▶ reviews built products for improvements
  └─▶ opens PRs with enhancements
  └─▶ replies to original Reddit/HN threads announcing the tool
```

## Setup

### 1. Clone and create venv

```bash
git clone https://github.com/vipulawl/ai-agent-product-factory
cd ai-agent-product-factory
git submodule update --init --recursive  # pulls Hermes
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e hermes/  # install Hermes as library
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your API keys (see section below)
```

### 3. Telegram bot setup

1. Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot`
2. Copy the bot token → `TELEGRAM_BOT_TOKEN` in `.env`
3. Start a chat with your new bot, send any message
4. Run `python scripts/get_chat_id.py` to get your `TELEGRAM_CHAT_ID`

### 4. Reddit API setup

1. Go to https://www.reddit.com/prefs/apps → "create another app"
2. Type: **script**, redirect URI: `http://localhost:8080`
3. Copy Client ID and Secret → `.env`

### 5. Hermes gateway (optional — for richer Telegram experience)

```bash
cd hermes
hermes gateway setup  # choose Telegram, paste same bot token
hermes gateway start  # runs in background
# Copy our hook into Hermes so it handles incoming approvals:
cp ../hermes_integration/approval_hook.py ~/.hermes/hermes-agent/gateway/builtin_hooks/
```

Without Hermes gateway: the pipeline uses its own Telegram poller for receiving replies.

### 6. Run

```bash
# Run all agents once (good for testing)
python orchestrator.py --run-now

# Start the scheduler (runs continuously on cron)
python orchestrator.py

# Run individual agents
python -m agents.discovery_agent
python -m agents.priority_agent
python -m agents.builder_agent
python -m agents.refiner_agent
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `OPENAI_API_KEY` | ✅ | GPT-4o for all agent reasoning |
| `TELEGRAM_BOT_TOKEN` | ✅ | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | ✅ | Your personal chat ID with the bot |
| `GITHUB_TOKEN` | ✅ | GitHub PAT with `repo` scope (for vipulawl account) |
| `REDDIT_CLIENT_ID` | ✅ | Reddit app client ID |
| `REDDIT_CLIENT_SECRET` | ✅ | Reddit app secret |
| `REDDIT_USER_AGENT` | ✅ | e.g. `ai-factory:v1.0 (by /u/yourname)` |
| `HERMES_GATEWAY_URL` | optional | `http://localhost:8642` if using Hermes gateway |

## File Structure

```
ai-agent-product-factory/
├── orchestrator.py          # Main scheduler and coordinator
├── agents/
│   ├── discovery_agent.py   # Scrapes Reddit/HN/GitHub, extracts pain points
│   ├── priority_agent.py    # Scores and ranks backlog items
│   ├── builder_agent.py     # Builds products, pushes GitHub, Telegram approval
│   └── refiner_agent.py     # Improves products, does outreach
├── storage/
│   └── backlog.py           # SQLite schema + CRUD (data/backlog.db)
├── config/
│   ├── sources.yaml         # Subreddits, HN searches, GitHub repos to monitor
│   └── priority_rules.yaml  # Scoring weights
├── hermes_integration/
│   └── approval_hook.py     # Hermes builtin_hook for routing Telegram replies
├── skills/
│   └── factory_status.md    # Hermes skill: browse backlog via chat
├── scripts/
│   └── get_chat_id.py       # Helper to find your Telegram chat ID
└── builds/                  # Generated project files (before GitHub push)
```

## Costs

- GPT-4o: discovery runs ~$0.05–0.15/run (depending on posts scraped), builder run ~$0.20–0.50
- Reddit/HN APIs: free
- GitHub API: free within rate limits

Daily cost estimate: ~$0.30–0.80 (4 discovery + 1 builder + 1 priority per day)
