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
export ITAD_API_KEY=...          # optional: tag each sale with all-time-low context
```

### 4. (Optional) IsThereAnyDeal enrichment
Set `ITAD_API_KEY` to a free key from https://isthereanydeal.com/apps/my/ to tag
each sale line with how the price compares to its all-time low — either
`🔥 matches all-time low` or `all-time low was 4,99€ (Nov 2024)`. Leave it unset
to disable. It's fully degradable: no key or a failed lookup just omits the tag,
and the digest still posts.

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

## Run with Docker

Config can come from a `.env` file instead of shell `export`s (loaded by
`envfile.py`, stdlib-only). Real environment variables still win over `.env`,
so `docker run -e ...` overrides it.

```sh
cp .env.example .env      # then fill in SLACK_WEBHOOK_URL etc.
docker compose build
docker compose run --rm notifier                       # post the sale digest
docker compose run --rm availability                   # post availability changes
docker compose run --rm notifier python notifier.py --dry-run   # preview only
```

State (`state.json` / `availability_state.json`) is written to the `STATE_DIR`
env var when set — the compose file points it at a named `state` volume so
"new since last digest" survives across one-shot runs. Unset, it defaults to
next to the script (unchanged for non-Docker runs).

The curated lists `titles.json` (games) and `watchlist.json` (hardware /
coming-soon) are **bind-mounted from the host** into the containers, so editing
them on the host takes effect on the next run without rebuilding the image.
(This is also what the planned web UI will edit.)

## Deploy on a schedule (recommended: systemd timers)

For an always-on VM, run both scripts as **one-shot containers on a timer**
rather than a constantly-running container: each run is crash-isolated, nothing
sits idle in RAM, and the two scripts get independent cadences (digest daily,
availability hourly). Ready-made units and full install steps are in
[`deploy/systemd/`](deploy/systemd/README.md):

```sh
sudo cp deploy/systemd/steam-*.service deploy/systemd/steam-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now steam-notifier.timer steam-availability.timer
systemctl list-timers 'steam-*'        # confirm next run times
```

systemd timers give you `journalctl` logs per run and `Persistent=true`
catch-up after downtime. (Plain host cron calling `docker compose run --rm`
also works — see the cron example above.)

## Web UI: curate the lists from a browser

A small Flask app (`webui/`) lets you **search Steam by name and add titles** to
either list without editing JSON by hand:

- **Games** → `titles.json` (watched by `notifier.py`)
- **Hardware / coming-soon** → `watchlist.json` (watched by `availability.py`)

It writes the same files the scripts read (atomically, preserving their
`_comment` header), so the next scheduled run picks up your changes. Search uses
Steam's own (unofficial, no-key) store-search endpoint; you can also add by
`appid` directly, and the name is looked up for you.

```sh
docker compose up -d webui      # build + run, then open http://<vm-ip>:8080
# or locally without Docker:
pip install -r webui/requirements.txt
python -m webui.app             # http://localhost:8080
```

> **Access:** LAN-only, **no authentication** — it can edit your lists, so don't
> expose port 8080 to the internet. Restrict it to a LAN interface
> (`"192.168.x.y:8080:8080"` in `docker-compose.yml`) or put it behind a
> reverse proxy / VPN. It runs Flask's built-in server, which is fine for
> personal LAN use; front it with a WSGI server (waitress/gunicorn) if you want
> something sturdier.

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
- **ITAD tag** (if `ITAD_API_KEY` is set) appends all-time-low context to each
  sale line. Prices/currency follow `STEAM_CC`. Adds one lookup per on-sale title
  plus one batched history-low call, only on days a digest is posted.
- The `GetItems` endpoint is unofficial but the same one the store front-end
  uses; it batches up to 50 appids per call.

## Second mode: availability watcher (`availability.py`)

Watches "coming soon" items — upcoming **games or hardware** (Steam Frame,
Steam Machine, …) — and posts when one becomes **purchasable** or gets a
**release-date change**. Same `GetItems` endpoint, same Slack webhook; its own
`watchlist.json` and `availability_state.json`.

The signal is the presence of a `best_purchase_option` in the GetItems response
— "purchasable" regardless of game vs. hardware. An item that's already
purchasable the first time it's seen is alerted once.

```sh
python3 availability.py --dry-run   # preview, posts nothing, no state change
python3 availability.py             # posts to Slack and updates availability_state.json
```

Edit `watchlist.json` (appid + name). Cron example, daily at 09:00:

```cron
0 9 * * * SLACK_WEBHOOK_URL="https://hooks.slack.com/services/XXX/YYY/ZZZ" /usr/bin/python3 /opt/steam-sale-notifier/availability.py >> /var/log/steam-availability.log 2>&1
```

- **When it posts:** only when something changes — an item becomes purchasable,
  or its release-date message changes. Otherwise silent (state still updated).
- **Already-available items** are alerted on first run, then go quiet. Delete
  `availability_state.json` to re-baseline.
- **Poll more often near a launch** if you want a faster alert (e.g. hourly):
  swap the cron to `0 * * * *`. "Purchasable" means *listed for sale* — it does
  not guarantee in-stock.

---

*This project was written with Claude Opus 4.8 (Anthropic).*
