# 🐋 Bankr Whale Alert Bot

Monitors [Bankr](https://bankr.bot/launches) token launches on Base chain.  
Alerts on Telegram when a token is launched by an X account with **10K+ followers**.

## How it works

1. Polls `https://api.bankr.bot/token-launches` every 30 seconds
2. For each new launch, checks if the deployer has an X/Twitter account
3. Looks up the account's follower count
4. If 10K+ followers → sends Telegram alert with token details + trading links

## Setup

### 1. Create Telegram Bot
- Message [@BotFather](https://t.me/BotFather) on Telegram
- Send `/newbot` and follow the prompts
- Copy the bot token

### 2. Get Chat ID
- Send `/start` to your new bot
- Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
- Find `"chat":{"id":` — that's your chat ID (use group ID for groups)

### 3. Deploy on Railway
- Push this repo to GitHub
- Connect to Railway
- Add environment variables (see below)

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | ✅ | — | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | ✅ | — | Chat/group ID for alerts |
| `MIN_FOLLOWERS` | ❌ | `10000` | Minimum follower count to trigger alert |
| `POLL_INTERVAL` | ❌ | `30` | Seconds between API polls |

## Local Testing

```bash
pip install -r requirements.txt
TELEGRAM_BOT_TOKEN=xxx TELEGRAM_CHAT_ID=xxx python main.py
```
