# Steam Sale Daily Digest → Slack

Posts one daily digest to a channel-scoped Slack incoming webhook for a
hand-curated list of Steam titles, with three sections:

1. 🆕 **New on sale** — titles that weren't on sale at the last digest
2. ⏰ **Ending today** — sales that expire today (last chance)
3. 📋 **All current sales** — every watched title currently discounted

It **only posts when there's a new sale or a sale ending today** — the
unchanged overview is not re-posted day after day. When it does post, all
three sections are included.

No Slack bot, no read scopes, no inbound traffic — the script only makes
outbound calls. State is a local `state.json` so "new" is computed against
the previous day.

## How it works

```
cron (daily, on your VM)
   -> notifier.py
        -> Steam IStoreBrowseService/GetItems   (batched: prices + end dates)
        -> Slack incoming webhook               (post the digest)
```

A single `GetItems` call returns each title's discount %, final/original
price, and the discount **end date**, so all three sections come from one
batched request.

## One-time setup

### 1. Create the Slack webhook
1. https://api.slack.com/apps → **Create New App** → *From scratch*.
2. **Incoming Webhooks** → toggle **On**.
3. **Add New Webhook to Workspace** → pick the LAN channel.
4. Copy the URL (`https://hooks.slack.com/services/...`). One URL = one channel.

### 2. Edit the watch list
Edit `titles.json`. The `appid` is the number in a title's store URL
(`https://store.steampowered.com/app/489830` → `489830`).

### 3. Configure the environment
Required:
```sh
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/XXX/YYY/ZZZ"
```
Optional (defaults shown):
```sh
export DISCOUNT_THRESHOLD=25     # a title counts as "on sale" only at/above this %
export STEAM_CC=DE               # region -> currency (DE = EUR)
export STEAM_LANG=english        # language of title names
```

## Run / test

```sh
python3 notifier.py --dry-run      # prints the digest JSON, posts nothing, no state change
python3 notifier.py                # posts to Slack and updates state.json
```
No third-party packages — standard library only (Python 3.8+).

## Schedule it on the VM

`crontab -e`, then post once a day at 10:00:

```cron
0 10 * * * SLACK_WEBHOOK_URL="https://hooks.slack.com/services/XXX/YYY/ZZZ" /usr/bin/python3 /opt/steam-sale-notifier/notifier.py >> /var/log/steam-sale-digest.log 2>&1
```

Adjust the path to wherever you place the folder.

## Notes / tuning
- **When it posts:** only on days with at least one new sale or a sale ending
  today. Otherwise it stays silent (state is still updated). The 📋 overview is
  included in the message, but its own changes don't trigger a post.
- **"New" vs. the previous digest:** computed from `state.json` (the set of
  titles on sale at the last run). Delete `state.json` to reset — the next run
  then treats every current sale as new (and will post if any exist).
- **"Ending today"** uses the VM's local date. Run the cron in the morning so
  the section is actionable (the sale is still live when people read it).
- **Threshold** applies to all three sections — a 10%-off title won't appear.
  Lower `DISCOUNT_THRESHOLD` to widen the net.
- **Free / unpriced / undiscounted titles** are simply omitted.
- The `GetItems` endpoint is unofficial but the same one the store front-end
  uses; it batches up to 50 appids per call.

---

*This project was written with Claude Opus 4.8 (Anthropic).*
