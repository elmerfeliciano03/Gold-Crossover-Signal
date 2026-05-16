import yfinance as yf
import requests
import logging
import sys
import time
from datetime import datetime

# ------------------------------
# CONFIGURATION - UPDATE THESE!
# ------------------------------
TELEGRAM_TOKEN = 'YOUR_BOT_TOKEN_HERE'  # REPLACE with your actual bot token
CHAT_ID = 'YOUR_CHAT_ID_HERE'  # REPLACE with your actual chat ID

# Use GLD instead of GC=F - GLD is an ETF that tracks gold price perfectly
SYMBOL = "GLD"  # SPDR Gold Shares ETF (tracks gold price almost 1:1)

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
    if TELEGRAM_TOKEN == 'YOUR_BOT_TOKEN_HERE' or CHAT_ID == 'YOUR_CHAT_ID_HERE':
        logging.error("Telegram credentials not configured!")
        return False
        
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
    
    sma = sum(data[:period]) / period
    ema_values.append(sma)
    
    for i in range(period, len(data)):
        ema = (data[i] - ema_values[-1]) * multiplier + ema_values[-1]
        ema_values.append(ema)
    
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
        
        tr1 = high[i] - low[i]
        tr2 = abs(high[i] - close[i-1])
        tr3 = abs(low[i] - close[i-1])
        tr[i] = max(tr1, tr2, tr3)
    
    atr = [0] * n
    smooth_plus = [0] * n
    smooth_minus = [0] * n
    
    atr[period-1] = sum(tr[1:period]) / period
    smooth_plus[period-1] = sum(plus_dm[1:period]) / period
    smooth_minus[period-1] = sum(minus_dm[1:period]) / period
    
    for i in range(period, n):
        atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period
        smooth_plus[i] = (smooth_plus[i-1] * (period - 1) + plus_dm[i]) / period
        smooth_minus[i] = (smooth_minus[i-1] * (period - 1) + minus_dm[i]) / period
    
    plus_di = [0] * n
    minus_di = [0] * n
    for i in range(period, n):
        if atr[i] != 0:
            plus_di[i] = 100 * (smooth_plus[i] / atr[i])
            minus_di[i] = 100 * (smooth_minus[i] / atr[i])
    
    dx = [0] * n
    for i in range(period, n):
        di_sum = plus_di[i] + minus_di[i]
        if di_sum != 0:
            dx[i] = 100 * abs(plus_di[i] - minus_di[i]) / di_sum
    
    adx = [0] * n
    adx_start = period * 2 - 2
    if adx_start < n:
        adx[adx_start] = sum(dx[period-1:period*2-1]) / period
        
        for i in range(adx_start + 1, n):
            adx[i] = (adx[i-1] * (period - 1) + dx[i]) / period
    
    return adx[-1], plus_di[-1], minus_di[-1]

def fetch_data_with_retry(max_retries=3, delay=3):
    """Fetch data with retry logic"""
    for attempt in range(max_retries):
        try:
            logging.info(f"Fetching {SYMBOL} data (attempt {attempt + 1}/{max_retries})")
            
            # Use different interval and period combination
            ticker = yf.Ticker(SYMBOL)
            
            # Try to get 5 days of 10-minute data
            df = ticker.history(period="5d", interval="10m")
            
            if df.empty:
                logging.warning(f"Empty data for {SYMBOL}, trying with different parameters...")
                # Fallback: try daily data to at least test
                df = ticker.history(period="1mo", interval="1d")
                if df.empty:
                    if attempt < max_retries - 1:
                        time.sleep(delay)
                        continue
                    return None
            
            # Check if we have enough data
            if len(df) < 50:
                logging.warning(f"Only {len(df)} candles available")
                if attempt < max_retries - 1:
                    time.sleep(delay)
                    continue
                return None
            
            logging.info(f"Successfully fetched {len(df)} candles")
            return df
            
        except Exception as e:
            logging.error(f"Attempt {attempt + 1} failed: {e}")
            if attempt < max_retries - 1:
                time.sleep(delay)
            else:
                return None
    
    return None

def check_gold_signal():
    """Main monitoring function"""
    logging.info(f"Scanning {SYMBOL} for EMA crossover...")
    
    df = fetch_data_with_retry()
    
    if df is None:
        logging.error("Failed to fetch data after retries")
        return None
    
    try:
        close_prices = df['Close'].tolist()
        high_prices = df['High'].tolist()
        low_prices = df['Low'].tolist()
        
        # Check minimum data requirement
        if len(close_prices) < EMA_LONG + 20:
            logging.warning(f"Insufficient data: {len(close_prices)} candles")
            return None
        
        # Calculate indicators
        ema50 = calculate_ema(close_prices, EMA_SHORT)
        ema200 = calculate_ema(close_prices, EMA_LONG)
        
        # Remove None values at start
        valid_ema50 = [x for x in ema50 if x is not None]
        valid_ema200 = [x for x in ema200 if x is not None]
        
        if len(valid_ema50) < 2 or len(valid_ema200) < 2:
            logging.info("Not enough valid EMA data")
            return None
        
        # Get current values
        current_ema50 = valid_ema50[-1]
        current_ema200 = valid_ema200[-1]
        prev_ema50 = valid_ema50[-2]
        prev_ema200 = valid_ema200[-2]
        current_price = close_prices[-1]
        
        # Calculate ADX
        adx, plus_di, minus_di = calculate_adx(high_prices, low_prices, close_prices, ADX_PERIOD)
        
        # Detect crossover
        signal = None
        if prev_ema50 <= prev_ema200 and current_ema50 > current_ema200:
            signal = "BUY"
            logging.info(f"🔔 BUY signal at ${current_price:.2f}")
        elif prev_ema50 >= prev_ema200 and current_ema50 < current_ema200:
            signal = "SELL"
            logging.info(f"🔔 SELL signal at ${current_price:.2f}")
        
        if signal:
            # Risk calculations
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
                adx_status = "🔥 STRONG TREND - Good for trading"
            elif adx < 20:
                adx_status = "⚠️ WEAK/RANGING - Trade with caution"
            else:
                adx_status = "📊 MODERATE TREND"
            
            message = f"""🚨 <b>{signal} SIGNAL</b> 🚨
━━━━━━━━━━━━━━━━━━━━
💰 Asset: Gold ({SYMBOL})
💵 Entry: ${current_price:.2f}
🛑 Stop Loss: ${stop_loss:.2f} ({RISK_PERCENT}%)
🎯 Take Profit: ${take_profit:.2f} ({REWARD_PERCENT}%)
━━━━━━━━━━━━━━━━━━━━
📊 Technicals:
• EMA50: {current_ema50:.2f}
• EMA200: {current_ema200:.2f}
• ADX: {adx:.1f} - {adx_status}
• +DI: {plus_di:.1f} | -DI: {minus_di:.1f}
━━━━━━━━━━━━━━━━━━━━
💼 Risk: ${risk_amount:.2f} | Reward: ${risk_amount*2:.2f}
📐 R:R = 1:2 ✅
━━━━━━━━━━━━━━━━━━━━
<i>Position: ${CAPITAL:,} | Always use stops!</i>"""
            
            send_telegram_message(message)
            return signal
        
        logging.info("No crossover detected")
        return None
        
    except Exception as e:
        logging.error(f"Error: {e}", exc_info=True)
        return None

if __name__ == "__main__":
    logging.info(f"Gold Bot starting - Monitoring {SYMBOL}")
    logging.info(f"Timeframe: 10-min candles | Checking EMA{EMA_SHORT}/{EMA_LONG}")
    
    result = check_gold_signal()
    
    # Send startup message on first run (optional)
    if result:
        sys.exit(0)
    else:
        sys.exit(1)
