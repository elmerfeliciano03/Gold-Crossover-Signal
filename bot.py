import yfinance as yf
import numpy as np
import requests
import logging
import sys
from datetime import datetime

# ------------------------------
# CONFIGURATION
# ------------------------------
TELEGRAM_TOKEN = 'YOUR_BOT_TOKEN'
CHAT_ID = 'YOUR_CHAT_ID'
SYMBOL = "GC=F"
CAPITAL = 10000
RISK_PERCENT = 0.5
REWARD_PERCENT = 1.0
EMA_SHORT = 50
EMA_LONG = 200
ADX_PERIOD = 14

LAST_SIGNAL_FILE = "/tmp/last_gold_signal.txt"

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        logging.info("Telegram message sent")
        return True
    except Exception as e:
        logging.error(f"Failed to send message: {e}")
        return False

def calculate_ema(data, period):
    """Calculate EMA without pandas using recursive formula"""
    ema = []
    multiplier = 2 / (period + 1)
    
    # Start with SMA for first value
    if len(data) < period:
        return [None] * len(data)
    
    sma = sum(data[:period]) / period
    ema.append(sma)
    
    # Calculate EMA recursively
    for price in data[period:]:
        ema_value = (price - ema[-1]) * multiplier + ema[-1]
        ema.append(ema_value)
    
    # Pad the beginning with None
    return [None] * (period - 1) + ema

def calculate_adx(high, low, close, period=14):
    """Calculate ADX without pandas"""
    n = len(close)
    if n < period + 1:
        return 0, 0, 0
    
    plus_dm = [0] * n
    minus_dm = [0] * n
    tr = [0] * n
    
    for i in range(1, n):
        # Calculate +DM and -DM
        up_move = high[i] - high[i-1]
        down_move = low[i-1] - low[i]
        
        if up_move > down_move and up_move > 0:
            plus_dm[i] = up_move
        else:
            plus_dm[i] = 0
            
        if down_move > up_move and down_move > 0:
            minus_dm[i] = down_move
        else:
            minus_dm[i] = 0
        
        # Calculate True Range
        tr1 = high[i] - low[i]
        tr2 = abs(high[i] - close[i-1])
        tr3 = abs(low[i] - close[i-1])
        tr[i] = max(tr1, tr2, tr3)
    
    # Smooth values (Wilder's smoothing)
    atr = [0] * n
    smooth_plus_dm = [0] * n
    smooth_minus_dm = [0] n
    
    # First values are averages
    atr[period-1] = sum(tr[1:period]) / period
    smooth_plus_dm[period-1] = sum(plus_dm[1:period]) / period
    smooth_minus_dm[period-1] = sum(minus_dm[1:period]) / period
    
    # Wilder's smoothing
    for i in range(period, n):
        atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period
        smooth_plus_dm[i] = (smooth_plus_dm[i-1] * (period - 1) + plus_dm[i]) / period
        smooth_minus_dm[i] = (smooth_minus_dm[i-1] * (period - 1) + minus_dm[i]) / period
    
    # Calculate +DI and -DI
    plus_di = [0] * n
    minus_di = [0] * n
    for i in range(period, n):
        if atr[i] != 0:
            plus_di[i] = 100 * (smooth_plus_dm[i] / atr[i])
            minus_di[i] = 100 * (smooth_minus_dm[i] / atr[i])
    
    # Calculate DX and ADX
    dx = [0] * n
    for i in range(period, n):
        di_sum = plus_di[i] + minus_di[i]
        if di_sum != 0:
            dx[i] = 100 * abs(plus_di[i] - minus_di[i]) / di_sum
    
    # Smooth DX to get ADX
    adx = [0] * n
    adx[period*2-2] = sum(dx[period-1:period*2-1]) / period
    
    for i in range(period*2-1, n):
        adx[i] = (adx[i-1] * (period - 1) + dx[i]) / period
    
    return adx[-1], plus_di[-1], minus_di[-1]

def check_gold_signal():
    logging.info(f"Scanning {SYMBOL} on 10-minute timeframe...")
    
    # Download data
    df = yf.download(SYMBOL, period="7d", interval="10m", progress=False, auto_adjust=False)
    
    if df.empty:
        logging.error("No data retrieved")
        return None
    
    # Extract data as lists (no pandas needed)
    close_prices = df['Close'].tolist()
    high_prices = df['High'].tolist()
    low_prices = df['Low'].tolist()
    
    if len(close_prices) < EMA_LONG + 10:
        logging.warning(f"Insufficient data: {len(close_prices)} candles")
        return None
    
    # Calculate EMAs
    ema50 = calculate_ema(close_prices, EMA_SHORT)
    ema200 = calculate_ema(close_prices, EMA_LONG)
    
    # Get current and previous values
    current_ema50 = ema50[-1]
    current_ema200 = ema200[-1]
    prev_ema50 = ema50[-2]
    prev_ema200 = ema200[-2]
    current_price = close_prices[-1]
    current_time = df.index[-1]
    
    # Calculate ADX
    adx_value, plus_di, minus_di = calculate_adx(high_prices, low_prices, close_prices, ADX_PERIOD)
    
    # Detect crossover
    signal = None
    if (prev_ema50 is not None and prev_ema200 is not None and 
        current_ema50 is not None and current_ema200 is not None):
        
        if prev_ema50 <= prev_ema200 and current_ema50 > current_ema200:
            signal = "BUY"
        elif prev_ema50 >= prev_ema200 and current_ema50 < current_ema200:
            signal = "SELL"
    
    if signal:
        # Send message (same as before)
        if signal == "BUY":
            stop_loss = current_price * (1 - RISK_PERCENT / 100)
            take_profit = current_price * (1 + REWARD_PERCENT / 100)
            direction_text = "LONG"
        else:
            stop_loss = current_price * (1 + RISK_PERCENT / 100)
            take_profit = current_price * (1 - REWARD_PERCENT / 100)
            direction_text = "SHORT"
        
        quantity = CAPITAL / current_price
        risk_amount = CAPITAL * (RISK_PERCENT / 100)
        
        adx_status = "🔥 Strong" if adx_value > 25 else "⚠️ Weak" if adx_value < 20 else "📊 Moderate"
        
        message = f"""
🚨 <b>TRADE SIGNAL: {signal}</b> 🚨
━━━━━━━━━━━━━━━━━━━━
💰 <b>Asset:</b> Gold (XAU/USD)
💵 <b>Entry:</b> ${current_price:.2f}
🛑 <b>Stop:</b> ${stop_loss:.2f} ({RISK_PERCENT}%)
🎯 <b>Target:</b> ${take_profit:.2f} ({REWARD_PERCENT}%)
━━━━━━━━━━━━━━━━━━━━
📊 <b>Technical:</b>
• EMA50: {current_ema50:.2f}
• EMA200: {current_ema200:.2f}
• ADX: {adx_value:.1f} ({adx_status})
━━━━━━━━━━━━━━━━━━━━
💼 <b>Risk: ${risk_amount:.2f}</b> | <b>Reward: ${risk_amount*2:.2f}</b>
<i>1:2 Risk/Reward | Position: ${CAPITAL:,}</i>
"""
        send_telegram_message(message)
        return signal
    
    return None

if __name__ == "__main__":
    logging.info("Gold Bot starting (pandas-free version)")
    check_gold_signal()
