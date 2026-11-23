#!/usr/bin/env python3
"""
Pokemon Deal Hunter - price list builder.

Downloads TCGplayer market prices for English Pokemon singles from tcgcsv.com
(a free, keyless nightly copy of TCGplayer's own pricing data) and saves a compact
lookup file, data/prices.json, that scan.py reads.

Runs once a day in GitHub Actions (.github/workflows/prices.yml). Python standard library only.

Important: the list keeps EVERY version of a card that shares a number (regular, stamped,
promo, cosmos holo, shadowless, ...) and labels the special ones with tags. The scanner
only compares a listing to a special version when the listing title says so. Older versions
of this script dropped cheap cards, which left only the expensive stamped promo to compare
against and made bulk cards look 97% off.

Be polite to tcgcsv.com: one full pull a day, a custom User-Agent, a short pause between
requests (about 450 requests in total, far below their limit).
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
FORMAT = 3
MIN_CARDS = 500            # refuse to overwrite a good price list with a suspiciously small one
MAX_FAILED_FRACTION = 0.2  # ... or one where more than this share of sets failed to download

NUMBER_RE = re.compile(r"^\s*0*(\d{1,3})\s*/\s*0*(\d{1,3})\s*$")

# Special versions of a card. A card carrying one of these tags is only used for a listing
# whose title mentions it. The same patterns are used by scan.py to read listing titles.
TAGS = {
    "cosmos": r"cosmos",
    "stamped": r"stamp",
    "prerelease": r"pre-?release",
    "staff": r"\bstaff\b",
    "league": r"\bleague\b",
    "champs": r"championship|\bworlds?\b|\bregionals?\b|\bnationals?\b|\btop (?:4|8|16|32|64)\b",
    "prizepack": r"prize pack",
    "promo": r"\bpromos?\b",
    "pokecenter": r"pok[eé]mon cent(?:er|re)",
    "crackedice": r"cracked ice",
    "shadowless": r"shadowless",
    "celebrations": r"classic collection|celebrations",
    "e3": r"\be3\b",
    "mcdonalds": r"mcdonald",
    "buildbattle": r"build (?:&|and) battle",
    "trickortrade": r"trick or trade",
    "holiday": r"holiday",
    "jumbo": r"jumbo|oversize",
    "error": r"\berror\b|misprint",
    "deckbox": r"deck ?box|exclusive|theme deck|trainer kit|battle academy|blister|\btin\b|collection box|\bdeck\b",
    "retail": r"toys ?r ?us|costco|walmart|gamestop|best buy|target exclusive",
}
TAG_RE = {k: re.compile(v, re.I) for k, v in TAGS.items()}


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


SAFE_PAREN = re.compile(r"rare|holo|foil|reverse|secret|ultra|hyper|illustration|full art|alternate|alt art|shiny|radiant|common|"
                        r"uncommon|1st|first|unlimited|edition|vmax|vstar|\bv\b|\bex\b|\bgx\b|break|prime|legend|lv\.?\s*x|"
                        r"character|amazing|^\s*\d+\s*$", re.I)


def odd_paren(name):
    """True when the product name has a bracketed note we do not recognise, like (Poke Ball Pattern).

    Such a card is treated as a special version, so it is never used to price a plain listing."""
    for m in re.finditer(r"\(([^)]*)\)", name or ""):
        txt = m.group(1)
        if not SAFE_PAREN.search(txt) and not special_tags(txt):
            return True
    return False


def special_tags(*texts):
    t = " ".join(x or "" for x in texts)
    return sorted(k for k, rx in TAG_RE.items() if rx.search(t))


def card_number(product):
    for e in product.get("extendedData") or []:
        if e.get("name") == "Number":
            m = NUMBER_RE.match(str(e.get("value", "")))
            if m and int(m.group(1)) > 0:
                return int(m.group(1)), int(m.group(2))
    return None


def group_year(group):
    m = re.match(r"(\d{4})", str(group.get("publishedOn") or ""))
    return int(m.group(1)) if m else None


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


def build_group(products, prices, group):
    """Returns {'58/102': [record, ...]} for one set. Keeps every card, cheap or not.

    Record: {"id": productId, "name", "set", "n": number, "y": year, "t": [tags], "p": {variant: [market, mid]}}
    """
    gname = group.get("name", "")
    year = group_year(group)
    by_product = {}
    for p in prices:
        market, mid = money(p.get("marketPrice")), money(p.get("midPrice"))
        if not (market or mid):
            continue
        by_product.setdefault(p.get("productId"), {})[variant_key(p.get("subTypeName"))] = [market, mid]
    out = {}
    for prod in products:
        num = card_number(prod)
        pr = by_product.get(prod.get("productId"))
        if not num or not pr:
            continue
        rec = {"id": prod.get("productId"), "name": clean_name(prod.get("name")), "set": gname,
               "n": str(num[0]), "y": year, "p": pr}
        tags = special_tags(prod.get("name"), gname)
        if odd_paren(prod.get("name")):
            tags = sorted(set(tags) | {"variant"})
        if tags:
            rec["t"] = tags
        out.setdefault(f"{num[0]}/{num[1]}", []).append(rec)
    return out


def rec_range(rec):
    vals = [(m or d) for m, d in rec["p"].values() if (m or d)]
    return (min(vals), max(vals)) if vals else (0, 0)


def main():
    settings = load_json(SETTINGS_PATH, {})
    lo = float(settings.get("min_market", 10)) * 0.5
    hi = float(settings.get("max_market", 150)) * 2

    src_ts = fetch(BASE + "/last-updated.txt", "text")
    log(f"tcgcsv last updated: {src_ts}")
    existing = load_json(OUT_PATH, {})
    if (os.environ.get("EVENT_NAME") == "schedule" and existing.get("source_updated") == src_ts
            and existing.get("cards") and existing.get("format") == FORMAT):
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
        for k, lst in build_group(products, prices, g).items():
            cards.setdefault(k, []).extend(lst)
        if i % 25 == 0:
            log(f"  {i}/{len(groups)} sets done, {len(cards)} card numbers so far")

    # Keep a card number only if at least one version of it is in the price range we care about,
    # but when we keep it, keep ALL versions so the scanner can tell them apart.
    kept = {}
    for k, lst in cards.items():
        if any(rec_range(r)[1] >= lo and rec_range(r)[0] <= hi for r in lst):
            kept[k] = lst
    total = sum(len(v) for v in kept.values())
    log(f"Built {total} cards under {len(kept)} card numbers (of {len(cards)}). {len(failed)} sets failed.")
    if len(failed) > max(5, len(groups) * MAX_FAILED_FRACTION) or total < MIN_CARDS:
        sys.exit("Too many sets failed or too few cards came back, so the old price list was kept.")

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    tmp = OUT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"format": FORMAT, "updated_at": now_iso(), "source_updated": src_ts, "category": CATEGORY,
                   "sets": len(groups), "count": total, "cards": kept},
                  f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUT_PATH)
    log(f"Saved {OUT_PATH} ({os.path.getsize(OUT_PATH) // 1024} KB)")


if __name__ == "__main__":
    main()
