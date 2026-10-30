#!/usr/bin/env python3
"""Intraday strategy backtester on Yahoo chart JSON (5-minute bars).

Usage:
  python3 backtest.py BARS.json[.gz] [--capital 100000] [--risk 0.01]
  python3 backtest.py --selftest

Every strategy: at most one trade a day, stop and target fixed at entry,
flat at the last bar of the session. Stop is checked before target inside a
bar (pessimistic). Costs are charged on both sides.
Sizing: risk `risk * capital` per trade, notional capped at 5x capital
(Indian intraday MIS leverage), so results are in % of capital and work
for INR or USD alike.
"""
import argparse, gzip, json, sys
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

COST = 0.0005  # per side: brokerage + STT + slippage, conservative for NSE intraday
LEVERAGE = 5


def load_sessions(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt") as f:
        r = json.load(f)["chart"]["result"][0]
    off = timedelta(seconds=r["meta"].get("gmtoffset", 0))
    q = r["indicators"]["quote"][0]
    days = OrderedDict()
    for i, ts in enumerate(r["timestamp"]):
        if None in (q["open"][i], q["high"][i], q["low"][i], q["close"][i]):
            continue
        t = datetime.fromtimestamp(ts, timezone.utc) + off
        days.setdefault(t.date(), []).append(
            dict(t=t, o=q["open"][i], h=q["high"][i], l=q["low"][i], c=q["close"][i], v=q["volume"][i] or 0))
    return [b for b in days.values() if len(b) >= 30]  # skip half/partial days


def vwap(bars):
    pv = vol = 0
    out = []
    for b in bars:
        tp = (b["h"] + b["l"] + b["c"]) / 3
        pv += tp * b["v"]; vol += b["v"]
        out.append(pv / vol if vol else tp)
    return out


def simulate(bars, i, side, stop, target):
    """Enter at close of bar i; walk forward. Returns gross return per unit of risk (R) and pct move."""
    entry = bars[i]["c"]
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    for b in bars[i + 1:]:
        if side > 0:
            if b["l"] <= stop: exit_ = min(stop, b["o"]); break
            if b["h"] >= target: exit_ = max(target, b["o"]); break
        else:
            if b["h"] >= stop: exit_ = max(stop, b["o"]); break
            if b["l"] <= target: exit_ = min(target, b["o"]); break
    else:
        exit_ = bars[-1]["c"]
    return dict(entry=entry, exit=exit_, side=side, risk=risk, t=bars[i]["t"])


# --- strategies: each returns (i, side, stop, target) or None for one session ---

def orb(bars, prev, p):
    n = p["or_bars"]
    hi = max(b["h"] for b in bars[:n]); lo = min(b["l"] for b in bars[:n]); mid = (hi + lo) / 2
    for i in range(n, len(bars) - 6):
        c = bars[i]["c"]
        if c > hi: return i, 1, mid, c + p["rr"] * (c - mid)
        if p["short"] and c < lo: return i, -1, mid, c - p["rr"] * (mid - c)
    return None


def vwap_pullback(bars, prev, p):
    vw = vwap(bars)
    for i in range(12, len(bars) - 6):  # after first hour
        b, a = bars[i], bars[i - 1]
        trend_up = all(x["c"] > v for x, v in zip(bars[i - 12:i - 2], vw[i - 12:i - 2]))
        if trend_up and a["l"] <= vw[i - 1] and b["c"] > vw[i] and b["c"] > a["h"]:
            stop = min(a["l"], b["l"]) - 0.001 * b["c"]
            return i, 1, stop, b["c"] + p["rr"] * (b["c"] - stop)
    return None


def gap_fade(bars, prev, p):
    if prev is None: return None
    pc = prev[-1]["c"]; o = bars[0]["o"]; gap = (o - pc) / pc
    if abs(gap) < p["gap"]: return None
    i = 2  # wait 15 minutes
    hi = max(b["h"] for b in bars[:3]); lo = min(b["l"] for b in bars[:3]); c = bars[i]["c"]
    if gap > 0 and c < bars[0]["o"]: return i, -1, hi, c - p["rr"] * (hi - c)
    if gap < 0 and c > bars[0]["o"]: return i, 1, lo, c + p["rr"] * (c - lo)
    return None


def compression_breakout(bars, prev, p):
    """New style: trade the opening-range break only when the range is tight
    relative to yesterday's range (energy stored), in the direction VWAP and
    yesterday's close agree on."""
    if prev is None: return None
    n = p["or_bars"]
    hi = max(b["h"] for b in bars[:n]); lo = min(b["l"] for b in bars[:n])
    prev_rng = max(b["h"] for b in prev) - min(b["l"] for b in prev)
    if (hi - lo) > p["squeeze"] * prev_rng: return None
    vw = vwap(bars); pc = prev[-1]["c"]
    for i in range(n, len(bars) - 6):
        c = bars[i]["c"]
        if c > hi and c > vw[i] and c > pc: return i, 1, lo, c + p["rr"] * (c - lo)
        if c < lo and c < vw[i] and c < pc: return i, -1, hi, c - p["rr"] * (hi - c)
    return None


STRATEGIES = {
    "orb": (orb, [dict(or_bars=n, rr=rr, short=s) for n in (3, 6) for rr in (1, 2) for s in (False, True)]),
    "vwap_pullback": (vwap_pullback, [dict(rr=rr) for rr in (1, 1.5, 2)]),
    "gap_fade": (gap_fade, [dict(gap=g, rr=rr) for g in (0.01, 0.02) for rr in (1, 2)]),
    "compression": (compression_breakout, [dict(or_bars=n, squeeze=s, rr=rr) for n in (3, 6) for s in (0.35, 0.5) for rr in (1.5, 2)]),
}


def run(sessions, fn, p, capital, risk_frac):
    trades = []
    for k, bars in enumerate(sessions):
        sig = fn(bars, sessions[k - 1] if k else None, p)
        if not sig: continue
        tr = simulate(bars, *sig)
        if not tr: continue
        qty = min(risk_frac * capital / tr["risk"], LEVERAGE * capital / tr["entry"])
        gross = tr["side"] * (tr["exit"] - tr["entry"]) * qty
        cost = COST * qty * (tr["entry"] + tr["exit"])
        tr["pnl"] = gross - cost
        trades.append(tr)
    return trades


def stats(trades, capital, days):
    pnl = [t["pnl"] for t in trades]
    eq = peak = dd = 0
    for x in pnl:
        eq += x; peak = max(peak, eq); dd = max(dd, peak - eq)
    wins = [x for x in pnl if x > 0]; losses = [x for x in pnl if x <= 0]
    return dict(trades=len(pnl), win=len(wins) / len(pnl) if pnl else 0, total=sum(pnl),
                per_day=sum(pnl) / days if days else 0,
                pf=sum(wins) / -sum(losses) if losses and sum(losses) else float("inf"),
                maxdd=dd, ret=sum(pnl) / capital)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bars", nargs="?")
    ap.add_argument("--capital", type=float, default=100000)
    ap.add_argument("--risk", type=float, default=0.01)
    ap.add_argument("--train", type=float, default=0.66, help="fraction of sessions used to pick params")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest: return selftest()
    sessions = load_sessions(a.bars)
    cut = int(len(sessions) * a.train)
    train, test = sessions, sessions[cut:]
    train = sessions[:cut]
    print(f"{len(sessions)} sessions {sessions[0][0]['t'].date()}..{sessions[-1][0]['t'].date()}; "
          f"train {len(train)}, test {len(test)}; capital {a.capital:,.0f}, risk/trade {a.risk:.1%}, cost {COST:.2%}/side\n")
    print(f"{'strategy':14} {'best params (train)':40} | {'TRAIN pnl':>10} {'pf':>5} | {'TEST trades':>11} {'win':>5} {'pnl':>9} {'/day':>7} {'pf':>5} {'maxDD':>8}")
    for name, (fn, grid) in STRATEGIES.items():
        scored = [(stats(run(train, fn, p, a.capital, a.risk), a.capital, len(train)), p) for p in grid]
        s_tr, best = max(scored, key=lambda x: (x[0]["total"], x[0]["trades"]))
        s = stats(run(test, fn, best, a.capital, a.risk), a.capital, len(test))
        print(f"{name:14} {json.dumps(best):40} | {s_tr['total']:10,.0f} {s_tr['pf']:5.2f} | "
              f"{s['trades']:11} {s['win']:5.0%} {s['total']:9,.0f} {s['per_day']:7,.0f} {s['pf']:5.2f} {s['maxdd']:8,.0f}")
    print(f"\ntarget asked for: {0.10 * a.capital:,.0f}/day (10% of capital every day)")


def selftest():
    # synthetic day: flat open range 100-101, then a clean rally -> ORB long must hit its 1R target
    t0 = datetime(2026, 1, 5, 9, 30)
    mk = lambda k, o, h, l, c: dict(t=t0 + timedelta(minutes=5 * k), o=o, h=h, l=l, c=c, v=1000)
    bars = [mk(k, 100.5, 101, 100, 100.5) for k in range(3)] + [mk(3, 100.5, 101.6, 100.5, 101.5)]
    bars += [mk(k, 101.5 + 0.5 * (k - 3), 102 + 0.5 * (k - 3), 101.4 + 0.5 * (k - 3), 102 + 0.5 * (k - 3)) for k in range(4, 40)]
    sig = orb(bars, None, dict(or_bars=3, rr=1, short=False))
    assert sig[:3] == (3, 1, 100.5), sig
    tr = simulate(bars, *sig)
    assert tr["exit"] == 102.5, tr  # entry 101.5 + 1R (1.0)
    t = run([bars], orb, dict(or_bars=3, rr=1, short=False), 100000, 0.01)[0]
    assert abs(t["pnl"] - (1000 - COST * 1000 * (101.5 + 102.5))) < 1e-6, t  # 1000 shares = 1% risk, under 5x cap
    print("selftest ok")


if __name__ == "__main__":
    sys.exit(main())
