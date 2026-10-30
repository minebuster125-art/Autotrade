#!/usr/bin/env python3
"""NIFTY weekly options paper trader on live Dhan data. No real orders, ever.

Strategy (experimental, not backtested on option prices): intraday iron fly.
Sell ATM CE + PE, buy CE/PE WING points away, entered once a day after 09:30.
Exit at 15:15, at -STOP_LOSS, or at +TARGET_FRAC of the credit.
Fills: buy at ask, sell at bid from the live chain. Costs charged at close.

Usage:
  python3 papertrader.py [--until 15:30]   # one tick; exit 1 outside market hours (the workflow loops it)
  python3 papertrader.py --selftest
Env: DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN (data API only).
"""
import argparse, json, os, sys, time, urllib.request
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "state.json")
CAPITAL = 100000
LOTS, LOT = 1, 65         # shortcut: margin not modelled; 1 hedged lot fits in 1L, add lots only after paper proves it
WING = 200
STOP_LOSS = 2000          # daily loss limit = 2% of capital
TARGET_FRAC = 0.4
ENTRY_FROM, ENTRY_TO, FLAT_AT = "09:30", "14:30", "15:15"


def api(path, body):
    req = urllib.request.Request("https://api.dhan.co/v2/" + path, json.dumps(body).encode(), {
        "access-token": os.environ["DHAN_ACCESS_TOKEN"], "client-id": os.environ["DHAN_CLIENT_ID"],
        "Content-Type": "application/json", "Accept": "application/json"})
    d = json.load(urllib.request.urlopen(req, timeout=20))
    if d.get("status") != "success":
        raise RuntimeError(f"Dhan {path}: {d.get('data') or d}")
    return d["data"]


def fetch_chain(today):
    # skip an expiry that is today: expiry-day gamma is the fastest way to blow up a small account
    exp = next(e for e in api("optionchain/expirylist", {"UnderlyingScrip": 13, "UnderlyingSeg": "IDX_I"}) if e > today)
    time.sleep(3)  # Dhan allows one option-chain call per 3 s
    d = api("optionchain", {"UnderlyingScrip": 13, "UnderlyingSeg": "IDX_I", "Expiry": exp})
    return exp, d["last_price"], {float(k): v for k, v in d["oc"].items()}


def quote(chain, strike, typ):
    q = chain[strike][typ.lower()]
    ltp = q["last_price"]
    return ltp, q.get("top_bid_price") or ltp, q.get("top_ask_price") or ltp


def costs(legs):
    """Approximate NSE option charges for one round trip of all legs."""
    sell_turnover = sum(l["qty"] * (l["entry"] if l["side"] < 0 else l["exit"]) for l in legs)
    turnover = sum(l["qty"] * (l["entry"] + l["exit"]) for l in legs)
    return 20 * 2 * len(legs) * 1.18 + 0.001 * sell_turnover + 0.0005 * turnover  # brokerage+GST, STT, exch/SEBI/stamp


def mtm(pos):
    return sum(l["side"] * (l["ltp"] - l["entry"]) * l["qty"] for l in pos["legs"])


def log(s, now, msg):
    s["log"] = (s["log"] + [f"{now:%d-%b %H:%M:%S} {msg}"])[-60:]


def close(s, now, chain, reason):
    pos = s["position"]
    for l in pos["legs"]:
        ltp, bid, ask = quote(chain, l["strike"], l["type"])
        l["exit"] = ask if l["side"] < 0 else bid  # buy back shorts at ask, sell longs at bid
    gross = sum(l["side"] * (l["exit"] - l["entry"]) * l["qty"] for l in pos["legs"])
    c = costs(pos["legs"])
    s["realized"] += gross - c
    s["day_realized"] += gross - c
    s["trades"].append(dict(date=now.strftime("%Y-%m-%d"), opened=pos["opened"], closed=now.strftime("%H:%M"),
                            expiry=pos["expiry"], legs=pos["legs"], gross=round(gross, 2), costs=round(c, 2),
                            pnl=round(gross - c, 2), reason=reason))
    s["position"] = None
    log(s, now, f"EXIT ({reason}) net {gross - c:+,.0f} (gross {gross:+,.0f}, costs {c:,.0f})")


def open_fly(s, now, exp, spot, chain):
    atm = min(chain, key=lambda k: abs(k - spot))
    legs = []
    for strike, typ, side in ((atm, "CE", -1), (atm, "PE", -1), (atm + WING, "CE", 1), (atm - WING, "PE", 1)):
        if strike not in chain:
            return log(s, now, f"skip: strike {strike} missing")
        ltp, bid, ask = quote(chain, strike, typ)
        legs.append(dict(strike=strike, type=typ, side=side, qty=LOTS * LOT,
                         entry=bid if side < 0 else ask, ltp=ltp))
    credit = sum(-l["side"] * l["entry"] for l in legs)
    if credit <= 0:
        return log(s, now, "skip: no credit")
    s["position"] = dict(opened=now.strftime("%H:%M"), expiry=exp, atm=atm, credit=round(credit, 2),
                         target=round(TARGET_FRAC * credit * LOTS * LOT, 2), legs=legs)
    s["traded_today"] = True
    log(s, now, f"ENTRY iron fly {exp} ATM {atm:.0f} wings ±{WING}, credit {credit:.2f} pts x {LOTS * LOT}")


def tick(s, now, fetch=fetch_chain):
    today = now.strftime("%Y-%m-%d")
    if s.get("day") != today:
        if s.get("day"):
            s["days"].append(dict(date=s["day"], pnl=round(s["day_realized"], 2)))
        s.update(day=today, day_realized=0.0, traded_today=False, curve=[])
    hm = now.strftime("%H:%M")
    exp, spot, chain = fetch(today)
    s.update(spot=spot, expiry=exp, error=None)
    pos = s["position"]
    if pos:
        for l in pos["legs"]:
            l["ltp"] = quote(chain, l["strike"], l["type"])[0]
        m = mtm(pos)
        pos["mtm"] = round(m, 2)
        if hm >= FLAT_AT: close(s, now, chain, "time")
        elif s["day_realized"] + m <= -STOP_LOSS: close(s, now, chain, "stop")
        elif m >= pos["target"]: close(s, now, chain, "target")
    elif not s["traded_today"] and ENTRY_FROM <= hm < ENTRY_TO:
        open_fly(s, now, exp, spot, chain)
    open_mtm = s["position"]["mtm"] if s["position"] and "mtm" in s["position"] else 0
    s["curve"].append([hm, round(s["day_realized"] + open_mtm, 2), spot])
    s["updated"] = now.isoformat(timespec="seconds")


def fresh():
    return dict(capital=CAPITAL, realized=0.0, position=None, trades=[], days=[], curve=[], log=[],
                rules=dict(lots=LOTS, lot=LOT, wing=WING, stop=STOP_LOSS, target_frac=TARGET_FRAC,
                           entry=f"{ENTRY_FROM}-{ENTRY_TO}", flat=FLAT_AT))


def load():
    try:
        return json.load(open(STATE))
    except FileNotFoundError:
        return fresh()


def save(s):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE + ".tmp", "w") as f:
        json.dump(s, f, indent=1)
    os.replace(STATE + ".tmp", STATE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--until", default="15:30")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest: return selftest()
    now = datetime.now(IST)
    # shortcut: NSE holidays not listed, a holiday tick trades on stale quotes; add the holiday list if that shows up
    if now.weekday() >= 5 or not ("09:15" <= now.strftime("%H:%M") < a.until):
        print("market closed")
        return 1
    s = load()
    try:
        tick(s, now)
    except Exception as e:  # a bad tick (network, expired token) must not lose state
        s["error"] = str(e); s["updated"] = now.isoformat(timespec="seconds")
        log(s, now, f"ERROR {e}")
    save(s)
    print(s["log"][-1] if s["log"] else "tick", flush=True)


def selftest():
    t0 = datetime(2026, 11, 2, 9, 35, tzinfo=IST)
    def chain_at(px):  # px: {(strike,typ): ltp}, spread 1 point
        def f(today):
            ch = {}
            for (k, typ), p in px.items():
                ch.setdefault(k, {})[typ.lower()] = dict(last_price=p, top_bid_price=p - 0.5, top_ask_price=p + 0.5)
            return "2026-11-03", 23000.0, ch
        return f
    s = fresh()
    tick(s, t0, chain_at({(23000.0, "CE"): 100, (23000.0, "PE"): 100, (23200.0, "CE"): 20, (22800.0, "PE"): 20}))
    p = s["position"]
    assert p and abs(p["credit"] - (99.5 * 2 - 20.5 * 2)) < 1e-9, p  # sells at bid, buys at ask
    # premiums decay: shorts 40, longs 8 -> mtm 94 pts x 65 beats target (40% of 158 pts x 65)
    tick(s, t0 + timedelta(minutes=30), chain_at({(23000.0, "CE"): 40, (23000.0, "PE"): 40, (23200.0, "CE"): 8, (22800.0, "PE"): 8}))
    tr = s["trades"][-1]
    assert s["position"] is None and tr["reason"] == "target", s["log"]
    gross = 65 * ((99.5 - 40.5) * 2 + (7.5 - 20.5) * 2)
    assert abs(tr["gross"] - gross) < 0.01 and abs(s["realized"] - (gross - tr["costs"])) < 0.01, tr
    # no second entry the same day
    tick(s, t0 + timedelta(minutes=60), chain_at({(23000.0, "CE"): 100, (23000.0, "PE"): 100, (23200.0, "CE"): 20, (22800.0, "PE"): 20}))
    assert s["position"] is None
    # next day: stop at -2000
    t1 = t0 + timedelta(days=1)
    tick(s, t1, chain_at({(23000.0, "CE"): 100, (23000.0, "PE"): 100, (23200.0, "CE"): 20, (22800.0, "PE"): 20}))
    tick(s, t1 + timedelta(minutes=5), chain_at({(23000.0, "CE"): 200, (23000.0, "PE"): 90, (23200.0, "CE"): 60, (22800.0, "PE"): 15}))
    assert s["trades"][-1]["reason"] == "stop" and s["days"][-1]["date"] == "2026-11-02", s["log"]
    print("selftest ok")


if __name__ == "__main__":
    sys.exit(main())
