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
ALPHA_VANTAGE_KEY = 'YOUR_API_KEY'  # Get from https://www.alphavantage.co/support/#api-key
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

def fetch_gold_data_alphavantage():
    """Fetch gold data from Alpha Vantage"""
    try:
        # Alpha Vantage endpoint for gold (using FX symbol XAUUSD)
        url = f"https://www.alphavantage.co/query"
        params = {
            "function": "FX_INTRADAY",
            "from_symbol": "XAU",
            "to_symbol": "USD",
            "interval": "10min",
            "apikey": ALPHA_VANTAGE_KEY,
            "outputsize": "compact"  # Get latest 100 data points
        }
        
        logging.info("Fetching data from Alpha Vantage...")
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        
        data = response.json()
        
        # Check for error messages
        if "Error Message" in data:
            logging.error(f"Alpha Vantage API error: {data['Error Message']}")
            return None
            
        if "Note" in data:
            logging.warning(f"API rate limit: {data['Note']}")
            return None
        
        # Extract time series data
        time_series_key = "Time Series FX (10min)"
        if time_series_key not in data:
            logging.error(f"Unexpected response format: {list(data.keys())}")
            return None
            
        time_series = data[time_series_key]
        
        # Parse data into lists (most recent first)
        timestamps = []
        opens = []
        highs = []
        lows = []
        closes = []
        
        for timestamp, values in sorted(time_series.items(), reverse=True):
            timestamps.append(timestamp)
            opens.append(float(values['1. open']))
            highs.append(float(values['2. high']))
            lows.append(float(values['3. low']))
            closes.append(float(values['4. close']))
        
        # Reverse to get chronological order (oldest first)
        timestamps.reverse()
        opens.reverse()
        highs.reverse()
        lows.reverse()
        closes.reverse()
        
        logging.info(f"Successfully fetched {len(closes)} candles")
        
        return {
            'timestamps': timestamps,
            'open': opens,
            'high': highs,
            'low': lows,
            'close': closes
        }
        
    except requests.exceptions.RequestException as e:
        logging.error(f"Network error: {e}")
        return None
    except Exception as e:
        logging.error(f"Error fetching data: {e}", exc_info=True)
        return None

def fetch_gold_data_alternative():
    """Fallback: Use Twelve Data API (free tier available)"""
    try:
        # Twelve Data API (requires free registration)
        # Sign up at https://twelvedata.com/apikey
        TWELVE_DATA_KEY = "YOUR_TWELVE_DATA_KEY"  # Optional fallback
        
        url = f"https://api.twelvedata.com/time_series"
        params = {
            "symbol": "XAU/USD",
            "interval": "10min",
            "apikey": TWELVE_DATA_KEY,
            "outputsize": "200"
        }
        
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        if "values" not in data:
            logging.error(f"Twelve Data error: {data.get('message', 'Unknown error')}")
            return None
            
        values = data['values']
        
        highs = []
        lows = []
        closes = []
        
        for candle in values:
            highs.append(float(candle['high']))
            lows.append(float(candle['low']))
            closes.append(float(candle['close']))
        
        # Reverse to chronological order
        highs.reverse()
        lows.reverse()
        closes.reverse()
        
        logging.info(f"Successfully fetched {len(closes)} candles from Twelve Data")
        
        return {
            'high': highs,
            'low': lows,
            'close': closes
        }
        
    except Exception as e:
        logging.error(f"Twelve Data fallback failed: {e}")
        return None

def check_gold_signal():
    """Main monitoring function with Alpha Vantage"""
    logging.info(f"Scanning Gold on 10-min timeframe...")
    
    # Fetch data from Alpha Vantage
    data = fetch_gold_data_alphavantage()
    
    if data is None:
        logging.error("Failed to fetch data from primary source")
        # Try fallback if available
        data = fetch_gold_data_alternative()
        
    if data is None:
        logging.error("Failed to fetch data from all sources")
        send_telegram_message("⚠️ <b>Gold Bot Warning</b>\nUnable to fetch price data. Will retry on next schedule.")
        return None
    
    close_prices = data['close']
    high_prices = data['high']
    low_prices = data['low']
    
    if len(close_prices) < EMA_LONG + 20:
        logging.warning(f"Insufficient data: {len(close_prices)} candles, need {EMA_LONG + 20}")
        return None
    
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
    
    # Get current timestamp
    if 'timestamps' in data and data['timestamps']:
        current_time = data['timestamps'][-1]
    else:
        current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    # Calculate ADX
    adx, plus_di, minus_di = calculate_adx(high_prices, low_prices, close_prices, ADX_PERIOD)
    
    # Check for crossover
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
        signal_key = f"{signal}_{current_time}"
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
⏰ <b>Time:</b> {current_time}
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

if __name__ == "__main__":
    logging.info("Gold Bot starting with Alpha Vantage API")
    
    result = check_gold_signal()
    
    if result:
        sys.exit(0)
    else:
        sys.exit(1)
