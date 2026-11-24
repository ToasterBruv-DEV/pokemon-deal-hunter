"""Auction Tracker: auctions that end soon and are priced far under market value.

Uses the same card matching, condition reading and price rules as scan.py.
The current bid is what is compared, so the final price is usually higher: treat the percent as
"how cheap it is right now".
"""
import os
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import scan
from scan import log, load_json, save_json

AUCTIONS_PATH = os.path.join(scan.ROOT, "data", "auctions.json")


def search_auctions(token, query, start, end, max_price, offset=0, limit=200):
    params = {
        "q": query,
        "category_ids": scan.CATEGORY_CCG_SINGLES,
        "filter": f"buyingOptions:{{AUCTION}},priceCurrency:USD,itemLocationCountry:US,"
                  f"price:[0..{int(max_price)}],itemEndDate:[{start}..{end}]",
        "sort": "endingSoonest",
        "limit": str(min(int(limit), 200)),
        "offset": str(int(offset)),
        "fieldgroups": "EXTENDED",
    }
    url = "https://api.ebay.com/buy/browse/v1/item_summary/search?" + urllib.parse.urlencode(
        params, quote_via=urllib.parse.quote, safe=":{}[]|,.")
    status, body = scan.http_json(url, {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"})
    if status != 200 or not isinstance(body, dict):
        log(f"  ! eBay auction search failed for '{query}' (HTTP {status}): {str(body)[:300]}")
        scan.SEARCH_ERRORS.append(f"HTTP {status}: {str(body)[:300]}")
        return []
    return body.get("itemSummaries", []) or []


def as_auction(it):
    """Browse results give the current bid in currentBidPrice. Put it where evaluate() reads the price."""
    cb = it.get("currentBidPrice") or it.get("price")
    if cb:
        it["price"] = cb
    return it


def iso_z(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def main():
    S = scan.load_settings()
    scheduled = os.environ.get("EVENT_NAME", "") == "schedule"
    prev = load_json(AUCTIONS_PATH, {"auctions": []})
    interval = max(5, int(S.get("auction_interval_minutes", 30)))
    if scheduled:
        if not S.get("auction_auto"):
            log("Automatic auction scanning is OFF, so this scheduled wake-up does nothing.")
            return
        last = scan.parse_iso(prev.get("updated_at"))
        if last and (datetime.now(timezone.utc) - last).total_seconds() / 60.0 < interval - 2:
            log("Too soon since the last auction scan.")
            return

    pdata = load_json(scan.PRICES_PATH, {})
    if not pdata.get("cards") or pdata.get("format") != 4:
        sys.exit("The price list is missing or old. Run Update prices in GitHub Actions first, then run this again.")
    cid = os.environ.get("EBAY_CLIENT_ID", "").strip()
    secret = os.environ.get("EBAY_CLIENT_SECRET", "").strip()
    if not cid or not secret:
        sys.exit("Missing EBAY_CLIENT_ID / EBAY_CLIENT_SECRET.")
    token = scan.get_token(cid, secret)

    fb = load_json(scan.FEEDBACK_PATH, {"removed": {}})
    removed = fb.get("removed") or {}
    stats = load_json(scan.STATS_PATH, {"samples": {}})
    ctx = {
        "S": S, "bad": scan.build_bad_regex(S["extra_exclude_words"]),
        "prices": pdata["cards"], "prices_updated": pdata.get("updated_at"),
        "removed": set(removed),
        "distrust": {r.get("pid") for r in removed.values() if r.get("reason") in ("version", "wrong_card") and r.get("pid")},
        "badprice": {r.get("pid") for r in removed.values() if r.get("reason") == "price" and r.get("pid")},
        "bad_sellers": {r.get("seller") for r in removed.values() if r.get("reason") == "scam" and r.get("seller")},
        "cond_sellers": {r.get("seller") for r in removed.values() if r.get("reason") == "cond_worse" and r.get("seller")},
        "nm_wrong": sum(1 for r in removed.values() if r.get("reason") == "cond_worse" and r.get("claimed") == "nm"),
        "learned": scan.build_learned(stats),
    }
    flag = float(S["flag_price_pct"])
    pool_pct = max(flag, float(S.get("pool_pct", 90)))
    P = S["pool"]
    window = max(5, int(S.get("auction_window_minutes", 30)))
    now_dt = datetime.now(timezone.utc)
    start, end = iso_z(now_dt), iso_z(now_dt + timedelta(minutes=window))

    queries = list(S["queries"])
    cursor = int(prev.get("query_cursor", 0) or 0)
    if scheduled:
        per_run = max(6, int(2000 / (1440 / interval)))
        nq = min(len(queries), max(4, per_run * 2 // 3))
        picked = [queries[(cursor + i) % len(queries)] for i in range(nq)]
        cursor = (cursor + nq) % len(queries)
        queries, pages, lookups = picked, 1, max(2, per_run - nq)
    else:
        pages, lookups = int(S.get("auction_pages", 3)), int(S.get("auction_lookups", 80))

    log(f"=== Auction scan ({'automatic' if scheduled else 'manual'}): auctions ending in the next {window} minutes ===")
    seen, calls = {}, {"search": 0, "item": 0}
    active = list(queries)
    for page in range(pages):
        keep = []
        for q in active:
            items = search_auctions(token, q, start, end, S["max_market"], page * 200)
            calls["search"] += 1
            for it in items:
                if it.get("itemId"):
                    seen.setdefault(it["itemId"], it)
            time.sleep(0.25)
            if len(items) >= 200:
                keep.append(q)
        active = keep
    log(f"Search: {len(seen)} auctions from {len(queries)} searches, {calls['search']} eBay calls")
    if not seen:
        if scan.SEARCH_ERRORS:
            sys.exit("eBay returned no auctions. First error: " + scan.SEARCH_ERRORS[0])
        log("No auctions ending in this window matched the searches.")

    cutoff = now_dt + timedelta(seconds=45)
    fresh = []
    for iid, it in seen.items():
        end_t = scan.parse_iso(it.get("itemEndDate"))
        if end_t is None or end_t < cutoff:
            continue
        as_auction(it)
        d = scan.evaluate(it, None, ctx)
        if d:
            fresh.append((it, d))
    log(f"First pass: {len(fresh)} auctions matched to a card and priced under market")

    def worth(d):
        if d["condition"] == "unknown":
            pct, gap = d["total"] / d["market"] * 100, d["market"] - d["total"]
        else:
            pct, gap = d["price_pct"], d["gap"]
        return pct <= flag and gap >= P["min_gap"]

    cand = [(it, d) for it, d in fresh if worth(d)]
    cand.sort(key=lambda x: x[0].get("itemEndDate") or "")           # soonest ending first
    final = {}
    looked = set()
    for it, d in cand[:lookups]:
        status, detail = scan.get_item(token, d["id"])
        calls["item"] += 1
        time.sleep(0.25)
        looked.add(d["id"])
        if status == 404:
            continue
        if status != 200 or not isinstance(detail, dict) or scan.is_gone(detail):
            final[d["id"]] = (it, d)
            continue
        merged = dict(it)
        merged.update({k: v for k, v in detail.items() if v is not None})
        as_auction(merged)
        d2 = scan.evaluate(merged, detail, ctx)
        if d2:
            final[d["id"]] = (merged, d2)
    for it, d in fresh:
        if d["id"] not in final and d["id"] not in looked:
            final[d["id"]] = (it, d)

    out_deals = []
    for it, d in final.values():
        if d["price_pct"] > pool_pct or d["gap"] < P["min_gap"] or d["confidence"] < P["min_confidence"]:
            continue
        if d.get("needs_detail") and not d.get("detailed"):
            continue
        d["end_time"] = it.get("itemEndDate")
        d["bids"] = int(it.get("bidCount") or 0)
        try:
            d["current_bid"] = float((it.get("currentBidPrice") or it.get("price") or {}).get("value"))
        except (TypeError, ValueError):
            d["current_bid"] = d.get("price")
        d["checked_at"] = scan.now_iso()
        out_deals.append(d)
    out_deals.sort(key=lambda d: (d["price_pct"], d["end_time"] or ""))

    for k, v in sorted(ctx.get("drops", {}).items(), key=lambda x: -x[1]):
        log(f"  {v:>5}  {k}")
    save_json(AUCTIONS_PATH, {
        "engine": scan.ENGINE,
        "updated_at": scan.now_iso(),
        "trigger": "auto" if scheduled else "manual",
        "window_minutes": window,
        "flag_price_pct": flag,
        "pool_pct": pool_pct,
        "query_cursor": cursor,
        "stats": {"listings_seen": len(seen), "matched": len(fresh), "searches": len(queries), "ebay_calls": calls["search"] + calls["item"]},
        "skipped": ctx.get("drops", {}),
        "auctions": out_deals,
    })
    log(f"Saved {len(out_deals)} auctions ({sum(1 for d in out_deals if d['price_pct'] <= flag)} at or under {flag:g}% of value).")


if __name__ == "__main__":
    main()
