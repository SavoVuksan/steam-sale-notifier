#!/usr/bin/env python3
"""Small Flask web UI to curate the notifier's watch lists.

Lets you search Steam by name (via the same unofficial storefront endpoints the
site itself uses — no API key) and add results to one of two lists:

  * games    -> titles.json   (key "titles")  — watched by notifier.py
  * hardware -> watchlist.json (key "items")   — watched by availability.py

Both files are the *same* ones the scripts read; the UI writes them atomically
and preserves their "_comment" header. Designed for LAN-only use (no auth) —
don't expose it to the internet without putting auth in front of it.

Run locally:   python -m webui.app          (then open http://localhost:8080)
In Docker:     see docker-compose.yml (service "webui").
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from flask import Flask, abort, jsonify, request

# --- Configuration -----------------------------------------------------------

HERE = Path(__file__).resolve().parent
# titles.json / watchlist.json live in the repo root (one level up) by default;
# override with CONFIG_DIR (the compose file points this at the mounted files).
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", HERE.parent))

COUNTRY = os.environ.get("STEAM_CC", "DE")
LANG = os.environ.get("STEAM_LANG", "english")

HTTP_TIMEOUT_SECONDS = 15
USER_AGENT = "lan-sale-notifier-webui/1.0 (+internal LAN tooling)"
STORESEARCH_URL = "https://store.steampowered.com/api/storesearch/"
GETITEMS_URL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
BATCH_SIZE = 50  # max appids per GetItems call

# which list -> (file, the JSON key that holds the array)
LISTS = {
    "games": {"path": CONFIG_DIR / "titles.json", "key": "titles"},
    "hardware": {"path": CONFIG_DIR / "watchlist.json", "key": "items"},
}

app = Flask(__name__, static_folder="static", static_url_path="")


# --- Steam lookups (unofficial storefront endpoints, no key) -----------------


def steam_search(term):
    """Return search hits with price info for a term.

    Each hit: {appid, name, type, image, currency, price_final, price_initial,
    discount_pct}. Prices are in minor units (cents) in the storefront currency;
    price_final is None for free/unpriced titles. discount_pct is computed from
    initial vs final (storesearch doesn't return it directly) and is 0 when there
    is no active sale.
    """
    params = urllib.parse.urlencode({"term": term, "cc": COUNTRY, "l": LANG})
    req = urllib.request.Request(
        f"{STORESEARCH_URL}?{params}", headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
        data = json.load(resp)

    results = []
    for item in data.get("items", []):
        if item.get("id") is None:
            continue
        price = item.get("price") or {}
        final = price.get("final")
        initial = price.get("initial")
        discount = 0
        if isinstance(initial, (int, float)) and isinstance(final, (int, float)) and initial > final:
            discount = round((initial - final) / initial * 100)
        results.append(
            {
                "appid": item.get("id"),
                "name": item.get("name", str(item.get("id"))),
                "type": item.get("type", ""),
                "image": item.get("tiny_image", ""),
                "currency": price.get("currency") or "",
                "price_final": final,
                "price_initial": initial,
                "discount_pct": discount,
            }
        )
    return results


def resolve_name(appid):
    """Best-effort canonical name for an appid via the GetItems endpoint."""
    payload = {
        "ids": [{"appid": int(appid)}],
        "context": {"language": LANG, "country_code": COUNTRY},
        "data_request": {"include_basic_info": True},
    }
    url = GETITEMS_URL + "?input_json=" + urllib.parse.quote(json.dumps(payload))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return None
    for item in data.get("response", {}).get("store_items", []):
        if str(item.get("appid")) == str(appid):
            return item.get("name")
    return None


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def fetch_prices(appids):
    """Live price/discount info for appids via batched GetItems.

    Returns {appid(str): {discount_pct, price_final, price_initial, fmt_final,
    fmt_initial}}. Prices are cents; fmt_* are Steam's pre-formatted strings (the
    right currency for the storefront region). Appids with no purchase option
    (free, unreleased, region-locked) are simply omitted. Fully degradable: a
    failed batch just yields no entries for those appids.
    """
    out = {}
    appids = [str(a) for a in appids]
    for i in range(0, len(appids), BATCH_SIZE):
        batch = appids[i:i + BATCH_SIZE]
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
            print(f"warning: GetItems price fetch failed for {batch}: {exc}", file=sys.stderr)
            continue
        for item in data.get("response", {}).get("store_items", []):
            bpo = item.get("best_purchase_option") or {}
            if not bpo:
                continue
            out[str(item.get("appid"))] = {
                "discount_pct": bpo.get("discount_pct") or 0,
                "price_final": _to_int(bpo.get("final_price_in_cents")),
                "price_initial": _to_int(bpo.get("original_price_in_cents")),
                "fmt_final": bpo.get("formatted_final_price") or "",
                "fmt_initial": bpo.get("formatted_original_price") or "",
            }
    return out


# --- List storage (atomic, preserves the file's _comment) --------------------


def load_list(which):
    """Return (full_document, entries_list) for a list, tolerant of a missing file."""
    cfg = LISTS[which]
    try:
        doc = json.loads(cfg["path"].read_text(encoding="utf-8"))
    except FileNotFoundError:
        doc = {}
    except json.JSONDecodeError:
        abort(500, description=f"{cfg['path'].name} is not valid JSON")
    if not isinstance(doc, dict):
        doc = {}
    entries = doc.get(cfg["key"])
    return doc, entries if isinstance(entries, list) else []


def save_list(which, doc, entries):
    cfg = LISTS[which]
    doc[cfg["key"]] = entries
    # Write in place (truncate + rewrite) rather than tmp+rename. These files are
    # bind-mounted into the container as individual files, i.e. mount points, and
    # you can't rename over a mount point (OSError EBUSY). The files are tiny and
    # this is the only writer, so a plain rewrite is safe enough.
    cfg["path"].write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# --- Routes ------------------------------------------------------------------


@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.get("/api/lists")
def get_lists():
    return jsonify({which: load_list(which)[1] for which in LISTS})


@app.get("/api/search")
def search():
    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify(results=[])
    try:
        return jsonify(results=steam_search(query))
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        return jsonify(error=f"Steam search failed: {exc}"), 502


@app.get("/api/prices")
def get_prices():
    """Live price/discount for the given appids (?appids=1,2,3).

    Degradable: returns whatever it can; failures yield an empty/partial map so
    the lists still render their names.
    """
    raw = request.args.get("appids", "")
    appids = [a for a in (x.strip() for x in raw.split(",")) if a.isdigit()]
    if not appids:
        return jsonify(prices={})
    try:
        return jsonify(prices=fetch_prices(appids))
    except Exception as exc:  # never break the page over price enrichment
        return jsonify(prices={}, error=str(exc))


@app.post("/api/<which>")
def add_entry(which):
    if which not in LISTS:
        abort(404)
    body = request.get_json(force=True, silent=True) or {}
    try:
        appid = int(body.get("appid"))
    except (TypeError, ValueError):
        return jsonify(error="appid must be an integer"), 400

    name = (body.get("name") or "").strip() or resolve_name(appid) or str(appid)

    doc, entries = load_list(which)
    if any(_as_int(e.get("appid")) == appid for e in entries):
        return jsonify(added=False, reason="already in the list", entries=entries)

    entries.append({"appid": appid, "name": name})
    entries.sort(key=lambda e: str(e.get("name", "")).lower())
    save_list(which, doc, entries)
    return jsonify(added=True, entry={"appid": appid, "name": name}, entries=entries)


@app.delete("/api/<which>/<int:appid>")
def remove_entry(which, appid):
    if which not in LISTS:
        abort(404)
    doc, entries = load_list(which)
    kept = [e for e in entries if _as_int(e.get("appid")) != appid]
    if len(kept) == len(entries):
        return jsonify(removed=False, entries=entries)
    save_list(which, doc, kept)
    return jsonify(removed=True, entries=kept)


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    host = os.environ.get("WEBUI_HOST", "0.0.0.0")
    port = int(os.environ.get("WEBUI_PORT", "8080"))
    app.run(host=host, port=port)
