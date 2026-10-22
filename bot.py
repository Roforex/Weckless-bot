# Wickless candle bot - AUDCHF 5m - Telegram alerts + automatic trading on an OANDA DEMO account.
# Runs on GitHub Actions every 5 min. If the OANDA secrets are missing it only sends alerts.
import os, json, urllib.request, urllib.parse, urllib.error, datetime as dt
from zoneinfo import ZoneInfo
import yfinance as yf, pandas as pd

TICKER, RR, PIP, MIN_STOP = "AUDCHF=X", 2.0, 0.0001, 3
# remove any spaces / line breaks that sneak in when copying the codes on a phone
TOKEN = "".join(os.environ.get("TG_TOKEN", "").split())
CHAT = "".join(os.environ.get("TG_CHAT", "").split())
STATE = "state/state.json"
# OANDA demo (practice) account - leave secrets empty for alerts only
OANDA_TOKEN = "".join(os.environ.get("OANDA_TOKEN", "").split())
OANDA_ACC = "".join(os.environ.get("OANDA_ACCOUNT", "").split())
OANDA_URL = "https://api-fxpractice.oanda.com/v3"   # practice = demo money only
RISK_CHF = 570         # money lost if the stop is hit: 570 CHF = about C$980 (1% of a 98k account)
ORDER_HOURS = 9        # unfilled orders expire by themselves after this many hours (safety net)
MAX_WAITING = 3        # up to 3 limit orders can wait at once, but only 1 trade can be open

def oanda(method, path, body=None):
    req = urllib.request.Request(OANDA_URL + path, method=method,
                                 data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {OANDA_TOKEN}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return {"error": e.code, "detail": e.read().decode(errors="ignore")[:300]}
    except Exception as e:
        return {"error": "network", "detail": str(e)[:300]}

def place_order(x):
    units = int(round(RISK_CHF / (x["pips"] * PIP)))      # e.g. 5-pip stop -> 60,000 units (0.6 lot)
    if x["side"] == "SELL": units = -units
    expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=ORDER_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {"order": {"type": "LIMIT", "instrument": "AUD_CHF", "units": str(units),
                      "price": f"{x['entry']:.5f}", "timeInForce": "GTD", "gtdTime": expiry,
                      "positionFill": "DEFAULT",
                      "stopLossOnFill": {"price": f"{x['sl']:.5f}"},
                      "takeProfitOnFill": {"price": f"{x['tp']:.5f}"}}}
    r = oanda("POST", f"/accounts/{OANDA_ACC}/orders", body)
    oid = (r.get("orderCreateTransaction") or {}).get("id")
    filled_now = "orderFillTransaction" in r
    return oid, units, filled_now, r

def oanda_status():
    """(open AUD_CHF trades, ids of waiting AUD_CHF limit orders) - or None if OANDA can't be reached."""
    t = oanda("GET", f"/accounts/{OANDA_ACC}/openTrades")
    o = oanda("GET", f"/accounts/{OANDA_ACC}/pendingOrders")
    if "error" in t or "error" in o: return None
    trades = [x for x in t.get("trades", []) if x.get("instrument") == "AUD_CHF"]
    waiting = {x["id"] for x in o.get("orders", []) if x.get("instrument") == "AUD_CHF" and x.get("type") == "LIMIT"}
    return trades, waiting

def cancel_order(oid):
    return oanda("PUT", f"/accounts/{OANDA_ACC}/orders/{oid}/cancel")

def send(msg):
    print("SEND:", msg)
    if not TOKEN: return
    data = urllib.parse.urlencode({"chat_id": CHAT, "text": msg}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data, timeout=20)
    except urllib.error.HTTPError as e:
        why = e.read().decode(errors="ignore")
        print("\n*** TELEGRAM REFUSED THE MESSAGE ***")
        if e.code == 404 or e.code == 401: print("-> TG_TOKEN is wrong. Copy the token from BotFather again.")
        elif "chat not found" in why: print("-> TG_CHAT is wrong, or you never pressed Start in your bot's chat.")
        elif e.code == 403: print("-> Open your bot in Telegram and press Start (or unblock it).")
        print("Telegram said:", e.code, why)
        raise SystemExit(1)

def get_data():
    d = yf.download(TICKER, interval="5m", period="5d", progress=False)
    if isinstance(d.columns, pd.MultiIndex): d.columns = d.columns.get_level_values(0)
    return d.dropna().iloc[:-1]   # drop the candle still forming

def analyse(d):
    H, L, O = d.High.values, d.Low.values, d.Open.values
    e = d.Close.ewm(span=50).mean(); s = e.diff(5); z = d.High - d.Low
    SL = ((d.Close < d.Open) & (d.High - d.Open <= .05*z) & (d.Close < e) & (s < 0)).values
    BL = ((d.Close > d.Open) & (d.Open - d.Low <= .05*z) & (d.Close > e) & (s > 0)).values
    lv, done, p = [], [], None
    for i in range(len(d)):
        h, l = H[i], L[i]
        if p and i > p["i"]:
            sh = h >= p["sl"] if p["side"] == "SELL" else l <= p["sl"]
            th = l <= p["tp"] if p["side"] == "SELL" else h >= p["tp"]
            if sh or th: p["result"] = "LOSS" if sh else "WIN"; p["x"] = i; done.append(p); p = None
        k = []
        for sd, pr, b in lv:
            if not (h >= pr if sd == "SELL" else l <= pr):
                if i - b < 100: k.append((sd, pr, b))
                continue
            if p is None and i - b >= 3:
                st = H[max(0, i-10):i].max() if sd == "SELL" else L[max(0, i-10):i].min()
                r = abs(st - pr)
                if (st > pr if sd == "SELL" else st < pr) and r / PIP >= MIN_STOP:
                    tp = pr - RR*r if sd == "SELL" else pr + RR*r
                    p = dict(side=sd, entry=pr, sl=st, tp=tp, i=i, time=str(d.index[i]))
                    if (sd == "SELL" and h >= st) or (sd == "BUY" and l <= st):
                        p["result"] = "LOSS"; p["x"] = i; done.append(p); p = None
        lv = k
        if SL[i]: lv.append(("SELL", O[i], i))
        if BL[i]: lv.append(("BUY", O[i], i))
    pend, n = {}, len(d)
    if p is None:
        for sd, pr, b in lv:
            if n - b < 3: continue
            st = H[n-10:n].max() if sd == "SELL" else L[n-10:n].min()
            r = abs(st - pr)
            if (st > pr if sd == "SELL" else st < pr) and r / PIP >= MIN_STOP:
                tp = pr - RR*r if sd == "SELL" else pr + RR*r
                pend[f"{sd} {pr:.5f}"] = dict(side=sd, entry=pr, sl=st, tp=tp, pips=r / PIP)
    return p, done, pend, n

def order_text(x):
    return f"{x['side']} LIMIT AUDCHF\nVolume: {RISK_CHF / (x['pips'] * PIP) / 100000:.2f}\nPrice: {x['entry']:.5f}\nStop loss: {x['sl']:.5f}\nTake profit: {x['tp']:.5f}"

def main():
    try: st = json.load(open(STATE))
    except Exception: st = None
    first = st is None
    st = st or {"pending": {}, "trades": [], "results": []}
    d = get_data()
    if len(d) < 60: print("not enough data"); return
    p, done, pend, n = analyse(d)
    RECENT = n - 12   # only care about things that happened in the last hour
    local = dt.datetime.now(ZoneInfo("America/St_Johns")).strftime("%a %H:%M")
    msgs = []
    auto = bool(OANDA_TOKEN and OANDA_ACC)
    if auto:
        status = oanda_status()
        trades, waiting = status if status else ([], set())
        mine = lambda: [(k, e) for k, e in st["pending"].items() if e.get("oid") and not e.get("filled")]
        # 1) did any of our waiting orders fill on OANDA?
        if status:
            for k, e in mine():
                if e["oid"] not in waiting:
                    e["filled"] = True
                    msgs.append(f"✅ FILLED on OANDA ({local}): {k}\nTrade is open with its stop loss and take profit.")
        # 2) a trade is open -> cancel the other waiting orders (only 1 trade at a time)
        if status and trades:
            for k, e in mine():
                if e["oid"] in waiting:
                    cancel_order(e.pop("oid")); e["skipped"] = True
                    msgs.append(f"❌ ORDER CANCELLED on OANDA ({local}): {k}\n(another trade is already open)")
            ids = sorted(x["id"] for x in trades)
            if len(trades) >= 2 and st.get("warn2") != ids:
                st["warn2"] = ids
                msgs.append(f"⚠️ {len(trades)} AUDCHF trades are open on OANDA at the same time ({local}).\n"
                            "Price hit two levels within 5 minutes. Both have stop loss + take profit; "
                            "you can close one in the OANDA app if you want.")
        # 3) levels that are no longer valid
        for k in [k for k in st["pending"] if k not in pend]:
            e = st["pending"].pop(k)
            if e.get("filled"):
                if p is not None and f"{p['side']} {p['entry']:.5f}" == k:
                    st.setdefault("live", []).append(p["time"])
            elif e.get("oid"):
                r = cancel_order(e["oid"])
                if r.get("error") == 404:          # it filled in the last few minutes
                    if p is not None and f"{p['side']} {p['entry']:.5f}" == k:
                        st.setdefault("live", []).append(p["time"])
                    msgs.append(f"✅ FILLED on OANDA ({local}): {k}\nTrade is open with its stop loss and take profit.")
                elif "error" in r:
                    msgs.append(f"⚠️ Could NOT cancel order {k} on OANDA ({local}).\n"
                                f"Please delete it in the OANDA app.\nOANDA said: {r.get('error')} {r.get('detail')}")
                else:
                    msgs.append(f"❌ ORDER CANCELLED on OANDA ({local}): {k}")
        # 4) place new valid levels (max 3 waiting, none while a trade is open)
        for k, x in pend.items():
            e = st["pending"].get(k)
            if e is not None and e.get("oid"):
                continue                                   # already on OANDA
            if e is None:
                e = st["pending"][k] = {kk: float(v) if kk != "side" else v for kk, v in x.items()}
            if not status or trades or len(mine()) >= MAX_WAITING:
                e["skipped"] = True                        # try again next run (silent)
                continue
            oid, units, filled_now, r = place_order(x)
            if oid:
                e["oid"] = oid; e.pop("skipped", None)
                msgs.append(f"🤖 ORDER PLACED on OANDA ({local})" + (" - after retry" if e.pop("retry", None) else "") +
                            "\n" + order_text(x))
                if filled_now:
                    e["filled"] = True; trades = [1]
                    msgs.append(f"✅ FILLED on OANDA ({local}): {k} (straight away)")
            else:
                if not e.get("retry"):
                    msgs.append(f"⚠️ OANDA did NOT accept the order ({local}) - will retry every 5 min\n" +
                                order_text(x) + f"\nReason: {r.get('detail') or r}")
                e["retry"] = True
    else:
        for k, x in pend.items():
            if k not in st["pending"]:
                st["pending"][k] = {kk: float(v) if kk != "side" else v for kk, v in x.items()}
                msgs.append(f"🔔 NEW ORDER ({local})\n" + order_text(x))
        for k in [k for k in st["pending"] if k not in pend]:
            st["pending"].pop(k)
            if p is not None and f"{p['side']} {p['entry']:.5f}" == k:
                msgs.append(f"✅ FILLED ({local}): {k}\nYour order should now be open in MT5. Cancel any other pending orders.")
            elif not first:
                msgs.append(f"❌ CANCEL ORDER ({local}): {k}\nDelete this pending order in MT5.")
    if p is not None and p["i"] >= RECENT and p["time"] not in st["trades"]:
        st["trades"].append(p["time"])
        key = f"{p['side']} {p['entry']:.5f}"
        if not any(key in m for m in msgs) and not first and not auto:
            msgs.append(f"⚡ FAST TRADE ({local}): price hit a level before an alert could go out.\n"
                        f"{p['side']} {p['entry']:.5f} | SL {p['sl']:.5f} | TP {p['tp']:.5f}\nSkip it unless price is still near the entry.")
    # results of trades the bot took
    for t in done:
        if t["time"] not in st["results"]:
            st["results"].append(t["time"])
            if t["time"] in st["trades"] or (t["x"] >= RECENT and not first):
                icon = "🟢" if t["result"] == "WIN" else "🔴"
                live = t["time"] in st.get("live", [])
                tag = "on OANDA" if live else ("PAPER only, not on OANDA" if auto else "")
                msgs.append(f"{icon} {t['result']} {tag} ({local}): {t['side']} from {t['entry']:.5f}\n"
                            f"{'+' + str(RR) if t['result'] == 'WIN' else '-1'}R" +
                            (" (bot's count - check OANDA for the exact amount)" if live else ""))
    if first:
        mode = "AUTO-TRADING on OANDA demo" if auto else "alerts only"
        msgs.insert(0, f"🤖 Bot started ({local}). Watching AUDCHF 5m - {mode}.\nPending orders right now: {len(pend)}")
    if auto and not st.get("oanda_ok"):
        r = oanda("GET", f"/accounts/{OANDA_ACC}/summary")
        if "account" in r:
            a = r["account"]; st["oanda_ok"] = True
            msgs.append(f"✅ Connected to OANDA demo {OANDA_ACC}\nBalance: {float(a['balance']):,.2f} {a['currency']}")
        else:
            msgs.append(f"⚠️ Could not connect to OANDA.\nCheck OANDA_TOKEN and OANDA_ACCOUNT.\nOANDA said: {r.get('error')} {r.get('detail')}")
    st["trades"], st["results"] = st["trades"][-300:], st["results"][-300:]
    st["live"] = st.get("live", [])[-300:]
    for m in msgs: send(m)
    os.makedirs("state", exist_ok=True)
    json.dump(st, open(STATE, "w"))

if __name__ == "__main__":
    main()


