#!/usr/bin/env python3
"""
Steam availability watcher -> Slack incoming webhook.

Watches a list of "coming soon" Steam items (upcoming games and/or hardware
like Steam Frame / Steam Machine) and posts to a channel-scoped Slack incoming
webhook when one of them:

  1. ✅ becomes purchasable  (a best_purchase_option / price appears)
  2. 🗓 gets a release-date change  (e.g. "To be announced" -> a real date)
  3. 🎟 opens its reservation / waitlist signup  (store-page markers appear)

The availability signal is the presence of `best_purchase_option` in Steam's
IStoreBrowseService/GetItems response — that's "purchasable" regardless of
whether the item is a game or hardware. An item already purchasable the first
time it's seen is alerted once.

The *signup* signal cannot come from that API at all: GetItems returns no
waitlist/reservation/notify field of any kind (verified against Steam Frame on
2026-08-26 — with every data_request flag set, a coming-soon item taking
signups is indistinguishable from one that isn't). So an item may declare a
`signup_page` in watchlist.json, and that page's text is checked for the
markers Valve's reservation flow puts there. See SIGNUP_MARKERS below.

Companion to notifier.py (the sale digest); shares the same webhook and the
same GetItems endpoint. Run from cron. See README.md.
"""

import gzip
import json
import os
import re
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

# Reservation / waitlist signup detection, for items with a `signup_page`.
#
# Every term here was measured against Valve's own hardware pages on
# 2026-08-26 and scores **0 on the Steam Frame page** (signup not open) and 0
# on the store home, so today's total is zero and the watcher is silent until
# something genuinely changes. Re-measure before adding one.
SIGNUP_MARKERS = (
    # Present on the pages whose signup has actually run — Steam Machine and
    # Steam Controller. Counts there: reservation 58/7, waitlist 7/0,
    # signup 4/0, "signed up" 2/0, randomization 3/0, shipping 3/0.
    "reservation",
    "waitlist",
    "signup",
    "signed up",
    "randomization",
    "shipping",
    # Zero on all four pages today, so they cost nothing to watch and cover
    # wording the Frame might use that the Machine and Controller didn't.
    "join the waitlist",
    "reserve your",
    "sign up for a reservation",
    "reservation email",
    "reservation window",
    "request an invite",
    "pre-order",
    "preorder",
    "add to cart",
    "in stock",
    "limited quantities",
    "purchase window",
    "estimated ship",
    "order now",
    "secure your",
    "get in line",
)

# Deliberately NOT markers, however obvious they look: "reserve" / "reserved"
# (1 hit on the Frame page today), "notify me" (1), "sign up" (4), "buy now"
# (2) and "queue" (2). The Frame's page links to the other two products'
# signups, so those words are already there and would fire immediately.
#
# Two hits are required so that a single stray or cross-promo mention of
# another product's reservation can't raise the alert on its own.
SIGNUP_MIN_HITS = 2


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


def fetch_page(url):
    """Return a store page's HTML, or None if it can't be fetched.

    Asks for gzip explicitly: these hardware pages are ~2 MB uncompressed and
    this runs every 20 minutes.
    """
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip", "Accept-Language": "en"},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            raw = resp.read()
            if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                raw = gzip.decompress(raw)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        print(f"warning: signup page fetch failed for {url}: {exc}", file=sys.stderr)
        return None
    return raw.decode("utf-8", "replace")


def signup_status(item):
    """Return "open" / "closed" for an item's signup page, or None if unknown.

    None means "couldn't tell this run" — no signup_page configured, or the
    fetch failed — and the caller then keeps whatever it already knew rather
    than inventing a transition.
    """
    url = item.get("signup_page")
    if not url:
        return None
    html = fetch_page(url)
    if html is None:
        return None
    low = html.lower()

    # Confirm we're looking at the right product page before trusting a
    # "closed" verdict. Steam answers an unknown /hardware/<slug> with a 200
    # and the generic "Steam Hardware" page, so a typo'd URL would otherwise
    # read as a permanently closed signup and quietly never alert — the one
    # failure mode that costs you the launch. Unknown beats a confident wrong.
    #
    # The check is against <title> ("Steam Frame"), not the body: the generic
    # page and the store home both mention every product by name somewhere,
    # and rel=canonical just echoes whatever URL was requested.
    title = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
    title_text = title.group(1).strip().lower() if title else ""
    if item.get("name", "").lower() not in title_text:
        print(f"warning: {url} has title {title_text!r}, not the page for "
              f"{item.get('name')!r}; signup state unknown this run", file=sys.stderr)
        return None

    hits = sum(low.count(marker) for marker in SIGNUP_MARKERS)
    return "open" if hits >= SIGNUP_MIN_HITS else "closed"


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


def signup_line(item):
    url = item.get("signup_page", "")
    return f"• <{url}|*{item.get('name', item['appid'])}*> — reservation / waitlist signup is open"


def section(title, lines):
    return {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*\n" + "\n".join(lines)}}


def build_blocks(became_available, date_changes, signup_opened):
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": "🛎️ Steam availability update"}}]
    if signup_opened:
        blocks.append(section("🎟 Reservation / waitlist signup open", [signup_line(i) for i in signup_opened]))
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
    signup_opened = []     # watchlist items whose signup just opened
    new_state = {}

    for item in items:
        appid = str(item["appid"])
        info = current.get(appid)
        was = prev.get(appid, {})

        # Only ask about signups while the item isn't purchasable yet: once it
        # has a price the waitlist is moot, and it saves a 2 MB page fetch per
        # run. Falls back to the last known status if GetItems failed above.
        known_status = ("available" if info["purchasable"] else "coming_soon") if info else was.get("status")
        signup = signup_status(item) if known_status != "available" else None
        if signup is None:
            signup = was.get("signup")  # keep what we knew; never guess
        elif signup == "open" and was.get("signup") != "open":
            signup_opened.append(item)

        if not info:
            # couldn't fetch this run; preserve whatever we knew, but don't lose
            # a signup transition we just observed independently of GetItems
            kept = dict(was)
            if signup is not None:
                kept["signup"] = signup
            if kept:
                new_state[appid] = kept
            continue

        status = known_status
        new_state[appid] = {"status": status, "release_msg": info["release_msg"]}
        if signup is not None:
            new_state[appid]["signup"] = signup

        # 1. became purchasable (also fires for an item first seen already available)
        if status == "available" and was.get("status") != "available":
            became_available.append((info, was.get("status")))
        # 2. release-date message changed while still coming soon
        elif status == "coming_soon" and appid in prev and was.get("release_msg") != info["release_msg"]:
            date_changes.append((info, was.get("release_msg")))

    print(f"checked {len(appids)} items: {len(became_available)} now available, "
          f"{len(date_changes)} release-date change(s), {len(signup_opened)} signup(s) opened")

    if not became_available and not date_changes and not signup_opened:
        if dry_run:
            print("(dry run) no availability changes; nothing would be posted")
            return 0
        save_state(new_state)
        print("no availability changes; nothing posted")
        return 0

    blocks = build_blocks(became_available, date_changes, signup_opened)
    text = (f"Steam availability: {len(signup_opened)} signup(s) open, "
            f"{len(became_available)} now available, {len(date_changes)} date update(s)")

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
