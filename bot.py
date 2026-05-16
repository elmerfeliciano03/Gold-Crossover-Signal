import yfinance as yf
import pandas as pd
import numpy as np
import requests
import logging
import sys
from datetime import datetime

# ------------------------------
# CONFIGURATION
# ------------------------------
TELEGRAM_TOKEN = 'YOUR_BOT_TOKEN'  # Replace with your bot token from BotFather
CHAT_ID = 'YOUR_CHAT_ID'  # Replace with your Telegram Chat ID
SYMBOL = "GC=F"  # Ticker for Gold on Yahoo Finance
CAPITAL = 10000  # Total position value ($10,000)
RISK_PERCENT = 0.5  # Risk 0.5% of position ($50)
REWARD_PERCENT = 1.0  # Target 1% of position ($100)

# Technical Analysis Parameters
EMA_SHORT = 50
EMA_LONG = 200
ADX_PERIOD = 14

# Track sent signals to avoid duplicates (store last signal timestamp)
LAST_SIGNAL_FILE = "/tmp/last_gold_signal.txt"  # Use /tmp for Render ephemeral storage

# Setup Logging
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)

def send_telegram_message(text):
    """Sends a message to the configured Telegram chat."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        logging.info("Telegram message sent successfully")
        return True
    except Exception as e:
        logging.error(f"Failed to send message: {e}")
        return False

def get_last_signal_time():
    """Read the timestamp of the last sent signal to avoid duplicates."""
    try:
        with open(LAST_SIGNAL_FILE, 'r') as f:
            return f.read().strip()
    except FileNotFoundError:
        return None

def save_signal_time(timestamp):
    """Save the timestamp of the current signal."""
    try:
        with open(LAST_SIGNAL_FILE, 'w') as f:
            f.write(timestamp)
    except Exception as e:
        logging.error(f"Failed to save signal time: {e}")

def calculate_adx(data, period=14):
    """Calculates ADX (Average Directional Index) for trend strength."""
    high, low, close = data['High'], data['Low'], data['Close']
    
    # Calculate +DM and -DM
    up_move = high.diff()
    down_move = low.diff().abs()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    
    # Calculate True Range
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    
    # Smooth values (Wilder's smoothing)
    atr = true_range.rolling(window=period).mean()
    smooth_plus_dm = plus_dm.rolling(window=period).mean()
    smooth_minus_dm = minus_dm.rolling(window=period).mean()
    
    # Calculate +DI and -DI
    plus_di = 100 * (smooth_plus_dm / atr)
    minus_di = 100 * (smooth_minus_dm / atr)
    
    # Calculate DX and ADX
    dx = 100 * (abs(plus_di - minus_di) / (plus_di + minus_di))
    adx = dx.rolling(window=period).mean()
    
    return adx.iloc[-1], plus_di.iloc[-1], minus_di.iloc[-1]

def check_gold_signal():
    """Main logic: Fetches data, calculates indicators, and decides on a signal."""
    logging.info(f"Scanning {SYMBOL} on 10-minute timeframe...")
    
    # Download enough data for EMA200 (need at least 200 candles)
    # For 10-min candles: 200 candles = ~33 hours, so download 7 days to be safe
    df = yf.download(SYMBOL, period="7d", interval="10m", progress=False, auto_adjust=False)
    
    if df.empty:
        logging.error("No data retrieved from Yahoo Finance")
        return None
    
    # Remove any NaN values from the beginning
    df = df.dropna()
    
    if len(df) < EMA_LONG + 10:
        logging.warning(f"Insufficient data: only {len(df)} candles available. Need at least {EMA_LONG + 10}")
        return None
    
    # Calculate EMAs
    df['EMA50'] = df['Close'].ewm(span=EMA_SHORT, adjust=False).mean()
    df['EMA200'] = df['Close'].ewm(span=EMA_LONG, adjust=False).mean()
    
    # Get current and previous values (last two candles)
    current_ema50 = df['EMA50'].iloc[-1]
    current_ema200 = df['EMA200'].iloc[-1]
    prev_ema50 = df['EMA50'].iloc[-2]
    prev_ema200 = df['EMA200'].iloc[-2]
    current_price = df['Close'].iloc[-1]
    current_time = df.index[-1]
    
    # Get high/low for the current candle
    current_high = df['High'].iloc[-1]
    current_low = df['Low'].iloc[-1]
    
    # Calculate ADX (using full dataset)
    adx_value, plus_di, minus_di = calculate_adx(df, ADX_PERIOD)
    
    # Determine Crossover Type
    signal = None
    crossover_type = None
    
    if prev_ema50 <= prev_ema200 and current_ema50 > current_ema200:
        signal = "BUY"
        crossover_type = "Golden Cross (Bullish)"
        logging.info(f"BUY signal detected at {current_time}")
    elif prev_ema50 >= prev_ema200 and current_ema50 < current_ema200:
        signal = "SELL"
        crossover_type = "Death Cross (Bearish)"
        logging.info(f"SELL signal detected at {current_time}")
    
    if signal:
        # Check if we already sent a signal for this candle
        signal_key = f"{signal}_{current_time}"
        last_signal = get_last_signal_time()
        
        if last_signal == signal_key:
            logging.info(f"Duplicate signal prevented for {signal_key}")
            return None
        
        # --- Position Sizing & Risk Management ---
        if signal == "BUY":
            stop_loss = current_price * (1 - RISK_PERCENT / 100)
            take_profit = current_price * (1 + REWARD_PERCENT / 100)
            direction_text = "LONG (Buying)"
        else:  # SELL
            stop_loss = current_price * (1 + RISK_PERCENT / 100)
            take_profit = current_price * (1 - REWARD_PERCENT / 100)
            direction_text = "SHORT (Selling)"
        
        # Calculate Quantity based on Position Size ($10,000)
        quantity = CAPITAL / current_price
        risk_amount = CAPITAL * (RISK_PERCENT / 100)  # $50
        reward_amount = risk_amount * 2  # $100
        
        # --- Compose Message ---
        adx_status = "🔥 Strong Trend" if adx_value > 25 else "⚠️ Weak/Ranging" if adx_value < 20 else "📊 Moderate Trend"
        
        # Format time nicely
        time_str = current_time.strftime("%Y-%m-%d %H:%M UTC")
        
        message = (
            f"🚨 <b>TRADE SIGNAL: {signal}</b> 🚨\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ <b>Time:</b> {time_str}\n"
            f"⏱️ <b>Timeframe:</b> 10 Minutes\n"
            f"💰 <b>Asset:</b> Gold (XAU/USD)\n"
            f"📈 <b>Signal:</b> {signal} - {crossover_type}\n"
            f"💵 <b>Entry Price:</b> ${current_price:.2f}\n"
            f"📊 <b>Range:</b> H: ${current_high:.2f} | L: ${current_low:.2f}\n"
            f"🛑 <b>Stop Loss:</b> ${stop_loss:.2f} ({RISK_PERCENT}% risk)\n"
            f"🎯 <b>Take Profit:</b> ${take_profit:.2f} ({REWARD_PERCENT}% gain)\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 <b>Technical Context:</b>\n"
            f"• EMA50: {current_ema50:.2f}\n"
            f"• EMA200: {current_ema200:.2f}\n"
            f"• Difference: {((current_ema50 - current_ema200)/current_ema200*100):.2f}%\n"
            f"• ADX: {adx_value:.1f} ({adx_status})\n"
            f"• +DI: {plus_di:.1f} | -DI: {minus_di:.1f}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💼 <b>Risk Management (1:2 R:R)</b>\n"
            f"• Position Size: ${CAPITAL:,}\n"
            f"• Quantity: {quantity:.4f} units\n"
            f"• Risk: ${risk_amount:.2f} | Reward: ${reward_amount:.2f}\n"
            f"• R:R Ratio: 1:2 ✅\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>⚡ Always use stop losses. Past signals don't guarantee future results.</i>"
        )
        
        # Send the message
        if send_telegram_message(message):
            save_signal_time(signal_key)
            logging.info(f"Signal sent successfully: {signal} at ${current_price:.2f}")
            return signal
        
    else:
        logging.info("No crossover detected in current candle")
        return None

if __name__ == "__main__":
    logging.info(f"Gold Bot starting (10-min timeframe mode)")
    logging.info(f"Checking for EMA{EMA_SHORT}/{EMA_LONG} crossover on {SYMBOL}")
    
    try:
        signal_result = check_gold_signal()
        
        # Exit codes for cron monitoring
        if signal_result:
            logging.info(f"Signal generated: {signal_result}")
            sys.exit(0)  # Success with signal
        else:
            logging.info("No signal generated")
            sys.exit(1)  # No signal (not an error, just expected)
            
    except Exception as e:
        logging.error(f"Critical error in bot execution: {e}", exc_info=True)
        sys.exit(2)  # Error occurred