# Steam Sale Change Alerts → Slack

Posts to a channel-scoped Slack incoming webhook for a hand-curated list of
Steam titles, **only when something actually changes**, with two sections:

1. 🆕 **New on sale** — titles that weren't on sale at the last run
2. 💸 **Cheaper than before** — already on sale, but now at a new lower price
   (or a deeper discount) than the best deal we've already shown

A title that was on sale yesterday and is **still on sale at the same (or a
worse) price today is not re-posted** — you only hear about genuine changes.
When nothing changed, it stays silent.

No Slack bot, no read scopes, no inbound traffic — the script only makes
outbound calls. State is a local `state.json` recording the best price seen for
each on-sale title, so "new" and "cheaper" are computed against previous runs.

## How it works

```
cron / systemd timer (on your VM)
   -> notifier.py
        -> Steam IStoreBrowseService/GetItems   (batched: prices + end dates)
        -> Slack incoming webhook               (only if a sale changed)
```

A single `GetItems` call returns each title's discount %, final/original
price, and the discount **end date**, so both sections come from one batched
request. Because it stays silent unless something changed, you can run it as
often as you like.

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
python3 notifier.py --dry-run      # prints the message JSON, posts nothing, no state change
python3 notifier.py                # posts to Slack (if anything changed) and updates state.json
```
No third-party packages — standard library only (Python 3.8+).

## Deploy on an always-on server (Docker + systemd)

The intended production setup: both scripts run as **one-shot containers on
systemd timers**. Each run starts fresh (crash-isolated), nothing sits idle in
RAM, and the two get independent cadences — **sale alerts daily**, **hardware /
availability every 20 min**. State (price baselines, "new since last run")
survives across runs in a named Docker volume.

### Prerequisites

- **Docker + the Compose plugin** — `docker compose version` should work.
- A user that can talk to Docker **without sudo**
  (`sudo usermod -aG docker <you>`, then log out/in once), or plan to run the
  units as `root`.
- This repo checked out on the server. The units assume the path
  `/home/vuksan/steam-sale-notifier` and user `vuksan` — edit the `.service`
  files if yours differ (see below).

### 1. Configure `.env`

Config comes from a `.env` file (loaded by `envfile.py`, stdlib-only). It is
**gitignored**, so secrets never land in git. Real environment variables
(`docker run -e …`) still override it.

```sh
cp .env.example .env      # then fill in SLACK_WEBHOOK_URL
```

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `SLACK_WEBHOOK_URL` | ✅ | — | Channel-scoped Slack incoming webhook |
| `DISCOUNT_THRESHOLD` | | `25` | Min discount % to count as "on sale" |
| `STEAM_CC` | | `DE` | Storefront region → currency (DE = EUR) |
| `STEAM_LANG` | | `english` | Language of title names |
| `ITAD_API_KEY` | | _(unset)_ | Optional all-time-low enrichment (see above) |
| `STATE_DIR` | | next to script | Where state JSON is written; compose sets `/app/data` |

Then edit the two watch lists (or use the [web UI](#optional-web-ui-to-curate-the-lists) below).
The `appid` is the number in a store URL
(`https://store.steampowered.com/app/489830` → `489830`):

- `titles.json` — games watched by `notifier.py`
- `watchlist.json` — hardware / coming-soon items watched by `availability.py`

### 2. Build the image and smoke-test

```sh
docker compose build
docker compose run --rm notifier python notifier.py --dry-run        # preview, posts nothing
docker compose run --rm availability python availability.py --dry-run
```

Pre-building means the first timer run doesn't block on a build. A `--dry-run`
prints the message JSON without posting or touching state.

### 3. Install the systemd timers

Ready-made units are in [`deploy/systemd/`](deploy/systemd/README.md); they call
`docker compose run --rm <service>`, reusing `.env` and the `state` volume.

| Unit | Schedule | Runs |
|------|----------|------|
| `steam-notifier.{service,timer}` | daily 09:00 | sale change alerts (`notifier.py`) |
| `steam-availability.{service,timer}` | every 20 min (`*:0/20`) | availability watcher (`availability.py`) |

```sh
# If your checkout path, user, or docker binary differ, first edit
# WorkingDirectory / User / ExecStart in the two .service files.
sudo cp deploy/systemd/steam-*.service deploy/systemd/steam-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now steam-notifier.timer steam-availability.timer
```

`Persistent=true` re-fires a missed run after downtime. To **change a cadence**,
edit the `OnCalendar=` line in the `.timer`, re-copy it, then
`sudo systemctl daemon-reload && sudo systemctl restart <name>.timer`.

### 4. Verify

```sh
systemctl list-timers 'steam-*'          # next/last fire times
journalctl -u steam-notifier.service     # output of past runs
journalctl -u steam-availability.service
sudo systemctl start steam-notifier.service   # trigger a run right now, don't wait for the timer
```

### State & persistence

State (`state.json` / `availability_state.json`) is written under `STATE_DIR` —
the compose file points it at a named `state` volume so price baselines and
"new since last run" survive one-shot containers. Unset (non-Docker runs), it
defaults to next to the script. The curated lists `titles.json` /
`watchlist.json` are **bind-mounted from the host** into the containers, so
editing them on the host takes effect on the next run without rebuilding.

Reset a tracker by deleting its state file (in the volume) — the next run then
re-baselines (every current sale counts as new, every available item is alerted
once).

### Plain cron (without Docker)

Docker isn't required. With `.env` exported (or the vars set inline) you can run
the scripts directly from host cron:

```cron
# sale alerts daily at 09:00, hardware/availability every 20 minutes
0 9 * * *    cd /opt/steam-sale-notifier && /usr/bin/python3 notifier.py     >> /var/log/steam-sale.log 2>&1
*/20 * * * * cd /opt/steam-sale-notifier && /usr/bin/python3 availability.py >> /var/log/steam-availability.log 2>&1
```

(Set `SLACK_WEBHOOK_URL` etc. in the environment or a sourced file; stdlib only,
Python 3.8+.)

### (Optional) Web UI to curate the lists

A small Flask app (`webui/`) lets you **search Steam by name and add titles** to
either list without editing JSON by hand:

- **Games** → `titles.json` (watched by `notifier.py`)
- **Hardware / coming-soon** → `watchlist.json` (watched by `availability.py`)

It writes the same files the scripts read (atomically, preserving their
`_comment` header), so the next scheduled run picks up your changes. Search uses
Steam's own (unofficial, no-key) store-search endpoint; you can also add by
`appid` directly, and the name is looked up for you.

```sh
docker compose up -d webui      # build + run; compose maps host 8083 -> container 8080
                                # then open http://<server-ip>:8083
# or locally without Docker (listens on 8080):
pip install -r webui/requirements.txt
python -m webui.app             # http://localhost:8080
```

> **Access:** LAN-only, **no authentication** — it can edit your lists, so don't
> expose its port to the internet. Restrict it to a LAN interface
> (e.g. `"192.168.x.y:8083:8080"` in `docker-compose.yml`) or put it behind a
> reverse proxy / VPN. It runs Flask's built-in server, which is fine for
> personal LAN use; front it with a WSGI server (waitress/gunicorn) if you want
> something sturdier.

## Notes / tuning
- **When it posts:** only when a watched title is **newly on sale** or has
  become **cheaper than the best deal already shown**. A title still on sale at
  the same (or a worse) price is not re-posted. Nothing changed => silent (state
  is still refreshed). The discount **end date** is shown inline on each posted
  line, but a sale merely ending today does not, by itself, trigger a post.
- **State / baselines:** `state.json` records the best price + deepest discount
  seen for each title during its current on-sale streak. A title that drops off
  sale is forgotten, so when it returns it counts as "new" again. Delete
  `state.json` to reset — the next run then treats every current sale as new
  (and will post if any exist).
- **"Cheaper than before"** fires when the final price drops below, or the
  discount % rises above, what we last alerted. Re-listing a price you've
  already seen (e.g. a sale that dips then rebounds to the same low) won't
  re-notify.
- **Threshold** applies to both sections — a 10%-off title won't appear.
  Lower `DISCOUNT_THRESHOLD` to widen the net.
- **Free / unpriced / undiscounted titles** are simply omitted.
- **ITAD tag** (if `ITAD_API_KEY` is set) appends all-time-low context to each
  sale line. Prices/currency follow `STEAM_CC`. Adds one lookup per posted title
  plus one batched history-low call, only when a message is actually posted.
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
- **Polling cadence:** the systemd timer runs this **every 20 minutes** to catch
  a launch fast; for plain host cron use `*/20 * * * *`. A few appids checked
  3×/hour is trivial, well-behaved traffic against the same public GetItems
  endpoint the store uses. "Purchasable" means *listed for sale* — it does not
  guarantee in-stock.

---

*This project was written with Claude Opus 4.8 (Anthropic).*
