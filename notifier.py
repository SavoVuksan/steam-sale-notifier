#!/usr/bin/env python3
"""
Steam sale change alerts -> Slack incoming webhook.

Posts to a channel-scoped Slack incoming webhook only when a watched title's
sale status actually changes, for a hand-curated list of Steam titles:

  1. 🆕  New on sale since the last run
  2. 💸  Cheaper than before (already on sale, but at a new lower price)

A title that was already on sale and is still on sale at the same (or a
worse) price is NOT re-posted — only genuine changes are alerted.

Pulls data from Steam's IStoreBrowseService/GetItems, which returns the
discount percent, prices, and the discount end date in one batched call.
A small state file remembers the best price seen for each on-sale title, so
"new" and "cheaper than before" are computed against the previous run.

Designed to be run from cron / a systemd timer on an always-on VM. Because it
stays silent unless something changed, it can be run as often as you like.
See README.md.
"""

import calendar
import json
import os
import sys
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, date
from pathlib import Path

from envfile import load_dotenv

# --- Configuration -----------------------------------------------------------

# Load .env (if present) before reading any config below. Real environment
# variables still take precedence over .env values.
load_dotenv()

HERE = Path(__file__).resolve().parent
TITLES_FILE = HERE / "titles.json"
# State lives next to the script by default; set STATE_DIR to redirect it to a
# persistent location (e.g. a mounted volume when running in a container).
STATE_DIR = Path(os.environ.get("STATE_DIR", HERE))
STATE_FILE = STATE_DIR / "state.json"

# Slack incoming webhook URL. Set as an env var so it never lands in git:
#   export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "").strip()

# A title counts as "on sale" for the digest only at/above this discount.
DISCOUNT_THRESHOLD = int(os.environ.get("DISCOUNT_THRESHOLD", "25"))

# Storefront region: country_code drives currency, language drives names.
COUNTRY = os.environ.get("STEAM_CC", "DE")
LANG = os.environ.get("STEAM_LANG", "english")

# Optional IsThereAnyDeal enrichment. If set, each sale line is tagged with how
# the current price compares to the title's all-time low. Leave unset to disable.
ITAD_API_KEY = os.environ.get("ITAD_API_KEY", "").strip()

HTTP_TIMEOUT_SECONDS = 20
USER_AGENT = "lan-sale-notifier/2.0 (+internal LAN tooling)"
GETITEMS_URL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
STORE_BASE = "https://store.steampowered.com/"
BATCH_SIZE = 50  # appids per GetItems call

ITAD_LOOKUP_URL = "https://api.isthereanydeal.com/games/lookup/v1"
ITAD_HISTLOW_URL = "https://api.isthereanydeal.com/games/historylow/v1"


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

    try:
        final_cents = int(bpo.get("final_price_in_cents") or 0)
    except (TypeError, ValueError):
        final_cents = 0

    return {
        "name": item.get("name", str(item.get("appid"))),
        "discount_pct": pct,
        "final": bpo.get("formatted_final_price", ""),
        "original": bpo.get("formatted_original_price", ""),
        "final_cents": final_cents,
        "end_ts": end_ts,
        "url": url,
        "itad": None,  # filled in by ITAD enrichment if enabled
    }


# --- IsThereAnyDeal enrichment (optional) ------------------------------------


def itad_lookup(appid):
    """Map a Steam appid to an ITAD game id (UUID), or None."""
    url = ITAD_LOOKUP_URL + "?" + urllib.parse.urlencode({"key": ITAD_API_KEY, "appid": appid})
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
        data = json.load(resp)
    return data.get("game", {}).get("id") if data.get("found") else None


def itad_historylow(game_ids):
    """Return {game_id: low_dict} for the given ITAD game ids."""
    url = ITAD_HISTLOW_URL + "?" + urllib.parse.urlencode({"key": ITAD_API_KEY, "country": COUNTRY})
    body = json.dumps(game_ids).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
        data = json.load(resp)
    return {row["id"]: row["low"] for row in data if row.get("low")}


def fmt_eur(amount):
    return f"{amount:.2f}".replace(".", ",") + "€"


def itad_tag(info, low):
    """Build the historical-low tag for one sale line."""
    low_amt = low["price"]["amount"]
    when = ""
    ts = low.get("timestamp", "")
    if len(ts) >= 7:
        try:
            when = f" ({calendar.month_abbr[int(ts[5:7])]} {ts[:4]})"
        except (ValueError, IndexError):
            when = ""
    current = info["final_cents"] / 100 if info.get("final_cents") else None
    # Within a cent of the all-time low counts as matching it.
    if current is not None and current <= low_amt + 0.01:
        return "🔥 matches all-time low"
    return f"all-time low was {fmt_eur(low_amt)}{when}"


def enrich_with_itad(on_sale):
    """Attach an 'itad' tag to each sale in-place. No-op without an API key.

    Fully degradable: any lookup/fetch failure simply leaves tags unset and the
    digest still posts normally.
    """
    if not ITAD_API_KEY or not on_sale:
        return

    id_map = {}
    for appid in on_sale:
        try:
            gid = itad_lookup(appid)
        except Exception as exc:
            print(f"warning: ITAD lookup failed for {appid}: {exc}", file=sys.stderr)
            gid = None
        if gid:
            id_map[appid] = gid

    if not id_map:
        return

    try:
        lows = itad_historylow(list(id_map.values()))
    except Exception as exc:
        print(f"warning: ITAD historylow failed: {exc}", file=sys.stderr)
        return

    for appid, gid in id_map.items():
        low = lows.get(gid)
        if low:
            try:
                on_sale[appid]["itad"] = itad_tag(on_sale[appid], low)
            except Exception as exc:
                print(f"warning: ITAD tag failed for {appid}: {exc}", file=sys.stderr)


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


def fmt_price(info):
    if info["original"] and info["original"] != info["final"]:
        return f"{info['final']} (was {info['original']})"
    return info["final"]


def fmt_line(info):
    line = f"• <{info['url']}|*{info['name']}*> — {info['discount_pct']}% off, {fmt_price(info)}"
    if info["end_ts"]:
        end = datetime.fromtimestamp(info["end_ts"])
        line += f" · ends {end.strftime('%a %d %b')}"
    if info.get("itad"):
        line += f" · {info['itad']}"
    return line


def section(title, lines):
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"*{title}*\n" + "\n".join(lines)},
    }


def build_blocks(new, better, today_str):
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"🎮 Steam sale update — {today_str}"}},
    ]
    if new:
        blocks.append(section("🆕 New on sale", [fmt_line(i) for i in new]))
    if better:
        blocks.append(section("💸 Cheaper than before", [fmt_line(i) for i in better]))
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


# --- Change detection ---------------------------------------------------------


def load_prev_on_sale():
    """Return {appid: baseline | None} for titles on sale at the last run.

    baseline is {"final_cents", "discount_pct"} — the best deal we've alerted
    for that title during its current on-sale streak. None means "known on sale
    but with no recorded price" (an old list-format state, pre-upgrade); such a
    title is adopted silently on the next run rather than re-alerted.
    """
    stored = load_json(STATE_FILE, {}).get("on_sale", {})
    if isinstance(stored, list):  # migrate the old "set of appids" format
        return {appid: None for appid in stored}
    return stored


def baseline_of(info):
    return {"final_cents": info["final_cents"], "discount_pct": info["discount_pct"]}


def is_better(info, base):
    """True if `info` is a strictly better deal than the recorded baseline."""
    cur_cents, base_cents = info["final_cents"], base.get("final_cents", 0)
    if cur_cents and base_cents and cur_cents < base_cents:
        return True
    return info["discount_pct"] > base.get("discount_pct", 0)


def merged_baseline(info, base):
    """Best deal seen so far: price only ever drops, discount only ever rises."""
    cur_cents, base_cents = info["final_cents"], base.get("final_cents", 0)
    if cur_cents and base_cents:
        cents = min(cur_cents, base_cents)
    else:
        cents = cur_cents or base_cents  # keep whichever price we actually know
    return {"final_cents": cents, "discount_pct": max(info["discount_pct"], base.get("discount_pct", 0))}


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

    # Keep only sales at/above the threshold.
    on_sale = {
        appid: info for appid, info in items.items() if info["discount_pct"] >= DISCOUNT_THRESHOLD
    }

    # Compare each on-sale title against what we last alerted for it. A title is
    # only worth posting when it's newly on sale, or now cheaper / more deeply
    # discounted than the best deal we've already shown. A title still on sale at
    # the same (or a worse) price stays silent. Titles that have dropped off sale
    # fall out of new_state, so if they return later they count as "new" again.
    prev = load_prev_on_sale()
    new, better = [], []          # info dicts to post
    posted = {}                   # appid -> info, for ITAD enrichment
    new_state = {}                # appid -> baseline, persisted for next run

    for appid, info in on_sale.items():
        base = prev.get(appid)
        if appid not in prev:                 # newly on sale
            new.append(info)
            posted[appid] = info
            new_state[appid] = baseline_of(info)
        elif base is None:                    # known from a pre-upgrade run; adopt silently
            new_state[appid] = baseline_of(info)
        elif is_better(info, base):           # already on sale, but a better price now
            better.append(info)
            posted[appid] = info
            new_state[appid] = merged_baseline(info, base)
        else:                                 # unchanged or worse — keep best seen, stay quiet
            new_state[appid] = merged_baseline(info, base)

    new.sort(key=lambda i: i["discount_pct"], reverse=True)
    better.sort(key=lambda i: i["discount_pct"], reverse=True)

    print(f"checked {len(titles)} titles: {len(on_sale)} on sale (>= {DISCOUNT_THRESHOLD}%), "
          f"{len(new)} new, {len(better)} cheaper than before")

    # Only post on a genuine change: a new sale or a better price. Nothing
    # changed => stay silent, but still persist the refreshed baselines.
    if not new and not better:
        if dry_run:
            print("(dry run) no new sales or price drops; nothing would be posted")
            return 0
        save_state({"on_sale": new_state})
        print("no new sales or price drops; nothing posted")
        return 0

    # Tag each posted sale with all-time-low context (no-op unless ITAD_API_KEY
    # is set). new/better reference the same dicts in `posted`, so tags reach
    # every line. Only the titles we're actually posting are looked up.
    enrich_with_itad(posted)

    blocks = build_blocks(new, better, date.today().strftime("%A, %d %B %Y"))
    text = f"Steam sales: {len(new)} new, {len(better)} cheaper than before"

    if dry_run:
        print(json.dumps({"text": text, "blocks": blocks}, indent=2, ensure_ascii=False))
        print("\n(dry run — not posted, state not updated)")
        return 0

    try:
        post_to_slack(blocks, text)
    except Exception as exc:
        print(f"error: Slack post failed: {exc}", file=sys.stderr)
        return 1

    save_state({"on_sale": new_state})
    print("sale update posted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
