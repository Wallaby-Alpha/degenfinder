import os

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


#!/usr/bin/env python3
"""
Solana Pump-Potential Screener
================================
Goal: find coins where current buy pressure relative to pool liquidity
is likely to produce a 50%+ move — NOT 10x moonshots, just "enough
buying against a thin enough pool that price gets pushed hard."

This is NOT trying to catch coordinated alpha-group wallets before
they buy (that's a different problem — see v2/v3). This is a pure
market-microstructure screen on public DexScreener data:

    - Buy/sell imbalance (more buyers than sellers, recently)
    - Volume acceleration (is trading picking up RIGHT NOW vs baseline)
    - Volume-to-liquidity ratio (thin pools move more per $ of buying)
    - Ignition momentum (price already ticking up, but not exhausted)

It intentionally does NOT require you to catch the whole move — the
scoring favors "starting to move" over "already found," since you
said you're fine being a bit late.

DISCLAIMER: This is a heuristic screening tool, not financial advice,
not a prediction engine, and not risk-free. Low-cap Solana tokens can
and do go to zero via rug pulls, LP removal, or just dying. Position
size accordingly. Nothing here should be treated as a guarantee of
any price movement.

CLI usage:
    python3 solana_pump_screener.py                      # scan + rank candidates
    python3 solana_pump_screener.py --track               # check open watchlist for exit signals
    python3 solana_pump_screener.py --min-score 65 --max-tokens 100
"""

try:
    import requests
except ImportError:
    import subprocess
    subprocess.run(["pip", "install", "requests", "-q"])
    import requests

import json
import time
import argparse
import math
from datetime import datetime, timezone

DEXSCREENER_BASE = "https://api.dexscreener.com"
WATCHLIST_FILE = "/tmp/solana_pump_watchlist.json"

# In Colab you can't pass --track on the command line — flip this to True,
# rerun the cell, then flip it back. CLI users can just use --track instead.
TRACK_MODE = False

MAX_RETRIES = 3
RETRY_DELAY = 1.0

SKIP_TOKENS = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
}


# ── helpers ───────────────────────────────────────────────────────────────
def fetch_json(url, retries=MAX_RETRIES):
    for attempt in range(retries):
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            if attempt < retries - 1:
                time.sleep(RETRY_DELAY * (2 ** attempt))
            else:
                return None


def safe_float(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    try:
        requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "disable_web_page_preview": True,
                "parse_mode": "HTML",
            },
            timeout=15,
        )
    except Exception as e:
        print(f"Telegram error: {e}")
        
# ── discovery (same sources/approach as before) ─────────────────────────────
def discover_candidates(max_mc=500_000, min_liq=8_000, max_liq=300_000):
    print("Discovering candidate tokens from multiple sources...")
    all_tokens = {}

    print("  Source 1: DexScreener latest profiles...")
    profiles = fetch_json(f"{DEXSCREENER_BASE}/token-profiles/latest/v1")
    if profiles:
        for p in profiles:
            if p.get("chainId") == "solana":
                addr = p.get("tokenAddress", "")
                if addr and addr not in SKIP_TOKENS:
                    all_tokens[addr] = {"source": "dexscreener_profiles"}

    print("  Source 2: DexScreener search (new/trending queries)...")
    for query in ["pump.fun", "solana new", "sol trending"]:
        data = fetch_json(f"{DEXSCREENER_BASE}/latest/dex/search?q={query}")
        if data and "pairs" in data:
            for pair in data["pairs"]:
                if pair.get("chainId") != "solana":
                    continue
                addr = pair.get("baseToken", {}).get("address", "")
                if addr and addr not in SKIP_TOKENS:
                    all_tokens[addr] = {"source": "dexscreener_search"}
        time.sleep(0.3)

    print("  Source 3: DexScreener trending...")
    trending = fetch_json(f"{DEXSCREENER_BASE}/token-profiles/top/v1")
    if trending:
        for p in trending:
            if p.get("chainId") == "solana":
                addr = p.get("tokenAddress", "")
                if addr and addr not in SKIP_TOKENS:
                    all_tokens[addr] = {"source": "dexscreener_trending"}

    print("  Source 4: Pump.fun recent launches...")
    pumpfun = fetch_json("https://frontend-api-v3.pump.fun/coins/latest")
    if pumpfun and isinstance(pumpfun, list):
        for coin in pumpfun[:50]:
            addr = coin.get("mint", "")
            if addr and addr not in SKIP_TOKENS:
                all_tokens[addr] = {"source": "pumpfun"}

    print(f"\n  Total unique candidates: {len(all_tokens)}")
    print("  Pulling full market data (liquidity, volume, txns, price change)...")

    enriched = []
    addresses = list(all_tokens.keys())

    for i in range(0, len(addresses), 30):
        batch = addresses[i:i + 30]
        batch_str = ",".join(batch)
        pairs_data = fetch_json(f"{DEXSCREENER_BASE}/latest/dex/tokens/{batch_str}")
        if not pairs_data or "pairs" not in pairs_data:
            time.sleep(0.4)
            continue

        by_token = {}
        for pair in pairs_data["pairs"]:
            if pair.get("chainId") != "solana":
                continue
            addr = pair.get("baseToken", {}).get("address", "")
            if not addr:
                continue
            # keep the deepest-liquidity pair per token
            cur = by_token.get(addr)
            if cur is None or pair.get("liquidity", {}).get("usd", 0) > cur.get("liquidity", {}).get("usd", 0):
                by_token[addr] = pair

        for addr, pair in by_token.items():
            mc  = pair.get("marketCap") or pair.get("fdv") or 0
            liq = pair.get("liquidity", {}).get("usd", 0)
            age_ms = time.time() * 1000 - (pair.get("pairCreatedAt") or 0)
            age_hours = age_ms / (1000 * 60 * 60)

            if mc > max_mc or liq < min_liq or liq > max_liq or age_hours < 0.25:
                continue

            enriched.append({
                "address": addr,
                "name": pair.get("baseToken", {}).get("name", ""),
                "symbol": pair.get("baseToken", {}).get("symbol", ""),
                "market_cap": mc,
                "liquidity": liq,
                "price_usd": safe_float(pair.get("priceUsd")),
                "age_hours": age_hours,
                "source": all_tokens.get(addr, {}).get("source", "unknown"),
                "dex": pair.get("dexId", ""),
                "volume": {
                    "m5": pair.get("volume", {}).get("m5", 0),
                    "h1": pair.get("volume", {}).get("h1", 0),
                    "h6": pair.get("volume", {}).get("h6", 0),
                    "h24": pair.get("volume", {}).get("h24", 0),
                },
                "price_change": {
                    "m5": pair.get("priceChange", {}).get("m5", 0) or 0,
                    "h1": pair.get("priceChange", {}).get("h1", 0) or 0,
                    "h6": pair.get("priceChange", {}).get("h6", 0) or 0,
                    "h24": pair.get("priceChange", {}).get("h24", 0) or 0,
                },
                "txns": {
                    "m5": pair.get("txns", {}).get("m5", {"buys": 0, "sells": 0}),
                    "h1": pair.get("txns", {}).get("h1", {"buys": 0, "sells": 0}),
                    "h6": pair.get("txns", {}).get("h6", {"buys": 0, "sells": 0}),
                    "h24": pair.get("txns", {}).get("h24", {"buys": 0, "sells": 0}),
                },
            })
        time.sleep(0.4)

    print(f"  Final candidates: {len(enriched)} (MC < ${max_mc:,}, ${min_liq:,} < liq < ${max_liq:,})")
    return enriched


# ── scoring ──────────────────────────────────────────────────────────────
def score_candidate(c):
    """
    Returns dict with sub-scores (0-100 each, roughly) and a combined
    0-100 pump-potential score. Higher = more likely to see a sharp move
    from here, based on current buy pressure vs. liquidity depth.
    """
    txns = c["txns"]
    vol = c["volume"]
    pc = c["price_change"]
    liq = max(c["liquidity"], 1)

    # 1. Buy/sell imbalance — weight recent (m5) more than h1
    b5, s5 = txns["m5"].get("buys", 0), txns["m5"].get("sells", 0)
    b1, s1 = txns["h1"].get("buys", 0), txns["h1"].get("sells", 0)
    ratio_m5 = b5 / (s5 + 1)
    ratio_h1 = b1 / (s1 + 1)
    imbalance = 0.6 * ratio_m5 + 0.4 * ratio_h1
    score_imbalance = max(0, min(100, (imbalance - 1) * 40))

    # 2. Volume acceleration — is the trading pace picking up right now?
    pace_m5 = vol["m5"] / 5
    pace_h1 = vol["h1"] / 60
    pace_h6 = vol["h6"] / 360
    accel_short = pace_m5 / max(pace_h1, 0.01)
    accel_med = pace_h1 / max(pace_h6, 0.01)
    accel = 0.5 * accel_short + 0.5 * accel_med
    score_accel = max(0, min(100, (accel - 1) * 50))

    # 3. Volume-to-liquidity ratio — thin pool + real volume = price gets pushed
    vol_liq_h1 = vol["h1"] / liq
    score_volliq = max(0, min(100, math.log1p(vol_liq_h1) * 45.0))

    # 4. Ignition momentum — some move already, but not exhausted.
    h1_pc = pc["h1"]
    if h1_pc <= 0:
        score_momentum = max(0, 20 + h1_pc)
    elif h1_pc <= 20:
        score_momentum = 40 + (h1_pc / 20) * 60
    elif h1_pc <= 60:
        score_momentum = 100 - ((h1_pc - 20) / 40) * 70
    else:
        score_momentum = max(0, 30 - (h1_pc - 60) * 0.5)

    # BUG FIX: Dynamically scale thinness score against your 300k liquidity upper bound
    # Instantly rewards tokens sitting perfectly on the thin, high-impact side of the pool spectrum
    score_thinness = max(0, min(100, 100 * (1.0 - (liq / 300000.0))))

    total = (
        0.32 * score_imbalance +
        0.26 * score_accel +
        0.20 * score_volliq +
        0.14 * score_momentum +
        0.08 * score_thinness
    )

    return {
        "total": round(total, 1),
        "imbalance": round(score_imbalance, 1),
        "accel": round(score_accel, 1),
        "vol_liq": round(score_volliq, 1),
        "momentum": round(score_momentum, 1),
        "thinness": round(score_thinness, 1),
        "ratio_m5": round(ratio_m5, 2),
        "ratio_h1": round(ratio_h1, 2),
        "vol_liq_h1_raw": round(vol_liq_h1, 3),
    }


def suggest_exit_plan(c):
    """
    Exit rules scaled to how thin/volatile the pool is. Thinner liquidity
    -> wider stop (more noise) but shorter time-stop (moves resolve faster
    either direction in thin pools). Returned as plain rules, not orders.
    """
    liq = c["liquidity"]
    if liq < 20_000:
        stop_loss_pct = 25
        time_stop_hours = 3
    elif liq < 75_000:
        stop_loss_pct = 20
        time_stop_hours = 5
    else:
        stop_loss_pct = 15
        time_stop_hours = 8

    return {
        "take_profit_ladder": ["+25% sell 1/3", "+50% sell 1/3", "trail remainder w/ 15% trailing stop"],
        "stop_loss_pct": stop_loss_pct,
        "liquidity_kill_pct": 30,   # exit immediately if pool liquidity drops this much from entry
        "buy_sell_reversal": "exit if m5 buy/sell ratio drops below 0.8 for two consecutive checks",
        "time_stop_hours": time_stop_hours,
    }


# ── watchlist / tracking ────────────────────────────────────────────────────
def load_watchlist():
    try:
        with open(WATCHLIST_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_watchlist(wl):
    with open(WATCHLIST_FILE, "w") as f:
        json.dump(wl, f, indent=2)


def track_positions():
    wl = load_watchlist()
    if not wl:
        print("Watchlist is empty. Run a scan first (without --track) to populate it.")
        return

    print(f"Checking {len(wl)} watched tokens for exit signals...\n")
    addresses = list(wl.keys())
    for i in range(0, len(addresses), 30):
        batch = addresses[i:i + 30]
        batch_str = ",".join(batch)
        pairs_data = fetch_json(f"{DEXSCREENER_BASE}/latest/dex/tokens/{batch_str}")
        if not pairs_data or "pairs" not in pairs_data:
            continue

        latest = {}
        for pair in pairs_data["pairs"]:
            addr = pair.get("baseToken", {}).get("address", "")
            if addr in wl:
                cur = latest.get(addr)
                if cur is None or pair.get("liquidity", {}).get("usd", 0) > cur.get("liquidity", {}).get("usd", 0):
                    latest[addr] = pair

        for addr, pair in latest.items():
            entry = wl[addr]
            now_price = safe_float(pair.get("priceUsd"))
            now_liq = pair.get("liquidity", {}).get("usd", 0)
            entry_price = entry["entry_price"]
            entry_liq = entry["entry_liquidity"]
            plan = entry["exit_plan"]

            pct_move = ((now_price - entry_price) / entry_price * 100) if entry_price else 0
            liq_drop_pct = ((entry_liq - now_liq) / entry_liq * 100) if entry_liq else 0

            entry_time = datetime.fromisoformat(entry["entry_time"])
            # BUG FIX: Guard against zero fractions if track runs instantly after script initialization
            time_delta = (datetime.now(timezone.utc) - entry_time).total_seconds()
            hours_held = max(0.001, time_delta / 3600.0)

            txns_m5 = pair.get("txns", {}).get("m5", {"buys": 0, "sells": 0})
            ratio_m5_now = txns_m5.get("buys", 0) / (txns_m5.get("sells", 0) + 1)

            signals = []
            if pct_move >= 50:
                signals.append("🟢 TAKE PROFIT: +50% target hit")
            elif pct_move >= 25:
                signals.append("🟡 partial target: +25% hit — consider trimming")
            if pct_move <= -plan["stop_loss_pct"]:
                signals.append(f"🔴 STOP LOSS: down {pct_move:.1f}% (limit -{plan['stop_loss_pct']}%)")
            if liq_drop_pct >= plan["liquidity_kill_pct"]:
                signals.append(f"🔴 LIQUIDITY PULLED: liquidity down {liq_drop_pct:.0f}% since entry — possible rug, exit now")
            if ratio_m5_now < 0.8:
                signals.append(f"¼ BUY PRESSURE REVERSED: m5 buy/sell ratio now {ratio_m5_now:.2f}")
            if hours_held >= plan["time_stop_hours"] and pct_move < 10:
                signals.append(f"⏱ TIME STOP: held {hours_held:.1f}h with no real move — thesis likely invalid")

                        symbol = entry.get("symbol", addr[:8])

            print(f"{symbol:12} {pct_move:+7.1f}%  liq {liq_drop_pct:+.0f}% vs entry  held {hours_held:.1f}h")

            for s in signals:
                print(f"    {s}")

            if not signals:
                print("    ⚪ HOLD — no exit signal yet")

            print()

            if signals:
                msg = f"⚠️ {symbol}\n\n"

                for signal in signals:
                    msg += signal + "\n"

                msg += f"\nCurrent P/L: {pct_move:+.1f}%"
                msg += f"\nhttps://dexscreener.com/solana/{addr}"

                send_telegram(msg)

        time.sleep(0.4)


    
# ── main ─────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Solana Pump-Potential Screener")
    parser.add_argument("--max-mc", type=int, default=500_000)
    parser.add_argument("--min-liq", type=int, default=8_000)
    parser.add_argument("--max-liq", type=int, default=300_000,
                         help="Cap liquidity so a realistic buy size could still move price 50%.")
    parser.add_argument("--min-score", type=float, default=55.0)
    parser.add_argument("--max-tokens", type=int, default=200,
                         help="Max candidates to fetch+score per run.")
    parser.add_argument("--top", type=int, default=15, help="How many top-scored tokens to print/save.")
    parser.add_argument("--track", action="store_true", help="Check existing watchlist for exit signals instead of scanning.")

    # argparse exits on unknown args in normal CLI; in Colab sys.argv contains
    # Jupyter's own "-f kernel.json" flag, so fall back to defaults there.
    try:
        args = parser.parse_args()
    except SystemExit:
        args = parser.parse_args([])

    if args.track or TRACK_MODE:
        track_positions()
        return

    print("=" * 60)
    print("  SOLANA PUMP-POTENTIAL SCREENER")
    print("=" * 60)
    print(f"  MC < ${args.max_mc:,} | ${args.min_liq:,} < Liq < ${args.max_liq:,} | min score {args.min_score}")
    print()

    candidates = discover_candidates(max_mc=args.max_mc, min_liq=args.min_liq, max_liq=args.max_liq)
    if not candidates:
        print("No candidates found. Try relaxing filters.")
        return
    candidates = candidates[:args.max_tokens]

    for c in candidates:
        c["score"] = score_candidate(c)

    ranked = sorted(candidates, key=lambda c: -c["score"]["total"])
    flagged = [c for c in ranked if c["score"]["total"] >= args.min_score][:args.top]

    print(f"\n{'='*90}")
    print(f"  TOP {len(flagged)} PUMP-POTENTIAL CANDIDATES (score >= {args.min_score})")
    print(f"{'='*90}\n")

    watchlist = load_watchlist()

    for rank, c in enumerate(flagged, 1):
        s = c["score"]
        plan = suggest_exit_plan(c)
        print(f"🟢 #{rank}  {c['symbol']} — {c['name'][:35]}")
        print(f"    Address: {c['address']}")
        print(f"    Score: {s['total']}  (imbalance {s['imbalance']} | accel {s['accel']} | vol/liq {s['vol_liq']} | momentum {s['momentum']} | thinness {s['thinness']})")
        print(f"    MC: ${c['market_cap']:,.0f} | Liq: ${c['liquidity']:,.0f} | Price: ${c['price_usd']:.8f}")
        print(f"    Price change: m5 {c['price_change']['m5']:+.1f}% | h1 {c['price_change']['h1']:+.1f}% | h6 {c['price_change']['h6']:+.1f}%")
        print(f"    Buy/sell ratio: m5 {s['ratio_m5']} | h1 {s['ratio_h1']}   Vol h1/liq: {s['vol_liq_h1_raw']}")
        print(f"    ENTRY: current price ${c['price_usd']:.8f}")
        print(f"    EXIT PLAN:")
        print(f"      Take profit: {', '.join(plan['take_profit_ladder'])}")
        print(f"      Stop loss:   -{plan['stop_loss_pct']}%")
        print(f"      Liquidity kill switch: exit if liquidity drops {plan['liquidity_kill_pct']}%+ from entry")
        print(f"      Reversal:    {plan['buy_sell_reversal']}")
        print(f"      Time stop:   {plan['time_stop_hours']}h with no move -> exit")
        print(f"    https://dexscreener.com/solana/{c['address']}")
        print()

telegram_msg = f"""
🚀 <b>Pump Candidate Found</b>

<b>{c['symbol']}</b>
Score: <b>{s['total']}</b>

MC: ${c['market_cap']:,.0f}
Liquidity: ${c['liquidity']:,.0f}

Buy/Sell m5: {s['ratio_m5']}
Buy/Sell h1: {s['ratio_h1']}

Dex:
https://dexscreener.com/solana/{c['address']}
"""

        send_telegram(telegram_msg)

       

        watchlist[c["address"]] = {
            "symbol": c["symbol"],
            "entry_price": c["price_usd"],
            "entry_liquidity": c["liquidity"],
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "entry_score": s["total"],
            "exit_plan": plan,
        }

    save_watchlist(watchlist)
    print(f"Watchlist saved to {WATCHLIST_FILE} ({len(watchlist)} tokens tracked).")
    print("Run with --track later to check these against exit rules.")


if __name__ == "__main__":
    main()
