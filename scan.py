#!/usr/bin/env python3
"""
Pokemon Deal Hunter - scanner.

Runs in GitHub Actions (see .github/workflows/scan.yml). Python standard library only.
Needs data/prices.json, which prices.py builds once a day from TCGplayer data.

What it does each scan:
  1. Searches eBay (official Browse API) for Buy It Now Pokemon singles.
  2. Matches each listing to an exact card using the card number in the title (like 58/102),
     the Pokemon name, and any special version the title mentions (stamped, promo, cosmos...).
  3. Looks up the Near Mint market price and adjusts it for the condition in the listing.
     Older sets lose more value when played, so the adjustment depends on the set's era.
  4. Scores each listing on three things:
       - price as a percent of market value (condition adjusted)
       - price gap in dollars
       - confidence, which is ONLY about how sure we are of the condition
     Scam and seller risk is shown as separate warnings and never changes confidence.
  5. Saves the results as a numbered scan (data/scans/<n>.json) and updates the running list
     of current deals (data/deals.json).

Automatic (scheduled) scans only run when auto_scan is true in settings.json.
Manual scans always run.
"""
import base64
import html
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from prices import TAGS  # the list of special card versions, shared with the price builder

ROOT = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(ROOT, "settings.json")
DEALS_PATH = os.path.join(ROOT, "data", "deals.json")
PRICES_PATH = os.path.join(ROOT, "data", "prices.json")
SCANS_PATH = os.path.join(ROOT, "data", "scans.json")
SCAN_DIR = os.path.join(ROOT, "data", "scans")
FEEDBACK_PATH = os.path.join(ROOT, "data", "feedback.json")     # written by the website (Remove buttons)
SEEN_PATH = os.path.join(ROOT, "data", "seen.json")             # listing ids already seen in earlier scans
STATE_PATH = os.path.join(ROOT, "data", "search_state.json")    # which exact cards were searched, and when
STATS_PATH = os.path.join(ROOT, "data", "condition_stats.json") # asking prices by condition, learned from eBay
MAX_SEEN = 20000
KEEP_SCAN_FILES = 20          # full results kept for this many recent scans
KEEP_SCAN_SUMMARIES = 200     # one-line history kept for this many scans
CATEGORY_CCG_SINGLES = "183454"
SEARCH_ERRORS = []

NAMES = [
    "charizard", "pikachu", "mewtwo", "mew", "eevee", "umbreon", "espeon", "sylveon", "vaporeon",
    "jolteon", "flareon", "leafeon", "glaceon", "gengar", "lugia", "ho-oh", "rayquaza", "blastoise",
    "venusaur", "dragonite", "gyarados", "lucario", "greninja", "gardevoir", "mimikyu", "arceus",
    "snorlax", "suicune", "entei", "raikou", "celebi", "giratina", "dialga", "palkia", "garchomp",
    "tyranitar", "alakazam", "machamp", "ninetales", "arcanine", "scizor", "charmander", "squirtle",
    "bulbasaur", "jigglypuff", "togepi", "lapras", "zoroark", "miraidon", "koraidon",
]
TERMS = [
    "base set holo", "jungle holo", "fossil holo", "team rocket holo", "gym heroes", "gym challenge",
    "neo genesis holo", "neo discovery", "neo revelation", "neo destiny", "legendary collection",
    "expedition holo", "aquapolis", "skyridge", "ex holo", "full art", "radiant", "vstar", "vmax",
    "alternate art", "illustration rare", "special illustration rare", "secret rare", "shiny",
    "reverse holo", "holo rare",
]
DEFAULT_QUERIES = [f"{n} pokemon card" for n in NAMES] + [f"{t} pokemon card" for t in TERMS]

ENGINE = 5   # bumped whenever matching rules change; deals saved by an older version are thrown away

DEFAULTS = {
    "queries": DEFAULT_QUERIES,
    "auto_scan": False,               # automatic scans are OFF unless you switch them on
    "interval_minutes": 20,           # how often automatic scans run (5, 10, 15, 30, 60)
    "flag_price_pct": 70,             # keep listings priced at or below this % of market value
    "min_market": 10,
    "max_market": 150,
    "min_listing_price": 3,           # ignore listings under this price (bulk cards crowd the results)
    "min_price_pct": 20,              # under this % of market is almost always a wrong match
    "skip_unclear_versions": True,    # True: skip listings when a special version (prize pack, stamped...) of the same number exists and the title does not say
    "pages_per_search": 5,            # most pages read per search. A search stops early once a page has no new listings
    "card_searches": 150,             # manual scans also search this many exact cards (name + number), oldest-searched first
    "max_search_calls": 700,          # hard cap on eBay search calls in one manual scan
    "default_filters": {"min_gap": 15, "min_confidence": 50},
    "pool": {"min_gap": 5, "min_confidence": 10, "max_deals": 400},
    "limits": {
        "results_per_query": 200,
        "max_item_lookups": 60,
        "max_verify": 10,
        "keep_hours": 36,
        "daily_call_budget": 4500,    # eBay's free tier is about 5,000 calls a day
    },
    "assumed_shipping": 6.0,
    "us_only": True,                  # only listings located in the USA
    "nm_single_source_factor": 0.90,   # a Near Mint claim backed by only one source is priced at this share of Near Mint (grows with your condition reports)
    # How much of the Near Mint price a copy is worth. Older cards lose more when played.
    # These are rules of thumb, not data. Tune them if they feel off.
    "condition_value_scaling": 0.12,   # pricier cards lose a bigger share when played
    "era_multipliers": {
        "vintage": {"nm": 1.0, "lp": 0.62, "mp": 0.45, "hp": 0.30, "dmg": 0.20, "unknown": 0.58},
        "classic": {"nm": 1.0, "lp": 0.75, "mp": 0.58, "hp": 0.40, "dmg": 0.27, "unknown": 0.68},
        "modern":  {"nm": 1.0, "lp": 0.85, "mp": 0.70, "hp": 0.50, "dmg": 0.35, "unknown": 0.80},
    },
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


def search_ebay(token, query, limit, max_price, min_price=1, offset=0):
    params = {
        "q": query,
        "category_ids": CATEGORY_CCG_SINGLES,
        "filter": f"buyingOptions:{{FIXED_PRICE}},priceCurrency:USD,itemLocationCountry:US,price:[{int(min_price)}..{int(max_price)}]",
        "sort": "newlyListed",
        "limit": str(min(int(limit), 200)),
        "offset": str(int(offset)),
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
    r"extended art(?:work)?s?|extended|ext\.? art|fan art|altered|handmade|acrylic|metal|gold plated|oversized|jumbo|sticker|coin|booster|bundle|lot|lots|collection|bulk|"
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
        r"\b(?:moderately played|moderate play|moderate wear|mp|play wear|indents?|indentation|rounded corners?|"
        r"visible wear|noticeable wear|obvious wear|very good|vg|light bend|slight bend)\b"
        r"|(?<!lightly )(?<!slightly )(?<!heavily )(?<!moderately )(?<!well )\bplayed\b"),
    "lp": re.compile(
        r"\b(?:lightly played|slightly played|light play|light wear|slight wear|minor wear|"
        r"minor whitening|light whitening|slight whitening|excellent|lp|whitening|edge ?wear|corner wear|"
        r"silvering|scratch(?:es|ed|ing)?|scuff(?:s|ed|ing)?|dings?|nicks?|imperfections?|blemish(?:es)?|"
        r"surface wear|print lines?|fingerprints?|shelf wear|minor flaws?|small flaws?)\b"),
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


# Store-wide boilerplate and hedges are not about this card.
DISCLAIM_RE = re.compile(
    r"\ball (?:of )?(?:my |our |the )?(?:cards|items|listings)\b[^.;\n]*"
    r"|\b(?:may|might|could|can|will)\s+(?:have|show|contain|include|be)\b[^.;\n]*"
    r"|\bif any\b[^.;\n]*|\bunless\b[^.;\n]*|\b(?:please\s+)?(?:check|see|view|review|look at|refer to)\s+(?:the\s+|all\s+)?(?:photos?|pictures?|pics?|images?)[^.;\n]*", re.I)


def classify_full(text):
    """Returns (worst condition key, matched phrase) found in the text, or (None, None)."""
    if not text:
        return None, None
    t = text.lower().replace("’", "'")
    t = DISCLAIM_RE.sub(" ", t)
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
    field_parts = [str(item.get("condition") or ""), str((detail or {}).get("condition") or ""),
                   str(item.get("conditionDescription") or ""), str((detail or {}).get("conditionDescription") or "")]
    for d in (src.get("conditionDescriptors") or []):
        for v in d.get("values", []) or []:
            field_parts.append(str(v.get("content") or ""))
    for name, value in aspect_map(detail).items():
        if "condition" in name:
            field_parts.append(value)
    texts = (
        ("field", " ".join(field_parts)),
        ("title", item.get("title") or ""),
        ("desc", clean_desc((item.get("shortDescription") or "") + " " + ((detail or {}).get("description") or "")
                            + " " + " ".join(v for n_, v in aspect_map(detail).items() if "condition" not in n_ and n_ in ("features", "notes", "additional information")))),
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
        lines.append("No condition stated, so a reduced price was assumed")
    else:
        agree = [s for s, (k, _) in srcs.items() if k == key]
        if len(agree) >= 3:
            base = 100
            lines.append("The eBay field, title and description all agree")
        elif len(agree) == 2:
            base = 90
            lines.append("Two sources agree")
        else:
            base = {"field": 70, "title": 65, "desc": 55}[agree[0]] if key == "nm" else {"field": 80, "title": 75, "desc": 60}[agree[0]]
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
# Card matching and market price (saved TCGplayer price list)
# ----------------------------------------------------------------------------

TITLE_TAG_RE = {k: re.compile(v, re.I) for k, v in TAGS.items()}


def expand(rec):
    """Turns a compact price-list record into the shape the scanner uses."""
    pid = rec.get("id")
    return {
        "id": pid, "name": rec.get("name", ""), "set": rec.get("set", ""), "number": rec.get("n"),
        "year": rec.get("y"), "tags": rec.get("t") or [],
        "url": f"https://www.tcgplayer.com/product/{pid}" if pid else None,
        "prices": {k: {"market": v[0], "mid": v[1]} for k, v in (rec.get("p") or {}).items()
                   if v and (v[0] or v[1])},
    }


def lookup_cards(num, total, ctx):
    """All versions of the card with this number and set size from the saved price list."""
    try:
        key = f"{int(num)}/{int(total)}"
    except ValueError:
        return []
    out = []
    for rec in ctx["prices"].get(key, []):
        c = expand(rec)
        c["updated"] = ctx.get("prices_updated")
        out.append(c)
    return out


def reference_price(v):
    """The Near Mint price to use for one variant, and where it came from.

    TCGplayer's market price is recent sales. On thin markets one big sale can inflate it, so if
    it is far above the mid (median listing) price, the mid price is used instead.
    """
    market, mid = (v or {}).get("market"), (v or {}).get("mid")
    if market and mid and market > 1.5 * mid:
        return mid, "TCGplayer's market price looked inflated, so the mid price was used"
    if market:
        return market, None
    return (mid or 0), ("no market price, so the mid price was used" if mid else None)


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


def title_tags(title):
    return {k for k, rx in TITLE_TAG_RE.items() if rx.search(title)}


def pick_candidates(title, cards, extra=""):
    """Chooses which versions of a card a listing can be compared against.

    A special version (stamped, promo, cosmos holo, shadowless, ...) is only used when the title
    says so. Otherwise only the regular versions are used. Returns (cards, reason_if_none).
    """
    named = match_cards(title, cards)
    if not named:
        return [], "name"
    mentioned = title_tags(title + " " + extra)
    if mentioned:
        tagged = [c for c in named if c["tags"] and mentioned & set(c["tags"])]
        if not tagged:
            return [], "special"
        best = max(len(mentioned & set(c["tags"])) for c in tagged)
        return [c for c in tagged if len(mentioned & set(c["tags"])) == best], None
    plain = [c for c in named if not c["tags"]]
    if plain:
        return plain, None
    return [], "only_special"


NONHOLO_RE = re.compile(r"non[\s-]?holo|no[\s-]holo|not[\s-]holo|\bnh\b", re.I)
HOLO_KEYS = ("holofoil", "unlimitedHolofoil", "1stEditionHolofoil")
PLAIN_KEYS = ("normal", "unlimitedNormal", "unlimited", "1stEditionNormal", "1stEdition")


def finish_of(text):
    """'nonholo', 'reverse', 'holo' or None, from the title (and eBay's item details)."""
    t = text.lower()
    if NONHOLO_RE.search(t):
        return "nonholo"
    if "reverse" in t:
        return "reverse"
    if re.search(r"\bholo|holographic|\bfoil\b", t):
        return "holo"
    return None


def pick_variant(title, card):
    prices = card["prices"]
    keys = list(prices.keys())
    if not keys:
        return None, False
    t = title.lower()
    want = []
    fin = finish_of(title)
    if fin == "nonholo":
        want = [k for k in PLAIN_KEYS if k in prices]
        if not want:
            return None, False
        for k in ("1stEditionNormal", "1stEdition"):
            if k in want and not re.search(r"\b(?:1st|first)\s*ed", t):
                want.remove(k)
    elif fin == "reverse":
        want = ["reverseHolofoil"]
    elif re.search(r"\b(?:1st|first)\s*ed", t) or "unlimited" in t:
        first = bool(re.search(r"\b(?:1st|first)\s*ed", t))
        group = [k for k in keys if k.startswith("1stEdition")] if first else [k for k in keys if k.startswith("unlimited")]
        if fin == "holo":
            group = [k for k in group if "Holofoil" in k] or group
        if group:
            if len(group) == 1 or fin == "holo":
                return group[0], True
            return min(group, key=lambda k: reference_price(prices[k])[0]), False
    elif fin == "holo":
        want = ["holofoil", "unlimitedHolofoil"]
    for k in want:
        if k in prices:
            return k, True
    if fin == "nonholo":
        return None, False
    if len(keys) == 1:
        return keys[0], True
    pool = [k for k in keys if not k.startswith("1stEdition")] or keys
    return min(pool, key=lambda k: reference_price(prices[k])[0]), False


ERA_HINTS = [
    ("vintage", re.compile(r"base set|jungle|fossil|team rocket|gym heroes|gym challenge|\bneo\b|legendary collection|expedition|"
                           r"aquapolis|skyridge|southern islands|wizards|best of game|e-?reader", re.I)),
    ("classic", re.compile(r"\bex\b.*(ruby|sandstorm|dragon|magma|aqua|hidden legends|firered|rocket returns|deoxys|emerald|unseen|"
                           r"delta|legend maker|holon|crystal guardians|power keepers)|diamond|pearl|platinum|heartgold|soulsilver|"
                           r"call of legends|black ?& ?white|\bbw\b|plasma|boundaries|dragons exalted|dark explorers|next destinies|"
                           r"noble victories|emerging powers|legendary treasures|\bxy|primal clash|roaring skies|ancient origins|"
                           r"furious fists|flashfire|phantom forces|breakthrough|breakpoint|fates collide|steam siege|evolutions|"
                           r"generations|double crisis|majestic dawn|great encounters|secret wonders|mysterious treasures|"
                           r"legends awakened|stormfront|rising rivals|supreme victors|arceus|triumphant|undaunted|unleashed|"
                           r"dragon vault|rumble|hgss", re.I)),
]


def era_of(year, set_name=""):
    """vintage (WOTC, 1999-2003), classic (2004-2016) or modern (2017 on). The set name wins over the year."""
    for era, rx in ERA_HINTS:
        if set_name and rx.search(set_name):
            return era
    if year is None:
        return "classic"          # unknown age: the middle setting
    if year <= 2003:
        return "vintage"
    if year <= 2016:
        return "classic"
    return "modern"


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return 0.0
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def build_learned(stats):
    """Real asking prices seen on eBay by condition: how much LP/MP/HP/Damaged copies ask relative to NM copies."""
    out = {}
    for era, d in (stats.get("samples") or {}).items():
        nm = d.get("nm") or []
        if len(nm) < 10:
            continue
        mn = median(nm)
        for c in ("lp", "mp", "hp", "dmg"):
            xs = d.get(c) or []
            if len(xs) >= 10 and mn > 0:
                out.setdefault(era, {})[c] = (len(xs), min(1.0, median(xs) / mn))
    return out


def cond_multiplier(S, ctx, era, cond, market):
    """What share of the Near Mint price this condition is worth. Returns (multiplier, note)."""
    if cond == "nm":
        return 1.0, None
    base = S["era_multipliers"][era]
    prior = base.get(cond, base["unknown"])
    k = 1 + float(S.get("condition_value_scaling", 0.12)) * math.log10(max(float(market), 10.0) / 10.0)
    prior = prior ** k
    st = (ctx.get("learned") or {}).get(era, {}).get(cond)
    if st:
        n, ratio = st
        w = min(0.5, n / 120.0)
        ratio = max(0.6 * prior, min(1.2 * prior, ratio))
        return round((1 - w) * prior + w * ratio, 3), f"blended with {n} {era} eBay listings in this condition"
    return round(prior, 3), None


DESC_STRONG = re.compile(r"deck exclusive|prize pack|stamped|cosmos|pre-?release|\bstaff\b|\b(?:25|30)th anniversary|celebrations|"
                         r"shadowless|build (?:&|and) battle|trick or trade|cracked ice|pok[eé]mon cent(?:er|re)", re.I)


def eid(item_id):
    """Short number for an eBay item id like v1|123456789012|0."""
    m = re.search(r"(\d{6,})", str(item_id or ""))
    return int(m.group(1)) if m else None


# ----------------------------------------------------------------------------
# Scam / seller warnings (shown as badges, never change confidence)
# ----------------------------------------------------------------------------

def parse_date(s):
    try:
        return datetime.strptime(str(s)[:10].replace("-", "/"), "%Y/%m/%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def build_warnings(item, detail, price_pct, seller, ship_known, assumed, amb_text, vclear, card, detailed, ref_note):
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
    if amb_text:
        add("info", amb_text)
    elif not vclear:
        add("info", "Card has several print versions (holo, reverse holo...), so it was compared to the cheapest")
    if ref_note:
        add("info", ref_note[0].upper() + ref_note[1:])
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


SPECIAL_EXAMPLES = "stamped, promo, cosmos, prerelease, shadowless..."


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

    if ctx.get("removed") and item.get("itemId") in ctx["removed"]:
        return skip(ctx, "you removed this listing", detail)
    loc = (detail or {}).get("itemLocation") or item.get("itemLocation") or {}
    if S.get("us_only", True) and loc.get("country") and loc.get("country") != "US":
        return skip(ctx, "item is not located in the USA", detail)
    a = aspect_map(detail)
    extra = " ".join(a.get(k, "") for k in ("features", "finish", "rarity", "set", "card type", "type"))
    if detail and detail.get("description"):
        extra += " " + " ".join(DESC_STRONG.findall(clean_desc(detail.get("description"), 3000)))
    ftext = title + " " + extra
    nums = parse_number(title)
    if not nums:
        return skip(ctx, "no card number like 58/102 in the title", detail)
    hits, used, why, any_cards = None, None, None, False
    for n, t in nums[:2]:
        cards = lookup_cards(n, t, ctx)
        if not cards:
            continue
        any_cards = True
        cands, w = pick_candidates(title, cards, extra)
        if cands:
            hits, used = cands, (n, t)
            break
        why = why or w
    if not hits:
        if not any_cards:
            return skip(ctx, "card number found but not in the TCGplayer price list (card may be too cheap, or a different set)", detail)
        if why == "special":
            return skip(ctx, f"title mentions a special version ({SPECIAL_EXAMPLES}) that is not in the price list", detail)
        if why == "only_special":
            return skip(ctx, f"only special versions ({SPECIAL_EXAMPLES}) of this card exist and the title does not say which", detail)
        return skip(ctx, "card number found but the name in the title does not match that card", detail)

    fin = finish_of(ftext)
    if fin in ("holo", "nonholo"):
        want_keys = HOLO_KEYS if fin == "holo" else PLAIN_KEYS
        same = [c for c in hits if any(k in want_keys for k in c["prices"])]
        if not same:
            return skip(ctx, "listing says holo or non-holo but the matching card only exists as the other kind", detail)
        hits = same
    picks = []
    for c in hits:
        k, clear = pick_variant(ftext, c)
        if k:
            m, note = reference_price(c["prices"][k])
            if m:
                picks.append((m, c, k, clear, note))
    if not picks:
        return skip(ctx, "matched card has no market price", detail)
    picks.sort(key=lambda x: x[0])
    market, card, variant, vclear, ref_note = picks[0]
    amb_text = None
    if len(picks) > 1 and picks[-1][0] > picks[0][0] * 1.15:
        amb_text = (f"Several cards share this number (market ${picks[0][0]:.2f} to ${picks[-1][0]:.2f}), "
                    "so the lowest price was used")
    pid = card.get("id")
    if pid in ctx.get("badprice", ()):
        return skip(ctx, "you reported this card's market price as wrong", detail)
    if pid in ctx.get("distrust", ()) and not title_tags(ftext):
        return skip(ctx, "you reported this card's version gets mixed up and the title does not say which", detail)
    if not (S["min_market"] <= market <= S["max_market"]):
        return skip(ctx, f"card market value outside ${S['min_market']}-${S['max_market']}", detail)

    cd = read_condition(item, detail)
    cond = cd["key"]
    seller_name = ((item.get("seller") or {}).get("username")) or ((detail or {}).get("seller") or {}).get("username")
    if seller_name and seller_name in ctx.get("bad_sellers", ()):
        return skip(ctx, "you marked this seller as fake or sketchy", detail)
    cond_trained = None
    if seller_name and seller_name in ctx.get("cond_sellers", ()) and cond == "nm":
        cond, cond_trained = "lp", "You reported this seller's condition as worse than listed, so Near Mint was treated as Lightly Played"
    era = era_of(card.get("year"), card.get("set"))
    mult, mult_note = cond_multiplier(S, ctx, era, cond, market)
    if cond == "nm" and len([k for k, _ in cd["sources"].values() if k == "nm"]) < 2 and cond_trained is None:
        f_ = float(S.get("nm_single_source_factor", 0.9)) - min(0.10, 0.02 * ctx.get("nm_wrong", 0))
        mult, mult_note = round(f_, 3), "Near Mint claimed by only one source, so priced a little under full Near Mint"
    expected = round(market * mult, 2)
    if detail is None and cd["source"] in ("field", "title") and vclear and "samples" in ctx:
        ratio = price / market
        if 0.15 <= ratio <= 3.0:
            ctx["samples"].setdefault(era, {}).setdefault(cond, []).append(round(ratio, 3))
    ship, ship_known = ship_cost(detail or item, S["assumed_shipping"])
    total = price + ship
    gap = expected - total
    if gap <= 0:
        return skip(ctx, "listed at or above its market value", detail)
    price_pct = total / expected * 100
    if price_pct < float(S["min_price_pct"]):
        return skip(ctx, f"priced under {S['min_price_pct']:g}% of market value (almost always a wrong match)", detail)

    confidence, reasons = condition_confidence(cd)
    if cond_trained:
        reasons.append(cond_trained)
        confidence = min(confidence, 50)

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
                              amb_text, vclear, card, bool(detail), ref_note)
    needs_detail = False
    if not title_tags(ftext):
        specials = [c for c in match_cards(title, lookup_cards(used[0], used[1], ctx)) if c["tags"] and c["prices"]]
        if specials:
            sp, risky = [], False
            for c in specials:
                top = max(reference_price(v)[0] for v in c["prices"].values())
                risky = risky or top > 1.3 * market
                sp.append(f"{c['set']} ({', '.join(c['tags'])}) about ${top:.0f}")
            if risky and S.get("skip_unclear_versions"):
                if detail is not None:
                    return skip(ctx, "a pricier special version (prize pack, stamped, deck exclusive...) exists and the full listing does not say which one this is", detail)
                needs_detail = True
            warnings.append({"level": "warn", "text": "Special versions of this exact card number exist: " + "; ".join(sp[:3]) + ". Check the photos show the regular one"})
    if era in ("vintage", "classic") and fin is None and variant in HOLO_KEYS and not any(k in PLAIN_KEYS for k in card["prices"]):
        warnings.append({"level": "warn", "text": "Title does not say holo or non-holo. This card is priced as the holo, so check the photos"})

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
        "year": card.get("year"),
        "era": era,
        "tags": card.get("tags") or [],
        "number": f"{used[0]}/{used[1]}",
        "variant": variant,
        "market": round(market, 2),
        "multiplier": mult,
        "mult_note": mult_note,
        "pid": pid,
        "listed": item.get("itemCreationDate"),
        "needs_detail": needs_detail,
        "cond_sources": {k_: list(v_) for k_, v_ in cd["sources"].items()},
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

def pick_card_queries(prices, S, state, n):
    """Exact-card searches ("Radiant Greninja 46/189"): the cards searched longest ago come first."""
    cands, seen_names = [], set()
    for key, recs in prices.items():
        for r in recs:
            if r.get("t"):
                continue
            vals = [(m or d) for m, d in r["p"].values() if (m or d)]
            if not vals or not (S["min_market"] <= max(vals) <= S["max_market"]):
                continue
            k = f"{key}|{r['name']}"
            if k in seen_names:
                continue
            seen_names.add(k)
            cands.append((state.get("cards", {}).get(k, 0), -max(vals), k, r["name"], r.get("n"), key))
    cands.sort()
    qs, keys = [], []
    for _, _, k, name, num, key in cands[:n]:
        qs.append(f"{name} {num}/{key.split('/')[1]} pokemon")
        keys.append(k)
    return qs, keys


def plan_run(S, scheduled, cursor):
    """Decides what this scan covers.

    Manual scans read every search for the configured number of pages. Automatic scans stay inside
    the daily eBay call budget, rotating through the searches when the run rate is fast.
    Returns (queries, pages, detail lookups, re-checks, next cursor).
    """
    lim = S["limits"]
    queries = [q for q in S["queries"] if str(q).strip()][:100]
    full_lookups, full_verify = int(lim["max_item_lookups"]), int(lim["max_verify"])
    if not scheduled or not queries:
        return queries, max(1, int(S["pages_per_search"])), full_lookups, full_verify, cursor
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
    return chosen, 1, lookups, verify, (start + nq) % n


def should_skip(S, prev):
    """Automatic runs wake up every 5 minutes and quit if it is too soon."""
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

    if scheduled and not S.get("auto_scan"):
        log("Automatic scanning is OFF, so this scheduled wake-up does nothing. "
            "Turn it on in the website, or press Scan now to scan by hand.")
        return
    if should_skip(S, prev):
        log(f"Skipping: last scan was recent and the run rate is {S['interval_minutes']} minutes.")
        return

    override = os.environ.get("FLAG_PRICE_PCT_OVERRIDE", "").strip()
    if override:
        try:
            S["flag_price_pct"] = max(5.0, min(100.0, float(override)))
            log(f"Using {S['flag_price_pct']:g}% of market for this scan only.")
        except ValueError:
            override = ""

    pdata = load_json(PRICES_PATH, {})
    if not pdata.get("cards"):
        sys.exit("There is no price list yet. In GitHub open Actions, click 'Update prices' on the left, "
                 "click Run workflow, wait for the green check, then run the scan again.")
    if pdata.get("format") != 4:
        sys.exit("The price list is in an old format. In GitHub open Actions, click 'Update prices', "
                 "click Run workflow, wait for the green check, then run the scan again.")
    pcount = int(pdata.get("count") or sum(len(v) for v in pdata["cards"].values()))
    log(f"Price list: {pcount} cards, downloaded {pdata.get('updated_at')} (TCGplayer data from tcgcsv.com)")

    cid = os.environ.get("EBAY_CLIENT_ID", "").strip()
    secret = os.environ.get("EBAY_CLIENT_SECRET", "").strip()
    if not cid or not secret:
        sys.exit("Missing EBAY_CLIENT_ID / EBAY_CLIENT_SECRET. Add them under Settings > Secrets in GitHub.")
    token = get_token(cid, secret)

    index = load_json(SCANS_PATH, {"next_id": 1, "scans": []})
    scan_id = int(index.get("next_id", 1))
    started = now_iso()
    log(f"=== Scan #{scan_id} ({'automatic' if scheduled else 'manual'}) ===")

    if prev.get("engine") != ENGINE:
        log(f"Old deal list came from an older scanner version ({prev.get('engine')}), so it was thrown away.")
        prev = {"deals": []}
    fb = load_json(FEEDBACK_PATH, {"removed": {}})
    removed = fb.get("removed") or {}
    distrust = {r.get("pid") for r in removed.values() if r.get("reason") in ("version", "wrong_card") and r.get("pid")}
    bad_sellers = {r.get("seller") for r in removed.values() if r.get("reason") == "scam" and r.get("seller")}
    cond_sellers = {r.get("seller") for r in removed.values() if r.get("reason") == "cond_worse" and r.get("seller")}
    nm_wrong = sum(1 for r in removed.values() if r.get("reason") == "cond_worse" and r.get("claimed") == "nm")
    badprice = {r.get("pid") for r in removed.values() if r.get("reason") == "price" and r.get("pid")}
    if removed:
        log(f"Feedback: {len(removed)} removed listings, {len(distrust)} cards flagged for version mix-ups, {len(badprice)} for wrong price, "
            f"{len(bad_sellers)} sellers blocked, {len(cond_sellers)} sellers whose condition is read one step lower, "
            f"{nm_wrong} Near Mint claims reported wrong.")
    prev_deals = {d["id"]: d for d in prev.get("deals", [])
                  if d.get("id") and d["id"] not in removed and d.get("price_pct", 0) >= float(S["min_price_pct"])}
    lim = S["limits"]
    stats = load_json(STATS_PATH, {"samples": {}})
    seen_before = set(load_json(SEEN_PATH, {"ids": []}).get("ids", []))
    state = load_json(STATE_PATH, {"cards": {}})
    ctx = {
        "S": S, "bad": build_bad_regex(S["extra_exclude_words"]),
        "prices": pdata["cards"], "prices_updated": pdata.get("updated_at"),
        "removed": set(removed), "distrust": distrust, "badprice": badprice,
        "bad_sellers": bad_sellers, "cond_sellers": cond_sellers, "nm_wrong": nm_wrong,
        "learned": build_learned(stats), "samples": {},
    }
    calls = {"search": 0, "item": 0}
    flag = float(S["flag_price_pct"])
    per_page = int(lim["results_per_query"])

    queries, pages, max_lookups, max_verify, cursor = plan_run(S, scheduled, int(prev.get("query_cursor", 0) or 0))

    # 1. search. Results come newest first, so a search keeps paging only while its pages still hold
    # listings we have never seen. Manual scans also search exact cards, oldest-searched first.
    seen = {}
    new_ids = set()
    budget = int(S["max_search_calls"]) if not scheduled else 10 ** 9

    def take(items):
        fresh_n = 0
        for it in items:
            iid = it.get("itemId")
            if not iid:
                continue
            seen.setdefault(iid, it)
            n = eid(iid)
            if n is not None and n not in seen_before:
                new_ids.add(iid)
                fresh_n += 1
        return fresh_n

    active = list(queries)
    for page in range(pages):
        keep = []
        for q in active:
            if calls["search"] >= budget:
                break
            items = search_ebay(token, q, per_page, S["max_market"], S["min_listing_price"], page * per_page)
            calls["search"] += 1
            fresh_n = take(items)
            time.sleep(0.25)
            if len(items) >= per_page and fresh_n >= 0.2 * len(items):
                keep.append(q)
        active = keep
    card_qs, searched_keys = [], []
    if not scheduled and int(S.get("card_searches", 0)) > 0:
        card_qs, searched_keys = pick_card_queries(pdata["cards"], S, state, int(S["card_searches"]))
        for q in card_qs:
            if calls["search"] >= budget + int(S["card_searches"]):
                break
            take(search_ebay(token, q, 50, S["max_market"], S["min_listing_price"], 0))
            calls["search"] += 1
            time.sleep(0.25)
        for k in searched_keys:
            state["cards"][k] = scan_id
    log(f"Search: {len(seen)} unique listings ({len(new_ids)} never seen before) from {len(queries)} searches "
        f"(up to {pages} pages each) and {len(card_qs)} exact-card searches; {calls['search']} eBay calls")
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

    # 5. keep what is worth a look
    final = [d for d in pool.values()
             if d["price_pct"] <= flag and d["gap"] >= P["min_gap"] and d["confidence"] >= P["min_confidence"]
             and not (d.get("needs_detail") and not d.get("detailed"))]
    final.sort(key=lambda d: d["score"], reverse=True)
    final = final[: int(P["max_deals"])]
    for d in final:
        old = prev_deals.get(d["id"])
        d["first_scan"] = old.get("first_scan") if old else scan_id

    log("Why listings were skipped (first pass):")
    for k, v in sorted(ctx.get("drops", {}).items(), key=lambda x: -x[1]):
        log(f"  {v:>5}  {k}")

    # 6. save this scan, then the running list
    scan_deals = []
    for d in final:
        if d["id"] in seen:
            dd = dict(d)
            dd["is_new"] = d["first_scan"] == scan_id
            dd["new_listing"] = d["id"] in new_ids
            scan_deals.append(dd)
    scan_deals.sort(key=lambda d: d["score"], reverse=True)
    new_count = sum(1 for d in scan_deals if d["is_new"])
    finished = now_iso()
    summary = {
        "engine": ENGINE,
        "id": scan_id,
        "code": f"#{scan_id}",
        "trigger": "auto" if scheduled else "manual",
        "started": started,
        "finished": finished,
        "flag_price_pct": flag,
        "one_time_percent": bool(override),
        "searches": len(queries),
        "searches_total": len([q for q in S["queries"] if str(q).strip()]),
        "pages": pages,
        "listings_seen": len(seen),
        "matched": len(fresh),
        "deals": len(scan_deals),
        "new": new_count,
        "carried": len(final) - len(scan_deals),
        "ebay_calls": calls["search"] + calls["item"],
        "new_listings": len(new_ids),
        "card_searches": len(card_qs),
        "skipped": ctx.get("drops", {}),
    }
    # remember what was seen, what was searched, and the asking prices by condition
    ids = list(load_json(SEEN_PATH, {"ids": []}).get("ids", []))
    have = set(ids)
    for iid in seen:
        n = eid(iid)
        if n is not None and n not in have:
            ids.append(n)
            have.add(n)
    save_json(SEEN_PATH, {"ids": ids[-MAX_SEEN:]})
    save_json(STATE_PATH, state)
    for era, d in ctx["samples"].items():
        mine = stats.setdefault("samples", {}).setdefault(era, {})
        for c, xs in d.items():
            mine[c] = (mine.get(c, []) + xs)[-300:]
    save_json(STATS_PATH, stats)
    save_json(os.path.join(SCAN_DIR, f"{scan_id}.json"), {"summary": summary, "deals": scan_deals})
    index["scans"] = ([summary] + [s for s in index.get("scans", []) if s.get("id") != scan_id])[:KEEP_SCAN_SUMMARIES]
    index["next_id"] = scan_id + 1
    save_json(SCANS_PATH, index)
    try:
        for fn in os.listdir(SCAN_DIR):
            m = re.fullmatch(r"(\d+)\.json", fn)
            if m and int(m.group(1)) <= scan_id - KEEP_SCAN_FILES:
                os.remove(os.path.join(SCAN_DIR, fn))
    except OSError:
        pass

    df = dict(S["default_filters"])
    df["max_price_pct"] = flag
    out = {
        "engine": ENGINE,
        "updated_at": finished,
        "last_scan_id": scan_id,
        "query_cursor": cursor,
        "settings": {
            "flag_price_pct": flag,
            "auto_scan": bool(S.get("auto_scan")),
            "interval_minutes": S["interval_minutes"],
            "default_filters": df,
            "min_market": S["min_market"],
            "max_market": S["max_market"],
        },
        "run": {"mode": summary["trigger"], "searches": len(queries), "searches_total": summary["searches_total"],
                "pages": pages, "skipped": ctx.get("drops", {}), "price_list_cards": pcount,
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
    log(f"Scan #{scan_id} saved: {len(scan_deals)} deals found in this scan ({new_count} new), "
        f"{len(final)} in the current list. eBay calls: {calls['search'] + calls['item']}.")


if __name__ == "__main__":
    main()
