#!/usr/bin/env python3
"""
Pokemon Deal Hunter - scanner.

Runs on a schedule inside GitHub Actions (see .github/workflows/scan.yml).
Python standard library only.

What it does each run:
  1. Searches eBay (official Browse API) for newly listed raw Pokemon singles.
  2. Matches each listing to an exact card using the card number in the title
     (like 58/102) plus the Pokemon name, then looks up the Near Mint market price
     from the free Pokemon TCG API (TCGplayer prices).
  3. Reads the condition from eBay's condition field, the title, or the description,
     and adjusts the Near Mint price down for Lightly Played, Played, and so on.
  4. Scores each listing on three things:
       - price as a percent of market value (condition adjusted)
       - price gap in dollars
       - confidence, which is ONLY about how sure we are of the condition
     Scam and seller risk is shown as separate warnings and never changes confidence.
  5. Saves everything worth looking at to data/deals.json for the website.
"""
import base64
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(ROOT, "settings.json")
DEALS_PATH = os.path.join(ROOT, "data", "deals.json")
PRICES_PATH = os.path.join(ROOT, "data", "prices.json")

CATEGORY_CCG_SINGLES = "183454"
SEARCH_ERRORS = []

DEFAULTS = {
    "queries": [
        "pikachu pokemon card", "charizard pokemon card", "mewtwo pokemon card",
        "mew pokemon card", "eevee pokemon card", "umbreon pokemon card",
        "espeon pokemon card", "gengar pokemon card", "lugia pokemon card",
        "rayquaza pokemon card", "blastoise pokemon card", "venusaur pokemon card",
        "dragonite pokemon card", "gyarados pokemon card", "lucario pokemon card",
        "greninja pokemon card", "gardevoir pokemon card", "mimikyu pokemon card",
        "arceus pokemon card", "snorlax pokemon card",
    ],
    "interval_minutes": 20,           # how often scheduled scans run (5, 10, 15, 30, 60)
    "flag_price_pct": 70,             # keep listings priced at or below this % of market value
    "min_market": 10,
    "max_market": 150,
    "default_filters": {"min_gap": 15, "min_confidence": 50},
    "pool": {"min_gap": 5, "min_confidence": 10, "max_deals": 300},
    "limits": {
        "results_per_query": 200,
        "max_item_lookups": 15,
        "max_verify": 8,
        "max_new_price_lookups": 60,
        "keep_hours": 36,
        "daily_call_budget": 4500,    # eBay's free tier is about 5,000 calls a day
    },
    "assumed_shipping": 4.0,
    "condition_multipliers": {"nm": 1.0, "lp": 0.85, "mp": 0.70, "hp": 0.50, "dmg": 0.35, "unknown": 0.80},
    "extra_exclude_words": [],
}


# ----------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    try:
        return datetime.strptime(str(s), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def log(*a):
    print(*a, flush=True)


def deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def load_settings():
    return deep_merge(DEFAULTS, load_json(SETTINGS_PATH, {}))


def http_json(url, headers=None, data=None, method=None, timeout=30, retries=2):
    """Returns (status, parsed_json_or_text). status 0 means network failure."""
    last = ""
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
                try:
                    return r.status, json.loads(raw)
                except ValueError:
                    return r.status, raw
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(3 * (attempt + 1))
                continue
            return e.code, body[:300]
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last = str(e)
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
                continue
    return 0, last


# ----------------------------------------------------------------------------
# eBay
# ----------------------------------------------------------------------------

def get_token(client_id, client_secret):
    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope",
    }).encode()
    status, body = http_json("https://api.ebay.com/identity/v1/oauth2/token", {
        "Authorization": f"Basic {creds}",
        "Content-Type": "application/x-www-form-urlencoded",
    }, data=data, method="POST")
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"Could not get an eBay token (HTTP {status}): {body}")
    return body["access_token"]


def search_ebay(token, query, limit, max_price):
    params = {
        "q": query,
        "category_ids": CATEGORY_CCG_SINGLES,
        "filter": f"buyingOptions:{{FIXED_PRICE}},priceCurrency:USD,price:[1..{int(max_price)}]",
        "sort": "newlyListed",
        "limit": str(min(int(limit), 200)),
        "fieldgroups": "EXTENDED",
    }
    url = "https://api.ebay.com/buy/browse/v1/item_summary/search?" + urllib.parse.urlencode(
        params, quote_via=urllib.parse.quote, safe=":{}[]|,.")
    status, body = http_json(url, {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    })
    if status != 200 or not isinstance(body, dict):
        log(f"  ! eBay search failed for '{query}' (HTTP {status}): {str(body)[:300]}")
        SEARCH_ERRORS.append(f"HTTP {status}: {str(body)[:300]}")
        return []
    return body.get("itemSummaries", []) or []


def get_item(token, item_id):
    url = "https://api.ebay.com/buy/browse/v1/item/" + urllib.parse.quote(item_id, safe="")
    return http_json(url, {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }, retries=1)


def is_gone(detail):
    for a in (detail.get("estimatedAvailabilities") or []):
        if a.get("estimatedAvailabilityStatus") == "OUT_OF_STOCK":
            return True
    return False


def ship_cost(item, assumed):
    costs = []
    for o in (item.get("shippingOptions") or []):
        sc = o.get("shippingCost") or {}
        try:
            costs.append(float(sc.get("value")))
        except (TypeError, ValueError):
            pass
    if costs:
        return min(costs), True
    return float(assumed), False


# ----------------------------------------------------------------------------
# Title / condition parsing
# ----------------------------------------------------------------------------

BAD_BASE = (
    r"psa|bgs|beckett|cgc|sgc|graded|slab|slabbed|proxy|custom|reprint|replica|fake|orica|"
    r"metal|gold plated|oversized|jumbo|sticker|coin|booster|bundle|lot|lots|collection|bulk|"
    r"playset|set of|mystery|random|you pick|pick your|choose your|choose|digital|code|tin|"
    r"binder|sleeves|deck|japanese|japan|jpn|korean|chinese|german|french|spanish|italian|"
    r"portuguese|thai|indonesian|dutch"
)


def build_bad_regex(extra):
    words = BAD_BASE
    for w in extra or []:
        w = str(w).strip()
        if w:
            words += "|" + re.escape(w.lower())
    return re.compile(r"\b(?:" + words + r")\b", re.I)


NUM_RE = re.compile(r"(?<![\w/])(\d{1,3})\s*/\s*(\d{1,3})(?![\w/])")


def parse_number(title):
    out = []
    for m in NUM_RE.finditer(title):
        n, t = int(m.group(1)), int(m.group(2))
        if n == 0 or t < 12 or n > t + 150:
            continue
        out.append((str(n), t))
    return out


def strip_html(s):
    s = re.sub(r"<(script|style).*?</\1>", " ", s or "", flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


NOISE = re.compile(
    r"shipping|ship |transit|carrier|usps|postal|bubble|toploader|top loader|sleeve|penny|"
    r"package|packag|return|refund|feedback|payment", re.I)


def clean_desc(text, limit=1200):
    t = strip_html(text)
    # Split into small clauses so a packaging or policy phrase does not hide a condition
    # written in the same sentence ("Near mint, shipped in a toploader").
    clauses = re.split(r"(?<=[.!?;])\s+|,\s+|\s+-\s+|\s+and\s+", t)
    keep = [s for s in clauses if not NOISE.search(s)]
    return " ".join(keep)[:limit]


NEG_RE = re.compile(
    r"\b(?:no|not|without|zero|free of|never|non)\s+(?:\w+\s+){0,2}?"
    r"(?:creases?|creased|damage[d]?|dents?|dented|bends?|bent|tears?|scratch(?:es|ed)?|wear|"
    r"whitening|marks?|ink|issues?|flaws?)\b"
    r"|\b(?:undamaged|uncreased|unmarked|crease[- ]free|damage[- ]free|scratch[- ]free)\b")

COND_RE = {
    "dmg": re.compile(
        r"\b(?:damaged|damage|creases?|creased|creasing|bent|bends?|torn|tears?|ripped|"
        r"water ?damage|ink|marked|dents?|dented|peeled|stained|stain)\b"),
    "hp": re.compile(
        r"\b(?:heavily played|heavy play|heavy wear|poor|well played|worn)\b"
        r"|(?<!\d )(?<!\d)\bhp\b(?!\s*\d)"),
    "mp": re.compile(
        r"\b(?:moderately played|moderate play|moderate wear|mp|play wear)\b"
        r"|(?<!lightly )(?<!slightly )(?<!heavily )(?<!moderately )(?<!well )\bplayed\b"),
    "lp": re.compile(
        r"\b(?:lightly played|slightly played|light play|light wear|slight wear|minor wear|"
        r"minor whitening|light whitening|slight whitening|excellent|lp)\b"),
    "nm": re.compile(
        r"\b(?:near mint|near-mint|nm|nm-mt|nm/mt|nm/m|mint|unplayed|pack fresh|pulled and sleeved)\b"),
}
COND_ORDER = ["dmg", "hp", "mp", "lp", "nm"]          # worst first
COND_LABEL = {"nm": "Near Mint", "lp": "Lightly Played", "mp": "Moderately Played",
              "hp": "Heavily Played", "dmg": "Damaged", "unknown": "Condition not stated"}
SRC_PRIORITY = ("field", "title", "desc")
SRC_NAME = {"field": "eBay condition field", "title": "Title", "desc": "Description"}

# Seller points at photos or uses wording that does not really state a condition.
VAGUE_RE = re.compile(
    r"\b(?:see|view|check|refer to|look at)\s+(?:the\s+|all\s+)?(?:photos?|pictures?|pics?|images?)\b"
    r"|\bas shown\b|\bread (?:the )?description\b"
    r"|\b(?:good|great|nice|fine|decent|clean)\s+(?:condition|shape)\b|\bas[- ]is\b", re.I)


def classify_full(text):
    """Returns (worst condition key, matched phrase) found in the text, or (None, None)."""
    if not text:
        return None, None
    t = text.lower().replace("’", "'")
    t = NEG_RE.sub(" ", t)
    for key in COND_ORDER:
        m = COND_RE[key].search(t)
        if m:
            return key, m.group(0).strip()
    return None, None


def classify(text):
    return classify_full(text)[0]


def aspect_map(detail):
    out = {}
    for a in (detail or {}).get("localizedAspects") or []:
        out[str(a.get("name", "")).strip().lower()] = str(a.get("value", ""))
    return out


def detail_reject(detail):
    a = aspect_map(detail)
    lang = a.get("language", "")
    if lang and "english" not in lang.lower():
        return "language"
    if a.get("graded", "").strip().lower() == "yes":
        return "graded"
    g = a.get("professional grader", "").strip().lower()
    if g and g not in ("n/a", "none", "no"):
        return "graded"
    return None


def read_condition(item, detail):
    """Reads the condition from the eBay field, the title and the description separately."""
    src = detail or item
    field_parts = [str(item.get("condition") or ""), str((detail or {}).get("condition") or "")]
    for d in (src.get("conditionDescriptors") or []):
        for v in d.get("values", []) or []:
            field_parts.append(str(v.get("content") or ""))
    for name, value in aspect_map(detail).items():
        if "condition" in name:
            field_parts.append(value)
    texts = (
        ("field", " ".join(field_parts)),
        ("title", item.get("title") or ""),
        ("desc", clean_desc((item.get("shortDescription") or "") + " " + ((detail or {}).get("description") or ""))),
    )
    sources = {}
    for name, text in texts:
        k, phrase = classify_full(text)
        if k:
            sources[name] = (k, phrase)
    vague = any(VAGUE_RE.search(t) for _, t in texts if t)
    if not sources:
        return {"key": "unknown", "source": None, "sources": {}, "vague": vague}
    key = min((k for k, _ in sources.values()), key=lambda k: COND_ORDER.index(k))
    primary = next(s for s in SRC_PRIORITY if s in sources and sources[s][0] == key)
    return {"key": key, "source": primary, "sources": sources, "vague": vague}


# Confidence is ONLY about how sure we are of the condition.
CONF_CAP = {"nm": 100, "lp": 80, "mp": 45, "hp": 40, "dmg": 40, "unknown": 25}


def condition_confidence(cd):
    """Returns (confidence 0-100, list of evidence lines)."""
    key, srcs = cd["key"], cd["sources"]
    lines = []
    for s in SRC_PRIORITY:
        if s in srcs:
            k, phrase = srcs[s]
            lines.append(f'{SRC_NAME[s]} says "{phrase}" ({COND_LABEL[k]})')
    agree = []
    if key == "unknown":
        base = 25
        lines.append("No condition stated, so a Lightly Played price was assumed")
    else:
        agree = [s for s, (k, _) in srcs.items() if k == key]
        if len(agree) >= 3:
            base = 100
            lines.append("The eBay field, title and description all agree")
        elif len(agree) == 2:
            base = 90
            lines.append("Two sources agree")
        else:
            base = {"field": 80, "title": 75, "desc": 60}[agree[0]]
            lines.append("Only one source states the condition")
        if len(agree) < len(srcs):
            base -= 25
            lines.append("Sources disagree, so the worst condition was used")
    # Pointing at photos only matters when the condition is not already clearly confirmed.
    if cd["vague"] and len(agree) < 2:
        base -= 15
        lines.append("Seller points to photos or uses vague wording instead of stating a condition")
    cap = CONF_CAP[key]
    if key == "lp":
        lines.append("Lightly Played is capped at 80 because wear varies between copies")
    elif key in ("mp", "hp", "dmg"):
        lines.append("Played and worse conditions get low confidence because they vary a lot between copies, so check the photos")
    return int(max(0, min(base, cap))), lines


# ----------------------------------------------------------------------------
# Card matching and market price (Pokemon TCG API)
# ----------------------------------------------------------------------------

def price_of(v):
    return (v or {}).get("market") or (v or {}).get("mid") or 0


def lookup_cards(num, total, ctx):
    """Cards in the saved TCGplayer price list that have this number and set size."""
    try:
        key = f"{int(num)}/{int(total)}"
    except ValueError:
        return []
    out = []
    for c in ctx["prices"].get(key, []):
        c = dict(c)
        c["updated"] = ctx.get("prices_updated")
        out.append(c)
    return out


def norm_tokens(s):
    return re.findall(r"[a-z0-9]+", (s or "").lower().replace("'", "").replace("’", ""))


def match_cards(title, cards):
    tt = set(norm_tokens(title))
    hits = []
    for c in cards:
        nt = norm_tokens(c["name"])
        if nt and all(t in tt for t in nt) and c["prices"]:
            hits.append(c)
    return hits


def pick_variant(title, card):
    prices = card["prices"]
    keys = list(prices.keys())
    if not keys:
        return None, False
    t = title.lower()
    want = []
    if "reverse" in t:
        want = ["reverseHolofoil"]
    elif re.search(r"\b(?:1st|first)\s*ed", t):
        want = ["1stEditionHolofoil", "1stEditionNormal", "1stEdition"]
    elif "unlimited" in t:
        want = ["unlimitedHolofoil", "unlimitedNormal", "unlimited"]
    elif re.search(r"\bholo", t):
        want = ["holofoil", "unlimitedHolofoil"]
    for k in want:
        if k in prices:
            return k, True
    if len(keys) == 1:
        return keys[0], True
    pool = [k for k in keys if not k.startswith("1stEdition")] or keys
    return min(pool, key=lambda k: price_of(prices[k])), False


# ----------------------------------------------------------------------------
# Scam / seller warnings (shown as badges, never change confidence)
# ----------------------------------------------------------------------------

def parse_date(s):
    try:
        return datetime.strptime(str(s)[:10].replace("-", "/"), "%Y/%m/%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def build_warnings(item, detail, price_pct, seller, ship_known, assumed, ambiguous, vclear, card, detailed):
    w = []

    def add(level, text):
        w.append({"level": level, "text": text})

    if price_pct <= 25:
        add("danger", f"Priced at only {price_pct:.0f}% of market. Make sure it is the right card and not a fake or a scam")
    elif price_pct <= 40:
        add("warn", f"Priced at {price_pct:.0f}% of market, so double check the card and photos")

    fb_pct, fb_score = seller.get("pct"), seller.get("score")
    if fb_score is not None and fb_score < 10:
        add("danger", f"Very new seller ({fb_score} feedback)")
    elif fb_score is not None and fb_score < 50:
        add("warn", f"Seller has few sales ({fb_score} feedback)")
    if fb_pct is not None and fb_pct < 90:
        add("danger", f"Low seller rating ({fb_pct:g}%)")
    elif fb_pct is not None and fb_pct < 95:
        add("warn", f"Seller rating is below average ({fb_pct:g}%)")

    loc = (detail or {}).get("itemLocation") or item.get("itemLocation") or {}
    country = loc.get("country")
    if country and country != "US":
        if country in ("CN", "HK"):
            add("danger", f"Ships from {country}, where fake cards are common")
        else:
            add("warn", f"Ships from {country}")
    if detail and (detail.get("returnTerms") or {}).get("returnsAccepted") is False:
        add("warn", "Seller does not accept returns")
    if not ship_known:
        add("info", f"Shipping not listed, assumed ${assumed:.2f}")
    updated = parse_date(card.get("updated"))
    if updated and (datetime.now(timezone.utc) - updated).days > 3:
        add("info", "Price list is more than 3 days old (the daily price update may have failed)")
    if ambiguous:
        add("info", "Several cards share this number, so the lowest price was used")
    elif not vclear:
        add("info", "Card has several print versions, so it was compared to the cheapest")
    if not detailed:
        add("info", "Full listing not read yet")
    return w


# ----------------------------------------------------------------------------
# Evaluate one listing
# ----------------------------------------------------------------------------

def skip(ctx, reason, detail):
    """Counts why a listing was dropped (first pass only) so the log can explain it."""
    if detail is None:
        d = ctx.setdefault("drops", {})
        d[reason] = d.get(reason, 0) + 1
    return None


def evaluate(item, detail, ctx):
    """Turns one eBay listing into a scored deal dict, or None if it is not usable."""
    S = ctx["S"]
    title = item.get("title") or ""
    if ctx["bad"].search(title):
        return skip(ctx, "title has an excluded word (lot, graded, Japanese, proxy, ...)", detail)
    price_obj = item.get("price") or {}
    if price_obj.get("currency", "USD") != "USD":
        return skip(ctx, "not priced in USD", detail)
    try:
        price = float(price_obj.get("value"))
    except (TypeError, ValueError):
        return skip(ctx, "no usable price", detail)
    if detail and detail_reject(detail):
        return None

    nums = parse_number(title)
    if not nums:
        return skip(ctx, "no card number like 58/102 in the title", detail)
    hits, used = None, None
    got_cards = False
    for n, t in nums[:2]:
        cards = lookup_cards(n, t, ctx)
        if cards is None:
            continue
        got_cards = True
        h = match_cards(title, cards)
        if h:
            hits, used = h, (n, t)
            break
    if not hits:
        if not got_cards:
            return skip(ctx, "price list not available", detail)
        return skip(ctx, "card number found but not in the TCGplayer price list (card may be too cheap, or a different set)", detail)

    picks = []
    for c in hits:
        k, clear = pick_variant(title, c)
        if k:
            m = price_of(c["prices"][k])
            if m:
                picks.append((m, c, k, clear))
    if not picks:
        return skip(ctx, "matched card has no market price", detail)
    picks.sort(key=lambda x: x[0])
    market, card, variant, vclear = picks[0]
    ambiguous = len(picks) > 1
    if not (S["min_market"] <= market <= S["max_market"]):
        return skip(ctx, f"card market value outside ${S['min_market']}-${S['max_market']}", detail)

    cd = read_condition(item, detail)
    cond = cd["key"]
    mult = S["condition_multipliers"].get(cond, S["condition_multipliers"]["unknown"])
    expected = round(market * mult, 2)
    ship, ship_known = ship_cost(detail or item, S["assumed_shipping"])
    total = price + ship
    gap = expected - total
    if gap <= 0:
        return skip(ctx, "listed at or above its market value", detail)
    price_pct = total / expected * 100

    confidence, reasons = condition_confidence(cd)

    seller_raw = item.get("seller") or {}
    try:
        fb_pct = float(seller_raw.get("feedbackPercentage"))
    except (TypeError, ValueError):
        fb_pct = None
    try:
        fb_score = int(seller_raw.get("feedbackScore"))
    except (TypeError, ValueError):
        fb_score = None
    seller = {"name": seller_raw.get("username"), "pct": fb_pct, "score": fb_score}
    warnings = build_warnings(item, detail, price_pct, seller, ship_known, S["assumed_shipping"],
                              ambiguous, vclear, card, bool(detail))

    img = (item.get("image") or {}).get("imageUrl")
    if not img:
        thumbs = item.get("thumbnailImages") or []
        img = thumbs[0].get("imageUrl") if thumbs else None
    now = now_iso()
    return {
        "id": item.get("itemId"),
        "title": title,
        "url": item.get("itemWebUrl"),
        "image": img,
        "price": round(price, 2),
        "shipping": round(ship, 2),
        "ship_known": ship_known,
        "total": round(total, 2),
        "card": card["name"],
        "set": card["set"],
        "number": f"{used[0]}/{used[1]}",
        "variant": variant,
        "market": round(market, 2),
        "condition": cond,
        "condition_label": COND_LABEL[cond],
        "condition_source": cd["source"],
        "expected": expected,
        "price_pct": round(price_pct, 1),
        "gap": round(gap, 2),
        "confidence": confidence,
        "score": round(gap * confidence / 100, 2),
        "reasons": reasons,
        "warnings": warnings,
        "tcg_url": card.get("url"),
        "seller": seller,
        "first_seen": now,
        "last_seen": now,
        "detailed": bool(detail),
        "checked_at": now if detail else None,
    }


# ----------------------------------------------------------------------------
# Run planning
# ----------------------------------------------------------------------------

def plan_run(S, scheduled, cursor):
    """Decides how many searches and detail lookups this run can afford.

    Manual runs use everything. Scheduled runs stay inside the daily eBay call budget,
    rotating through the searches when the run rate is fast.
    """
    lim = S["limits"]
    queries = [q for q in S["queries"] if str(q).strip()][:40]
    full_lookups, full_verify = int(lim["max_item_lookups"]), int(lim["max_verify"])
    if not scheduled or not queries:
        return queries, full_lookups, full_verify, cursor
    interval = max(5.0, float(S["interval_minutes"]))
    runs_per_day = 1440.0 / interval
    per_run = int(float(lim["daily_call_budget"]) / runs_per_day)
    detail = min(full_lookups + full_verify, max(4, per_run // 3))
    nq = max(2, min(len(queries), per_run - detail))
    lookups = max(2, round(detail * 0.65))
    verify = max(1, detail - lookups)
    n = len(queries)
    start = cursor % n
    chosen = [queries[(start + i) % n] for i in range(nq)]
    return chosen, lookups, verify, (start + nq) % n


def should_skip(S, prev):
    """Scheduled runs wake up every 5 minutes and quit if it is too soon."""
    if os.environ.get("EVENT_NAME", "") != "schedule":
        return False
    last = parse_iso(prev.get("updated_at"))
    if not last:
        return False
    elapsed = (datetime.now(timezone.utc) - last).total_seconds() / 60.0
    return elapsed < float(S["interval_minutes"]) - 2.0


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main():
    S = load_settings()
    prev = load_json(DEALS_PATH, {"deals": []})
    scheduled = os.environ.get("EVENT_NAME", "") == "schedule"

    if should_skip(S, prev):
        log(f"Skipping: last scan was recent and the run rate is {S['interval_minutes']} minutes.")
        return

    override = os.environ.get("FLAG_PRICE_PCT_OVERRIDE", "").strip()
    if override:
        try:
            S["flag_price_pct"] = max(5.0, min(100.0, float(override)))
            log(f"Using {S['flag_price_pct']:g}% of market for this run only.")
        except ValueError:
            pass

    pdata = load_json(PRICES_PATH, {})
    if not pdata.get("cards"):
        sys.exit("There is no price list yet. In GitHub open Actions, click 'Update prices' on the left, "
                 "click Run workflow, wait for the green check, then run the scan again.")
    pcount = int(pdata.get("count") or sum(len(v) for v in pdata["cards"].values()))
    log(f"Price list: {pcount} cards, downloaded {pdata.get('updated_at')} (TCGplayer data from tcgcsv.com)")

    cid = os.environ.get("EBAY_CLIENT_ID", "").strip()
    secret = os.environ.get("EBAY_CLIENT_SECRET", "").strip()
    if not cid or not secret:
        sys.exit("Missing EBAY_CLIENT_ID / EBAY_CLIENT_SECRET. Add them under Settings > Secrets in GitHub.")
    token = get_token(cid, secret)

    prev_deals = {d["id"]: d for d in prev.get("deals", []) if d.get("id")}
    lim = S["limits"]
    ctx = {
        "S": S, "bad": build_bad_regex(S["extra_exclude_words"]),
        "prices": pdata["cards"], "prices_updated": pdata.get("updated_at"),
    }
    calls = {"search": 0, "item": 0}
    flag = float(S["flag_price_pct"])

    queries, max_lookups, max_verify, cursor = plan_run(S, scheduled, int(prev.get("query_cursor", 0) or 0))

    # 1. search
    seen = {}
    for q in queries:
        items = search_ebay(token, q, lim["results_per_query"], S["max_market"])
        calls["search"] += 1
        for it in items:
            if it.get("itemId"):
                seen.setdefault(it["itemId"], it)
        time.sleep(0.25)
    log(f"Search: {len(seen)} unique listings from {len(queries)} searches")
    if not seen:
        msg = "eBay returned 0 listings."
        if SEARCH_ERRORS:
            msg += " First error: " + SEARCH_ERRORS[0]
        else:
            msg += " No error was reported, so the searches matched nothing."
        sys.exit(msg)

    # 2. first pass on search results
    now = now_iso()
    P = S["pool"]
    pool = {}
    fresh = []
    for iid, it in seen.items():
        old = prev_deals.get(iid)
        try:
            price = float((it.get("price") or {}).get("value"))
        except (TypeError, ValueError):
            continue
        if old and old.get("detailed") and abs(old.get("price", -1) - price) < 0.01:
            old["last_seen"] = now
            pool[iid] = old
            continue
        d = evaluate(it, None, ctx)
        if d:
            if old:
                d["first_seen"] = old.get("first_seen", d["first_seen"])
            fresh.append((it, d))
    log(f"First pass: {len(fresh)} listings matched to a card and priced under market")

    # 3. second pass: read the full listing for the most promising ones.
    # Listings with no stated condition might be Near Mint once the description is read,
    # so they are judged against the full Near Mint price when choosing what to look at.
    def promising(d):
        if d["condition"] == "unknown":
            pct, gap = d["total"] / d["market"] * 100, d["market"] - d["total"]
        else:
            pct, gap = d["price_pct"], d["gap"]
        return pct <= flag and gap >= P["min_gap"], gap

    cand = []
    for it, d in fresh:
        ok, gap = promising(d)
        if ok:
            cand.append((gap, it, d))
    cand.sort(key=lambda x: x[0], reverse=True)
    looked = set()
    for _, it, d in cand[:max_lookups]:
        status, detail = get_item(token, d["id"])
        calls["item"] += 1
        time.sleep(0.25)
        looked.add(d["id"])
        if status == 404:
            continue
        if status != 200 or not isinstance(detail, dict) or is_gone(detail):
            pool[d["id"]] = d
            continue
        merged = dict(it)
        merged.update({k: v for k, v in detail.items() if v is not None})
        d2 = evaluate(merged, detail, ctx)
        if d2:
            d2["first_seen"] = d["first_seen"]
            pool[d["id"]] = d2
    for it, d in fresh:
        if d["id"] not in looked and d["id"] not in pool:
            pool[d["id"]] = d

    # 4. re-check earlier deals that did not show up this time (they may have sold)
    cutoff = time.time() - float(lim["keep_hours"]) * 3600
    stale = [d for iid, d in prev_deals.items() if iid not in seen and iid not in pool]
    stale.sort(key=lambda d: d.get("checked_at") or "")
    for i, d in enumerate(stale):
        first = parse_iso(d.get("first_seen"))
        if first is not None and first.timestamp() < cutoff:
            continue
        if i < max_verify:
            status, detail = get_item(token, d["id"])
            calls["item"] += 1
            time.sleep(0.25)
            if status == 404:
                continue
            if status == 200 and isinstance(detail, dict):
                if is_gone(detail):
                    continue
                d2 = evaluate(detail, detail, ctx)
                if d2:
                    d2["first_seen"] = d.get("first_seen", d2["first_seen"])
                    pool[d["id"]] = d2
                continue
        pool[d["id"]] = d

    # 5. keep what is worth a look and save
    final = [d for d in pool.values()
             if d["price_pct"] <= flag and d["gap"] >= P["min_gap"] and d["confidence"] >= P["min_confidence"]]
    final.sort(key=lambda d: d["score"], reverse=True)
    final = final[: int(P["max_deals"])]

    log("Why listings were skipped (first pass):")
    for k, v in sorted(ctx.get("drops", {}).items(), key=lambda x: -x[1]):
        log(f"  {v:>5}  {k}")

    df = dict(S["default_filters"])
    df["max_price_pct"] = flag
    out = {
        "updated_at": now_iso(),
        "query_cursor": cursor,
        "settings": {
            "flag_price_pct": flag,
            "interval_minutes": S["interval_minutes"],
            "default_filters": df,
            "min_market": S["min_market"],
            "max_market": S["max_market"],
        },
        "run": {"mode": "scheduled" if scheduled else "manual", "searches": len(queries),
                "searches_total": len([q for q in S["queries"] if str(q).strip()]),
                "skipped": ctx.get("drops", {}), "price_list_cards": pcount,
                "price_list_updated": pdata.get("updated_at")},
        "stats": {
            "listings_seen": len(seen),
            "matched": len(fresh),
            "deals_saved": len(final),
            "ebay_calls": calls["search"] + calls["item"],
        },
        "deals": final,
    }
    save_json(DEALS_PATH, out)

    log(f"Saved {len(final)} deals. eBay calls this run: {calls['search'] + calls['item']}.")


if __name__ == "__main__":
    main()
