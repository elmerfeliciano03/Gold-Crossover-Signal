echo 'import requests
import json
import logging
import sys
import os
from datetime import datetime

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
ALPHA_VANTAGE_KEY = os.environ.get("ALPHA_VANTAGE_KEY", "")
CAPITAL = 10000
RISK_PERCENT = 0.5
REWARD_PERCENT = 1.0
EMA_SHORT = 50
EMA_LONG = 200

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)

def send_telegram_message(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logging.warning("Telegram credentials not set")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        response = requests.post(url, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
        return response.ok
    except Exception as e:
        logging.error(f"Failed to send: {e}")
        return False

def calculate_ema(data, period):
    if len(data) < period:
        return [None] * len(data)
    multiplier = 2 / (period + 1)
    ema = [None] * (period - 1)
    ema.append(sum(data[:period]) / period)
    for i in range(period, len(data)):
        ema.append((data[i] - ema[-1]) * multiplier + ema[-1])
    while len(ema) < len(data):
        ema.insert(0, None)
    return ema

def fetch_gold_data():
    if not ALPHA_VANTAGE_KEY:
        logging.error("Alpha Vantage API key not set")
        return None
    url = "https://www.alphavantage.co/query"
    params = {
        "function": "FX_INTRADAY",
        "from_symbol": "XAU",
        "to_symbol": "USD",
        "interval": "10min",
        "apikey": ALPHA_VANTAGE_KEY,
        "outputsize": "compact"
    }
    try:
        logging.info("Fetching gold data...")
        response = requests.get(url, params=params, timeout=15)
        data = response.json()
        if "Error Message" in data:
            logging.error(f"API Error: {data['Error Message']}")
            return None
        if "Note" in data:
            logging.warning(f"API Limit: {data['Note']}")
            return None
        time_series_key = "Time Series FX (10min)"
        if time_series_key not in data:
            logging.error(f"Unexpected response: {list(data.keys())}")
            return None
        time_series = data[time_series_key]
        closes = []
        for timestamp in sorted(time_series.keys()):
            closes.append(float(time_series[timestamp]["4. close"]))
        logging.info(f"Fetched {len(closes)} candles")
        return {"close": closes}
    except Exception as e:
        logging.error(f"Fetch error: {e}")
        return None

def check_signal():
    logging.info("Checking gold for EMA crossover...")
    data = fetch_gold_data()
    if not data:
        return None
    close = data["close"]
    if len(close) < EMA_LONG + 20:
        logging.warning(f"Only {len(close)} candles")
        return None
    ema50 = calculate_ema(close, EMA_SHORT)
    ema200 = calculate_ema(close, EMA_LONG)
    valid50 = [x for x in ema50 if x is not None]
    valid200 = [x for x in ema200 if x is not None]
    if len(valid50) < 2 or len(valid200) < 2:
        return None
    current_price = close[-1]
    prev_ema50 = valid50[-2]
    prev_ema200 = valid200[-2]
    curr_ema50 = valid50[-1]
    curr_ema200 = valid200[-1]
    signal = None
    if prev_ema50 <= prev_ema200 and curr_ema50 > curr_ema200:
        signal = "BUY"
    elif prev_ema50 >= prev_ema200 and curr_ema50 < curr_ema200:
        signal = "SELL"
    if signal:
        if signal == "BUY":
            stop_loss = current_price * (1 - RISK_PERCENT / 100)
            take_profit = current_price * (1 + REWARD_PERCENT / 100)
        else:
            stop_loss = current_price * (1 + RISK_PERCENT / 100)
            take_profit = current_price * (1 - REWARD_PERCENT / 100)
        quantity = CAPITAL / current_price
        risk_amount = CAPITAL * (RISK_PERCENT / 100)
        message = f"""🚨 <b>{signal} SIGNAL - GOLD</b> 🚨
━━━━━━━━━━━━━━━━━━━━
💵 Entry: ${current_price:.2f}
🛑 Stop: ${stop_loss:.2f} ({RISK_PERCENT}%)
🎯 Target: ${take_profit:.2f} ({REWARD_PERCENT}%)
━━━━━━━━━━━━━━━━━━━━
📊 EMA50: ${curr_ema50:.2f} | EMA200: ${curr_ema200:.2f}
━━━━━━━━━━━━━━━━━━━━
💼 Risk: ${risk_amount:.2f} | Reward: ${risk_amount * 2:.2f}
📐 R:R = 1:2 ✅
━━━━━━━━━━━━━━━━━━━━
<i>Not financial advice</i>"""
        return signal, message, current_price
    return None

if __name__ == "__main__":
    logging.info("Gold Bot Starting")
    if not ALPHA_VANTAGE_KEY:
        logging.error("ALPHA_VANTAGE_KEY not set!")
        sys.exit(1)
    result = check_signal()
    if result:
        signal, message, price = result
        send_telegram_message(message)
        print(f"Signal sent: {signal} at ${price:.2f}")
        sys.exit(0)
    else:
        print("No signal")
        sys.exit(1)' > bot.py
