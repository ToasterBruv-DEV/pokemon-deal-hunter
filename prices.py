#!/usr/bin/env python3
"""
Pokemon Deal Hunter - price list builder.

Downloads TCGplayer market prices for English Pokemon singles from tcgcsv.com
(a free, keyless nightly copy of TCGplayer's own pricing data) and saves a compact
lookup file, data/prices.json, that scan.py reads.

Runs once a day in GitHub Actions (.github/workflows/prices.yml). Python standard library only.

Be polite to tcgcsv.com: one full pull a day, a custom User-Agent, a short pause
between requests (about 450 requests in total, far below their limit).
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(ROOT, "settings.json")
OUT_PATH = os.path.join(ROOT, "data", "prices.json")

BASE = "https://tcgcsv.com"
CATEGORY = 3  # Pokemon (English)
UA = "PokemonDealHunter/1.0 (+https://github.com/toasterbruv-dev/pokemon-deal-hunter)"
PAUSE = 0.25
MIN_CARDS = 500            # refuse to overwrite a good price list with a suspiciously small one
MAX_FAILED_FRACTION = 0.2  # ... or one where more than this share of sets failed to download

NUMBER_RE = re.compile(r"^\s*0*(\d{1,3})\s*/\s*0*(\d{1,3})\s*$")


def log(*a):
    print(*a, flush=True)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch(url, kind="json", retries=3):
    last = ""
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read().decode("utf-8", "replace")
            return json.loads(raw) if kind == "json" else raw.strip()
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(30 * (attempt + 1))
                continue
            break
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            last = str(e)
            if attempt < retries:
                time.sleep(10 * (attempt + 1))
                continue
    raise RuntimeError(f"{last} for {url}")


def variant_key(sub):
    """'Reverse Holofoil' -> 'reverseHolofoil', '1st Edition Holofoil' -> '1stEditionHolofoil'."""
    words = re.sub(r"[^A-Za-z0-9 ]", " ", sub or "Normal").split()
    if not words:
        return "normal"
    return words[0].lower() + "".join(w.capitalize() for w in words[1:])


def clean_name(name):
    n = re.sub(r"\s*\([^)]*\)", "", name or "")                       # (Special Illustration Rare)
    n = re.sub(r"\s*-\s*[A-Za-z]*\d+\s*/\s*[A-Za-z]*\d+.*$", "", n)    # - 58/102
    return n.strip()


def card_number(product):
    for e in product.get("extendedData") or []:
        if e.get("name") == "Number":
            m = NUMBER_RE.match(str(e.get("value", "")))
            if m and int(m.group(1)) > 0:
                return int(m.group(1)), int(m.group(2))
    return None


def money(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def build_group(products, prices, lo_market, hi_market, group_name):
    """Returns {'58/102': [card, ...]} for one set."""
    by_product = {}
    for p in prices:
        v = p.get("marketPrice")
        mid = money(p.get("midPrice"))
        market = money(v) if v is not None else None
        if not (market or mid):
            continue
        by_product.setdefault(p.get("productId"), {})[variant_key(p.get("subTypeName"))] = {
            "market": market, "mid": mid, "low": money(p.get("lowPrice"))}
    out = {}
    for prod in products:
        num = card_number(prod)
        pr = by_product.get(prod.get("productId"))
        if not num or not pr:
            continue
        vals = [(v["market"] or v["mid"]) for v in pr.values()]
        if max(vals) < lo_market or min(vals) > hi_market:
            continue
        key = f"{num[0]}/{num[1]}"
        out.setdefault(key, []).append({
            "name": clean_name(prod.get("name")),
            "set": group_name,
            "number": str(num[0]),
            "url": prod.get("url"),
            "prices": pr,
        })
    return out


def main():
    settings = load_json(SETTINGS_PATH, {})
    lo = float(settings.get("min_market", 10)) * 0.5
    hi = float(settings.get("max_market", 150)) * 2

    src_ts = fetch(BASE + "/last-updated.txt", "text")
    log(f"tcgcsv last updated: {src_ts}")
    existing = load_json(OUT_PATH, {})
    if os.environ.get("EVENT_NAME") == "schedule" and existing.get("source_updated") == src_ts and existing.get("cards"):
        log("Prices are already up to date. Nothing to do.")
        return

    groups = fetch(f"{BASE}/tcgplayer/{CATEGORY}/groups")
    if not groups.get("success", True) or not groups.get("results"):
        sys.exit(f"Could not read the list of sets: {groups.get('errors')}")
    groups = groups["results"]
    log(f"{len(groups)} sets to read")

    cards, failed = {}, []
    for i, g in enumerate(groups, 1):
        gid, gname = g.get("groupId"), g.get("name", "")
        try:
            products = fetch(f"{BASE}/tcgplayer/{CATEGORY}/{gid}/products").get("results", [])
            time.sleep(PAUSE)
            prices = fetch(f"{BASE}/tcgplayer/{CATEGORY}/{gid}/prices").get("results", [])
            time.sleep(PAUSE)
        except RuntimeError as e:
            failed.append(f"{gname}: {e}")
            log(f"  ! skipped set '{gname}': {e}")
            continue
        for k, lst in build_group(products, prices, lo, hi, gname).items():
            cards.setdefault(k, []).extend(lst)
        if i % 25 == 0:
            log(f"  {i}/{len(groups)} sets done, {len(cards)} card numbers so far")

    total = sum(len(v) for v in cards.values())
    log(f"Built {total} cards under {len(cards)} card numbers. {len(failed)} sets failed.")
    if len(failed) > max(5, len(groups) * MAX_FAILED_FRACTION) or total < MIN_CARDS:
        sys.exit("Too many sets failed or too few cards came back, so the old price list was kept.")

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    tmp = OUT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"updated_at": now_iso(), "source_updated": src_ts, "category": CATEGORY,
                   "sets": len(groups), "count": total, "cards": cards},
                  f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUT_PATH)
    log(f"Saved {OUT_PATH} ({os.path.getsize(OUT_PATH) // 1024} KB)")


if __name__ == "__main__":
    main()
