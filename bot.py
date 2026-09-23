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
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests
import pandas as pd
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


def send_telegram(text, parse_mode="HTML"):
    if BOT_TOKEN.startswith("YAHAN"):
        print("\n[DRY RUN - token set nahi hai, sirf screen par dikha raha hun]\n" + text + "\n")
        return True
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text, "parse_mode": parse_mode},
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


def send_telegram_photo(path, caption, parse_mode="HTML"):
    if BOT_TOKEN.startswith("YAHAN"):
        print(f"\n[DRY RUN - photo] {caption}\n")
        return True
    try:
        with open(path, "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                data={"chat_id": CHAT_ID, "caption": caption, "parse_mode": parse_mode},
                files={"photo": f},
                timeout=30,
            )
        if not r.ok:
            log(f"Telegram photo error: {r.status_code} {r.text}")
            return False
        return True
    except Exception as e:
        log(f"Telegram photo error: {e}")
        return False


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


def _text_card(header, body, bg_color, footer="STREXX CRYPTO SIGNALS", wrap=42):
    """Colorful card image banata hai (tips/news ke liye), text ke saath."""
    import textwrap
    wrapped = "\n".join(textwrap.wrap(body, width=wrap))
    plt.style.use("dark_background")
    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=130)
    fig.patch.set_facecolor(bg_color)
    ax.set_facecolor(bg_color)
    ax.axis("off")
    ax.text(0.5, 0.90, header.upper(), ha="center", va="top", fontsize=21,
             fontweight="bold", color="white", transform=ax.transAxes)
    ax.text(0.5, 0.72, wrapped, ha="center", va="top", fontsize=14.5,
             color="#f5f5f5", linespacing=1.7, transform=ax.transAxes)
    ax.text(0.5, 0.05, footer, ha="center", va="bottom", fontsize=9.5,
             color="#dddddd", alpha=0.85, transform=ax.transAxes)
    fig.savefig(CHART_FILE, facecolor=bg_color)
    plt.close(fig)
    return CHART_FILE


def make_tip_card(label, color, body_text):
    return _text_card(label, body_text, color)


def make_news_card(headline):
    return _text_card("Crypto News", headline, "#7c2d12", wrap=36)


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
    icon = "🟢🚀" if s["side"] == "LONG" else "🔴📉"
    name = s["symbol"][:-4] + "/USDT"

    def pct(x):
        return f"{(x - s['entry']) / s['entry'] * 100:+.2f}%"

    return (
        f"<b>{icon} {s['side']} SIGNAL — {name}</b>\n"
        f"⏱ 1H entry (4H trend confirmed)\n\n"
        f"📍 <b>Entry:</b> {fmt_price(s['entry'])}\n"
        f"🛑 <b>Stop Loss:</b> {fmt_price(s['sl'])} <i>({pct(s['sl'])})</i>\n"
        f"🎯 <b>TP1:</b> {fmt_price(s['tp1'])} <i>({pct(s['tp1'])})</i>\n"
        f"🎯 <b>TP2:</b> {fmt_price(s['tp2'])} <i>({pct(s['tp2'])})</i>\n\n"
        f"⚖️ <b>Risk:Reward:</b> 1:{TP1_RR:g} / 1:{TP2_RR:g}\n"
        f"💪 <b>Strength:</b> {s['score']}/4\n\n"
        f"💡 TP1 par half profit lo, phir SL ko entry par le aao.\n"
        f"⚠️ Risk sirf 1-2% per trade, leverage kam rakho.\n\n"
        f"<i>Not financial advice.</i>"
    )


# ============================================================
#  EXTRA CONTENT (education, psychology, risk, news, market)
# ============================================================

EDUCATION_TIPS = [
    "📚 Support wo level hai jahan price baar baar rukti hai aur upar jati hai. Resistance is ka ulta hai.",
    "📚 RSI 70 se upar 'overbought' aur 30 se neeche 'oversold' mana jata hai, lekin strong trend mein ye kaafi der rukta nahi.",
    "📚 MACD do EMAs ka farq hai. Jab wo zero line ke upar cross kare to momentum bullish hota hai.",
    "📚 Candlestick ki lambi neeche wali baati (wick) aksar batati hai ke sellers haar gaye aur buyers wapas aaye.",
    "📚 Volume kisi bhi move ki 'sachai' batata hai. Bina volume ke breakout aksar fake hota hai.",
    "📚 Leverage sirf loss ko nahi, profit ko bhi barhata hai, lekin risk zyada tezi se barhta hai. Beginners kam leverage rakhein.",
    "📚 'Bull market' matlab prices lambay arse ke liye barh rahi hon, 'Bear market' iska ulta.",
    "📚 Liquidity zyada honay se buy/sell aasani se hoti hai aur price kam ajeeb tarah se hilti hai.",
    "📚 Stop Loss ek automatic order hai jo loss ko pehle se tay had tak mehdood rakhta hai.",
    "📚 Diversification matlab poora paisa ek hi coin mein na lagana, balke kai coins mein taqseem karna.",
    "📚 Market cap = price x total coins. Ye company ki size ka andaza deta hai, price akela nahi.",
    "📚 Fear & Greed Index batata hai market overall khaufzada hai ya lalchi. Extreme fear par aksar mauqay bante hain.",
    "📚 Doji candle wo hai jis ka open aur close lagbhag barabar ho, ye market mein indecision dikhati hai.",
    "📚 Hammer candle neeche lambi baati ke saath bante hai aur aksar downtrend ke aakhir mein reversal ka signal deta hai.",
    "📚 Engulfing pattern: nayi candle purani candle ko poora 'nigal' le, strong reversal ka ishara hota hai.",
    "📚 Trendline do ya zyada highs/lows ko jorne se banti hai aur trend ki direction dikhati hai.",
    "📚 Fibonacci retracement levels (23.6%, 38.2%, 50%, 61.8%) batatay hain price kahan wapas ruk sakti hai.",
    "📚 Bollinger Bands price ke aas paas volatility dikhatay hain. Bands tang hon to bara move aane wala ho sakta hai.",
    "📚 Moving Average (MA) purani prices ka average hoti hai jo trend ko smooth kar ke dikhati hai.",
    "📚 EMA (Exponential MA) recent prices ko zyada wazan deti hai, is liye SMA se tezi se react karti hai.",
    "📚 Market order foran current price par execute hoti hai, Limit order sirf tab jab aap ki di hui price aaye.",
    "📚 Stop-Limit order do hisson mein kaam karta hai: pehle trigger price, phir limit price par order jati hai.",
    "📚 Spot trading mein aap asli coin khareedtay hain, Futures mein aap price ki movement par bet lagatay hain.",
    "📚 Long position matlab price barhne par profit, Short position matlab price girne par profit.",
    "📚 Funding rate futures market mein Longs aur Shorts ke darmiyan periodic payment hoti hai jo price ko spot ke qareeb rakhti hai.",
    "📚 Open Interest kisi futures contract ke tamam khule positions ki total value hoti hai.",
    "📚 Liquidation tab hoti hai jab loss margin se zyada ho jaye aur exchange position force-close kar de.",
    "📚 Slippage wo farq hai jo aap ki expected price aur asal execution price ke darmiyan aata hai.",
    "📚 Order book buy aur sell orders ki list hoti hai jo bataati hai kis price par kitni demand/supply hai.",
    "📚 CEX (Centralized Exchange) jaise Binance ek company chalati hai, DEX (Decentralized Exchange) blockchain par khud kaam karta hai.",
    "📚 Hot wallet internet se juda hota hai (aasan lekin kam mehfooz), Cold wallet offline hota hai (mushkil lekin zyada mehfooz).",
    "📚 Private key kabhi kisi ko na do. Jis ke paas key hai, uske paas hi asal control hota hai.",
    "📚 Stablecoin (jaise USDT, USDC) ki value $1 ke qareeb fix rakhi jati hai, aam tor par reserves ki bunyad par.",
    "📚 DCA (Dollar Cost Averaging) matlab thora thora paisa waqfay waqfay se lagana, taake average price behtar bane.",
    "📚 HODL crypto community ki zaban mein 'long-term hold karna' ke liye mashhoor lafz ban gaya hai.",
    "📚 On-chain metrics jaise wallet activity aur exchange inflow/outflow market ka rukh samajhne mein madad detay hain.",
    "📚 BTC Dominance batata hai total crypto market cap mein Bitcoin ka hissa kitna hai.",
    "📚 Altcoin season wo waqt hai jab altcoins Bitcoin se behtar performance dikhatay hain.",
    "📚 Halving har 4 saal Bitcoin mining reward ko aadha kar deta hai, is se naye coins ki supply kam hoti hai.",
    "📚 Staking apne coins network ko lock kar ke reward kamane ka tareeqa hai.",
    "📚 DeFi (Decentralized Finance) bina bank ke lending, borrowing aur trading ki suhulat deta hai.",
    "📚 Layer 1 (jaise Ethereum) khud ki blockchain hoti hai, Layer 2 (jaise Arbitrum) uske upar bani hoti hai taake speed barhe.",
    "📚 Gas fee kisi bhi blockchain transaction ko process karne ki fees hoti hai.",
    "📚 ATH (All-Time High) sab se zyada price hoti hai jo coin ne kabhi chhuwi ho, ATL iska ulta.",
    "📚 Retracement matlab trend ke doran price ka thora peeche jana, is se pehle wo dobara aage barhti hai.",
    "📚 Breakout wo waqt hota hai jab price kisi resistance ko paar kar ke upar chali jaye.",
    "📚 Consolidation wo period hai jab price ek chhoti range mein idhar udhar hoti rahay, decision ka intezar.",
    "📚 Volatility batati hai price kitni tezi se upar neeche ho rahi hai, crypto stocks se zyada volatile hoti hai.",
    "📚 Crypto market aksar US stock market (khaas kar tech stocks) ke saath correlation rakhta hai.",
    "📚 Interest rate decisions (Fed) crypto par bhi asar dalte hain, kyunke ye overall risk appetite badalte hain.",
    "📚 Dollar Index (DXY) barhne se aksar crypto par dabao ata hai, kyunke dollar mazboot ho jata hai.",
    "📚 KYC (Know Your Customer) exchange ka wo process hai jis mein aap apni pehchan verify karatay hain.",
    "📚 Arbitrage do alag exchanges ke price farq se profit kamane ka tareeqa hai.",
    "📚 Whale wo bara investor hota hai jiske paas itna coin ho ke uska trade market ko hila sake.",
    "📚 Token Unlock wo waqt hai jab locked huay tokens market mein release hotay hain, ye supply barha deta hai.",
    "📚 Tokenomics kisi coin ki total supply, distribution aur use-case ka mutalea hai.",
    "📚 Circulating supply wo coins hain jo abhi market mein maujood hain, Total supply sab coins (locked bhi) shamil karti hai.",
    "📚 FDV (Fully Diluted Valuation) us waqt ki market cap hoti hai jab tamam tokens circulation mein aa jayein.",
    "📚 Correlation coefficient batata hai do assets aapas mein kitni milti julti move karte hain.",
    "📚 Trailing Stop Loss price ke sath sath khud upar (ya neeche) move hota hai taake profit lock ho sake.",
    "📚 Scalping bohat chhoti aur tezi se hone wali trades ka style hai, aam tor par minutes mein.",
    "📚 Swing trading kai din ya hafton tak position rakh kar bare moves pakarne ka style hai.",
    "📚 Position trading mahinon ya saalon ke liye hold karne ka style hai, jaisay long-term investors karte hain.",
    "📚 Chart pattern 'Head and Shoulders' aksar trend reversal ka signal deta hai.",
    "📚 Chart pattern 'Double Top/Bottom' batata hai price ek level ko do baar test kar ke wapas gayi.",
    "📚 Triangle pattern (ascending/descending/symmetrical) aksar breakout se pehle ki consolidation hoti hai.",
    "📚 ATR (Average True Range) kisi asset ki average daily volatility napta hai.",
    "📚 Ichimoku Cloud ek advanced indicator hai jo trend, support/resistance aur momentum ek saath dikhata hai.",
    "📚 Stochastic Oscillator RSI ki tarah hai, lekin ye closing price ko high-low range se compare karta hai.",
    "📚 VWAP (Volume Weighted Average Price) din bhar ki trading ka volume-adjusted average price hota hai.",
    "📚 Pump and Dump scheme mein log coin ki price artificially barhatay hain phir bech kar bhaag jatay hain, hoshiyar rahein.",
    "📚 Rug Pull tab hota hai jab kisi project ke developers achanak sara liquidity nikal kar ghayab ho jayein.",
    "📚 Smart contract ek self-executing code hai jo blockchain par pehle se tay sharait ke mutabiq chalta hai.",
    "📚 NFT (Non-Fungible Token) ek unique digital asset hai jo blockchain par record hota hai.",
    "📚 Airdrop wo free tokens hain jo projects apne early users ya holders ko deti hain.",
    "📚 Testnet ek trial blockchain hoti hai jahan asal paisay ke bagair testing hoti hai.",
    "📚 Mainnet blockchain ka asal, live version hai jahan asal transactions hoti hain.",
    "📚 Cross-chain bridge alag alag blockchains ke darmiyan assets transfer karne ki suhulat deta hai.",
    "📚 Impermanent Loss DeFi liquidity providing mein hone wala ek risk hai jab dono assets ki price alag alag move karein.",
    "📚 Yield Farming apne crypto assets ko lend/stake kar ke extra reward kamane ka tareeqa hai.",
    "📚 Oracle blockchain ko bahar ki duniya (jaise real prices) ka data faraham karta hai.",
    "📚 Consensus mechanism (jaise Proof of Work, Proof of Stake) blockchain par transactions verify karne ka tareeqa hai.",
    "📚 Proof of Work miners ko computing power laga kar naye blocks banane par majboor karta hai (jaise Bitcoin).",
    "📚 Proof of Stake validators ko coins 'stake' karne par blocks banane deta hai, kam energy use hoti hai.",
    "📚 Wallet address ek public location hai jahan crypto bheja ya wahan se receive kiya ja sakta hai.",
    "📚 Seed phrase (12-24 lafz) aap ke wallet ki master key hai. Ise likh kar mehfooz jagah rakhein, kisi ko na dikhayein.",
    "📚 2FA (Two-Factor Authentication) account ki security ki ek extra layer hai, hamesha on rakhein.",
    "📚 Phishing scam mein fake websites ya links se log aap ka password ya keys churatay hain, links check kar ke click karein.",
    "📚 Contract address verify karna zaroori hai, kyunke bohat se scam coins asal coin ka naam copy kar letay hain.",
    "📚 Audit report batati hai kisi project ka smart contract security experts ne check kiya hai ya nahi.",
    "📚 Circulating vs Locked supply ka farq samajhna zaroori hai taake unlock events ka andaza ho sake.",
    "📚 Seasonality: kuch mahine (jaise September) historically crypto ke liye kamzor rahe hain, lekin ye guarantee nahi.",
    "📚 Correlation break tab hoti hai jab crypto achanak stocks se alag direction mein move kare, ye khaas waqt hota hai.",
    "📚 Market maker bara volume trade kar ke market mein liquidity faraham karta hai.",
    "📚 Order flow dekhna matlab bara buy/sell orders ka pattern samajhna jo price ko move karta hai.",
    "📚 Divergence tab hoti hai jab price naya high banaye lekin indicator (jaise RSI) naya high na banaye, ye weakness ka signal hai.",
    "📚 Golden Cross tab hota hai jab chhoti EMA (jaise 50) bari EMA (jaise 200) ko upar cross kare, bullish signal.",
    "📚 Death Cross Golden Cross ka ulta hai, chhoti EMA bari EMA ko neeche cross kare, bearish signal.",
    "📚 Wyckoff Accumulation/Distribution theory batati hai bare players kis tarah chupke se position banatay ya nikaaltay hain.",
]

PSYCHOLOGY_TIPS = [
    "🧠 FOMO (chootne ka dar) sab se bara dushman hai. Jab sab log ek coin ke baare mein baat karein, tab hi entry lena khatarnak hota hai.",
    "🧠 Ek trade ka loss aap ki poori strategy ko ghalat nahi banata. Har strategy mein losses hotay hain, poora record dekho.",
    "🧠 Revenge trading (ghussay mein trade lagana) aksar bara nuksan deta hai. Loss ke baad thora waqt lo.",
    "🧠 Plan pehle banao, trade baad mein lo. Trade ke doran plan badalna aksar emotions ki wajah se hota hai.",
    "🧠 Har trade jeetna zaroori nahi. Achi risk-management ke saath 50% win rate bhi profitable ho sakta hai.",
    "🧠 Overtrading (zyada trades lena) aksar boriyat ya greed se hoti hai, profit se nahi.",
    "🧠 Jab profit mein ho to greed control karo, jab loss mein ho to panic control karo. Dono emotions nuksan dete hain.",
    "🧠 Trading journal rakho: har trade ki wajah likho. Ye aap ko apni ghaltiyan samajhne mein madad karta hai.",
    "🧠 Market kisi ka intezar nahi karta. Ek acha setup miss ho jaye to agla dhoondo, us mein zabardasti entry mat lo.",
    "🧠 Sabar (patience) trading ka sab se underrated skill hai. Best trades aksar intezar ke baad aatay hain.",
    "🧠 FUD (Fear, Uncertainty, Doubt) social media par phailti hai. Panic mein bikri karne se pehle khud research karo.",
    "🧠 Confirmation bias se bacho: sirf wo khabrein mat dhoondo jo aap ki soch se milti hon, doosri taraf bhi dekho.",
    "🧠 Sunk cost fallacy: sirf is liye kisi ghatay wali position mein rehna ke pehle hi bohat paisa laga chuke ho, ghalat soch hai.",
    "🧠 Anchoring bias tab hoti hai jab aap kisi purani price par atak jayein aur asal situation ko nazar-andaz karein.",
    "🧠 Herd mentality se bacho: sirf is liye buy/sell mat karo ke baaqi sab log kar rahe hain.",
    "🧠 Analysis paralysis tab hoti hai jab itni zyada info ho ke faisla karna mushkil ho jaye. Simple plan par qayam raho.",
    "🧠 Ek acha trade karne ke baad overconfidence se bacho, agli trade bhi utni hi disciplined rakho.",
    "🧠 Accept karo ke losses trading ka hissa hain. Jo trader losses ko personal nahi leta, wo lambay arse tak tikta hai.",
    "🧠 Social media hype se real research alag cheez hai. Apni khud ki analysis par bharosa karo.",
    "🧠 Doosron ke profits se apne aap ko compare mat karo, har kisi ka risk aur capital alag hota hai.",
    "🧠 Realistic expectations rakho. Raatoraat ameer honay ka khayal aksar bare losses ki taraf le jata hai.",
    "🧠 Jab bohat gussa ya udaas ho, tab trading se door raho. Emotions clear honay ka intezar karo.",
    "🧠 Thoray thoray break lena dimaag ko fresh rakhta hai aur ghalat faislon se bachata hai.",
    "🧠 Neend poori na hona faisla karne ki salahiyat kam kar deta hai, aram poora karo phir trade karo.",
    "🧠 Har pump ke peeche mat bhaago. Jo move chhoot gaya, wo chhoot gaya, agla mauqa aayega.",
    "🧠 Apna daily loss limit tay karo aur us se aagay us din trading band kar do.",
    "🧠 Process ko celebrate karo, sirf profit ko nahi. Achi discipline se ki gayi losing trade bhi 'achi trade' ho sakti hai.",
    "🧠 Average down (girti hui position mein aur paisa lagana) sirf plan ka hissa ho, umeed ka nateeja nahi.",
    "🧠 Ek losing position ko sirf 'wapas aa jayega' soch kar pakre rehna, discipline ki kami hai.",
    "🧠 Trading mein 'main hamesha sahi hoon' wali soch khatarnak hai. Market se seekhte raho, ego ko darmiyan mein mat lao.",
    "🧠 Apni comfort zone se bara risk lena aksar panic decisions ki taraf le jata hai. Apni limit pehchano.",
    "🧠 Perfectionism se bacho: koi bhi strategy 100% perfect nahi hoti. 'Achi kaafi hai' wali soch behtar hai.",
    "🧠 Loss aane par foran doosri trade lagana galti hai. Pehle wajah samjho, phir aage barho.",
    "🧠 Trading sirf numbers ka khel nahi, apne emotions ko manage karna bhi utna hi zaroori hai.",
    "🧠 Kisi bhi single trade ka nateeja aap ki overall skill ka faisla nahi karta. Bara sample size dekho.",
    "🧠 Boredom trading (sirf is liye trade lagana ke kuch karne ko chahiye) aksar accounts khatam kar deti hai.",
    "🧠 Apne rules likh kar rakho aur unhein follow karo, chahe emotions kuch bhi kahein.",
    "🧠 Achi trading boring hoti hai. Excitement dhoondne wale traders aksar zyada risk le letay hain.",
    "🧠 Har trade se pehle khud se poocho: 'Agar ye loss ho jaye, to kya main is se theek rahunga?'",
    "🧠 Market ko 'harana' ka khayal chhor do, sirf apni strategy ko consistently follow karna hi maqsad hona chahiye.",
    "🧠 Impulsive entries se bacho. Kam az kam ek baar apne plan ko dobara padh lo phir click karo.",
    "🧠 Jab market chhoti si move kare aur aap bar bar chart dekhein, to ye anxiety ki nishani hai. Screen time kam karo.",
    "🧠 Doosron ki 'guaranteed profit' wali calls par andha bharosa mat karo, khud verify karo.",
    "🧠 Apni galtiyon ko chhupane ki bajaye unhein likho aur unse seekho, journal isi liye hota hai.",
    "🧠 Patience aur discipline ke bagair best strategy bhi fail ho jati hai.",
    "🧠 Ek bara win milne ke baad position size achanak barhana overconfidence ki nishani hai, control rakho.",
    "🧠 Loss ke baad turant recover karne ki koshish (jaldi mein bara trade lagana) aksar aur nuksan deti hai.",
    "🧠 Apni trading ko job ki tarah lo, casino ki tarah nahi. Plan, execute, review — bas.",
    "🧠 Jitna zyada aap news headlines par react karo ge, utna zyada emotional trading hogi. Apne setup par focus rakho.",
    "🧠 Kabhi bhi doosron ke pressure mein aa kar apna trading plan mat badlo.",
    "🧠 Apna khud ka mizaj (risk tolerance) pehchano. Jo strategy doosray ke liye acha hai zaroori nahi aap ke liye bhi ho.",
    "🧠 Trading break lena kamzori nahi, samajhdari hai. Thak jane par dimagh ghalat faisle karta hai.",
    "🧠 Ek din mein bohat zyada charts dekhna (overanalysis) confusion aur ghalat entries ki wajah banta hai.",
    "🧠 Loss ko 'seekhne ki fees' samjho, na ke apni izzat ka masla.",
    "🧠 Apni win rate ko 100% banane ki koshish mat karo, behtareen traders bhi kaafi trades haartay hain.",
    "🧠 Jab market bohat quiet ho to bhi trade dhoondnay ki zaroorat nahi, sabar rakho.",
    "🧠 Apni trading history dekh kar pattern pehchano: kya aap zyada tar loss thakawat ya jaldbaazi mein karte ho?",
    "🧠 Ek acha trader apni ghaltiyan chhupata nahi, unhein accept kar ke sudharta hai.",
    "🧠 Doosron ko apni open positions dikhane ka lalach control karo, ye faisle par emotional pressure dalta hai.",
    "🧠 Har green candle par excited hona aur har red candle par pareshan hona normal hai, lekin usay control mein rakho.",
    "🧠 Apna 'why' yaad rakho: aap trading kyun kar rahe ho, ye clarity mushkil waqt mein madad deti hai.",
    "🧠 Chhoti chhoti consistent wins bhi lambay arse mein bara farq dalti hain, sirf 'bade trades' ke peechay mat bhago.",
    "🧠 Jab tak apna risk management set na ho, entry lena hi mat socho.",
    "🧠 Dusron ki success stories sirf unke wins dikhati hain, unke losses nahi. Poori tasveer kabhi nahi milti.",
    "🧠 Trading mein humility zaroori hai, market kabhi bhi aap ko ghalat sabit kar sakta hai.",
    "🧠 Apne aap ko 'main is baar zaroor sahi hunga' wali soch se bachao, ye overconfidence hai.",
    "🧠 Emotional trades aksar bina stop-loss ke hoti hain, kyunke dimagh sirf 'jeetne' par lagta hai.",
    "🧠 Trading journal mein sirf entry/exit nahi, apni us waqt ki feelings bhi likho, pattern samajh aayega.",
    "🧠 Loss ke baad apne aap ko punish karne ki bajaye, agle din calm ho kar dobara plan banao.",
    "🧠 Trading mein 'zero risk' wali soch khatarnak hai, har trade mein kuch na kuch risk hamesha hota hai.",
    "🧠 Doosron ki calls copy karna seekhne ka acha tareeqa nahi, khud samajhna zyada zaroori hai.",
    "🧠 Apni progress ko sirf profit se nahi, discipline follow karne se bhi napo.",
    "🧠 Bara loss hone par account delete ya trading chhorne ki bajaye, thora ruk kar wajah samjho.",
    "🧠 Har coin ke baare mein opinion rakhna zaroori nahi, sirf apni strategy ke mutabiq setups par focus karo.",
    "🧠 Positive self-talk trading mein bhi kaam karta hai, apne aap ko bar bar 'main fail hoon' mat kaho.",
    "🧠 Apni comfort ke liye chhota size trade karna seekhna barre size ke liye zaroori qadam hai.",
    "🧠 Ek acha trader apni emotions ko signal ki tarah use karta hai, unhein nazar-andaz nahi karta.",
    "🧠 Overtrading ka ilaj hai: din mein sirf mehdood tadad mein trades ki limit lagao.",
    "🧠 Market ka mood roz badalta hai, apna mood bhi us par control mein rakhna seekho.",
    "🧠 Kisi bhi bare decision se pehle 10 minute ruk kar sochna aksar bara nuksan bacha leta hai.",
    "🧠 Apni pichli success ko permanent mat samjho, market hamesha badalti rehti hai.",
    "🧠 Loss aane par sabse pehla sawal 'kya SL sahi tha' hona chahiye, 'market galat hai' nahi.",
    "🧠 Trading mein consistency, intensity se zyada zaroori hai.",
    "🧠 Apni energy roz-marra ki chhoti trades mein zaya karne ki bajaye, best setups ke liye bacha kar rakho.",
    "🧠 Ek trade ka outcome, aap ki qeemat ka faisla nahi karta.",
    "🧠 Doosron ki 'lambo' lifestyle dekh kar apni strategy risk-bhari banana ek aam ghalti hai.",
    "🧠 Apne trading goals chhotay chhotay steps mein todo, sirf 'ameer banna' kaafi nahi hota.",
    "🧠 Jab plan follow karne ke bawajood loss ho, to wo bhi 'acha trade' tha, sirf outcome ghalat tha.",
    "🧠 Apni emotional state ko trade lagane se pehle ek number (1-10) de kar check karo, agar zyada ho to ruk jao.",
    "🧠 Trading skill waqt ke sath aati hai, jaldi mein result mangna hi sab se bara stress ka sabab hai.",
    "🧠 Apni identity ko trading results se mat jorо, ek loss ka matlab ye nahi ke aap bure trader ho.",
    "🧠 Apne trading dosto ke sath sirf wins nahi, losses bhi share karo, isse healthy perspective banta hai.",
    "🧠 Chart ko bar bar refresh karna anxiety barhata hai, apna signal set karo aur intezar karo.",
    "🧠 Trading mein boring consistency, exciting bade gambles se hamesha behtar hoti hai.",
    "🧠 Apni sab se bari trading ghalti likh kar rakho aur roz uska yaad dahani lo taake dobara na ho.",
    "🧠 Kisi bhi trade ke doran agar dil dhak dhak kare ya haath kanpein, to samjho size zyada bara hai.",
    "🧠 Apna confidence apni strategy ke backtested data se banao, sirf umeed se nahi.",
    "🧠 Market ka har din ek naya din hai, kal ka loss aaj ke faisle par asar na dalne do.",
    "🧠 Apni trading routine (waqt, checklist, review) fix karo, ye emotions ko control mein rakhti hai.",
]

RISK_TIPS = [
    "⚠️ Kisi bhi ek trade mein apni total capital ka 1-2% se zyada risk mat lo.",
    "⚠️ Stop Loss lagana kabhi mat bhoolo, chahe kitna bhi confident kyun na ho.",
    "⚠️ Jitna zyada leverage, utna chhota move aap ko liquidate kar sakta hai. Beginners 3-5x se zyada na jayein.",
    "⚠️ Ek hi sector ke coins (jaise sab meme coins) mein poora paisa mat lagao, correlation zyada hoti hai.",
    "⚠️ Position size hamesha Entry aur Stop Loss ke farq se calculate karo, sirf 'feel' se nahi.",
    "⚠️ Kabhi bhi udhar liya hua paisa ya zaroori khareed-o-farokht ka paisa trading mein mat lagao.",
    "⚠️ TP1 par partial profit lena aur SL ko entry par le aana risk ko bohat kam kar deta hai.",
    "⚠️ Bohat sari open positions ek sath rakhna risk ko control se bahar kar deta hai. Focused raho.",
    "⚠️ News ya bara event (Fed meeting, halving) se pehle position size choti rakho, volatility barh jati hai.",
    "⚠️ Har mahine apna win rate aur risk-reward record dekho. Agar consistently loss ho raha hai to trading rok kar strategy review karo.",
    "⚠️ Apna trading capital aur zindagi ka zaroori paisa hamesha alag rakho, kabhi mix mat karo.",
    "⚠️ Sirf wahi paisa trade karo jise agar poora bhi kho do to aap ki zindagi par bara asar na pare.",
    "⚠️ Risk-reward ratio kam az kam 1:1.5 rakho, taake kam win rate ke sath bhi profitable rehna mumkin ho.",
    "⚠️ Ek din mein bohat zyada loss ho jaye to us din trading band kar do, agle din fresh dimagh se aao.",
    "⚠️ Liquid coins (high volume) ko prefer karo, illiquid coins mein bara order lagane se price kaafi hil jati hai.",
    "⚠️ Limit order use karo jab possible ho, market order slippage ka risk barhata hai.",
    "⚠️ High leverage positions ko bare news events (CPI, FOMC) se pehle chhota kar do ya close kar do.",
    "⚠️ Apni total portfolio ka ek hissa hamesha cash/stablecoin mein rakho taake mauqay par use ho sake.",
    "⚠️ Averaging down (ghatay wali position mein aur paisa dalna) sirf pehle se tay plan ke mutabiq karo, jazbaat se nahi.",
    "⚠️ Apni exchange account ki security check karo: strong password, 2FA aur alag email use karo.",
    "⚠️ Bara amount lambe arse ke liye cold wallet mein rakho, poora paisa exchange par chhorna khatarnak hai.",
    "⚠️ Naye ya unaudited projects mein bara size lagane se pehle unki team aur audit history check karo.",
    "⚠️ Har position ka liquidation price pehle se pata hona chahiye, taake achanak surprise na ho.",
    "⚠️ Correlated assets (jaise BTC aur zyada tar altcoins) ko ek sath long/short karne se risk double ho jata hai.",
    "⚠️ Apna maximum daily aur weekly loss limit likh kar rakho aur us par sakhti se amal karo.",
    "⚠️ Trailing stop use karo jab trade profit mein ho, taake bara move miss na ho aur profit bhi lock rahe.",
    "⚠️ Kabhi bhi apni saari capital ek hi entry mein mat lagao, thora reserve rakho taake average kar sako.",
    "⚠️ High volatility ke waqt (bari news, low liquidity hours) position size khud-ba-khud kam kar do.",
    "⚠️ Apna Stop Loss kabhi bhi loss dekh kar door mat karo, ye disciplined risk management ko tabah karta hai.",
    "⚠️ Multiple exchanges use karo taake ek exchange down ya hack hone par sab paisa na phanse.",
    "⚠️ Apna phone aur laptop update rakho aur suspicious links par click na karo, phishing scams aam hain.",
    "⚠️ Naye token ka contract address hamesha official source se verify karo, fake tokens aam hain.",
    "⚠️ Withdraw karte waqt address ek baar dobara check karo, ghalat address par bheja paisa wapas nahi milta.",
    "⚠️ Apna seed phrase kisi bhi website, app ya insaan ko kabhi mat do, chahe wo khud ko support bataye.",
    "⚠️ High leverage futures beginners ke liye nahi, pehle spot par confidence banao phir leverage try karo.",
    "⚠️ Apna risk per trade fix rakho chahe aap ka mood kaisa bhi ho, emotions ke sath size mat badlo.",
    "⚠️ Bohat choti timeframe (1m, 5m) par trading zyada noise aur zyada ghalat signals deti hai, ehtiyat rakho.",
    "⚠️ Apni open positions ka regular review karo, purane thesis change ho jaye to position bhi adjust karo.",
    "⚠️ Kabhi bhi 'sab kuch ek coin mein' wali soch mat rakho, chahe wo coin kitna bhi acha lage.",
    "⚠️ Exchange ki withdrawal limits aur fees pehle se check kar lo, emergency mein pata chalna acha nahi.",
    "⚠️ Apna risk tolerance market ke mood ke hisaab se mat badlo, wo hamesha ek jaisa (pre-decided) rehna chahiye.",
    "⚠️ Kisi bhi 'guaranteed profit' wali scheme se door raho, aisi cheez trading mein hoti hi nahi.",
    "⚠️ Portfolio ko har kuch mahine baad rebalance karo taake ek asset ka weight zyada na ho jaye.",
    "⚠️ Apna emergency exit plan hamesha ready rakho, agar internet ya exchange down ho jaye to kya karo ge.",
    "⚠️ High funding rate wale leveraged positions ko lambe arse tak hold karna mehenga par sakta hai.",
    "⚠️ Apni trades ka size dheeray dheeray barhao jab confidence aur consistency dono ban jayein, jaldi mat karo.",
    "⚠️ Low market cap coins mein bara size lagane se pehle unki daily volume zaroor check karo.",
    "⚠️ Apna risk management plan likh kar rakho aur usay kisi bhi trade se pehle dobara parh lo.",
    "⚠️ Kabhi bhi 'is baar zaroor sahi hoga' soch kar apna normal risk se zyada size mat lagao.",
    "⚠️ Apni trading capital ko chhotay chhotay hissay (jaise 10 parts) mein taqseem karo taake ek galti sab kuch na le jaye.",
    "⚠️ Apna Take Profit bhi pehle se tay karo, sirf 'aur barhega' soch kar profit book karna na bhoolo.",
    "⚠️ Ek achi trade ka matlab profit nahi, plan follow karna hai. Loss ke bawajood plan follow karna behtareen risk management hai.",
    "⚠️ Kabhi bhi apna Stop Loss dekh kar 'ye zaroor wapas aayega' soch kar hata mat do.",
    "⚠️ Naye traders ko pehle demo ya bohat chhotay amount se practice karni chahiye, foran bara size na lagayen.",
    "⚠️ Apna total exposure (sab open positions ka combined risk) kabhi bhi apni capital ke 10% se zyada na hone do.",
    "⚠️ Kisi bhi ek exchange par apni poori savings rakhna khatarnak hai, hack ya freeze ka risk hamesha rehta hai.",
    "⚠️ Trading bot ya signal service use kar rahe ho to bhi apna khud ka Stop Loss aur position size manually set karo.",
    "⚠️ Weekend aur low-liquidity hours mein price bohat ajeeb move kar sakti hai, position size chhota rakho.",
    "⚠️ Apna risk per trade percentage me socho, dollar amount mein nahi, taake capital barhne ghatne par size khud adjust ho.",
    "⚠️ Agar ek hafte mein 3 trades consecutively loss hon, to ruk kar apni strategy dobara review karo.",
    "⚠️ Margin call ya liquidation warning aane par foran react karo, ignore karna poori position khatam kar sakta hai.",
    "⚠️ Kisi bhi token ko 'sirf isliye' na kharido ke uska naam kisi famous coin se milta julta hai.",
    "⚠️ Apna exchange API key trading bots ko dete waqt sirf 'trade' permission do, 'withdraw' permission kabhi na do.",
    "⚠️ High leverage short-term trades aur long-term investment ko apni portfolio mein alag alag section mein rakho.",
    "⚠️ Kabhi bhi apni loss recover karne ke liye normal se do guna size ki trade mat lagao.",
    "⚠️ Naye coin listing ke pehle ghante mein volatility bohat zyada hoti hai, foran bara size lagane se bacho.",
    "⚠️ Apna risk sirf market risk tak mehdood nahi, exchange risk aur regulatory risk ko bhi zehan mein rakho.",
    "⚠️ Agar kisi trade ka reasoning bhool gaye ho to usay close kar do, bina wajah ke position rakhna risk hai.",
    "⚠️ Apni portfolio ka periodic 'stress test' socho: agar BTC 30% gir jaye to mera poora portfolio kitna girega?",
    "⚠️ Kabhi bhi sirf ek indicator par bharosa kar ke bara size trade na lagao, multiple confirmations dekho.",
    "⚠️ Apni risk-reward calculation mein trading fees aur funding rate ko bhi shamil karo, ye chhoti trades mein farq dal sakti hain.",
    "⚠️ Jab bazar mein extreme greed ho (sab log lalchi), tab apna risk normal se kam kar do, ye tez reversal ka waqt hota hai.",
    "⚠️ Jab bazar mein extreme fear ho, tab bhi jaldi mein bara size na lagao, gira hua price aur gir sakta hai.",
    "⚠️ Apni har trade se pehle worst-case scenario socho: agar SL hit ho jaye to portfolio par kitna asar hoga?",
    "⚠️ Kisi bhi single signal ya tip par 100% capital lagana kabhi na karo, chahe wo kitni bhi confident lage.",
    "⚠️ Apni risk management rules likhi hui rakho aur mahine mein ek baar unhein review kar ke behtar karo.",
    "⚠️ Bohat zyada open orders (pending limit orders) rakhna bhool jane par accidental execution ka risk barhata hai, list clean rakho.",
    "⚠️ Apni exit strategy entry lagane se pehle hi tay karo, trade ke doran faisla lena aksar emotional hota hai.",
    "⚠️ Kabhi bhi apna sara profit foran nayi trade mein wapas mat lagao, kuch hissa withdraw kar ke secure karo.",
    "⚠️ Apni Stop Loss ki placement chart ki structure (support/resistance) par rakho, random number par nahi.",
    "⚠️ Apni portfolio mein stablecoins ka hissa market ke mutabiq badalte raho, sab waqt 100% invested rehna zaroori nahi.",
    "⚠️ Naye exchange par paisa jama karane se pehle chhoti amount se test karo ke withdrawal theek se kaam karta hai.",
    "⚠️ Apni trading ke liye alag bank account ya wallet use karo, personal aur trading paisa mix na karo.",
    "⚠️ Agar koi coin achanak bohat tezi se upar jaye (pump), to us mein chase kar ke entry lena aksar bara risk hota hai.",
    "⚠️ Apni leverage settings har exchange par dobara check karo, kabhi kabhi default leverage bohat zyada hoti hai.",
    "⚠️ Kisi bhi naye DeFi protocol mein bara amount lagane se pehle uska TVL (Total Value Locked) aur history dekho.",
    "⚠️ Apna risk sirf price movement tak mehdood nahi, smart contract bugs aur hacks bhi DeFi mein bara risk hain.",
    "⚠️ Kabhi bhi apni poori position ek hi price par close karne ki koshish mat karo, order thora thora scale out karo.",
    "⚠️ Apni trading capital ka wo hissa jo aap agle 6 mahine mein use nahi karo ge, wahi trading ke liye rakho.",
    "⚠️ High-risk, high-reward coins (micro caps) ko apni total portfolio ka sirf choti percentage rakho.",
    "⚠️ Apni risk management sirf crypto tak mehdood na rakho, apni doosri investments (savings, gold) ko bhi dhyan mein rakho.",
    "⚠️ Jab market mein bohat zyada leverage (high open interest) ho, to sudden liquidation cascades ka risk barh jata hai, ehtiyat karo.",
    "⚠️ Apna trading plan kisi bhi bare loss ke baad emotionally badalne ki bajaye, calm hokar review karo.",
    "⚠️ Kisi bhi coin ko 'ye kabhi neeche nahi ja sakta' soch kar unlimited risk lena bara khatra hai.",
    "⚠️ Apni har open position ka ek clear invalidation point (jahan thesis ghalat sabit ho) hona chahiye.",
    "⚠️ Bara withdrawal karte waqt exchange ki 24-48 ghante ki security hold ka khayal rakho, last-minute plan na banao.",
    "⚠️ Apni sari trading strategy ek hi timeframe par mat banao, bare aur chhote dono charts dekho.",
    "⚠️ Kabhi bhi apni trading app ko public wifi par login karne se bacho, private aur secure connection use karo.",
    "⚠️ Apna password aur seed phrase kisi cloud notes app mein plain text mein save karne se bacho.",
    "⚠️ Naye leverage products (jaise 100x) beginners ke liye bohat khatarnak hotay hain, in se door rehna behtar hai.",
]

CONTENT_SEQUENCE = [
    "overview", "technical", "education", "coin", "psychology",
    "news", "risk", "summary",
]

TIP_STYLES = {
    "education": {"emoji": "📚", "label": "Education Tip", "color": "#1d3557", "pool": EDUCATION_TIPS},
    "psychology": {"emoji": "🧠", "label": "Psychology Tip", "color": "#5b21b6", "pool": PSYCHOLOGY_TIPS},
    "risk": {"emoji": "⚠️", "label": "Risk Management Tip", "color": "#9a1b1b", "pool": RISK_TIPS},
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
                    f"Ab SL <b>entry ({fmt_price(s['entry'])})</b> par le aao. Risk-free trade! 🛡️"
                )
                img = _text_card("TP1 Hit", f"{name} {s['side']} target 1 reached", "#166534", wrap=36)
                send_telegram_photo(img, text)
            still_open.append(s)
        else:
            if state == "sl":
                data["stats"]["losses"] += 1
                header, color = "Stop Loss Hit", "#7f1d1d"
                text = f"<b>❌ {header} — {name} {s['side']}</b>"
            elif state == "tp2":
                data["stats"]["wins"] += 1
                header, color = "TP2 Hit - Full Target", "#166534"
                text = f"<b>🏆 {header} — {name} {s['side']}</b> ✅✅"
            elif state == "be":
                data["stats"]["wins"] += 1
                header, color = "Closed at Breakeven", "#1e3a5f"
                text = f"<b>✅ {header} — {name} {s['side']}</b>\nTP1 mil chuka tha, phir entry par band."
            elif state == "tp1_close":
                data["stats"]["wins"] += 1
                header, color = "Closed after TP1", "#1e3a5f"
                text = f"<b>✅ {header} — {name} {s['side']}</b>\nTime-out par band hua."
            else:  # expired
                data["stats"]["expired"] += 1
                header, color = "Signal Expired", "#525252"
                text = f"<b>⏱ {header} — {name} {s['side']}</b>\n{EXPIRE_H}h mein koi target nahi mila."
            log_result(s, state)
            full_text = text + "\n\n" + stats_line(data["stats"])
            img = _text_card(header, f"{name} {s['side']}", color, wrap=36)
            send_telegram_photo(img, full_text)
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
        try:
            path = make_chart(sig["symbol"], f"{sig['symbol'][:-4]}/USDT - Entry Signal")
            ok = send_telegram_photo(path, signal_message(sig))
        except Exception as e:
            log(f"Chart error {sig['symbol']}: {e}")
            ok = send_telegram(signal_message(sig))
        if ok:
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

    if CONTENT_ENABLED:
        now = time.time()
        if now - data.get("last_content_time", 0) >= CONTENT_INTERVAL_H * 3600:
            post_content(data)
            data["last_content_time"] = now
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
