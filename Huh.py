# Wickless candle bot - AUDCHF 5m - sends Telegram alerts. Runs on GitHub Actions every 5 min.
import os, json, urllib.request, urllib.parse, urllib.error, datetime as dt
from zoneinfo import ZoneInfo
import yfinance as yf, pandas as pd

TICKER, RR, PIP, MIN_STOP = "AUDCHF=X", 2.0, 0.0001, 3
# remove any spaces / line breaks that sneak in when copying the codes on a phone
TOKEN = "".join(os.environ.get("TG_TOKEN", "").split())
CHAT = "".join(os.environ.get("TG_CHAT", "").split())
STATE = "state/state.json"

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
    return (f"{x['side']} LIMIT AUDCHF\nEntry: {x['entry']:.5f}\nStop loss: {x['sl']:.5f}\n"
            f"Take profit: {x['tp']:.5f}\n(stop = {x['pips']:.1f} pips)")

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
    # new pending orders
    for key, x in pend.items():
        if key not in st["pending"]:
            msgs.append(f"🔔 NEW ORDER ({local})\n" + order_text(x))
            st["pending"][key] = {k: float(v) if k != "side" else v for k, v in x.items()}
    # pending orders that are no longer valid
    for key in list(st["pending"]):
        if key not in pend:
            filled = p is not None and f"{p['side']} {p['entry']:.5f}" == key
            if filled:
                msgs.append(f"✅ FILLED ({local}): {key}\nYour order should now be open in MT5. Cancel any other pending orders.")
            elif not first:
                msgs.append(f"❌ CANCEL ORDER ({local}): {key}\nDelete this pending order in MT5.")
            del st["pending"][key]
    if p is not None and p["i"] >= RECENT and p["time"] not in st["trades"]:
        st["trades"].append(p["time"])
        key = f"{p['side']} {p['entry']:.5f}"
        if not any(key in m for m in msgs) and not first:
            msgs.append(f"⚡ FAST TRADE ({local}): price hit a level before an alert could go out.\n"
                        f"{p['side']} {p['entry']:.5f} | SL {p['sl']:.5f} | TP {p['tp']:.5f}\nSkip it unless price is still near the entry.")
    # results of trades the bot took
    for t in done:
        if t["time"] not in st["results"]:
            st["results"].append(t["time"])
            if t["time"] in st["trades"] or (t["x"] >= RECENT and not first):
                icon = "🟢" if t["result"] == "WIN" else "🔴"
                msgs.append(f"{icon} {t['result']} ({local}): {t['side']} from {t['entry']:.5f}\n"
                            f"{'+' + str(RR) if t['result'] == 'WIN' else '-1'}R")
    if first:
        msgs.insert(0, f"🤖 Bot started ({local}). Watching AUDCHF 5m.\nPending orders right now: {len(pend)}")
    st["trades"], st["results"] = st["trades"][-300:], st["results"][-300:]
    for m in msgs: send(m)
    os.makedirs("state", exist_ok=True)
    json.dump(st, open(STATE, "w"))

if __name__ == "__main__":
    main()
