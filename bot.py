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
import random
import traceback
from collections import Counter
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ============================================================
#  SETTINGS  (sirf ye 2 lines badalni hain)
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN") or "YAHAN_APNA_BOT_TOKEN_LIKHO"   # PC/VPS par yahan token likho. GitHub par KABHI mat likho (Secrets use hota hai)
CHAT_ID = os.getenv("CHAT_ID") or "@Strexx_Crypto_Signals"   # aap ka channel

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
BTC_FILTER = False       # true karne se sirf BTC ke trend ki taraf ke signals aatay hain
COOLDOWN_H = 8           # same coin par dobara signal 8 ghante baad
MAX_SIGNALS_PER_SCAN = 3 # ek scan mein max signals (sab se strong)
EXPIRE_H = 48            # 48 ghante mein kuch na hua to signal band
LOOP_SECONDS = 300       # bot har 5 minute mein check kare

CONTENT_ENABLED = True         # extra posts (analysis/news/tips) on/off
CONTENT_INTERVAL_H = 2         # har kitne ghante baad ek extra post

DATA_FILE = "bot_data.json"
LOG_FILE = "signals_log.csv"
CHART_FILE = "chart.png"

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


def send_telegram(text, parse_mode="HTML", reply_to_message_id=None):
    """Kamyabi par Telegram message_id return karta hai, warna None."""
    if BOT_TOKEN.startswith("YAHAN"):
        print("\n[DRY RUN - token set nahi hai, sirf screen par dikha raha hun]\n" + text + "\n")
        return True
    try:
        payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": parse_mode}
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
            payload["allow_sending_without_reply"] = True
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data=payload,
            timeout=15,
        )
        if not r.ok:
            log(f"Telegram error: {r.status_code} {r.text}")
            return None
        return r.json().get("result", {}).get("message_id")
    except Exception as e:
        log(f"Telegram error: {e}")
        return None


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
    for c in ["open", "high", "low", "close", "volume", "tb"]:
        df[c] = df[c].astype(float)
    df["delta"] = 2 * df["tb"] - df["volume"]   # taker buy - taker sell (CVD/Delta proxy)
    df["open_time"] = df["open_time"].astype("int64")
    return df


def fmt_price(p):
    if p <= 0:
        return "0"
    decimals = max(2, min(8, 5 - int(math.floor(math.log10(p)))))
    return f"{p:.{decimals}f}"


def send_telegram_photo(path, caption, parse_mode="HTML", reply_to_message_id=None):
    """Kamyabi par Telegram message_id return karta hai, warna None."""
    if BOT_TOKEN.startswith("YAHAN"):
        print(f"\n[DRY RUN - photo] {caption}\n")
        return True
    try:
        payload = {"chat_id": CHAT_ID, "caption": caption, "parse_mode": parse_mode}
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
            payload["allow_sending_without_reply"] = True
        with open(path, "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                data=payload,
                files={"photo": f},
                timeout=30,
            )
        if not r.ok:
            log(f"Telegram photo error: {r.status_code} {r.text}")
            return None
        return r.json().get("result", {}).get("message_id")
    except Exception as e:
        log(f"Telegram photo error: {e}")
        return None


def make_chart(symbol, title):
    """1H candles ka simple price+EMA chart banata hai, PNG file ka path return karta hai."""
    df = get_klines(symbol, "1h", 100).iloc[:-1]
    c = df["close"]
    e20 = ema(c, 20)
    e50 = ema(c, 50)
    x = range(len(c))

    plt.style.use("dark_background")
    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=130)
    ax.plot(x, c, color="#f0f0f0", linewidth=1.6, label="Price")
    ax.plot(x, e20, color="#2dd4bf", linewidth=1.1, label="EMA20")
    ax.plot(x, e50, color="#f59e0b", linewidth=1.1, label="EMA50")
    ax.set_title(title, color="white", fontsize=13, loc="left")
    ax.legend(loc="upper left", frameon=False, labelcolor="white", fontsize=8)
    ax.tick_params(colors="#888888", labelsize=8)
    ax.set_xticks([])
    for spine in ax.spines.values():
        spine.set_color("#333333")
    fig.tight_layout()
    fig.savefig(CHART_FILE, facecolor="#0d0d0d")
    plt.close(fig)
    return CHART_FILE


def _darken(hex_color, factor=0.45):
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    return (r / 255 * factor, g / 255 * factor, b / 255 * factor)


def _hex_rgb(hex_color):
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    return (r / 255, g / 255, b / 255)


def _badge(ax, kind, cx=0.5, cy=0.84):
    """Bara check/cross/dash badge banata hai (win=green, loss=red, neutral=grey)."""
    from matplotlib.patches import Ellipse
    colors = {"win": "#22c55e", "loss": "#ef4444", "neutral": "#94a3b8"}
    face = colors.get(kind, colors["neutral"])
    ew, eh = 0.16, 0.16 * (8 / 4.5)
    ax.add_patch(Ellipse((cx, cy), ew, eh, facecolor=face, edgecolor="white", linewidth=2.5, zorder=5))
    if kind == "win":
        ax.plot([cx - 0.045, cx - 0.01, cx + 0.055], [cy - 0.01, cy - 0.065, cy + 0.075],
                 color="white", linewidth=5, zorder=6, solid_capstyle="round")
    elif kind == "loss":
        d = 0.05
        ax.plot([cx - d, cx + d], [cy - d * 1.78, cy + d * 1.78], color="white", linewidth=5, zorder=6, solid_capstyle="round")
        ax.plot([cx - d, cx + d], [cy + d * 1.78, cy - d * 1.78], color="white", linewidth=5, zorder=6, solid_capstyle="round")
    else:
        ax.plot([cx - 0.055, cx + 0.055], [cy, cy], color="white", linewidth=5, zorder=6, solid_capstyle="round")


def _text_card(header, body, bg_color, footer="STREXX CRYPTO SIGNALS", wrap=42, badge=None):
    """Gradient + decorative colorful card image banata hai (tips/news/results ke liye)."""
    import textwrap
    import numpy as np
    from matplotlib.colors import LinearSegmentedColormap
    from matplotlib.patches import Ellipse

    wrapped = "\n".join(textwrap.wrap(body, width=wrap))
    top_c = _hex_rgb(bg_color)
    bottom_c = _darken(bg_color, 0.35)
    cmap = LinearSegmentedColormap.from_list("grad", [bottom_c, top_c])
    grad = np.linspace(0, 1, 256).reshape(256, 1)

    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=130)
    ax.imshow(grad, aspect="auto", cmap=cmap, extent=[0, 1, 0, 1], origin="lower", zorder=0)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    # decorative soft circles for depth
    ax.add_patch(Ellipse((0.9, 0.88), 0.5, 0.9, facecolor="white", alpha=0.05, zorder=1))
    ax.add_patch(Ellipse((0.05, 0.05), 0.4, 0.7, facecolor="white", alpha=0.05, zorder=1))

    header_y = 0.90
    body_y = 0.72
    if badge:
        _badge(ax, badge, cy=0.90)
        header_y = 0.68
        body_y = 0.50

    ax.text(0.5, header_y, header.upper(), ha="center", va="top", fontsize=20,
             fontweight="bold", color="white", transform=ax.transAxes, zorder=6)
    ax.text(0.5, body_y, wrapped, ha="center", va="top", fontsize=14.5,
             color="#f5f5f5", linespacing=1.7, transform=ax.transAxes, zorder=6)
    ax.text(0.5, 0.04, footer, ha="center", va="bottom", fontsize=9.5,
             color="#dddddd", alpha=0.85, transform=ax.transAxes, zorder=6)
    fig.savefig(CHART_FILE, facecolor=bg_color)
    plt.close(fig)
    return CHART_FILE


def make_tip_card(label, color, body_text):
    return _text_card(label, body_text, color)


def make_news_card(headline):
    return _text_card("Crypto News", headline, "#0f766e", wrap=36)


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


def vwap(df):
    tp = (df["high"] + df["low"] + df["close"]) / 3
    return (tp * df["volume"]).cumsum() / df["volume"].cumsum()


def trend_of(df4):
    """4H EMA trend: LONG / SHORT / None"""
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
#  ADVANCED SMC ENGINE
#  Regime -> Multi-timeframe -> Liquidity -> Smart-money price action
#  -> Volume/order-flow -> Quant filters -> Context -> Confirmation
#  -> 0-100 score -> Anti-loss protection -> Adaptive learning
# ============================================================

MIN_SIGNAL_SCORE = 60      # 60 se neeche = NO SIGNAL
FALLBACK_MIN_SCORE = 55    # fallback (simple trend-pullback) setups ke liye alag, chhota threshold
VERY_STRONG_SCORE = 90     # 90+ = Very strong, 80-89 = Strong
SWING_L = 2
SWING_R = 2
VOL_LOOKBACK = 100
VOL_PCTL_MAX = 0.85        # ATR% is percentile se upar = high-volatility (no trade)
ADX_MIN = 15               # trend strength minimum
MIN_NET_RR = 1.15          # fees + spread ke baad expected R:R
MIN_EV = 0.05              # expected value (R units) minimum
MAX_SPREAD_PCT = 0.10
FEE_PCT = 0.05             # per side (round trip = 2x)
FUNDING_EXTREME = 0.0005   # 0.05% per 8h
DOM_HARD = 0.8             # BTC dominance 24h change (pp) jis se upar/neeche alt trade block
MAX_SAME_DIR_OPEN = 3      # ek direction mein max open signals (correlation protection)
MAX_SIGNALS_PER_DAY = 5
DAILY_MAX_LOSSES = 3
LOSS_STREAK_COOLDOWN = 3   # itne consecutive losses ke baad cooldown
LOSS_STREAK_COOLDOWN_H = 6
RAISE_AFTER_LOSSES = 2     # itne consecutive losses ke baad sirf 90+ score
MIN_TRADES_FOR_ADAPT = 12
# High-impact news (FOMC/CPI) ke waqt manually yahan add karo, UTC mein:
# NEWS_BLACKOUT_UTC = [("2026-10-28 17:30", "2026-10-28 20:30")]
NEWS_BLACKOUT_UTC = []
WIN_RESULTS = {"tp2", "tp1_close", "be"}
FUT_BASE = "https://fapi.binance.com"

WEIGHTS = {
    "htf_trend": 8, "btc_context": 5, "market_context": 5, "structure_1h": 4,
    "sweep": 8, "failed_breakout": 2, "displacement": 6, "break": 6,
    "zone": 8, "retest": 7, "pd_ote": 5, "volume_confirm": 8,
    "vwap": 3, "cvd_delta": 4, "derivatives": 6, "momentum": 3,
    "rr_quality": 4, "liquidity_target": 4, "spread_quality": 2, "ev_quality": 2,
}
TAG_LABELS = {
    "htf_trend": "1D+4H Trend", "btc_context": "BTC Aligned", "market_context": "Market Context",
    "structure_1h": "1H Structure", "sweep": "Liquidity Sweep", "failed_breakout": "Failed Breakout",
    "displacement": "Displacement", "break": "BOS/CHOCH", "zone": "FVG/OB Zone",
    "retest": "Retest", "pd_ote": "Discount/OTE", "volume_confirm": "Volume Confirm",
    "vwap": "VWAP", "cvd_delta": "CVD/Delta", "derivatives": "OI/Funding/LS",
    "momentum": "Momentum", "rr_quality": "Good R:R", "liquidity_target": "Liquidity Target",
    "spread_quality": "Tight Spread", "ev_quality": "Positive EV",
}
LEVEL_Q = {"PWL": 1.0, "PWH": 1.0, "PDL": 1.0, "PDH": 1.0, "EQL": 0.9, "EQH": 0.9,
           "1H swing": 0.7, "15m swing": 0.6}

REJECTS = Counter()
_FUT = {"ok": True}


def _rej(reason):
    REJECTS[reason] += 1
    return None


def today_key():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def adx(df, n=14):
    up = df["high"].diff()
    dn = -df["low"].diff()
    plus = ((up > dn) & (up > 0)) * up
    minus = ((dn > up) & (dn > 0)) * dn
    a = atr(df, n)
    pdi = 100 * plus.ewm(alpha=1 / n, adjust=False).mean() / a
    mdi = 100 * minus.ewm(alpha=1 / n, adjust=False).mean() / a
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, float("nan"))
    return dx.ewm(alpha=1 / n, adjust=False).mean().fillna(0), pdi.fillna(0), mdi.fillna(0)


def mirror_df(df):
    """SHORT setups ko LONG logic se check karne ke liye price ko ulta (negative) kar deta hai."""
    m = df.copy()
    m["open"] = -df["open"]
    m["close"] = -df["close"]
    m["high"] = -df["low"]
    m["low"] = -df["high"]
    if "delta" in m:
        m["delta"] = -df["delta"]
    return m


def find_swings(df, left=SWING_L, right=SWING_R):
    highs, lows = df["high"].values, df["low"].values
    n = len(df)
    sh, sl = [], []
    for i in range(left, n - right):
        if highs[i] == highs[i - left:i + right + 1].max():
            sh.append((i, float(highs[i])))
        if lows[i] == lows[i - left:i + right + 1].min():
            sl.append((i, float(lows[i])))
    return sh, sl


def market_regime(df4):
    """Regime: bias (LONG/SHORT/None=sideways) + volatility state."""
    if len(df4) < 210:
        return None, "unknown"
    bias = trend_of(df4)
    a = atr(df4)
    atr_pct = (a / df4["close"]) * 100
    recent = atr_pct.tail(VOL_LOOKBACK)
    if len(recent) < 20:
        return bias, "normal"
    rank = (recent < recent.iloc[-1]).mean()
    return bias, ("high" if rank >= VOL_PCTL_MAX else "normal")


def day_bias(d):
    if len(d) < 60:
        return None
    c = d["close"]
    e20, e50, p = ema(c, 20).iloc[-1], ema(c, 50).iloc[-1], c.iloc[-1]
    if p > e50 and e20 > e50:
        return "LONG"
    if p < e50 and e20 < e50:
        return "SHORT"
    return None


def structure_check(df1h, bias):
    """1H: BOS/higher-structure (continuation) aur CHOCH (trend ulatne ka warning)."""
    sh, sl = find_swings(df1h)
    if len(sh) < 2 or len(sl) < 2:
        return False, False
    price = df1h["close"].iloc[-1]
    last_sh, last_sl = sh[-1][1], sl[-1][1]
    prev_sh, prev_sl = sh[-2][1], sl[-2][1]
    if bias == "LONG":
        return (price > last_sh or (last_sh > prev_sh and last_sl > prev_sl)), price < last_sl
    return (price < last_sl or (last_sh < prev_sh and last_sl < prev_sl)), price > last_sh


def htf_levels(d1):
    """Previous Day / Previous Week high-low."""
    lv = {}
    if len(d1) >= 2:
        lv["PDH"] = float(d1["high"].iloc[-1])
        lv["PDL"] = float(d1["low"].iloc[-1])
    try:
        dt = pd.to_datetime(d1["open_time"], unit="ms")
        wk = dt.dt.to_period("W")
        weeks = list(dict.fromkeys(wk.tolist()))
        if len(weeks) >= 2:
            last_is_sunday = dt.iloc[-1].weekday() == 6
            target = weeks[-1] if last_is_sunday else weeks[-2]
            prev = d1[wk == target]
            if len(prev):
                lv["PWH"] = float(prev["high"].max())
                lv["PWL"] = float(prev["low"].min())
    except Exception:
        pass
    return lv


def equal_levels(df1h, a1h):
    """Equal highs / equal lows (liquidity pools)."""
    sh, sl = find_swings(df1h.tail(200).reset_index(drop=True))
    tol = max(a1h * 0.25, float(df1h["close"].iloc[-1]) * 0.001)

    def cluster(pts):
        vals = [p for _, p in pts]
        out = set()
        for x in range(len(vals)):
            for y in range(x + 1, len(vals)):
                if abs(vals[x] - vals[y]) <= tol:
                    out.add(round((vals[x] + vals[y]) / 2, 8))
        return sorted(out)[-6:]

    return cluster(sh), cluster(sl)


def corr_with_btc(df1h, ctx):
    br = ctx.get("btc_ret")
    if br is None:
        return None
    r = df1h["close"].pct_change().tail(len(br)).values
    m = min(len(r), len(br))
    r, b = r[-m:], br[-m:]
    if m < 30 or np.nanstd(r) == 0 or np.nanstd(b) == 0:
        return None
    r = np.nan_to_num(r)
    b = np.nan_to_num(b)
    return float(np.corrcoef(r, b)[0, 1])


# ---------------- external data (futures / spread) ----------------

def fut_get(path, params):
    if not _FUT["ok"]:
        return None
    try:
        r = requests.get(FUT_BASE + path, params=params, timeout=8)
        if r.status_code == 200:
            return r.json()
        if r.status_code in (403, 418, 429, 451):
            _FUT["ok"] = False   # region-blocked / rate-limited: is scan mein futures data band
        return None
    except Exception:
        _FUT["ok"] = False
        return None


def futures_metrics(symbol):
    out = {"funding": None, "oi_chg": None, "ls": None}
    fr = fut_get("/fapi/v1/premiumIndex", {"symbol": symbol})
    if isinstance(fr, dict) and "lastFundingRate" in fr:
        out["funding"] = float(fr["lastFundingRate"])
    oi = fut_get("/futures/data/openInterestHist", {"symbol": symbol, "period": "15m", "limit": 12})
    if isinstance(oi, list) and len(oi) >= 2:
        first, last = float(oi[0]["sumOpenInterest"]), float(oi[-1]["sumOpenInterest"])
        if first > 0:
            out["oi_chg"] = (last / first - 1) * 100
    ls = fut_get("/futures/data/globalLongShortAccountRatio", {"symbol": symbol, "period": "1h", "limit": 1})
    if isinstance(ls, list) and ls:
        out["ls"] = float(ls[-1]["longShortRatio"])
    return out


def deriv_frac(side, fm):
    parts = []
    if fm["funding"] is not None:
        f = fm["funding"] if side == "LONG" else -fm["funding"]
        parts.append(1.0 if f < 0.0002 else 0.6)
    if fm["oi_chg"] is not None:
        parts.append(1.0 if fm["oi_chg"] > 0.5 else 0.5)   # OI barh raha = naye positions trend ke sath
    if fm["ls"] is not None:
        ratio = fm["ls"] if side == "LONG" else (1 / fm["ls"] if fm["ls"] > 0 else 1)
        parts.append(1.0 if ratio < 1.5 else 0.6 if ratio < 2.5 else 0.2)   # crowded trade = risky
    return sum(parts) / len(parts) if parts else None


def spread_pct(symbol):
    try:
        t = api_get("/api/v3/ticker/bookTicker", {"symbol": symbol})
        bid, ask = float(t["bidPrice"]), float(t["askPrice"])
        mid = (bid + ask) / 2
        return (ask - bid) / mid * 100 if mid > 0 else None
    except Exception:
        return None


# ---------------- market context (ek baar per scan) ----------------

def build_context(data):
    ctx = {"ok": True, "reasons": [], "btc_bias": None, "btc_1d": None, "eth_btc": None,
           "dominance": None, "dom_change": None, "mkt_change": None, "btc_ret": None}
    now_utc = datetime.now(timezone.utc)
    for a, b in NEWS_BLACKOUT_UTC:
        try:
            s = datetime.strptime(a, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            e = datetime.strptime(b, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            if s <= now_utc <= e:
                ctx["ok"] = False
                ctx["reasons"].append("high-impact news window")
        except Exception:
            pass
    try:
        b4 = get_klines("BTCUSDT", "4h", 300).iloc[:-1]
        b1d = get_klines("BTCUSDT", "1d", 120).iloc[:-1]
        b1h = get_klines("BTCUSDT", "1h", 300).iloc[:-1]
        b15 = get_klines("BTCUSDT", "15m", 200).iloc[:-1]
        ctx["btc_bias"] = trend_of(b4)
        ctx["btc_1d"] = day_bias(b1d)
        ctx["btc_ret"] = b1h["close"].pct_change().tail(100).values
        rng = (b15["high"] - b15["low"]).tail(3)
        prev_atr = atr(b15).shift(1).tail(3)
        if bool((rng > 3.5 * prev_atr).any()):   # news/shock proxy
            ctx["ok"] = False
            ctx["reasons"].append("BTC volatility shock")
    except Exception as e:
        log(f"BTC context error: {e}")
    try:
        eb = get_klines("ETHBTC", "4h", 200).iloc[:-1]
        c = eb["close"]
        e50 = ema(c, 50)
        if c.iloc[-1] > e50.iloc[-1] and e50.iloc[-1] > e50.iloc[-6]:
            ctx["eth_btc"] = "LONG"
        elif c.iloc[-1] < e50.iloc[-1] and e50.iloc[-1] < e50.iloc[-6]:
            ctx["eth_btc"] = "SHORT"
    except Exception as e:
        log(f"ETHBTC error: {e}")
    try:
        g = requests.get("https://api.coingecko.com/api/v3/global", timeout=15).json()["data"]
        ctx["mkt_change"] = g.get("market_cap_change_percentage_24h_usd")
        ctx["dominance"] = g["market_cap_percentage"]["btc"]
        hist = data.setdefault("dom_hist", [])
        hist.append([int(time.time()), ctx["dominance"]])
        data["dom_hist"] = hist[-150:]
        old = [v for t, v in data["dom_hist"] if time.time() - t >= 20 * 3600]
        if old:
            ctx["dom_change"] = ctx["dominance"] - old[-1]
    except Exception as e:
        log(f"Global market data error: {e}")
    if ctx["mkt_change"] is not None and ctx["mkt_change"] < -8:
        ctx["ok"] = False
        ctx["reasons"].append("market crash (-8% 24h)")
    return ctx


# ---------------- anti-loss protection + adaptive learning ----------------

def protection_state(data):
    """(allowed, reason, min_score)"""
    hist = data.get("history", [])
    day = today_key()
    if data.get("sent_today", {}).get(day, 0) >= MAX_SIGNALS_PER_DAY:
        return False, "daily signal limit reached", MIN_SIGNAL_SCORE
    if sum(1 for h in hist if h.get("day") == day and h.get("result") == "sl") >= DAILY_MAX_LOSSES:
        return False, "daily loss limit reached", MIN_SIGNAL_SCORE
    streak, last_loss_t = 0, 0
    for h in reversed(hist):
        if h.get("result") == "sl":
            streak += 1
            last_loss_t = max(last_loss_t, h.get("closed", 0))
        elif h.get("result") in WIN_RESULTS:
            break
    if streak >= LOSS_STREAK_COOLDOWN and time.time() - last_loss_t < LOSS_STREAK_COOLDOWN_H * 3600:
        return False, f"cooldown after {streak} consecutive losses", MIN_SIGNAL_SCORE
    return True, "", (VERY_STRONG_SCORE if streak >= RAISE_AFTER_LOSSES else MIN_SIGNAL_SCORE)


def adaptive_stats(data):
    """History se: har condition (tag) aur har regime ka (trades, wins)."""
    tag, reg = {}, {}
    for h in data.get("history", []):
        res = h.get("result")
        if res != "sl" and res not in WIN_RESULTS:
            continue
        win = 1 if res in WIN_RESULTS else 0
        for t in h.get("tags", []):
            n, w = tag.get(t, (0, 0))
            tag[t] = (n + 1, w + win)
        r = h.get("regime")
        if r:
            n, w = reg.get(r, (0, 0))
            reg[r] = (n + 1, w + win)
    return tag, reg


def tag_factor(tag_stats, name):
    n, w = tag_stats.get(name, (0, 0))
    if n < MIN_TRADES_FOR_ADAPT:
        return 1.0
    return max(0.6, min(1.3, 0.5 + w / n))   # win-rate 50% -> 1.0


def est_win_prob(data, score):
    band = [h for h in data.get("history", [])
            if h.get("score") is not None and h["score"] >= score - 5
            and (h.get("result") == "sl" or h.get("result") in WIN_RESULTS)]
    if len(band) >= 20:
        return sum(1 for h in band if h["result"] in WIN_RESULTS) / len(band)
    return max(0.35, min(0.65, 0.45 + (score - 80) * 0.01))


# ---------------- core sequence: Sweep -> Displacement -> BOS/CHOCH -> FVG/OB -> Retest ----------------

def seq_long(m, sell_levels, a):
    """LONG-space par kaam karta hai (SHORT ke liye mirrored df aata hai)."""
    n = len(m)
    H, L, O, C = m["high"].values, m["low"].values, m["open"].values, m["close"].values
    V, A = m["volume"].values, a.values
    sh, sl = find_swings(m)

    # 1) Liquidity sweep
    best = None
    for i in range(n - 3, max(n - 61, 10), -1):
        cands = list(sell_levels) + [("15m swing", p) for idx, p in sl if idx < i - 2][-3:]
        hit = None
        for name, lv in cands:
            if L[i] < lv and C[i] > lv:
                q = LEVEL_Q.get(name, 0.6)
                if hit is None or q > hit[2]:
                    hit = (name, lv, q)
        if hit:
            best = (i, hit)
            break
    if best is None:
        return _rej("no liquidity sweep")
    i, (lname, lvl, lq) = best
    extreme = float(L[max(i - 2, 0):min(i + 3, n)].min())
    if (C[i + 1:] < extreme).any():
        return _rej("sweep low violated (setup invalid)")

    # 2) Displacement
    d = None
    for j in range(i, min(i + 8, n - 1)):
        rng, body = H[j] - L[j], C[j] - O[j]
        if rng > 0 and body > 0 and rng >= 1.3 * A[max(j - 1, 0)] and body / rng >= 0.55:
            d = j
            break
    if d is None:
        return _rej("no displacement after sweep")

    # 3) BOS / CHOCH / MSS
    pre_sh = [p for idx, p in sh if idx < i]
    pre_sl = [p for idx, p in sl if idx < i]
    if not pre_sh:
        return _rej("no swing high to break")
    H0 = pre_sh[-1]
    if not (C[d:] > H0).any():
        return _rej("no BOS/CHOCH after displacement")
    bearish_prior = (len(pre_sl) >= 2 and len(pre_sh) >= 2
                     and pre_sl[-1] < pre_sl[-2] and pre_sh[-1] < pre_sh[-2])
    kind = "CHOCH" if bearish_prior else "BOS"

    # 4) Zones: FVG / Order Block / Breaker / IFVG
    zones = []
    for k in range(max(d - 1, 1), min(d + 2, n - 1)):
        if L[k + 1] > H[k - 1]:
            zones.append(("FVG", float(H[k - 1]), float(L[k + 1])))
    for k in range(d - 1, max(i - 6, 0), -1):
        if C[k] < O[k]:
            zones.append(("OB", float(L[k]), float(H[k])))
            break
    for k in range(i - 1, max(i - 16, 0), -1):
        if C[k] > O[k]:
            if C[d:].max() > H[k]:
                zones.append(("BREAKER", float(L[k]), float(H[k])))
            break
    for k in range(i, max(i - 21, 1), -1):
        if k + 1 < n and H[k + 1] < L[k - 1] and C[d:].max() > L[k - 1]:
            zones.append(("IFVG", float(H[k + 1]), float(L[k - 1])))
            break
    if not zones:
        return _rej("no FVG/OB zone")

    # 5) Retest confirmation
    chosen = None
    last_close = C[n - 1]
    for typ, lo, hi in zones:
        touched = any(L[k] <= hi and H[k] >= lo for k in range(max(d + 1, n - 10), n))
        if touched and lo <= last_close <= hi + 1.2 * A[-1]:
            chosen = (typ, lo, hi)
            break
    if chosen is None:
        return _rej("no retest of entry zone")

    # 6) Premium / Discount / OTE
    leg_high = float(H[d:n].max())
    rngl = leg_high - extreme
    retr = (leg_high - last_close) / rngl if rngl > 0 else 0.0

    # 7) Failed breakout
    failed = False
    for k in range(max(i - 3, 0), min(i + 3, n - 3)):
        if C[k] < lvl and (C[k + 1:k + 4] > lvl).any():
            failed = True
            break

    vol_base = V[max(d - 20, 0):d].mean() if d > 0 else 0
    return {
        "level_name": lname, "level_q": float(lq), "extreme": extreme, "d": int(d),
        "kind": kind, "zones": sorted(set(z[0] for z in zones)), "zone": chosen,
        "discount": bool(retr >= 0.5), "ote": bool(0.62 <= retr <= 0.79),
        "failed_breakout": bool(failed),
        "disp_ratio": float((H[d] - L[d]) / A[max(d - 1, 0)]) if A[max(d - 1, 0)] > 0 else 1.0,
        "disp_rv": float(V[d] / vol_base) if vol_base > 0 else 1.0,
        "bull_close": bool(C[n - 1] > O[n - 1]),
    }


def fallback_setup(m15, a15):
    """Jab poori Sweep->Displacement->BOS->FVG/OB sequence na mile, tab simple
    EMA20 pullback + bullish-close setup dhundta hai (LONG-space mein, SHORT ke liye
    mirrored df aata hai). Isse bot kabhi bilkul khaali nahi rehta."""
    c = m15["close"]
    n = len(m15)
    if n < 30:
        return None
    a15v = float(a15.iloc[-1])
    e20 = ema(c, 20)
    price = float(c.iloc[-1])
    dist = abs(price - float(e20.iloc[-1]))
    if dist > 2.5 * a15v:   # bohat door hai (chase) ya bohat gehra pullback, skip karo
        return None
    last3 = m15.tail(3)
    if not bool((last3["close"] > last3["open"]).any()):
        return None
    extreme = float(m15["low"].tail(12).min())
    vol_avg = m15["volume"].rolling(20).mean().iloc[-1]
    disp_rv = float(m15["volume"].iloc[-1] / vol_avg) if vol_avg and vol_avg > 0 else 1.0
    return {
        "level_name": "EMA20 Pullback", "level_q": 0.55, "extreme": extreme,
        "kind": "BOS", "zones": ["EMA20"], "zone": ("EMA20", extreme, price),
        "discount": True, "ote": False, "failed_breakout": False,
        "disp_ratio": 1.1, "disp_rv": disp_rv, "bull_close": bool(c.iloc[-1] > m15["open"].iloc[-1]),
        "fallback": True,
    }


def five_min_confirm(df5, side):
    """5M execution: strong candle + relative volume + delta/CVD."""
    if len(df5) < 30:
        return False, {}
    last = df5.iloc[-1]
    rng = last["high"] - last["low"]
    if rng <= 0:
        return False, {}
    body = (last["close"] - last["open"]) if side == "LONG" else (last["open"] - last["close"])
    avg_v = df5["volume"].rolling(20).mean().iloc[-1]
    rv = last["volume"] / avg_v if avg_v and avg_v > 0 else 0
    has_delta = bool(df5["delta"].abs().sum() > 0)
    d_last = last["delta"] if side == "LONG" else -last["delta"]
    cvd = df5["delta"].tail(12).sum()
    cvd_ok = (cvd > 0) if side == "LONG" else (cvd < 0)
    ok = body > 0 and body / rng >= 0.5 and rv >= VOL_MULT and (not has_delta or d_last > 0)
    return bool(ok), {"rv": float(rv), "has_delta": has_delta, "cvd_ok": bool(cvd_ok)}


def market_context_frac(side, symbol, ctx):
    votes = []
    if symbol != "BTCUSDT":
        dc = ctx.get("dom_change")
        if dc is not None:
            votes.append(1.0 if (side == "LONG" and dc < 0) or (side == "SHORT" and dc > 0) else 0.3)
        eb = ctx.get("eth_btc")
        if eb is not None:
            votes.append(1.0 if eb == side else 0.4)
    mc = ctx.get("mkt_change")
    if mc is not None:
        votes.append(1.0 if (side == "LONG" and mc > 0) or (side == "SHORT" and mc < 0) else 0.3)
    return sum(votes) / len(votes) if votes else None


def analyze_basic(symbol):
    """Fallback: simple 4H trend + 1H momentum/volume signal, jab advanced SMC ko kuch na mile.
    Grade 'BASIC' — kam sakht, jaldi signals deta hai (LONG aur SHORT dono, coin ke apne trend par)."""
    try:
        df4 = get_klines(symbol, "4h", 300).iloc[:-1]
        side = trend_of(df4)
        if side is None:
            return None
        df1h = get_klines(symbol, "1h", 300).iloc[:-1]
        if len(df1h) < 60:
            return None
        c = df1h["close"]
        e20 = ema(c, 20)
        r = rsi(c)
        macd_line = ema(c, 12) - ema(c, 26)
        hist = macd_line - ema(macd_line, 9)
        a = atr(df1h)
        a_now = float(a.iloc[-1])
        price = float(c.iloc[-1])
        if a_now / price * 100 < MIN_ATR_PCT:
            return None
        vol_ratio = float((df1h["volume"] / df1h["volume"].rolling(20).mean()).iloc[-1])
        if side == "LONG":
            ok = hist.iloc[-1] > 0 and 40 <= r.iloc[-1] <= 72 and price > e20.iloc[-1]
        else:
            ok = hist.iloc[-1] < 0 and 28 <= r.iloc[-1] <= 60 and price < e20.iloc[-1]
        if not ok:
            return None
        risk = 1.5 * a_now
        if side == "LONG":
            sl, tp1, tp2 = price - risk, price + TP1_RR * risk, price + TP2_RR * risk
        else:
            sl, tp1, tp2 = price + risk, price - TP1_RR * risk, price - TP2_RR * risk
        score = 55 + min(15, vol_ratio * 8)
        return {
            "symbol": symbol, "side": side, "entry": price, "sl": sl, "tp1": tp1, "tp2": tp2,
            "score": int(score), "rank": score, "grade": "BASIC",
            "tags": ["htf_trend", "momentum", "volume_confirm"],
            "regime": "bull" if side == "LONG" else "bear",
            "steps": ["4H Trend", "1H Momentum", "Volume"],
            "rr1": TP1_RR, "rr2": TP2_RR, "btc": "n/a", "funding": None, "adx": 0,
            "time": int(time.time() * 1000),
        }
    except Exception as e:
        log(f"analyze_basic error {symbol}: {e}")
        return None


def analyze(symbol, ctx, data, min_score, tag_stats, reg_stats):
    # ---- 1) Market regime (4H): bull/bear/sideways, trend strength, volatility ----
    df4 = get_klines(symbol, "4h", 300).iloc[:-1]
    side, vol_state = market_regime(df4)
    if side is None:
        return _rej("sideways / no clear 4H trend")
    if vol_state == "high":
        return _rej("high-volatility regime")
    adx4 = float(adx(df4)[0].iloc[-1])
    if adx4 < ADX_MIN:
        return _rej("weak trend strength (ADX)")
    regime = "bull" if side == "LONG" else "bear"
    n_r, w_r = reg_stats.get(regime, (0, 0))
    if n_r >= MIN_TRADES_FOR_ADAPT and w_r / n_r < 0.30:
        return _rej("regime underperforming (adaptive block)")
    # (dominance ab hard-reject nahi, comp['market_context'] mein soft factor hai)

    # ---- 2) Multi-timeframe: 1D bias, 1H structure ----
    df1d = get_klines(symbol, "1d", 120).iloc[:-1]
    b1d = day_bias(df1d)  # ab hard-reject nahi, sirf score mein weight deta hai (neeche comp['htf_trend'])
    df1h = get_klines(symbol, "1h", 300).iloc[:-1]
    if len(df1h) < 100:
        return _rej("not enough 1H data")
    structure_ok, choch = structure_check(df1h, side)
    # (CHOCH aur BTC-correlation ab hard-reject nahi, comp['structure_1h']/['btc_context'] mein soft factor hain)
    btc4 = ctx.get("btc_bias")
    corr = corr_with_btc(df1h, ctx)

    df15 = get_klines(symbol, "15m", 300).iloc[:-1]
    if len(df15) < 100:
        return _rej("not enough 15M data")
    a15 = atr(df15)
    a15v = float(a15.iloc[-1])
    if a15v / float(df15["close"].iloc[-1]) * 100 < MIN_ATR_PCT:
        return _rej("market too flat")
    a1h = float(atr(df1h).iloc[-1])

    # ---- 3) Liquidity map ----
    htf = htf_levels(df1d)
    eq_h, eq_l = equal_levels(df1h, a1h)
    sh1, sl1 = find_swings(df1h)
    sw_hi = [p for _, p in sh1[-5:]]
    sw_lo = [p for _, p in sl1[-5:]]
    if side == "LONG":
        sell = [(k, htf[k]) for k in ("PWL", "PDL") if k in htf]
        sell += [("EQL", p) for p in eq_l] + [("1H swing", p) for p in sw_lo]
        blockers = [htf[k] for k in ("PDH", "PWH") if k in htf] + eq_h
        targets = blockers + sw_hi
        m15 = df15
    else:
        sell = [(k, -htf[k]) for k in ("PWH", "PDH") if k in htf]
        sell += [("EQH", -p) for p in eq_h] + [("1H swing", -p) for p in sw_hi]
        blockers = [-htf[k] for k in ("PDL", "PWL") if k in htf] + [-p for p in eq_l]
        targets = blockers + [-p for p in sw_lo]
        m15 = mirror_df(df15)

    # ---- 4) Sweep -> Displacement -> BOS/CHOCH -> FVG/OB -> Retest ----
    seq = seq_long(m15, sell, a15)
    if seq is None:
        seq = fallback_setup(m15, a15)   # poori sequence nahi mili -> simple trend-pullback try karo
        if seq is None:
            return None

    # ---- 5) 5M execution + volume/order flow ----
    df5 = get_klines(symbol, "5m", 200).iloc[:-1]
    ok5, x5 = five_min_confirm(df5, side)
    if not ok5:
        return _rej("no 5M volume/entry confirmation")
    price = float(df5["close"].iloc[-1])
    conv = 1 if side == "LONG" else -1
    price_m = conv * price

    # ---- 6) Quant filters: spread, SL/TP, net R:R ----
    sp = spread_pct(symbol)
    if sp is not None and sp > MAX_SPREAD_PCT:
        return _rej("spread too wide")
    cost_pct = FEE_PCT * 2 + (sp if sp is not None else 0.05)

    sl_m = seq["extreme"] - 0.25 * a15v
    risk = price_m - sl_m
    if risk < 1.0 * a15v:
        sl_m = price_m - 1.0 * a15v
        risk = 1.0 * a15v
    risk_pct = risk / abs(price_m) * 100
    if risk > 6.0 * a15v or risk_pct > 6.0:
        return _rej("structural stop too far")
    if any(price_m < p < price_m + 0.5 * risk for p in blockers):
        return _rej("major level blocks path to TP1")
    tp1_m = price_m + TP1_RR * risk
    lt = sorted(p for p in targets if price_m + 2.5 * risk <= p <= price_m + 6 * risk)
    tp2_m = lt[0] if lt else price_m + TP2_RR * risk
    entry, sl, tp1, tp2 = price, conv * sl_m, conv * tp1_m, conv * tp2_m

    r1 = abs(tp1 - entry) / entry * 100
    r2 = abs(tp2 - entry) / entry * 100
    rr1n = (r1 - cost_pct) / (risk_pct + cost_pct)
    rr2n = (r2 - cost_pct) / (risk_pct + cost_pct)
    rr_eff = 0.5 * rr1n + 0.5 * rr2n
    if rr_eff < MIN_NET_RR:
        return _rej("net R:R too low after costs")

    # momentum + VWAP
    c15 = df15["close"]
    e20 = ema(c15, 20).iloc[-1]
    mom = (c15.iloc[-1] > e20 and c15.iloc[-1] > c15.iloc[-6]) if side == "LONG" \
        else (c15.iloc[-1] < e20 and c15.iloc[-1] < c15.iloc[-6])
    w = df15.tail(96)
    tp_ = (w["high"] + w["low"] + w["close"]) / 3
    vw = float((tp_ * w["volume"]).sum() / w["volume"].sum()) if w["volume"].sum() > 0 else price
    vwap_ok = price > vw if side == "LONG" else price < vw

    # ---- 7) Derivatives: funding / OI / long-short ----
    fm = futures_metrics(symbol)
    fnd = fm["funding"]
    # (extreme funding ab hard-reject nahi, comp['derivatives'] mein already penalize hota hai)

    # ---- 8) Scoring (0-100) ----
    comp = {}
    if b1d == side:
        comp["htf_trend"] = 1.0 if adx4 >= 25 else 0.75
    else:
        comp["htf_trend"] = 0.5
    if symbol == "BTCUSDT":
        comp["btc_context"] = 1.0
    elif btc4 is None:
        comp["btc_context"] = 0.5
    elif btc4 == side:
        comp["btc_context"] = 1.0 if ctx.get("btc_1d") in (None, side) else 0.8
    else:
        comp["btc_context"] = 0.3
    comp["market_context"] = market_context_frac(side, symbol, ctx)
    comp["structure_1h"] = 0.25 if choch else (1.0 if structure_ok else 0.4)
    comp["sweep"] = seq["level_q"]
    comp["failed_breakout"] = 1.0 if seq["failed_breakout"] else 0.0
    comp["displacement"] = 1.0 if seq["disp_ratio"] >= 2.0 else 0.75 if seq["disp_ratio"] >= 1.6 else 0.55
    comp["break"] = 1.0 if seq["kind"] == "CHOCH" else 0.85
    nz = len(seq["zones"])
    comp["zone"] = 1.0 if nz >= 3 else 0.85 if nz == 2 else 0.65
    comp["retest"] = 1.0 if seq["bull_close"] else 0.7
    comp["pd_ote"] = 1.0 if seq["ote"] else 0.7 if seq["discount"] else 0.3
    comp["volume_confirm"] = min(1.0, 0.5 + 0.25 * (x5["rv"] >= 1.5) + 0.25 * (seq["disp_rv"] >= 1.5))
    comp["vwap"] = 1.0 if vwap_ok else 0.2
    comp["cvd_delta"] = (1.0 if x5["cvd_ok"] else 0.2) if x5["has_delta"] else None
    comp["derivatives"] = deriv_frac(side, fm)
    comp["momentum"] = 1.0 if mom else 0.3
    comp["rr_quality"] = 1.0 if rr_eff >= 2.2 else 0.75 if rr_eff >= 1.8 else 0.5
    comp["liquidity_target"] = 1.0 if lt else 0.5
    comp["spread_quality"] = None if sp is None else (1.0 if sp < 0.03 else 0.6 if sp < 0.06 else 0.3)

    def total(c):
        num = den = 0.0
        tags = []
        for name, wgt in WEIGHTS.items():
            fr = c.get(name)
            if fr is None:
                continue
            f = tag_factor(tag_stats, name)
            num += wgt * f * fr
            den += wgt * f
            if fr >= 0.6:
                tags.append(name)
        return (int(round(100 * num / den)) if den else 0), tags

    pre_score, _ = total(comp)
    p_win = est_win_prob(data, pre_score)
    ev = p_win * rr_eff - (1 - p_win)
    # (EV gate abhi soft hai jab tak trade history kam ho, andaza unreliable hota hai)
    comp["ev_quality"] = 1.0 if ev >= 0.5 else 0.75 if ev >= 0.3 else 0.5 if ev >= MIN_EV else 0.3
    score, tags = total(comp)
    is_fallback = bool(seq.get("fallback"))
    required = FALLBACK_MIN_SCORE if is_fallback else min_score
    if score < required:
        return _rej(f"score below {required}" + (" (fallback)" if is_fallback else ""))

    if is_fallback:
        steps = ["Trend + EMA20 Pullback", "Volume Confirm"]
        grade = "TREND SETUP"
    else:
        brk = "MSS/CHOCH" if seq["kind"] == "CHOCH" else "BOS"
        steps = [f"Sweep ({seq['level_name']})", "Displacement", brk, "+".join(seq["zones"]), "Retest", "Volume"]
        grade = "VERY STRONG" if score >= VERY_STRONG_SCORE else "STRONG"
    return {
        "symbol": symbol, "side": side, "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2,
        "score": score, "rank": score - (10 if is_fallback else 0), "grade": grade,
        "tags": tags, "regime": regime, "steps": steps, "fallback": is_fallback,
        "rr1": float(rr1n), "rr2": float(rr2n), "btc": btc4 or "n/a",
        "funding": fnd, "adx": adx4,
        "time": int(time.time() * 1000),
    }


def signal_message(s):
    icon = "🟢🚀" if s["side"] == "LONG" else "🔴📉"
    name = s["symbol"][:-4] + "/USDT"
    tags_txt = ", ".join(TAG_LABELS[t] for t in s.get("tags", []) if t in TAG_LABELS)
    steps_txt = " → ".join(s.get("steps", []))

    def pct(x):
        return f"{(x - s['entry']) / s['entry'] * 100:+.2f}%"

    fnd = s.get("funding")
    fnd_txt = f" | Funding: {fnd * 100:+.3f}%" if fnd is not None else ""
    return (
        f"<b>{icon} {s['side']} SIGNAL — {name}</b>\n"
        f"🏅 <b>{s.get('grade', 'STRONG')}</b> — Score <b>{s['score']}/100</b>\n\n"
        f"📍 <b>Entry:</b> {fmt_price(s['entry'])}\n"
        f"🛑 <b>Stop Loss:</b> {fmt_price(s['sl'])} <i>({pct(s['sl'])})</i>\n"
        f"🎯 <b>TP1:</b> {fmt_price(s['tp1'])} <i>({pct(s['tp1'])})</i>\n"
        f"🎯 <b>TP2:</b> {fmt_price(s['tp2'])} <i>({pct(s['tp2'])})</i>\n\n"
        f"⚖️ <b>Net R:R:</b> {s.get('rr1', 0):.1f} / {s.get('rr2', 0):.1f} <i>(after fees &amp; spread)</i>\n"
        f"🧩 <b>Setup:</b> {steps_txt}\n"
        f"🌍 <b>Regime:</b> {s.get('regime', '-')} | BTC: {s.get('btc', 'n/a')}{fnd_txt}\n"
        f"✅ <b>Confluence:</b> {tags_txt}\n\n"
        f"💡 Take half profit at TP1, then move SL to entry.\n"
        f"⚠️ Risk only 1-2% per trade, keep leverage low.\n\n"
        f"<i>Not financial advice.</i>"
    )




# ============================================================
#  EXTRA CONTENT (education, psychology, risk, news, market)
# ============================================================

EDUCATION_TIPS = [
    "📚 Support is a level where price repeatedly bounces up. Resistance is the opposite.",
    "📚 RSI above 70 is 'overbought', below 30 is 'oversold' — but in a strong trend it can stay there a while.",
    "📚 MACD is the difference between two EMAs. When it crosses above zero, momentum turns bullish.",
    "📚 A long lower wick on a candle often shows sellers lost control and buyers stepped back in.",
    "📚 Volume shows the 'truth' behind any move. Breakouts without volume are often fake.",
    "📚 Leverage amplifies profit, but risk grows even faster. Beginners should keep leverage low.",
    "📚 A 'bull market' means prices are rising over a long period; a 'bear market' is the opposite.",
    "📚 Higher liquidity means easier buying/selling and less erratic price movement.",
    "📚 A Stop Loss is an automatic order that caps your loss at a pre-set level.",
    "📚 Diversification means not putting all your money into one coin, but spreading it across several.",
    "📚 Market cap = price x total coins. It gives a size estimate, price alone doesn't.",
    "📚 The Fear & Greed Index shows whether the market is fearful or greedy overall. Extreme fear often creates opportunities.",
    "📚 A Doji candle has an open and close that are nearly equal, showing market indecision.",
    "📚 A Hammer candle has a long lower wick and often signals a reversal at the end of a downtrend.",
    "📚 An Engulfing pattern is when a new candle completely 'swallows' the previous one — a strong reversal signal.",
    "📚 A trendline connects two or more highs/lows and shows the direction of the trend.",
    "📚 Fibonacci retracement levels (23.6%, 38.2%, 50%, 61.8%) show where price might pause.",
    "📚 Bollinger Bands show volatility around price. Tight bands often mean a big move is coming.",
    "📚 A Moving Average (MA) smooths out past prices to show the trend.",
    "📚 An EMA (Exponential MA) weights recent prices more, so it reacts faster than an SMA.",
    "📚 A Market order executes instantly at the current price; a Limit order only fills at your chosen price.",
    "📚 A Stop-Limit order works in two parts: a trigger price, then a limit price for execution.",
    "📚 In Spot trading you buy the actual coin; in Futures you bet on price movement.",
    "📚 A Long position profits when price rises; a Short position profits when price falls.",
    "📚 The funding rate is a periodic payment between Longs and Shorts in futures that keeps price close to spot.",
    "📚 Open Interest is the total value of all open positions in a futures contract.",
    "📚 Liquidation happens when losses exceed margin and the exchange force-closes the position.",
    "📚 Slippage is the gap between your expected price and the actual execution price.",
    "📚 The order book is a list of buy and sell orders showing demand/supply at each price.",
    "📚 A CEX (Centralized Exchange) like Binance is run by a company; a DEX (Decentralized Exchange) runs on the blockchain itself.",
    "📚 A hot wallet is connected to the internet (easy but less secure); a cold wallet is offline (harder but safer).",
    "📚 Never share your private key. Whoever holds the key has actual control.",
    "📚 A stablecoin (like USDT, USDC) is kept near $1 in value, usually backed by reserves.",
    "📚 DCA (Dollar Cost Averaging) means investing small amounts at regular intervals to smooth out your average price.",
    "📚 HODL became a famous crypto-community term for 'holding long-term'.",
    "📚 On-chain metrics like wallet activity and exchange inflow/outflow help gauge market sentiment.",
    "📚 BTC Dominance shows Bitcoin's share of total crypto market cap.",
    "📚 'Altcoin season' is when altcoins outperform Bitcoin.",
    "📚 Halving cuts Bitcoin's mining reward in half every 4 years, slowing new supply.",
    "📚 Staking means locking your coins to secure the network in exchange for rewards.",
    "📚 DeFi (Decentralized Finance) offers lending, borrowing and trading without a bank.",
    "📚 Layer 1 (like Ethereum) is its own blockchain; Layer 2 (like Arbitrum) is built on top to boost speed.",
    "📚 Gas fees are the cost of processing a transaction on any blockchain.",
    "📚 ATH (All-Time High) is the highest price a coin has ever hit; ATL is the opposite.",
    "📚 A retracement is a temporary pullback within a trend before it continues.",
    "📚 A breakout happens when price pushes through a resistance level.",
    "📚 Consolidation is a period when price moves sideways in a tight range, waiting for a decision.",
    "📚 Volatility shows how fast price swings up and down — crypto is more volatile than stocks.",
    "📚 Crypto often correlates with the US stock market, especially tech stocks.",
    "📚 Interest rate decisions (Fed) affect crypto too, by shifting overall risk appetite.",
    "📚 A rising Dollar Index (DXY) often pressures crypto, since the dollar strengthens.",
    "📚 KYC (Know Your Customer) is an exchange's identity verification process.",
    "📚 Arbitrage means profiting from a price difference between two exchanges.",
    "📚 A whale is a large investor whose trades can move the market.",
    "📚 A token unlock is when previously locked tokens enter circulation, increasing supply.",
    "📚 Tokenomics is the study of a coin's total supply, distribution and use-case.",
    "📚 Circulating supply is coins currently in the market; total supply includes locked coins too.",
    "📚 FDV (Fully Diluted Valuation) is the market cap if all tokens were in circulation.",
    "📚 A correlation coefficient shows how closely two assets move together.",
    "📚 A Trailing Stop Loss moves with price to lock in profit as a trade goes your way.",
    "📚 Scalping is a style of very short, quick trades, often just minutes long.",
    "📚 Swing trading holds positions for days or weeks to capture bigger moves.",
    "📚 Position trading holds for months or years, like long-term investors do.",
    "📚 The 'Head and Shoulders' chart pattern often signals a trend reversal.",
    "📚 A 'Double Top/Bottom' pattern shows price testing a level twice and reversing.",
    "📚 Triangle patterns (ascending/descending/symmetrical) are often consolidation before a breakout.",
    "📚 ATR (Average True Range) measures an asset's average daily volatility.",
    "📚 Ichimoku Cloud is an advanced indicator showing trend, support/resistance and momentum together.",
    "📚 Stochastic Oscillator is similar to RSI but compares closing price to the high-low range.",
    "📚 VWAP (Volume Weighted Average Price) is the volume-adjusted average price for the day.",
    "📚 A Pump and Dump scheme artificially inflates a coin's price then dumps it — stay alert.",
    "📚 A Rug Pull happens when developers suddenly drain a project's liquidity and disappear.",
    "📚 A smart contract is self-executing code on the blockchain that follows pre-set rules.",
    "📚 An NFT (Non-Fungible Token) is a unique digital asset recorded on the blockchain.",
    "📚 An airdrop is free tokens given by projects to early users or holders.",
    "📚 A testnet is a trial blockchain for testing without real money.",
    "📚 Mainnet is the live, real blockchain where actual transactions happen.",
    "📚 A cross-chain bridge lets assets move between different blockchains.",
    "📚 Impermanent Loss is a DeFi liquidity-providing risk that occurs when two pooled assets diverge in price.",
    "📚 Yield Farming means lending/staking your crypto assets to earn extra rewards.",
    "📚 An Oracle feeds blockchains real-world data, like live prices.",
    "📚 A consensus mechanism (Proof of Work, Proof of Stake) is how a blockchain verifies transactions.",
    "📚 Proof of Work has miners use computing power to create new blocks (like Bitcoin).",
    "📚 Proof of Stake lets validators create blocks by staking coins, using far less energy.",
    "📚 A wallet address is a public location where crypto can be sent or received.",
    "📚 Your seed phrase (12-24 words) is the master key to your wallet. Write it down and keep it safe — never share it.",
    "📚 2FA (Two-Factor Authentication) adds an extra security layer to your account — always keep it on.",
    "📚 Phishing scams use fake sites/links to steal your password or keys — always check links before clicking.",
    "📚 Always verify a token's contract address — many scam coins copy a real coin's name.",
    "📚 An audit report shows whether security experts have reviewed a project's smart contract.",
    "📚 Understanding circulating vs locked supply helps you anticipate unlock events.",
    "📚 Seasonality: some months (like September) have historically been weak for crypto, but it's never guaranteed.",
    "📚 A correlation break is when crypto suddenly moves opposite to stocks — a notable moment.",
    "📚 A market maker trades large volume to provide liquidity to the market.",
    "📚 Watching order flow means tracking large buy/sell orders that move price.",
    "📚 Divergence occurs when price makes a new high but an indicator (like RSI) doesn't — a sign of weakness.",
    "📚 A Golden Cross is when a shorter EMA (e.g. 50) crosses above a longer EMA (e.g. 200) — a bullish signal.",
    "📚 A Death Cross is the opposite of a Golden Cross — a shorter EMA crosses below a longer one, a bearish signal.",
    "📚 Wyckoff Accumulation/Distribution theory explains how big players quietly build or exit positions.",
]

PSYCHOLOGY_TIPS = [
    "🧠 FOMO (fear of missing out) is the biggest enemy. Entering just because everyone's talking about a coin is dangerous.",
    "🧠 One losing trade doesn't invalidate your whole strategy. Every strategy has losses — look at the full record.",
    "🧠 Revenge trading (trading out of anger) often leads to bigger losses. Take a break after a loss.",
    "🧠 Make the plan first, trade second. Changing the plan mid-trade is usually driven by emotion.",
    "🧠 You don't need to win every trade. With good risk management, even a 50% win rate can be profitable.",
    "🧠 Overtrading usually comes from boredom or greed, not from profit opportunity.",
    "🧠 Control greed when in profit, control panic when in loss. Both emotions cause damage.",
    "🧠 Keep a trading journal: write down the reason for every trade. It helps you spot your own mistakes.",
    "🧠 The market never waits. If you miss a good setup, find the next one — don't force an entry into it.",
    "🧠 Patience is trading's most underrated skill. The best trades often come after waiting.",
    "🧠 FUD (Fear, Uncertainty, Doubt) spreads fast on social media. Research yourself before panic-selling.",
    "🧠 Watch for confirmation bias: don't only seek news that matches your view — check the other side too.",
    "🧠 Sunk cost fallacy: staying in a losing position just because you've already put in a lot of money is flawed thinking.",
    "🧠 Anchoring bias happens when you fixate on an old price and ignore the current situation.",
    "🧠 Beware herd mentality: don't buy/sell just because everyone else is.",
    "🧠 Analysis paralysis happens when too much information makes deciding hard. Stick to a simple plan.",
    "🧠 After one good trade, avoid overconfidence — keep the next trade just as disciplined.",
    "🧠 Accept that losses are part of trading. Traders who don't take losses personally last longer.",
    "🧠 Social media hype and real research are different things. Trust your own analysis.",
    "🧠 Don't compare your profits to others' — everyone's risk and capital are different.",
    "🧠 Keep realistic expectations. The idea of getting rich overnight often leads to big losses.",
    "🧠 When very angry or upset, stay away from trading. Wait for your emotions to clear.",
    "🧠 Taking short breaks keeps your mind fresh and prevents bad decisions.",
    "🧠 Lack of sleep hurts decision-making — rest fully before you trade.",
    "🧠 Don't chase every pump. If a move is gone, it's gone — the next one will come.",
    "🧠 Set a daily loss limit and stop trading for the day once you hit it.",
    "🧠 Celebrate the process, not just the profit. A losing trade taken with good discipline can still be a 'good trade'.",
    "🧠 Averaging down (adding to a losing position) should be part of a plan, not driven by hope.",
    "🧠 Holding a losing position just because 'it'll come back' is a lack of discipline.",
    "🧠 The 'I'm always right' mindset is dangerous in trading. Keep learning from the market, leave ego aside.",
    "🧠 Taking bigger risks than your comfort zone often leads to panic decisions. Know your limits.",
    "🧠 Avoid perfectionism — no strategy is ever 100% perfect. 'Good enough' is a healthier mindset.",
    "🧠 Jumping straight into another trade after a loss is a mistake. Understand the reason first, then move on.",
    "🧠 Trading isn't just numbers — managing your emotions matters just as much.",
    "🧠 The outcome of a single trade doesn't define your overall skill. Look at a large sample size.",
    "🧠 Boredom trading (trading just because you need something to do) often wrecks accounts.",
    "🧠 Write down your rules and follow them, no matter what your emotions say.",
    "🧠 Good trading is boring. Traders chasing excitement often take on too much risk.",
    "🧠 Before every trade, ask yourself: 'If this hits my SL, will I still be okay?'",
    "🧠 Drop the idea of 'beating' the market — the only goal should be following your strategy consistently.",
    "🧠 Avoid impulsive entries. Re-read your plan once before clicking.",
    "🧠 If a small market move has you refreshing the chart constantly, that's a sign of anxiety — reduce screen time.",
    "🧠 Don't blindly trust others' 'guaranteed profit' calls — verify yourself.",
    "🧠 Write down your mistakes instead of hiding them, and learn from them — that's what a journal is for.",
    "🧠 Even the best strategy fails without patience and discipline.",
    "🧠 Suddenly increasing your position size after one big win is a sign of overconfidence — stay controlled.",
    "🧠 Trying to recover a loss with a hasty, bigger trade often leads to more loss.",
    "🧠 Treat trading like a job, not a casino. Plan, execute, review — that's it.",
    "🧠 The more you react to news headlines, the more emotional your trading becomes. Focus on your own setup.",
    "🧠 Never change your trading plan just because of pressure from others.",
    "🧠 Know your own risk tolerance. A strategy that works for someone else isn't automatically right for you.",
    "🧠 Taking a break from trading isn't weakness, it's wisdom. A tired mind makes bad decisions.",
    "🧠 Staring at too many charts in one day (overanalysis) causes confusion and bad entries.",
    "🧠 Treat a loss as a 'learning fee', not a hit to your self-worth.",
    "🧠 Don't try to make your win rate 100% — even great traders lose plenty of trades.",
    "🧠 It's fine not to force a trade when the market is quiet — be patient.",
    "🧠 Review your trading history and spot patterns: do most of your losses happen from tiredness or rushing?",
    "🧠 A good trader doesn't hide mistakes — they accept and fix them.",
    "🧠 Resist the urge to show off your open positions to others — it adds emotional pressure to your decisions.",
    "🧠 Getting excited on every green candle and worried on every red one is normal — just keep it in check.",
    "🧠 Remember your 'why' — knowing why you trade helps in tough moments.",
    "🧠 Small, consistent wins add up to a big difference over time — don't just chase 'big trades'.",
    "🧠 Don't even think about entering until your risk management is set.",
    "🧠 Other people's success stories only show their wins, never their losses. You never get the full picture.",
    "🧠 Humility matters in trading — the market can prove you wrong at any time.",
    "🧠 Avoid the 'I'm definitely right this time' mindset — that's overconfidence.",
    "🧠 Emotional trades often skip the stop-loss, because the mind is focused only on 'winning'.",
    "🧠 In your journal, note your feelings at the time too, not just entry/exit — patterns will emerge.",
    "🧠 Instead of punishing yourself after a loss, calm down and plan again the next day.",
    "🧠 The idea of 'zero risk' is dangerous — every trade always carries some risk.",
    "🧠 Copying others' calls isn't a great way to learn — understanding it yourself matters more.",
    "🧠 Measure your progress by discipline followed, not just by profit.",
    "🧠 After a big loss, don't delete your account or quit trading — pause and understand why first.",
    "🧠 You don't need an opinion on every coin — just focus on setups that match your strategy.",
    "🧠 Positive self-talk works in trading too — don't keep telling yourself 'I'm a failure'.",
    "🧠 Learning to trade small for comfort is a necessary step before trading big.",
    "🧠 A good trader treats emotions as a signal, not something to ignore.",
    "🧠 The cure for overtrading: set a strict daily limit on the number of trades.",
    "🧠 The market's mood changes daily — learn to keep your own mood in check too.",
    "🧠 Pausing 10 minutes before any big decision often prevents a big loss.",
    "🧠 Don't assume your past success is permanent — the market always changes.",
    "🧠 After a loss, the first question should be 'was my SL correct', not 'the market is wrong'.",
    "🧠 Consistency matters more than intensity in trading.",
    "🧠 Instead of burning energy on everyday small trades, save it for your best setups.",
    "🧠 The outcome of one trade doesn't decide your worth.",
    "🧠 Watching others' flashy lifestyle and making your strategy riskier is a common mistake.",
    "🧠 Break your trading goals into small steps — 'getting rich' alone isn't a real goal.",
    "🧠 A loss taken while following your plan is still a 'good trade' — only the outcome was unlucky.",
    "🧠 Rate your emotional state (1-10) before a trade — if it's high, pause.",
    "🧠 Trading skill comes with time — wanting quick results is the biggest source of stress.",
    "🧠 Don't tie your identity to trading results — one loss doesn't make you a bad trader.",
    "🧠 Share losses with trading friends too, not just wins — it builds a healthier perspective.",
    "🧠 Constantly refreshing the chart increases anxiety — set your alert and wait.",
    "🧠 Boring consistency always beats exciting big gambles in trading.",
    "🧠 Write down your biggest trading mistake and revisit it daily so it doesn't repeat.",
    "🧠 If your heart races or hands shake during a trade, your position size is too big.",
    "🧠 Build confidence from your backtested strategy data, not just hope.",
    "🧠 Every day in the market is a new day — don't let yesterday's loss affect today's decisions.",
    "🧠 Keep a fixed trading routine (time, checklist, review) — it keeps emotions in check.",
]

RISK_TIPS = [
    "⚠️ Never risk more than 1-2% of your total capital on a single trade.",
    "⚠️ Never forget to set a Stop Loss, no matter how confident you are.",
    "⚠️ The higher the leverage, the smaller the move that can liquidate you. Beginners shouldn't go beyond 3-5x.",
    "⚠️ Don't put all your money into one sector (like all meme coins) — correlation is high there.",
    "⚠️ Always calculate position size from the gap between Entry and Stop Loss, not just by 'feel'.",
    "⚠️ Never trade with borrowed money or money you need for essentials.",
    "⚠️ Taking partial profit at TP1 and moving SL to entry greatly reduces risk.",
    "⚠️ Holding too many open positions at once puts risk out of control. Stay focused.",
    "⚠️ Keep position size small before big events (Fed meetings, halvings) — volatility spikes.",
    "⚠️ Review your win rate and risk-reward every month. If you're consistently losing, pause and review your strategy.",
    "⚠️ Always keep your trading capital separate from essential life savings.",
    "⚠️ Only trade money you could afford to lose entirely without it affecting your life.",
    "⚠️ Keep a risk-reward ratio of at least 1:1.5, so profitability is possible even with a lower win rate.",
    "⚠️ If you have a big loss in one day, stop trading for the day and come back fresh tomorrow.",
    "⚠️ Prefer liquid coins (high volume) — illiquid coins move a lot when you place a big order.",
    "⚠️ Use limit orders when possible — market orders carry more slippage risk.",
    "⚠️ Reduce or close high-leverage positions before major news events (CPI, FOMC).",
    "⚠️ Always keep a portion of your portfolio in cash/stablecoins for opportunities.",
    "⚠️ Only average down (add to a losing position) as part of a pre-set plan, not out of emotion.",
    "⚠️ Secure your exchange account: strong password, 2FA and a separate email.",
    "⚠️ Keep large amounts in a cold wallet for the long term — leaving everything on an exchange is risky.",
    "⚠️ Check the team and audit history of new or unaudited projects before committing a large size.",
    "⚠️ Always know a position's liquidation price in advance so there are no surprises.",
    "⚠️ Longing/shorting correlated assets (like BTC and most altcoins) together doubles your risk.",
    "⚠️ Write down your max daily and weekly loss limit and stick to it strictly.",
    "⚠️ Use a trailing stop when a trade is in profit, to avoid missing a big move while locking in gains.",
    "⚠️ Never put all your capital into one entry — keep some reserve to average if needed.",
    "⚠️ Automatically reduce position size during high volatility (big news, low-liquidity hours).",
    "⚠️ Never move your Stop Loss further away just because you're watching a loss grow — it destroys disciplined risk management.",
    "⚠️ Use multiple exchanges so one exchange going down or getting hacked doesn't strand all your funds.",
    "⚠️ Keep your phone and laptop updated and avoid suspicious links — phishing scams are common.",
    "⚠️ Always verify a new token's contract address from an official source — fake tokens are common.",
    "⚠️ Double-check the withdrawal address before sending — money sent to the wrong address can't be recovered.",
    "⚠️ Never share your seed phrase with any website, app or person, even if they claim to be support.",
    "⚠️ High-leverage futures aren't for beginners — build confidence on spot first, then try leverage.",
    "⚠️ Keep your risk per trade fixed regardless of your mood — don't change size based on emotions.",
    "⚠️ Very short timeframes (1m, 5m) have more noise and more false signals — be careful.",
    "⚠️ Regularly review your open positions — if your original thesis changes, adjust the position too.",
    "⚠️ Never think 'everything in one coin' no matter how good that coin looks.",
    "⚠️ Check an exchange's withdrawal limits and fees beforehand — finding out in an emergency isn't ideal.",
    "⚠️ Don't change your risk tolerance based on market mood — it should stay fixed, decided in advance.",
    "⚠️ Stay away from any 'guaranteed profit' scheme — that doesn't exist in trading.",
    "⚠️ Rebalance your portfolio every few months so one asset's weight doesn't grow too large.",
    "⚠️ Always have an emergency exit plan ready — what will you do if internet or the exchange goes down.",
    "⚠️ Holding high-funding-rate leveraged positions for a long time can get expensive.",
    "⚠️ Increase your trade size gradually only once confidence and consistency are both established — don't rush.",
    "⚠️ Always check daily volume before committing a large size to low market-cap coins.",
    "⚠️ Write down your risk management plan and re-read it before every trade.",
    "⚠️ Never take a bigger-than-normal size just because 'this time it'll definitely work'.",
    "⚠️ Split your trading capital into small portions (e.g. 10 parts) so one mistake doesn't wipe you out.",
    "⚠️ Always set your Take Profit in advance too — don't skip booking profit just because 'it'll go higher'.",
    "⚠️ A 'good' trade means following the plan, not necessarily profit. Following the plan despite a loss is still great risk management.",
    "⚠️ Never remove your Stop Loss just because you think 'it'll definitely bounce back'.",
    "⚠️ New traders should practice with demo or very small amounts first, not jump straight to big size.",
    "⚠️ Never let your total exposure (combined risk of all open positions) exceed about 10% of your capital.",
    "⚠️ Keeping all your savings on one exchange is risky — hacks or freezes are always a possibility.",
    "⚠️ Even when using a trading bot or signal service, manually set your own Stop Loss and position size.",
    "⚠️ Price can move oddly on weekends and low-liquidity hours — keep position size smaller then.",
    "⚠️ Think in terms of risk per trade as a percentage, not a dollar amount, so size auto-adjusts as capital grows or shrinks.",
    "⚠️ If you lose 3 trades in a row in a week, pause and review your strategy.",
    "⚠️ React immediately to a margin call or liquidation warning — ignoring it can wipe out the whole position.",
    "⚠️ Don't buy a token just because its name resembles a famous coin's.",
    "⚠️ When giving a trading bot your exchange API key, only grant 'trade' permission, never 'withdraw'.",
    "⚠️ Keep high-leverage short-term trades and long-term investments in separate sections of your portfolio.",
    "⚠️ Never place a trade double your normal size just to try to recover a loss.",
    "⚠️ Volatility is very high in the first hour after a new listing — avoid committing large size right away.",
    "⚠️ Your risk isn't just market risk — keep exchange risk and regulatory risk in mind too.",
    "⚠️ If you can't remember the reasoning for a trade, close it — holding a position without a reason is a risk.",
    "⚠️ Think through a periodic 'stress test' of your portfolio: if BTC drops 30%, how much would my whole portfolio fall?",
    "⚠️ Never take a big-size trade based on a single indicator alone — look for multiple confirmations.",
    "⚠️ Include trading fees and funding rate in your risk-reward calculation — they can matter on small trades.",
    "⚠️ When the market shows extreme greed, reduce your risk below normal — sharp reversals often happen then.",
    "⚠️ When the market shows extreme fear, still don't rush into large size — a falling price can fall further.",
    "⚠️ Before every trade, think through the worst-case scenario: how much would my portfolio be hit if SL is hit?",
    "⚠️ Never put 100% of your capital on a single signal or tip, no matter how confident it seems.",
    "⚠️ Keep your risk management rules written down and review them once a month to improve.",
    "⚠️ Too many open pending limit orders left forgotten risks accidental execution — keep your list clean.",
    "⚠️ Decide your exit strategy before entering — deciding mid-trade is usually emotional.",
    "⚠️ Never put all your profit straight back into a new trade — withdraw and secure a portion.",
    "⚠️ Place your Stop Loss based on chart structure (support/resistance), not a random number.",
    "⚠️ Keep the stablecoin portion of your portfolio flexible with the market — you don't need to be 100% invested all the time.",
    "⚠️ Test with a small amount before depositing on a new exchange to confirm withdrawals work properly.",
    "⚠️ Use a separate bank account or wallet for trading — don't mix personal and trading money.",
    "⚠️ Chasing a coin that suddenly pumps and entering late is often a big risk.",
    "⚠️ Double-check your leverage settings on every exchange — default leverage is sometimes surprisingly high.",
    "⚠️ Check a new DeFi protocol's TVL (Total Value Locked) and history before committing a large amount.",
    "⚠️ Your risk isn't limited to price movement — smart contract bugs and hacks are a big risk in DeFi too.",
    "⚠️ Never try to close your entire position at a single price — scale out gradually instead.",
    "⚠️ Only trade with the portion of your capital you won't need for the next 6 months.",
    "⚠️ Keep high-risk, high-reward coins (micro caps) to only a small percentage of your total portfolio.",
    "⚠️ Don't limit risk management to crypto alone — keep your other investments (savings, gold) in mind too.",
    "⚠️ When the market has very high leverage (high open interest), sudden liquidation cascades become more likely — be careful.",
    "⚠️ Review your trading plan calmly after a big loss, rather than changing it emotionally.",
    "⚠️ Taking unlimited risk on a coin because 'it can never go down' is a big danger.",
    "⚠️ Every open position should have a clear invalidation point (where the thesis is proven wrong).",
    "⚠️ Account for an exchange's 24-48 hour security hold on large withdrawals — don't plan at the last minute.",
    "⚠️ Don't build your entire strategy on one timeframe — check both bigger and smaller charts.",
    "⚠️ Avoid logging into your trading app on public wifi — use a private, secure connection.",
    "⚠️ Don't save your password or seed phrase in plain text in a cloud notes app.",
    "⚠️ New high-leverage products (like 100x) are very dangerous for beginners — best to avoid them.",
]

CONTENT_SEQUENCE = [
    "overview", "technical", "education", "coin", "psychology",
    "news", "risk", "gainers", "summary",
]

TIP_STYLES = {
    "education": {"emoji": "📚", "label": "Education Tip", "color": "#1d3557", "pool": EDUCATION_TIPS},
    "psychology": {"emoji": "🧠", "label": "Psychology Tip", "color": "#5b21b6", "pool": PSYCHOLOGY_TIPS},
    "risk": {"emoji": "⚠️", "label": "Risk Management Tip", "color": "#b45309", "pool": RISK_TIPS},
}


def post_tip(data, category):
    style = TIP_STYLES[category]
    i = next_from_bag(data, category, len(style["pool"]))
    tip = style["pool"][i]
    body = tip.split(" ", 1)[1] if " " in tip else tip  # emoji prefix hata do
    img = make_tip_card(style["label"], style["color"], body)
    caption = f"<b>{style['emoji']} {style['label']}</b>\n\n{body}\n\n<i>Strexx Crypto Signals</i>"
    send_telegram_photo(img, caption)


def next_from_bag(data, category, pool_len):
    """Har category ki tips bina repeat kiye baari baari deta hai (poori list khatam hone tak)."""
    bag = data.setdefault("tip_bag", {})
    remaining = bag.get(category) or []
    if not remaining:
        remaining = list(range(pool_len))
        random.shuffle(remaining)
    idx = remaining.pop(0)
    bag[category] = remaining
    return idx


def post_market_overview():
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "bitcoin,ethereum", "vs_currencies": "usd",
                    "include_24hr_change": "true", "include_market_cap": "true"},
            timeout=15,
        ).json()
        btc, eth = r["bitcoin"], r["ethereum"]
        fng = requests.get("https://api.alternative.me/fng/?limit=1", timeout=15).json()
        fng_val = fng["data"][0]["value"]
        fng_label = fng["data"][0]["value_classification"]
        text = (
            "<b>🔥 Market Overview</b>\n\n"
            f"₿ <b>BTC:</b> ${btc['usd']:,.0f}  <i>({btc['usd_24h_change']:+.2f}% 24h)</i>\n"
            f"Ξ <b>ETH:</b> ${eth['usd']:,.0f}  <i>({eth['usd_24h_change']:+.2f}% 24h)</i>\n"
            f"😨 <b>Fear &amp; Greed:</b> {fng_val} ({fng_label})\n\n"
            "<i>Not financial advice.</i>"
        )
        path = make_chart("BTCUSDT", "BTC/USDT - 1H")
        send_telegram_photo(path, text)
    except Exception as e:
        log(f"Market overview error: {e}")


def post_technical_analysis():
    try:
        symbol = random.choice(["BTCUSDT", "ETHUSDT"])
        df = get_klines(symbol, "1h", 200).iloc[:-1]
        c = df["close"]
        r = rsi(c)
        macd_line = ema(c, 12) - ema(c, 26)
        hist = macd_line - ema(macd_line, 9)
        rsi_val = r.iloc[-1]
        rsi_tag = "Overbought" if rsi_val > 70 else "Oversold" if rsi_val < 30 else "Neutral"
        macd_tag = "Bullish" if hist.iloc[-1] > 0 else "Bearish"
        support = df["low"].tail(30).min()
        resistance = df["high"].tail(30).max()
        name = symbol[:-4] + "/USDT"
        text = (
            f"<b>📊 Technical Analysis — {name}</b>\n\n"
            f"📈 <b>RSI(14):</b> {rsi_val:.1f}  <i>({rsi_tag})</i>\n"
            f"📉 <b>MACD:</b> {macd_tag}\n"
            f"🟢 <b>Support:</b> {fmt_price(support)}\n"
            f"🔴 <b>Resistance:</b> {fmt_price(resistance)}\n\n"
            "<i>Not financial advice.</i>"
        )
        path = make_chart(symbol, f"{name} - 1H")
        send_telegram_photo(path, text)
    except Exception as e:
        log(f"Technical analysis error: {e}")


def post_coin_analysis():
    try:
        coins = get_top_coins()
        symbol = random.choice(coins[5:30]) if len(coins) > 30 else random.choice(coins)
        df4 = get_klines(symbol, "4h", 250).iloc[:-1]
        side = trend_of(df4) or "Sideways"
        df = get_klines(symbol, "1h", 200).iloc[:-1]
        c = df["close"]
        r = rsi(c).iloc[-1]
        chg = (c.iloc[-1] - c.iloc[-25]) / c.iloc[-25] * 100
        name = symbol[:-4] + "/USDT"
        text = (
            f"<b>🔍 Coin Analysis — {name}</b>\n\n"
            f"📊 <b>4H Trend:</b> {side}\n"
            f"⏱ <b>24h Change:</b> {chg:+.2f}%\n"
            f"📈 <b>RSI(14):</b> {r:.1f}\n"
            f"💰 <b>Price:</b> {fmt_price(c.iloc[-1])}\n\n"
            "<i>Not financial advice.</i>"
        )
        path = make_chart(symbol, f"{name} - 1H")
        send_telegram_photo(path, text)
    except Exception as e:
        log(f"Coin analysis error: {e}")


def post_news(data):
    try:
        r = requests.get("https://www.coindesk.com/arc/outboundfeeds/rss/", timeout=15)
        root = ET.fromstring(r.content)
        posted = set(data.setdefault("posted_news", []))
        for item in root.iter("item"):
            title = item.findtext("title", "").strip()
            link = item.findtext("link", "").strip()
            if title and title not in posted:
                img = make_news_card(title)
                caption = f"<b>📰 Crypto News</b>\n\n{title}\n\n🔗 {link}"
                send_telegram_photo(img, caption)
                posted.add(title)
                data["posted_news"] = list(posted)[-100:]
                return
        log("News: koi nayi headline nahi mili")
    except Exception as e:
        log(f"News error: {e}")


def post_daily_summary(data):
    try:
        is_weekly = datetime.now(timezone.utc).weekday() == 0  # Monday
        stats = data["stats"]
        title = "🌙 Weekly Market Recap" if is_weekly else "📋 Daily Market Summary"
        text = (
            f"<b>{title}</b>\n\n"
            f"📂 Open signals: {len(data['open'])}\n"
            f"{stats_line(stats)}\n\n"
            "<i>Not financial advice.</i>"
        )
        path = make_chart("BTCUSDT", "BTC/USDT - 1H")
        send_telegram_photo(path, text)
    except Exception as e:
        log(f"Summary error: {e}")


def post_gainers_losers():
    try:
        if datetime.now(timezone.utc).weekday() != 0:  # sirf Monday = weekly
            log("Gainers/losers: skip, aaj Monday nahi hai")
            return
        raw = api_get("/api/v3/ticker/24hr")
        rows = []
        for t in raw:
            sym = t["symbol"]
            if not sym.endswith("USDT") or sym[:-4] in EXCLUDE:
                continue
            try:
                chg = float(t["priceChangePercent"])
                vol = float(t["quoteVolume"])
            except (KeyError, ValueError):
                continue
            if vol < 5_000_000:  # bohat illiquid coins hata do (noisy % moves)
                continue
            rows.append((sym[:-4], chg))
        if not rows:
            return
        rows.sort(key=lambda x: -x[1])
        gainers = rows[:5]
        losers = rows[-5:][::-1]
        g_txt = "\n".join(f"🟢 {s}: {c:+.2f}%" for s, c in gainers)
        l_txt = "\n".join(f"🔴 {s}: {c:+.2f}%" for s, c in losers)
        text = (
            "<b>🏁 Weekly Top Gainers &amp; Losers</b>\n\n"
            f"<b>📈 Top Gainers</b>\n{g_txt}\n\n"
            f"<b>📉 Top Losers</b>\n{l_txt}\n\n"
            "<i>Based on 24h change (min $5M volume). Not financial advice.</i>"
        )
        img = _text_card("Top Gainers & Losers", "Weekly market movers", "#0e7490", wrap=40)
        send_telegram_photo(img, text)
    except Exception as e:
        log(f"Gainers/losers error: {e}")


def check_new_listings(data):
    """Har run mein Binance par nayi USDT listing check karta hai."""
    try:
        info = api_get("/api/v3/exchangeInfo")
        current = set()
        for s in info.get("symbols", []):
            if s.get("status") == "TRADING" and s["symbol"].endswith("USDT") and s["symbol"][:-4] not in EXCLUDE:
                current.add(s["symbol"])

        known = data.get("known_symbols")
        if known is None:
            data["known_symbols"] = list(current)  # pehli baar: sirf baseline banao, alert mat karo
            log(f"New-listing baseline set: {len(current)} symbols")
            return

        new_syms = current - set(known)
        for sym in sorted(new_syms):
            name = sym[:-4] + "/USDT"
            text = (
                f"<b>🆕 NEW LISTING — {name}</b>\n\n"
                f"Just started trading on Binance. Expect high volatility — trade carefully!"
            )
            img = _text_card("New Listing", f"{name} is now live on Binance", "#7c3aed", wrap=34)
            send_telegram_photo(img, text)
            time.sleep(1)
        data["known_symbols"] = list(current)
    except Exception as e:
        log(f"New listing check error: {e}")


def post_content(data):
    idx = data.get("content_idx", 0) % len(CONTENT_SEQUENCE)
    kind = CONTENT_SEQUENCE[idx]
    data["content_idx"] = idx + 1
    log(f"Content post: {kind}")
    try:
        if kind == "overview":
            post_market_overview()
        elif kind == "technical":
            post_technical_analysis()
        elif kind == "coin":
            post_coin_analysis()
        elif kind == "news":
            post_news(data)
        elif kind == "summary":
            post_daily_summary(data)
        elif kind == "gainers":
            post_gainers_losers()
        elif kind == "education":
            post_tip(data, "education")
        elif kind == "psychology":
            post_tip(data, "psychology")
        elif kind == "risk":
            post_tip(data, "risk")
    except Exception as e:
        log(f"Content post error ({kind}): {e}")


# ============================================================
#  DATA SAVE / LOAD
# ============================================================


def load_data():
    default = {
        "open": [],
        "last_sent": {},
        "stats": {"wins": 0, "losses": 0, "expired": 0},
        "last_scan_hour": 0,
        "last_content_time": 0,
        "content_idx": 0,
        "tip_bag": {},
        "posted_news": [],
        "history": [],
        "sent_today": {},
        "dom_hist": [],
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
    return f"📊 <b>Record:</b> {stats['wins']}W / {stats['losses']}L  ({rate:.0f}% win rate)"


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
                text = (
                    f"<b>🎯 TP1 HIT — {name} {s['side']}</b>\n\n"
                    f"Move SL to <b>entry ({fmt_price(s['entry'])})</b> now. Risk-free trade! 🛡️"
                )
                img = _text_card("TP1 Hit", f"{name} {s['side']} target 1 reached", "#15803d", wrap=36, badge="win")
                send_telegram_photo(img, text, reply_to_message_id=s.get("message_id"))
            still_open.append(s)
        else:
            if state == "sl":
                data["stats"]["losses"] += 1
                header, color, badge = "Stop Loss Hit", "#7f1d1d", "loss"
                text = f"<b>❌ {header} — {name} {s['side']}</b>"
            elif state == "tp2":
                data["stats"]["wins"] += 1
                header, color, badge = "TP2 Hit - Full Target", "#166534", "win"
                text = f"<b>🏆 {header} — {name} {s['side']}</b> ✅✅"
            elif state == "be":
                data["stats"]["wins"] += 1
                header, color, badge = "Closed at Breakeven", "#14532d", "win"
                text = f"<b>✅ {header} — {name} {s['side']}</b>\nTP1 was reached, then closed at entry."
            elif state == "tp1_close":
                data["stats"]["wins"] += 1
                header, color, badge = "Closed after TP1", "#166534", "win"
                text = f"<b>✅ {header} — {name} {s['side']}</b>\nClosed on time-out."
            else:  # expired
                data["stats"]["expired"] += 1
                header, color, badge = "Signal Expired", "#334155", "neutral"
                text = f"<b>⏱ {header} — {name} {s['side']}</b>\nNo target reached within {EXPIRE_H}h."
            log_result(s, state)
            data.setdefault("history", []).append({
                "symbol": s["symbol"], "side": s["side"], "score": s.get("score"),
                "tags": s.get("tags", []), "regime": s.get("regime"), "result": state,
                "opened": int(s["time"] / 1000), "closed": int(time.time()), "day": today_key(),
            })
            data["history"] = data["history"][-1000:]
            full_text = text + "\n\n" + stats_line(data["stats"])
            img = _text_card(header, f"{name} {s['side']}", color, wrap=36, badge=badge)
            send_telegram_photo(img, full_text, reply_to_message_id=s.get("message_id"))
        time.sleep(0.2)
    data["open"] = still_open


# ============================================================
#  MAIN SCAN
# ============================================================


def scan_for_signals(data):
    REJECTS.clear()
    _FUT["ok"] = True
    allowed, reason, min_score = protection_state(data)
    if not allowed:
        log(f"NO TRADE (protection): {reason}")
        return
    ctx = build_context(data)
    if not ctx["ok"]:
        log(f"NO TRADE (market conditions): {', '.join(ctx['reasons'])}")
        return
    log(f"Context: BTC 4H={ctx['btc_bias']} 1D={ctx['btc_1d']} ETH/BTC={ctx['eth_btc']} "
        f"dom_change={ctx['dom_change']} mkt24h={ctx['mkt_change']} | min_score={min_score}")

    coins = get_top_coins()
    log(f"Scan started: {len(coins)} coins (advanced SMC engine)")
    tag_stats, reg_stats = adaptive_stats(data)

    now = time.time()
    open_syms = {s["symbol"] for s in data["open"]}
    found = []
    checked = 0
    errors = 0
    first_tb = None
    for sym in coins:
        if sym in open_syms:
            continue
        if now - data["last_sent"].get(sym, 0) < COOLDOWN_H * 3600:
            continue
        checked += 1
        try:
            sig = analyze(sym, ctx, data, min_score, tag_stats, reg_stats)
            if sig:
                found.append(sig)
        except Exception as e:
            errors += 1
            if first_tb is None:
                first_tb = traceback.format_exc()
            log(f"Analyze error {sym}: {e}")
        time.sleep(0.15)
    log(f"Coins actually analyzed: {checked} | Exceptions: {errors}")
    if first_tb:
        log("First exception traceback:\n" + first_tb)

    # Fallback pass: advanced strategy bohat sakht hai, is liye agar wo signal na de
    # to simpler trend+momentum ('BASIC') signals se gap bhara jata hai (LONG/SHORT dono).
    adv_syms = {s["symbol"] for s in found}
    if len(found) < MAX_SIGNALS_PER_SCAN:
        basic_found = 0
        for sym in coins:
            if len(found) >= MAX_SIGNALS_PER_SCAN * 2:
                break
            if sym in open_syms or sym in adv_syms:
                continue
            if now - data["last_sent"].get(sym, 0) < COOLDOWN_H * 3600:
                continue
            sig = analyze_basic(sym)
            if sig:
                found.append(sig)
                basic_found += 1
            time.sleep(0.1)
        log(f"Fallback (BASIC) pass added: {basic_found} signals")

    found.sort(key=lambda x: -x["rank"])
    day = today_key()
    sent_today = data.setdefault("sent_today", {})
    quota = max(0, MAX_SIGNALS_PER_DAY - sent_today.get(day, 0))
    same_dir = Counter(s["side"] for s in data["open"])
    sent = 0
    for sig in found:
        if sent >= min(MAX_SIGNALS_PER_SCAN, quota):
            break
        if same_dir[sig["side"]] >= MAX_SAME_DIR_OPEN:
            log(f"Skip {sig['symbol']}: {MAX_SAME_DIR_OPEN} {sig['side']} signals already open (correlation protection)")
            continue
        try:
            path = make_chart(sig["symbol"], f"{sig['symbol'][:-4]}/USDT - Entry Signal")
            msg_id = send_telegram_photo(path, signal_message(sig))
        except Exception as e:
            log(f"Chart error {sig['symbol']}: {e}")
            msg_id = send_telegram(signal_message(sig))
        if msg_id:
            sig["message_id"] = msg_id if isinstance(msg_id, int) else None
            data["open"].append(sig)
            data["last_sent"][sig["symbol"]] = now
            sent_today[day] = sent_today.get(day, 0) + 1
            same_dir[sig["side"]] += 1
            sent += 1
            save_data(data)
        time.sleep(1)
    for k in sorted(sent_today)[:-7]:
        del sent_today[k]
    top = ", ".join(f"{r} x{c}" for r, c in REJECTS.most_common(6))
    log(f"Scan finished: {len(found)} setups passed, {sent} signals sent")
    log(f"Top rejection reasons: {top or 'none'}")


def run_once(force=False):
    data = load_data()
    check_open_signals(data)
    try:
        check_new_listings(data)
    except Exception as e:
        log(f"New listing outer error: {e}")
    # 15M/5M strategy ke liye har run (~15 min) scan hota hai (minimum 10 min gap)
    if force or time.time() - data.get("last_scan_time", 0) >= 600:
        scan_for_signals(data)
        data["last_scan_time"] = time.time()
    save_data(data)

    if CONTENT_ENABLED:
        now = time.time()
        if now - data.get("last_content_time", 0) >= CONTENT_INTERVAL_H * 3600:
            post_content(data)
            data["last_content_time"] = now
            save_data(data)


def main():
    arg = sys.argv[1].lower() if len(sys.argv) > 1 else ""

    if arg == "test":
        ok = send_telegram("✅ Test message: the bot is working fine!")
        log("Test message sent" if ok else "Test failed, check token/chat id")
        return

    if arg == "scan":
        run_once(force=True)
        return

    if arg == "once":
        run_once()
        return

    log("Bot start ho gaya. Band karne ke liye Ctrl+C dabao.")
    send_telegram("🤖 Signal bot has started.")
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
