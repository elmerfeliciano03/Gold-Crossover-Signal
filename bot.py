import yfinance as yf
import requests
import logging
import sys
import time
from datetime import datetime

# ------------------------------
# CONFIGURATION
# ------------------------------
TELEGRAM_TOKEN = 'YOUR_BOT_TOKEN'  # Replace with your bot token
CHAT_ID = 'YOUR_CHAT_ID'  # Replace with your Chat ID
SYMBOL = "GC=F"
CAPITAL = 10000
RISK_PERCENT = 0.5
REWARD_PERCENT = 1.0
EMA_SHORT = 50
EMA_LONG = 200
ADX_PERIOD = 14

LAST_SIGNAL_FILE = "/tmp/last_gold_signal.txt"

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)

def send_telegram_message(text):
    """Send message to Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID, 
        "text": text, 
        "parse_mode": "HTML"
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        logging.info("Message sent successfully")
        return True
    except Exception as e:
        logging.error(f"Failed to send: {e}")
        return False

def calculate_ema(data, period):
    """Calculate EMA without pandas"""
    if len(data) < period:
        return [None] * len(data)
    
    multiplier = 2 / (period + 1)
    ema_values = [None] * (period - 1)
    
    # Start with SMA
    sma = sum(data[:period]) / period
    ema_values.append(sma)
    
    # Calculate EMA recursively
    for i in range(period, len(data)):
        ema = (data[i] - ema_values[-1]) * multiplier + ema_values[-1]
        ema_values.append(ema)
    
    # Pad beginning with None to match original length
    while len(ema_values) < len(data):
        ema_values.insert(0, None)
    
    return ema_values

def calculate_adx(high, low, close, period=14):
    """Calculate ADX without pandas"""
    n = len(close)
    if n < period * 2:
        return 0, 0, 0
    
    plus_dm = [0] * n
    minus_dm = [0] * n
    tr = [0] * n
    
    for i in range(1, n):
        # Directional movement
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
        
        # True Range
        tr1 = high[i] - low[i]
        tr2 = abs(high[i] - close[i-1])
        tr3 = abs(low[i] - close[i-1])
        tr[i] = max(tr1, tr2, tr3)
    
    # Smooth with Wilder's method
    atr = [0] * n
    smooth_plus = [0] * n
    smooth_minus = [0] * n
    
    # Initial averages
    atr[period-1] = sum(tr[1:period]) / period
    smooth_plus[period-1] = sum(plus_dm[1:period]) / period
    smooth_minus[period-1] = sum(minus_dm[1:period]) / period
    
    # Wilder's smoothing
    for i in range(period, n):
        atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period
        smooth_plus[i] = (smooth_plus[i-1] * (period - 1) + plus_dm[i]) / period
        smooth_minus[i] = (smooth_minus[i-1] * (period - 1) + minus_dm[i]) / period
    
    # Calculate +DI and -DI
    plus_di = [0] * n
    minus_di = [0] * n
    for i in range(period, n):
        if atr[i] != 0:
            plus_di[i] = 100 * (smooth_plus[i] / atr[i])
            minus_di[i] = 100 * (smooth_minus[i] / atr[i])
    
    # Calculate DX
    dx = [0] * n
    for i in range(period, n):
        di_sum = plus_di[i] + minus_di[i]
        if di_sum != 0:
            dx[i] = 100 * abs(plus_di[i] - minus_di[i]) / di_sum
    
    # Calculate ADX (smoothed DX)
    adx = [0] * n
    adx_start = period * 2 - 2
    if adx_start < n:
        adx[adx_start] = sum(dx[period-1:period*2-1]) / period
        
        for i in range(adx_start + 1, n):
            adx[i] = (adx[i-1] * (period - 1) + dx[i]) / period
    
    return adx[-1], plus_di[-1], minus_di[-1]

def fetch_with_retry(max_retries=3, delay=5):
    """Fetch data with retry logic for transient errors"""
    for attempt in range(max_retries):
        try:
            logging.info(f"Fetching data for {SYMBOL} (attempt {attempt + 1}/{max_retries})")
            
            # Use Ticker object instead of download for better error handling
            ticker = yf.Ticker(SYMBOL)
            df = ticker.history(period="7d", interval="10m")
            
            # Critical: Always check if DataFrame is empty[citation:9]
            if df.empty:
                logging.warning(f"Empty DataFrame returned for {SYMBOL}")
                if attempt < max_retries - 1:
                    time.sleep(delay)
                    continue
                return None
            
            # Additional validation - check if we have enough data
            if len(df) < EMA_LONG + 10:
                logging.warning(f"Insufficient data: {len(df)} candles, need {EMA_LONG + 10}")
                if attempt < max_retries - 1:
                    time.sleep(delay)
                    continue
                return None
            
            logging.info(f"Successfully fetched {len(df)} candles")
            return df
            
        except Exception as e:
            logging.error(f"Attempt {attempt + 1} failed: {str(e)}")
            if attempt < max_retries - 1:
                logging.info(f"Retrying in {delay} seconds...")
                time.sleep(delay)
            else:
                logging.error("Max retries exceeded")
                return None
    
    return None

def check_gold_signal():
    """Main monitoring function with robust error handling"""
    logging.info(f"Scanning {SYMBOL} on 10-min timeframe...")
    
    # Fetch data with retry logic
    df = fetch_with_retry(max_retries=3, delay=5)
    
    if df is None:
        logging.error("Failed to fetch data after retries")
        # Send alert about API issues (optional)
        send_telegram_message("⚠️ <b>Gold Bot Warning</b>\nUnable to fetch data from Yahoo Finance. This is usually temporary. The bot will retry on next schedule.")
        return None
    
    try:
        # Extract data as lists
        close_prices = df['Close'].tolist()
        high_prices = df['High'].tolist()
        low_prices = df['Low'].tolist()
        
        # Calculate EMAs
        ema50 = calculate_ema(close_prices, EMA_SHORT)
        ema200 = calculate_ema(close_prices, EMA_LONG)
        
        # Get current values (last 2 candles)
        current_ema50 = ema50[-1]
        current_ema200 = ema200[-1]
        prev_ema50 = ema50[-2]
        prev_ema200 = ema200[-2]
        current_price = close_prices[-1]
        current_high = high_prices[-1]
        current_low = low_prices[-1]
        
        # Calculate ADX
        adx, plus_di, minus_di = calculate_adx(high_prices, low_prices, close_prices, ADX_PERIOD)
        
        # Check for crossover (ignore None values)
        if None in [current_ema50, current_ema200, prev_ema50, prev_ema200]:
            logging.info("Insufficient data for EMA calculation")
            return None
        
        # Detect signals
        signal = None
        crossover_type = None
        
        if prev_ema50 <= prev_ema200 and current_ema50 > current_ema200:
            signal = "BUY"
            crossover_type = "Golden Cross ↑"
            logging.info(f"BUY signal detected at ${current_price:.2f}")
        elif prev_ema50 >= prev_ema200 and current_ema50 < current_ema200:
            signal = "SELL"
            crossover_type = "Death Cross ↓"
            logging.info(f"SELL signal detected at ${current_price:.2f}")
        
        if signal:
            # Check for duplicate signals
            signal_key = f"{signal}_{df.index[-1]}"
            try:
                with open(LAST_SIGNAL_FILE, 'r') as f:
                    last_signal = f.read().strip()
                    if last_signal == signal_key:
                        logging.info("Duplicate signal prevented")
                        return None
            except FileNotFoundError:
                pass
            
            # Calculate risk management
            if signal == "BUY":
                stop_loss = current_price * (1 - RISK_PERCENT / 100)
                take_profit = current_price * (1 + REWARD_PERCENT / 100)
            else:
                stop_loss = current_price * (1 + RISK_PERCENT / 100)
                take_profit = current_price * (1 - REWARD_PERCENT / 100)
            
            quantity = CAPITAL / current_price
            risk_amount = CAPITAL * (RISK_PERCENT / 100)
            
            # ADX interpretation
            if adx > 25:
                adx_status = "🔥 STRONG TREND"
            elif adx < 20:
                adx_status = "⚠️ WEAK/RANGING"
            else:
                adx_status = "📊 MODERATE TREND"
            
            # Build message
            message = f"""🚨 <b>TRADE SIGNAL: {signal}</b> 🚨
━━━━━━━━━━━━━━━━━━━━
⏰ <b>Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}
💰 <b>Asset:</b> Gold (XAU/USD)
📈 <b>Signal:</b> {signal} - {crossover_type}
💵 <b>Entry:</b> ${current_price:.2f}
📊 <b>Range:</b> H: ${current_high:.2f} | L: ${current_low:.2f}
🛑 <b>Stop Loss:</b> ${stop_loss:.2f} ({RISK_PERCENT}%)
🎯 <b>Take Profit:</b> ${take_profit:.2f} ({REWARD_PERCENT}%)
━━━━━━━━━━━━━━━━━━━━
📊 <b>Technical Indicators:</b>
• EMA50: {current_ema50:.2f}
• EMA200: {current_ema200:.2f}
• Difference: {((current_ema50 - current_ema200)/current_ema200*100):.2f}%
• ADX: {adx:.1f} {adx_status}
• +DI: {plus_di:.1f} | -DI: {minus_di:.1f}
━━━━━━━━━━━━━━━━━━━━
💼 <b>Risk Management (1:2 R:R)</b>
• Position Size: ${CAPITAL:,}
• Quantity: {quantity:.4f} units
• Risk: ${risk_amount:.2f} | Reward: ${risk_amount * 2:.2f}
• R:R Ratio: 1:2 ✅
━━━━━━━━━━━━━━━━━━━━
<i>⚡ Always use stop losses. Not financial advice.</i>"""
            
            # Send message
            if send_telegram_message(message):
                with open(LAST_SIGNAL_FILE, 'w') as f:
                    f.write(signal_key)
                logging.info(f"Signal sent: {signal}")
                return signal
        
        logging.info("No crossover detected")
        return None
        
    except Exception as e:
        logging.error(f"Error in check_gold_signal: {e}", exc_info=True)
        return None

if __name__ == "__main__":
    logging.info("Gold Bot starting with retry logic")
    logging.info(f"Monitoring {SYMBOL} for EMA{EMA_SHORT}/{EMA_LONG} crossover")
    
    result = check_gold_signal()
    
    # Exit codes for monitoring
    if result:
        sys.exit(0)
    else:
        sys.exit(1)
