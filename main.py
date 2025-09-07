import os
import time
import threading
import requests
from flask import Flask
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime

# ---------------------------
# Config
# ---------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
if not BOT_TOKEN or not CHAT_ID:
    raise SystemExit("Set BOT_TOKEN and CHAT_ID in Render environment.")

TG_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
TG_PHOTO_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"

SYMBOLS = [
    ("BTCUSDT_PERP.A", "BTC"),
    ("ETHUSDT_PERP.A", "ETH"),
    ("XRPUSDT_PERP.A", "XRP"),
    ("SOLUSDT_PERP.A", "SOL"),
]
INTERVAL = "2h"
CANDLES_LIMIT = 300
RUN_EVERY_SECONDS = 2 * 60 * 60  # every 2h
BASE_URL = "https://api.coinalyze.net/v1/futures"

last_signals = {}
SILENCE_SECONDS = 6 * 60 * 60  # 6h deduplication

# ---------------------------
# Telegram
# ---------------------------
def send_telegram_text(msg):
    try:
        r = requests.post(TG_URL, data={"chat_id": CHAT_ID, "text": msg})
        print("Telegram text:", r.status_code, r.text)
    except Exception as e:
        print("Telegram send error:", e)

def send_telegram_photo(path, caption=""):
    try:
        with open(path, "rb") as f:
            r = requests.post(
                TG_PHOTO_URL,
                data={"chat_id": CHAT_ID, "caption": caption},
                files={"photo": f},
            )
        print("Telegram photo:", r.status_code, r.text)
    except Exception as e:
        print("Telegram photo error:", e)

# ---------------------------
# Data fetching
# ---------------------------
def safe_get(url):
    try:
        r = requests.get(url, timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print("Request error:", e)
    return None

def get_candles(symbol, interval=INTERVAL, limit=CANDLES_LIMIT):
    url = f"{BASE_URL}/candles?symbol={symbol}&interval={interval}&limit={limit}"
    data = safe_get(url)
    return data if isinstance(data, list) else []

# ---------------------------
# Chart plotting
# ---------------------------
def plot_chart(symbol, short, candles, desc, filename):
    closes = [float(c["close"]) for c in candles]
    opens = [float(c["open"]) for c in candles]
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    times = [datetime.fromtimestamp(c["timestamp"] / 1000) for c in candles]

    fig, ax = plt.subplots(figsize=(8, 4))
    for i in range(len(candles)):
        color = "green" if closes[i] >= opens[i] else "red"
        ax.plot([times[i], times[i]], [lows[i], highs[i]], color=color, linewidth=1)
        ax.add_patch(
            plt.Rectangle(
                (times[i], min(opens[i], closes[i])),
                width=(times[-1] - times[0]).total_seconds() / len(candles) / 3,
                height=abs(closes[i] - opens[i]),
                color=color,
            )
        )

    ax.set_title(f"{short} — {desc}")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %Hh"))
    plt.xticks(rotation=45)
    plt.tight_layout()
    plt.savefig(filename)
    plt.close()

# ---------------------------
# Pattern Detection
# ---------------------------
def detect_patterns(candles, short):
    closes = [float(c["close"]) for c in candles]
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]

    results = []

    # --- Double Top / Bottom ---
    if closes[-1] < max(highs) * 0.97:
        results.append(("bearish", 0.75, f"{short}: Double Top"))
    if closes[-1] > min(lows) * 1.03:
        results.append(("bullish", 0.75, f"{short}: Double Bottom"))

    # --- Head & Shoulders ---
    mid = len(closes) // 2
    if highs[mid] > highs[mid - 2] and highs[mid] > highs[mid + 2]:
        results.append(("bearish", 0.85, f"{short}: Head & Shoulders"))

    # --- Cup & Handle ---
    if closes[-1] > sum(closes) / len(closes) and closes[-1] > max(closes[:-20]):
        results.append(("bullish", 0.80, f"{short}: Cup & Handle"))

    # --- Wedge ---
    if highs[-1] < highs[-2] and lows[-1] > lows[-2]:
        results.append(("bullish", 0.70, f"{short}: Falling Wedge"))
    if highs[-1] > highs[-2] and lows[-1] < lows[-2]:
        results.append(("bearish", 0.70, f"{short}: Rising Wedge"))

    # --- Flag ---
    recent = closes[-10:]
    if max(recent) / min(recent) < 1.02:  # tight consolidation
        if closes[-1] > sum(closes[:-10]) / (len(closes) - 10):
            results.append(("bullish", 0.65, f"{short}: Bull Flag"))
        else:
            results.append(("bearish", 0.65, f"{short}: Bear Flag"))

    return results

def analyze_symbol_patterns(symbol_tuple):
    sym, short = symbol_tuple
    candles = get_candles(sym, interval=INTERVAL, limit=CANDLES_LIMIT)
    if not candles:
        return []
    return detect_patterns(candles, short)

# ---------------------------
# Ranking & Filtering
# ---------------------------
def collect_top_patterns():
    bull, bear = [], []
    for sym in SYMBOLS:
        res = analyze_symbol_patterns(sym)
        for dirx, score, desc in res:
            entry = {"symbol": sym[1], "score": score, "desc": desc}
            if dirx == "bullish":
                bull.append(entry)
            else:
                bear.append(entry)
    bull_sorted = sorted(bull, key=lambda x: x["score"], reverse=True)[:5]
    bear_sorted = sorted(bear, key=lambda x: x["score"], reverse=True)[:5]
    return bull_sorted, bear_sorted

def should_post(symbol, desc):
    key = (symbol, desc)
    now = time.time()
    if key in last_signals and now - last_signals[key] < SILENCE_SECONDS:
        return False
    last_signals[key] = now
    return True

# ---------------------------
# Bot loop
# ---------------------------
def run_once_and_report():
    bull, bear = collect_top_patterns()
    all_signals = bull + bear

    if not all_signals:
        send_telegram_text("⚠️ No strong patterns detected this cycle.")
        return

    summary = "📊 Top 2H Pattern Signals\n\n"
    if bull:
        summary += "🔥 Bullish:\n"
        for sig in bull:
            summary += f"• {sig['symbol']} → {sig['desc']} (score {sig['score']:.2f})\n"
    if bear:
        summary += "\n❄️ Bearish:\n"
        for sig in bear:
            summary += f"• {sig['symbol']} → {sig['desc']} (score {sig['score']:.2f})\n"

    send_telegram_text(summary)

    # Send charts
    for sig in all_signals:
        symbol, desc = sig["symbol"], sig["desc"]
        if not should_post(symbol, desc):
            print(f"Silenced duplicate {symbol} - {desc}")
            continue

        full_symbol = [s for s in SYMBOLS if s[1] == symbol][0][0]
        candles = get_candles(full_symbol, interval=INTERVAL, limit=120)
        chart_file = f"{symbol}_{int(time.time())}.png"
        plot_chart(full_symbol, symbol, candles, desc, chart_file)

        caption = f"{'🔥' if 'bull' in desc.lower() else '❄️'} {desc} (score {sig['score']:.2f})"
        send_telegram_photo(chart_file, caption)
        os.remove(chart_file)

def bot_loop():
    send_telegram_text("✅ 2H Pattern Scanner started with ranking + deduplication.")
    while True:
        try:
            run_once_and_report()
        except Exception as e:
            send_telegram_text(f"⚠️ Bot error: {e}")
        time.sleep(RUN_EVERY_SECONDS)

# ---------------------------
# Flask keep-alive
# ---------------------------
app = Flask(__name__)

@app.route("/")
def home():
    return "2H Pattern scanner with ranking & charts is running!"

if __name__ == "__main__":
    t = threading.Thread(target=bot_loop, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
