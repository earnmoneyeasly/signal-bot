# -*- coding: utf-8 -*-
"""
CRYPTO SIGNAL BOT -> TELEGRAM
--------------------------------
- Binance ke top 50 USDT coins scan karta hai
- 4H trend (EMA50/EMA200) + 1H entry (MACD, RSI, Volume, EMA20)
- LONG / SHORT signal Telegram channel par bhejta hai (Entry, SL, TP1, TP2)
- Har signal ka result (TP/SL) khud track karke Telegram par batata hai
  aur win/loss record rakhta hai

Chalane ka tareeqa:
    python bot.py test     -> Telegram par test message
    python bot.py scan     -> ek baar abhi scan karo
    python bot.py once     -> ek round chalao aur band (GitHub Actions ke liye)
    python bot.py          -> bot 24/7 chalao

NOT financial advice. Pehle paper trading karo.
"""

import os
import sys
import time
import json
import csv
import math
from datetime import datetime, timezone

import requests
import pandas as pd

# ============================================================
#  SETTINGS  (sirf ye 2 lines badalni hain)
# ============================================================
BOT_TOKEN = "8954226044:AAFv9RThMlKxQjoUCU84wM6yWwP1EF4Wfac"   # <-- baad mein yahan asli token paste karna (quotes " " ke andar)
CHAT_ID = "@Strexx_Crypto_Signals"   # <-- aap ka channel

# ---- Advanced settings (chaho to chhor do) ----
TOP_N = 50               # kitne top coins (24h volume ke hisaab se)
TREND_TF = "4h"          # trend timeframe
ENTRY_TF = "1h"          # entry timeframe
SL_ATR_MULT = 1.5        # Stop Loss = 1.5 x ATR
TP1_RR = 1.5             # TP1 = 1.5 x risk
TP2_RR = 3.0             # TP2 = 3 x risk
MIN_SCORE = 3            # 4 mein se kam az kam 3 conditions match hon
VOL_MULT = 1.2           # volume average se 1.2x zyada
MIN_ATR_PCT = 0.25       # bohat sust (flat) coins skip
BTC_FILTER = True        # BTC ke khilaf altcoin signal nahi
COOLDOWN_H = 8           # same coin par dobara signal 8 ghante baad
MAX_SIGNALS_PER_SCAN = 3 # ek scan mein max signals (sab se strong)
EXPIRE_H = 48            # 48 ghante mein kuch na hua to signal band
LOOP_SECONDS = 300       # bot har 5 minute mein check kare

DATA_FILE = "bot_data.json"
LOG_FILE = "signals_log.csv"

EXCLUDE = {
    "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "USD1", "USDT",
    "EUR", "AEUR", "EURI", "GBP", "TRY", "BRL", "WBTC", "WBETH", "PAXG", "XAUT",
    "BFUSD", "RLUSD", "USDS",
}

BASE_URLS = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
]

# ============================================================
#  HELPERS
# ============================================================


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def send_telegram(text):
    if BOT_TOKEN.startswith("YAHAN"):
        print("\n[DRY RUN - token set nahi hai, sirf screen par dikha raha hun]\n" + text + "\n")
        return True
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text},
            timeout=15,
        )
        if not r.ok:
            log(f"Telegram error: {r.status_code} {r.text}")
            return False
        return True
    except Exception as e:
        log(f"Telegram error: {e}")
        return False


def api_get(path, params=None):
    last_err = None
    for base in BASE_URLS:
        try:
            r = requests.get(base + path, params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
            last_err = f"{base} status {r.status_code}"
        except Exception as e:
            last_err = f"{base} {e}"
    raise RuntimeError(f"Binance API fail: {last_err}")


def get_top_coins():
    data = api_get("/api/v3/ticker/24hr")
    rows = []
    for t in data:
        s = t["symbol"]
        if not s.endswith("USDT"):
            continue
        base = s[:-4]
        if base in EXCLUDE:
            continue
        rows.append((s, float(t["quoteVolume"])))
    rows.sort(key=lambda x: -x[1])
    return [s for s, _ in rows[:TOP_N]]


def get_klines(symbol, interval, limit=300, start=None):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    if start is not None:
        params["startTime"] = int(start)
    raw = api_get("/api/v3/klines", params)
    df = pd.DataFrame(
        raw,
        columns=["open_time", "open", "high", "low", "close", "volume",
                 "close_time", "qv", "n", "tb", "tq", "ig"],
    )
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    df["open_time"] = df["open_time"].astype("int64")
    return df


def fmt_price(p):
    if p <= 0:
        return "0"
    decimals = max(2, min(8, 5 - int(math.floor(math.log10(p)))))
    return f"{p:.{decimals}f}"


# ============================================================
#  INDICATORS
# ============================================================


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0)
    dn = -d.clip(upper=0)
    ru = up.ewm(alpha=1 / n, adjust=False).mean()
    rd = dn.ewm(alpha=1 / n, adjust=False).mean()
    rs = ru / rd.replace(0, float("nan"))
    return (100 - 100 / (1 + rs)).fillna(50)


def atr(df, n=14):
    pc = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def trend_of(df4):
    """4H trend: LONG / SHORT / None"""
    if len(df4) < 210:
        return None
    c = df4["close"]
    e50 = ema(c, 50).iloc[-1]
    e200 = ema(c, 200).iloc[-1]
    price = c.iloc[-1]
    if price > e50 > e200:
        return "LONG"
    if price < e50 < e200:
        return "SHORT"
    return None


# ============================================================
#  SIGNAL LOGIC
# ============================================================


def analyze(symbol, btc_trend):
    # 1) 4H trend
    df4 = get_klines(symbol, TREND_TF, 300).iloc[:-1]  # aakhri (adhoori) candle hata do
    side = trend_of(df4)
    if side is None:
        return None
    if BTC_FILTER and symbol != "BTCUSDT" and btc_trend is not None and btc_trend != side:
        return None

    # 2) 1H entry
    df = get_klines(symbol, ENTRY_TF, 300).iloc[:-1]
    if len(df) < 60:
        return None

    c = df["close"]
    ema20 = ema(c, 20)
    r = rsi(c)
    macd_line = ema(c, 12) - ema(c, 26)
    hist = macd_line - ema(macd_line, 9)
    a = atr(df)
    vol_ratio = df["volume"] / df["volume"].rolling(20).mean()

    price = float(c.iloc[-1])
    a_now = float(a.iloc[-1])
    if a_now / price * 100 < MIN_ATR_PCT:
        return None

    if side == "LONG":
        checks = {
            "MACD": hist.iloc[-1] > 0 and hist.iloc[-1] > hist.iloc[-2],
            "RSI": 45 <= r.iloc[-1] <= 65 and r.iloc[-1] > r.iloc[-2],
            "Volume": vol_ratio.iloc[-1] >= VOL_MULT,
            "EMA20": price > ema20.iloc[-1] and (price - ema20.iloc[-1]) <= 1.5 * a_now,
        }
    else:
        checks = {
            "MACD": hist.iloc[-1] < 0 and hist.iloc[-1] < hist.iloc[-2],
            "RSI": 35 <= r.iloc[-1] <= 55 and r.iloc[-1] < r.iloc[-2],
            "Volume": vol_ratio.iloc[-1] >= VOL_MULT,
            "EMA20": price < ema20.iloc[-1] and (ema20.iloc[-1] - price) <= 1.5 * a_now,
        }

    score = sum(1 for v in checks.values() if bool(v))
    if score < MIN_SCORE:
        return None

    risk = SL_ATR_MULT * a_now
    if side == "LONG":
        sl, tp1, tp2 = price - risk, price + TP1_RR * risk, price + TP2_RR * risk
    else:
        sl, tp1, tp2 = price + risk, price - TP1_RR * risk, price - TP2_RR * risk

    vr = float(vol_ratio.iloc[-1])
    return {
        "symbol": symbol,
        "side": side,
        "entry": price,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "score": score,
        "rank": score + min(vr, 3) / 10,
        "rsi": float(r.iloc[-1]),
        "time": int(time.time() * 1000),
    }


def signal_message(s):
    icon = "🟢" if s["side"] == "LONG" else "🔴"
    name = s["symbol"][:-4] + "/USDT"

    def pct(x):
        return f"{(x - s['entry']) / s['entry'] * 100:+.2f}%"

    return (
        f"{icon} {s['side']} SIGNAL | {name}\n"
        f"Timeframe: 1H (4H trend confirmed)\n\n"
        f"Entry: {fmt_price(s['entry'])}\n"
        f"Stop Loss: {fmt_price(s['sl'])} ({pct(s['sl'])})\n"
        f"TP1: {fmt_price(s['tp1'])} ({pct(s['tp1'])})\n"
        f"TP2: {fmt_price(s['tp2'])} ({pct(s['tp2'])})\n\n"
        f"Risk:Reward = 1:{TP1_RR:g} / 1:{TP2_RR:g}\n"
        f"Strength: {s['score']}/4\n\n"
        f"Tip: TP1 par half profit lo aur SL entry par le aao.\n"
        f"Risk sirf 1-2% per trade, leverage kam rakho.\n"
        f"⚠️ Not financial advice."
    )


# ============================================================
#  DATA SAVE / LOAD
# ============================================================


def load_data():
    default = {
        "open": [],
        "last_sent": {},
        "stats": {"wins": 0, "losses": 0, "expired": 0},
        "last_scan_hour": 0,
    }
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                d = json.load(f)
            for k, v in default.items():
                d.setdefault(k, v)
            return d
        except Exception:
            pass
    return default


def save_data(d):
    with open(DATA_FILE, "w") as f:
        json.dump(d, f)


def log_result(sig, result):
    new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time_utc", "symbol", "side", "entry", "sl", "tp1", "tp2", "score", "result"])
        w.writerow([
            datetime.fromtimestamp(sig["time"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            sig["symbol"], sig["side"], sig["entry"], sig["sl"], sig["tp1"], sig["tp2"],
            sig["score"], result,
        ])


# ============================================================
#  SIGNAL TRACKING (TP / SL check)
# ============================================================


def evaluate(s):
    """15m candles se check: 'open', 'tp1', 'sl', 'tp2', 'be', 'expired', 'tp1_close'"""
    df = get_klines(s["symbol"], "15m", 1000, start=s["time"])
    state = "open"
    long = s["side"] == "LONG"
    for h, l in zip(df["high"], df["low"]):
        if state == "open":
            if long:
                if l <= s["sl"]:
                    return "sl"          # agar dono ek candle mein hon to SL maano (conservative)
                if h >= s["tp2"]:
                    return "tp2"
                if h >= s["tp1"]:
                    state = "tp1"
            else:
                if h >= s["sl"]:
                    return "sl"
                if l <= s["tp2"]:
                    return "tp2"
                if l <= s["tp1"]:
                    state = "tp1"
        else:  # TP1 ho chuka, SL ab entry par
            if long:
                if l <= s["entry"]:
                    return "be"
                if h >= s["tp2"]:
                    return "tp2"
            else:
                if h >= s["entry"]:
                    return "be"
                if l <= s["tp2"]:
                    return "tp2"
    age_h = (time.time() * 1000 - s["time"]) / 3600000
    if age_h > EXPIRE_H:
        return "tp1_close" if state == "tp1" else "expired"
    return state


def stats_line(stats):
    total = stats["wins"] + stats["losses"]
    rate = (stats["wins"] / total * 100) if total else 0
    return f"Record: {stats['wins']}W / {stats['losses']}L  ({rate:.0f}% win rate)"


def check_open_signals(data):
    still_open = []
    for s in data["open"]:
        name = s["symbol"][:-4] + "/USDT"
        try:
            state = evaluate(s)
        except Exception as e:
            log(f"Track error {s['symbol']}: {e}")
            still_open.append(s)
            continue

        if state == "open":
            still_open.append(s)
        elif state == "tp1":
            if not s.get("tp1_notified"):
                s["tp1_notified"] = True
                send_telegram(f"🎯 {name} {s['side']}: TP1 hit ✅\nAb SL entry ({fmt_price(s['entry'])}) par le aao. Risk-free trade.")
            still_open.append(s)
        else:
            if state == "sl":
                data["stats"]["losses"] += 1
                text = f"❌ {name} {s['side']}: Stop Loss hit"
            elif state == "tp2":
                data["stats"]["wins"] += 1
                text = f"🏆 {name} {s['side']}: TP2 hit ✅✅ (full target)"
            elif state == "be":
                data["stats"]["wins"] += 1
                text = f"✅ {name} {s['side']}: TP1 mila, phir entry par band (breakeven)"
            elif state == "tp1_close":
                data["stats"]["wins"] += 1
                text = f"✅ {name} {s['side']}: TP1 hit, trade time-out par band"
            else:  # expired
                data["stats"]["expired"] += 1
                text = f"⏱ {name} {s['side']}: {EXPIRE_H}h mein koi target nahi mila, signal band"
            log_result(s, state)
            send_telegram(text + "\n" + stats_line(data["stats"]))
        time.sleep(0.2)
    data["open"] = still_open


# ============================================================
#  MAIN SCAN
# ============================================================


def scan_for_signals(data):
    coins = get_top_coins()
    log(f"Scan shuru: {len(coins)} coins")

    btc_trend = None
    try:
        btc_trend = trend_of(get_klines("BTCUSDT", TREND_TF, 300).iloc[:-1])
    except Exception as e:
        log(f"BTC trend error: {e}")
    log(f"BTC 4H trend: {btc_trend}")

    now = time.time()
    open_syms = {s["symbol"] for s in data["open"]}
    found = []
    for sym in coins:
        if sym in open_syms:
            continue
        if now - data["last_sent"].get(sym, 0) < COOLDOWN_H * 3600:
            continue
        try:
            sig = analyze(sym, btc_trend)
            if sig:
                found.append(sig)
        except Exception as e:
            log(f"Analyze error {sym}: {e}")
        time.sleep(0.15)

    found.sort(key=lambda x: -x["rank"])
    sent = 0
    for sig in found[:MAX_SIGNALS_PER_SCAN]:
        if send_telegram(signal_message(sig)):
            data["open"].append(sig)
            data["last_sent"][sig["symbol"]] = now
            sent += 1
            save_data(data)
        time.sleep(1)
    log(f"Scan khatam: {len(found)} setups mile, {sent} signals bheje")


def run_once(force=False):
    data = load_data()
    check_open_signals(data)
    hour_key = int(time.time() // 3600)
    if force or data["last_scan_hour"] != hour_key:
        scan_for_signals(data)
        data["last_scan_hour"] = hour_key
    save_data(data)


def main():
    arg = sys.argv[1].lower() if len(sys.argv) > 1 else ""

    if arg == "test":
        ok = send_telegram("✅ Test message: bot theek kaam kar raha hai!")
        log("Test message bhej diya" if ok else "Test fail hua, token/chat id check karo")
        return

    if arg == "scan":
        run_once(force=True)
        return

    if arg == "once":
        run_once()
        return

    log("Bot start ho gaya. Band karne ke liye Ctrl+C dabao.")
    send_telegram("🤖 Signal bot start ho gaya.")
    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log(f"Loop error: {e}")
        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Bot band kar diya.")
