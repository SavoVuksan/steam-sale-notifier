#!/usr/bin/env python3
"""
Steam availability watcher -> Slack incoming webhook.

Watches a list of "coming soon" Steam items (upcoming games and/or hardware
like Steam Frame / Steam Machine) and posts to a channel-scoped Slack incoming
webhook when one of them:

  1. ✅ becomes purchasable  (a best_purchase_option / price appears)
  2. 🗓 gets a release-date change  (e.g. "To be announced" -> a real date)

The availability signal is the presence of `best_purchase_option` in Steam's
IStoreBrowseService/GetItems response — that's "purchasable" regardless of
whether the item is a game or hardware. An item already purchasable the first
time it's seen is alerted once.

Companion to notifier.py (the sale digest); shares the same webhook and the
same GetItems endpoint. Run from cron. See README.md.
"""

import json
import os
import sys
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime
from pathlib import Path

from envfile import load_dotenv

# --- Configuration -----------------------------------------------------------

# Load .env (if present) before reading any config below. Real environment
# variables still take precedence over .env values.
load_dotenv()

HERE = Path(__file__).resolve().parent
WATCHLIST_FILE = HERE / "watchlist.json"
# State lives next to the script by default; set STATE_DIR to redirect it to a
# persistent location (e.g. a mounted volume when running in a container).
STATE_DIR = Path(os.environ.get("STATE_DIR", HERE))
STATE_FILE = STATE_DIR / "availability_state.json"

WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
COUNTRY = os.environ.get("STEAM_CC", "DE")
LANG = os.environ.get("STEAM_LANG", "english")

HTTP_TIMEOUT_SECONDS = 20
USER_AGENT = "lan-availability-watcher/1.0 (+internal LAN tooling)"
GETITEMS_URL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
STORE_BASE = "https://store.steampowered.com/"
BATCH_SIZE = 50


# --- Data fetch ---------------------------------------------------------------


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def fetch_items(appids):
    """Return {appid(str): parsed_status} for each appid found."""
    results = {}
    for batch in chunked(appids, BATCH_SIZE):
        payload = {
            "ids": [{"appid": int(a)} for a in batch],
            "context": {"language": LANG, "country_code": COUNTRY},
            "data_request": {"include_basic_info": True, "include_release": True},
        }
        url = GETITEMS_URL + "?input_json=" + urllib.parse.quote(json.dumps(payload))
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                data = json.load(resp)
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
            print(f"warning: GetItems fetch failed for batch {batch}: {exc}", file=sys.stderr)
            continue
        for item in data.get("response", {}).get("store_items", []):
            results[str(item.get("appid"))] = parse_item(item)
    return results


def parse_item(item):
    bpo = item.get("best_purchase_option") or {}
    purchasable = bool(bpo)

    rel = item.get("release") or {}
    release_msg = rel.get("custom_release_date_message") or ""
    if not release_msg and rel.get("steam_release_date"):
        try:
            release_msg = datetime.fromtimestamp(int(rel["steam_release_date"])).strftime("%d %b %Y")
        except (ValueError, TypeError, OSError):
            release_msg = ""

    path = item.get("store_url_path")
    url = (STORE_BASE + path) if path else f"{STORE_BASE}app/{item.get('appid')}"

    return {
        "name": item.get("name", str(item.get("appid"))),
        "purchasable": purchasable,
        "price": bpo.get("formatted_final_price", ""),
        "is_free": bool(item.get("is_free")),
        "release_msg": release_msg,
        "url": url,
    }


# --- State --------------------------------------------------------------------


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as exc:
        print(f"warning: {path} invalid JSON ({exc}); treating as empty", file=sys.stderr)
        return default


def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False)
    tmp.replace(STATE_FILE)


# --- Formatting ---------------------------------------------------------------


def avail_line(info):
    if info["is_free"]:
        price = "free"
    elif info["price"]:
        price = info["price"]
    else:
        price = "see store"
    line = f"• <{info['url']}|*{info['name']}*> — now available, {price}"
    if info["release_msg"]:
        line += f" · {info['release_msg']}"
    return line


def date_line(info, old_msg):
    old = old_msg or "—"
    return f"• <{info['url']}|*{info['name']}*> — release date: {info['release_msg']} (was: {old})"


def section(title, lines):
    return {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*\n" + "\n".join(lines)}}


def build_blocks(became_available, date_changes):
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": "🛎️ Steam availability update"}}]
    if became_available:
        blocks.append(section("✅ Now available to purchase", [avail_line(i) for i, _ in became_available]))
    if date_changes:
        blocks.append(section("🗓 Release date updates", [date_line(i, old) for i, old in date_changes]))
    return blocks


def post_to_slack(blocks, text):
    body = json.dumps({"text": text, "blocks": blocks}).encode("utf-8")
    req = urllib.request.Request(
        WEBHOOK_URL, data=body,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
        return resp.read().decode("utf-8")


# --- Main ---------------------------------------------------------------------


def main():
    dry_run = "--dry-run" in sys.argv

    if not WEBHOOK_URL and not dry_run:
        print("error: SLACK_WEBHOOK_URL is not set", file=sys.stderr)
        return 1

    items = load_json(WATCHLIST_FILE, {}).get("items", [])
    if not items:
        print("error: no items configured in watchlist.json", file=sys.stderr)
        return 1

    appids = [str(i["appid"]) for i in items]
    current = fetch_items(appids)
    prev = load_json(STATE_FILE, {})

    became_available = []  # (info, prev_status)
    date_changes = []      # (info, old_msg)
    new_state = {}

    for appid in appids:
        info = current.get(appid)
        if not info:
            # couldn't fetch this run; preserve whatever we knew
            if appid in prev:
                new_state[appid] = prev[appid]
            continue

        status = "available" if info["purchasable"] else "coming_soon"
        new_state[appid] = {"status": status, "release_msg": info["release_msg"]}
        was = prev.get(appid, {})

        # 1. became purchasable (also fires for an item first seen already available)
        if status == "available" and was.get("status") != "available":
            became_available.append((info, was.get("status")))
        # 2. release-date message changed while still coming soon
        elif status == "coming_soon" and appid in prev and was.get("release_msg") != info["release_msg"]:
            date_changes.append((info, was.get("release_msg")))

    print(f"checked {len(appids)} items: {len(became_available)} now available, "
          f"{len(date_changes)} release-date change(s)")

    if not became_available and not date_changes:
        if dry_run:
            print("(dry run) no availability changes; nothing would be posted")
            return 0
        save_state(new_state)
        print("no availability changes; nothing posted")
        return 0

    blocks = build_blocks(became_available, date_changes)
    text = f"Steam availability: {len(became_available)} now available, {len(date_changes)} date update(s)"

    if dry_run:
        print(json.dumps({"text": text, "blocks": blocks}, indent=2, ensure_ascii=False))
        print("\n(dry run — not posted, state not updated)")
        return 0

    try:
        post_to_slack(blocks, text)
    except Exception as exc:
        print(f"error: Slack post failed: {exc}", file=sys.stderr)
        return 1

    save_state(new_state)
    print("availability update posted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
