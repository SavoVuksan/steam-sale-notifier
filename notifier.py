#!/usr/bin/env python3
"""
Steam sale daily digest -> Slack incoming webhook.

Once a day, posts a single digest to a channel-scoped Slack incoming webhook
with three sections, for a hand-curated list of Steam titles:

  1. 🆕  NEW on sale since the last digest
  2. ⏰  Ending today (last chance)
  3. 📋  All titles currently on sale

Pulls data from Steam's IStoreBrowseService/GetItems, which returns the
discount percent, prices, and the discount end date in one batched call.
A small state file remembers which titles were on sale last run, so "NEW"
is computed against the previous digest.

Designed to be run from cron once a day on an always-on VM. See README.md.
"""

import json
import os
import sys
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, date
from pathlib import Path

# --- Configuration -----------------------------------------------------------

HERE = Path(__file__).resolve().parent
TITLES_FILE = HERE / "titles.json"
STATE_FILE = HERE / "state.json"

# Slack incoming webhook URL. Set as an env var so it never lands in git:
#   export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "").strip()

# A title counts as "on sale" for the digest only at/above this discount.
DISCOUNT_THRESHOLD = int(os.environ.get("DISCOUNT_THRESHOLD", "25"))

# Storefront region: country_code drives currency, language drives names.
COUNTRY = os.environ.get("STEAM_CC", "DE")
LANG = os.environ.get("STEAM_LANG", "english")

HTTP_TIMEOUT_SECONDS = 20
USER_AGENT = "lan-sale-notifier/2.0 (+internal LAN tooling)"
GETITEMS_URL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
STORE_BASE = "https://store.steampowered.com/"
BATCH_SIZE = 50  # appids per GetItems call


# --- Data fetch ---------------------------------------------------------------


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def fetch_items(appids):
    """Return {appid(str): parsed_sale_info} for the given appids.

    parsed_sale_info has: name, discount_pct, final, original, end_ts, url.
    Titles that aren't discounted (or aren't visible) are simply omitted.
    """
    results = {}
    for batch in chunked(appids, BATCH_SIZE):
        payload = {
            "ids": [{"appid": int(a)} for a in batch],
            "context": {"language": LANG, "country_code": COUNTRY},
            "data_request": {"include_basic_info": True},
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
            parsed = parse_item(item)
            if parsed:
                results[str(item.get("appid"))] = parsed
    return results


def parse_item(item):
    bpo = item.get("best_purchase_option") or {}
    pct = bpo.get("discount_pct") or 0
    if pct <= 0:
        return None  # not on sale

    end_ts = None
    for disc in bpo.get("active_discounts") or []:
        if disc.get("discount_end_date"):
            end_ts = int(disc["discount_end_date"])
            break

    path = item.get("store_url_path")
    url = (STORE_BASE + path) if path else f"{STORE_BASE}app/{item.get('appid')}"

    return {
        "name": item.get("name", str(item.get("appid"))),
        "discount_pct": pct,
        "final": bpo.get("formatted_final_price", ""),
        "original": bpo.get("formatted_original_price", ""),
        "end_ts": end_ts,
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
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False)
    tmp.replace(STATE_FILE)


# --- Formatting ---------------------------------------------------------------


def fmt_price(info):
    if info["original"] and info["original"] != info["final"]:
        return f"{info['final']} (was {info['original']})"
    return info["final"]


def fmt_line(info, show_time=False):
    line = f"• <{info['url']}|*{info['name']}*> — {info['discount_pct']}% off, {fmt_price(info)}"
    if info["end_ts"]:
        end = datetime.fromtimestamp(info["end_ts"])
        line += f" · ends {end.strftime('%H:%M today' if show_time else '%a %d %b')}"
    return line


def section(title, lines):
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"*{title}*\n" + "\n".join(lines)},
    }


def build_blocks(new, ending, current, today_str):
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"🎮 Daily Steam Sales — {today_str}"}},
    ]
    if new:
        blocks.append(section("🆕 New on sale", [fmt_line(i) for i in new]))
    if ending:
        blocks.append(section("⏰ Ending today — last chance", [fmt_line(i, show_time=True) for i in ending]))
    if current:
        blocks.append({"type": "divider"})
        blocks.append(section(f"📋 All current sales ({len(current)})", [fmt_line(i) for i in current]))
    return blocks


def post_to_slack(blocks, text):
    body = json.dumps({"text": text, "blocks": blocks}).encode("utf-8")
    req = urllib.request.Request(
        WEBHOOK_URL,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
        return resp.read().decode("utf-8")


# --- Main ---------------------------------------------------------------------


def main():
    dry_run = "--dry-run" in sys.argv

    if not WEBHOOK_URL and not dry_run:
        print("error: SLACK_WEBHOOK_URL is not set", file=sys.stderr)
        return 1

    titles = load_json(TITLES_FILE, {}).get("titles", [])
    if not titles:
        print("error: no titles configured in titles.json", file=sys.stderr)
        return 1

    appids = [str(t["appid"]) for t in titles]
    items = fetch_items(appids)

    # Keep only sales at/above the threshold; sort by deepest discount first.
    on_sale = {
        appid: info for appid, info in items.items() if info["discount_pct"] >= DISCOUNT_THRESHOLD
    }
    current = sorted(on_sale.values(), key=lambda i: i["discount_pct"], reverse=True)

    prev_on_sale = set(load_json(STATE_FILE, {}).get("on_sale", []))
    new = [on_sale[a] for a in on_sale if a not in prev_on_sale]
    new.sort(key=lambda i: i["discount_pct"], reverse=True)

    today = date.today()
    ending = [
        i for i in current if i["end_ts"] and datetime.fromtimestamp(i["end_ts"]).date() == today
    ]

    print(f"checked {len(titles)} titles: {len(current)} on sale (>= {DISCOUNT_THRESHOLD}%), "
          f"{len(new)} new, {len(ending)} ending today")

    # Only post when something actionable changed: a new sale, or a sale ending
    # today. We don't re-post the unchanged overview day after day. The overview
    # is still included in the message whenever we do post.
    if not new and not ending:
        if dry_run:
            print("(dry run) no new or ending-today sales; nothing would be posted")
            return 0
        save_state({"on_sale": list(on_sale.keys())})
        print("no new or ending-today sales; nothing posted")
        return 0

    blocks = build_blocks(new, ending, current, today.strftime("%A, %d %B %Y"))
    text = f"Daily Steam sales: {len(new)} new, {len(ending)} ending today ({len(current)} on sale)"

    if dry_run:
        print(json.dumps({"text": text, "blocks": blocks}, indent=2, ensure_ascii=False))
        print("\n(dry run — not posted, state not updated)")
        return 0

    try:
        post_to_slack(blocks, text)
    except Exception as exc:
        print(f"error: Slack post failed: {exc}", file=sys.stderr)
        return 1

    save_state({"on_sale": list(on_sale.keys())})
    print("digest posted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
