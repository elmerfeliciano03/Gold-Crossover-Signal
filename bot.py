"""
EMA Crossover Trading Bot - 10-Minute Timeframe
For Gold, SPY, QQQ, ETH, ADA
"""

import os
import logging
import requests
from datetime import datetime, timezone
import time
import yfinance as yf
import json
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ============ CONFIGURATION ============
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ALPHA_VANTAGE_KEY = os.environ.get("ALPHA_VANTAGE_API_KEY")  # Optional backup

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    log.error("❌ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID")
    exit(1)

# Cooldown file
COOLDOWN_FILE = "/tmp/ema_signal_tracker.json"
SIGNAL_COOLDOWN = 43200  # 12 hours

# Asset configurations with multiple symbols for fallback
ASSETS = {
    "GOLD": {
        "symbols": ["GC=F", "GLD", "XAUUSD=X"],
        "display_name": "💰 GOLD",
        "risk_percent": 0.5,
        "profit_percent": 1.0,
        "position_size": 10000,
        "currency": "EUR",
        "mt5_units": 0.03
    },
    "SPY": {
        "symbols": ["SPY", "VOO", "IVV"],
        "display_name": "📈 SPY",
        "risk_percent": 2.0,
        "profit_percent": 4.0,
        "position_size": 2500,
        "currency": "EUR",
        "mt5_units": 0.03
    },
    "QQQ": {
        "symbols": ["QQQ", "TQQQ"],
        "display_name": "🚀 QQQ",
        "risk_percent": 1.0,
        "profit_percent": 2.0,
        "position_size": 2500,
        "currency": "EUR",
        "mt5_units": None
    },
    "ETH": {
        "symbols": ["ETH-USD", "ETHUSD=X"],
        "display_name": "🔷 ETHEREUM",
        "risk_percent": 1.0,
        "profit_percent": 2.0,
        "position_size": 2500,
        "currency": "EUR",
        "mt5_units": None
    },
    "ADA": {
        "symbols": ["ADA-USD", "ADAUSD=X"],
        "display_name": "📊 CARDANO",
        "risk_percent": 1.0,
        "profit_percent": 2.0,
        "position_size": 2500,
        "currency": "EUR",
        "mt5_units": None
    }
}

def load_tracker():
    try:
        with open(COOLDOWN_FILE, 'r') as f:
            return json.load(f)
    except:
        return {}

def save_tracker(data):
    try:
        with open(COOLDOWN_FILE, 'w') as f:
            json.dump(data, f)
    except:
        pass

def check_signal_allowed(asset, signal_type):
    tracker = load_tracker()
    key = f"{asset}_{signal_type}"
    now = datetime.now(timezone.utc).timestamp()
    
    if key in tracker:
        last_time = tracker[key]
        if (now - last_time) < SIGNAL_COOLDOWN:
            return False
    return True

def save_signal_time(asset, signal_type):
    tracker = load_tracker()
    key = f"{asset}_{signal_type}"
    tracker[key] = datetime.now(timezone.utc).timestamp()
    save_tracker(tracker)

def fetch_yahoo_data(symbol):
    """Fetch data from Yahoo Finance with proper session"""
    try:
        # Create a session with proper headers
        session = requests.Session()
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        
        # Download with retry
        ticker = yf.Ticker(symbol, session=session)
        df = ticker.history(period="7d", interval="10m")
        
        if df is not None and not df.empty:
            log.info(f"✅ Yahoo: Got {len(df)} bars for {symbol}")
            return df
        return None
    except Exception as e:
        log.debug(f"Yahoo error for {symbol}: {e}")
        return None

def fetch_alpha_vantage_data(symbol, asset_type="stock"):
    """Fetch data from Alpha Vantage as fallback"""
    if not ALPHA_VANTAGE_KEY:
        return None
    
    try:
        if asset_type == "crypto":
            url = f"https://www.alphavantage.co/query?function=DIGITAL_CURRENCY_INTRADAY&symbol={symbol}&market=USD&interval=10min&apikey={ALPHA_VANTAGE_KEY}"
        else:
            url = f"https://www.alphavantage.co/query?function=TIME_SERIES_INTRADAY&symbol={symbol}&interval=10min&outputsize=full&apikey={ALPHA_VANTAGE_KEY}"
        
        response = requests.get(url, timeout=10)
        data = response.json()
        
        if 'Time Series' in data:
            time_series = data['Time Series']
            df = pd.DataFrame.from_dict(time_series, orient='index')
            df.index = pd.to_datetime(df.index)
            df = df.sort_index()
            df.columns = ['open', 'high', 'low', 'close', 'volume']
            df = df.astype(float)
            log.info(f"✅ Alpha Vantage: Got {len(df)} bars for {symbol}")
            return df
        elif 'Digital Currency Intraday' in data:
            time_series = data['Digital Currency Intraday']
            df = pd.DataFrame.from_dict(time_series, orient='index')
            df.index = pd.to_datetime(df.index)
            df = df.sort_index()
            df['close'] = df['4a. close (USD)'].astype(float)
            df['high'] = df['2a. high (USD)'].astype(float)
            df['low'] = df['3a. low (USD)'].astype(float)
            df['open'] = df['1a. open (USD)'].astype(float)
            log.info(f"✅ Alpha Vantage: Got {len(df)} crypto bars for {symbol}")
            return df
        return None
    except Exception as e:
        log.debug(f"Alpha Vantage error: {e}")
        return None

def fetch_data(asset_name, config):
    """Try multiple sources to get data"""
    # Try Yahoo Finance with multiple symbols
    for symbol in config['symbols']:
        df = fetch_yahoo_data(symbol)
        if df is not None and len(df) >= 100:
            return df
    
    # Try Alpha Vantage as fallback
    asset_type = "crypto" if asset_name in ['ETH', 'ADA'] else "stock"
    for symbol in config['symbols']:
        df = fetch_alpha_vantage_data(symbol, asset_type)
        if df is not None and len(df) >= 100:
            return df
    
    return None

def calculate_ema(prices, period):
    """Calculate EMA"""
    if len(prices) < period:
        return None
    
    multiplier = 2 / (period + 1)
    ema = prices[0]
    
    for price in prices[1:]:
        ema = (price - ema) * multiplier + ema
    
    return ema

def calculate_ema_series(prices, period):
    """Calculate full EMA series"""
    if len(prices) < period:
        return []
    
    multiplier = 2 / (period + 1)
    ema_values = []
    current_ema = prices[0]
    
    for price in prices:
        current_ema = (price - current_ema) * multiplier + current_ema
        ema_values.append(current_ema)
    
    return ema_values

def calculate_adx(df, period=14):
    """Calculate ADX indicator"""
    try:
        high = df['high'].values if 'high' in df.columns else df['High'].values
        low = df['low'].values if 'low' in df.columns else df['Low'].values
        close = df['close'].values if 'close' in df.columns else df['Close'].values
        
        if len(high) < period + 1:
            return 0
        
        tr = []
        plus_dm = []
        minus_dm = []
        
        for i in range(1, len(high)):
            tr1 = high[i] - low[i]
            tr2 = abs(high[i] - close[i-1])
            tr3 = abs(low[i] - close[i-1])
            tr.append(max(tr1, tr2, tr3))
            
            up_move = high[i] - high[i-1]
            down_move = low[i-1] - low[i]
            
            plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0)
            minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0)
        
        # Simple averages
        atr = sum(tr[:period]) / period
        avg_plus = sum(plus_dm[:period]) / period
        avg_minus = sum(minus_dm[:period]) / period
        
        if atr == 0:
            return 0
        
        plus_di = 100 * (avg_plus / atr)
        minus_di = 100 * (avg_minus / atr)
        
        if plus_di + minus_di == 0:
            return 0
        
        dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
        return dx
        
    except Exception as e:
        log.debug(f"ADX error: {e}")
        return 0

def check_ema_crossover(df):
    """Check if EMA200 crossed EMA50 in the last bar"""
    if df is None or len(df) < 200:
        return False, None
    
    close_col = 'close' if 'close' in df.columns else 'Close'
    closes = df[close_col].values
    
    # Calculate EMAs
    ema50_series = calculate_ema_series(closes, 50)
    ema200_series = calculate_ema_series(closes, 200)
    
    if len(ema50_series) < 2 or len(ema200_series) < 2:
        return False, None
    
    current_ema50 = ema50_series[-1]
    current_ema200 = ema200_series[-1]
    prev_ema50 = ema50_series[-2]
    prev_ema200 = ema200_series[-2]
    
    # Bullish: EMA200 crosses above EMA50
    if prev_ema200 <= prev_ema50 and current_ema200 > current_ema50:
        log.info(f"📈 BULLISH crossover! EMA200: {current_ema200:.2f} > EMA50: {current_ema50:.2f}")
        return True, "BULLISH"
    
    # Bearish: EMA200 crosses below EMA50
    if prev_ema200 >= prev_ema50 and current_ema200 < current_ema50:
        log.info(f"📉 BEARISH crossover! EMA200: {current_ema200:.2f} < EMA50: {current_ema50:.2f}")
        return True, "BEARISH"
    
    return False, None

def calculate_risk_reward(price, signal_type, risk_pct, profit_pct):
    """Calculate entry, stop loss, take profit"""
    if signal_type == "BULLISH":
        entry = price
        stop_loss = entry * (1 - risk_pct / 100)
        take_profit = entry * (1 + profit_pct / 100)
    else:
        entry = price
        stop_loss = entry * (1 + risk_pct / 100)
        take_profit = entry * (1 - profit_pct / 100)
    
    risk_amount = abs(entry - stop_loss)
    reward_amount = abs(take_profit - entry)
    ratio = round(reward_amount / risk_amount, 2) if risk_amount > 0 else 0
    
    return {
        'entry': round(entry, 4),
        'stop_loss': round(stop_loss, 4),
        'take_profit': round(take_profit, 4),
        'risk_pct': risk_pct,
        'profit_pct': profit_pct,
        'ratio': ratio
    }

def send_telegram_message(message):
    """Send message to Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        response = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
        return response.status_code == 200
    except Exception as e:
        log.error(f"Telegram error: {e}")
        return False

def analyze_asset(asset_name, config):
    """Analyze single asset"""
    log.info(f"\n{'='*40}")
    log.info(f"🔍 Analyzing {asset_name}...")
    
    # Fetch data
    df = fetch_data(asset_name, config)
    if df is None or len(df) < 200:
        log.warning(f"⚠️ {asset_name}: Only {len(df) if df is not None else 0} bars")
        return None
    
    # Get close column name
    close_col = 'close' if 'close' in df.columns else 'Close'
    
    # Check crossover
    has_cross, signal_type = check_ema_crossover(df)
    if not has_cross:
        return None
    
    # Current values
    current_price = df[close_col].iloc[-1]
    closes = df[close_col].values
    
    # Calculate EMAs for display
    ema50_series = calculate_ema_series(closes, 50)
    ema200_series = calculate_ema_series(closes, 200)
    
    ema50 = ema50_series[-1] if ema50_series else 0
    ema200 = ema200_series[-1] if ema200_series else 0
    
    # Calculate ADX
    adx = calculate_adx(df, 14)
    
    # ADX interpretation
    if adx > 40:
        adx_text = f"{adx:.1f} 🔥 VERY STRONG"
    elif adx > 25:
        adx_text = f"{adx:.1f} ✅ STRONG"
    elif adx > 20:
        adx_text = f"{adx:.1f} 📊 MODERATE"
    else:
        adx_text = f"{adx:.1f} ⚠️ WEAK"
    
    # Risk/Reward
    rr = calculate_risk_reward(
        current_price, signal_type,
        config['risk_percent'],
        config['profit_percent']
    )
    
    # Position sizing for Trading 212
    shares = int(config['position_size'] / rr['entry']) if rr['entry'] > 0 else 0
    position_value = shares * rr['entry']
    total_risk = shares * abs(rr['entry'] - rr['stop_loss'])
    
    log.info(f"✅ SIGNAL: {signal_type} at ${current_price:.2f}")
    log.info(f"   EMA50: ${ema50:.2f}, EMA200: ${ema200:.2f}")
    log.info(f"   ADX: {adx:.1f}, Entry: ${rr['entry']}")
    
    return {
        'signal_type': signal_type,
        'price': current_price,
        'ema50': ema50,
        'ema200': ema200,
        'adx': adx_text,
        'rr': rr,
        'shares': shares,
        'position_value': position_value,
        'total_risk': total_risk,
        'config': config
    }

def format_signal_message(asset_name, data):
    """Format the signal message for Telegram"""
    config = data['config']
    rr = data['rr']
    
    arrow = "📈" if data['signal_type'] == "BULLISH" else "📉"
    direction = "🟢 BULLISH (LONG)" if data['signal_type'] == "BULLISH" else "🔴 BEARISH (SHORT)"
    
    message = f"""<b>{arrow} {config['display_name']} - {direction} SIGNAL {arrow}</b>

━━━━━━━━━━━━━━━━━━━━━
📊 <b>EMA CROSSOVER (10-Min)</b>
━━━━━━━━━━━━━━━━━━━━━
• EMA50: ${data['ema50']:.2f}
• EMA200: ${data['ema200']:.2f}
• <b>EMA200 crossed {data['signal_type'].lower()} EMA50</b>

━━━━━━━━━━━━━━━━━━━━━
📈 <b>ADX TREND STRENGTH</b>
━━━━━━━━━━━━━━━━━━━━━
• ADX: {data['adx']}

━━━━━━━━━━━━━━━━━━━━━
⚡ <b>RISK MANAGEMENT</b>
━━━━━━━━━━━━━━━━━━━━━
• Risk: {rr['risk_pct']}% from entry
• Take Profit: {rr['profit_pct']}% from entry
• Risk:Reward: 1:{rr['ratio']}

📍 <b>Levels:</b>
• Entry: ${rr['entry']}
• Stop Loss: ${rr['stop_loss']}
• Take Profit: ${rr['take_profit']}

━━━━━━━━━━━━━━━━━━━━━
💼 <b>POSITION SIZING ({config['currency']})</b>
━━━━━━━━━━━━━━━━━━━━━
📱 <b>Trading 212:</b>
• Capital: {config['position_size']:,}
• Shares: {data['shares']:,} units
• Position Value: {data['position_value']:,.2f}
• Total Risk: {data['total_risk']:,.2f}
"""
    
    # Add MT5 info for Gold and SPY
    if config.get('mt5_units') and asset_name in ['GOLD', 'SPY']:
        if asset_name == 'GOLD':
            risk_pips = abs(rr['entry'] - rr['stop_loss']) / 0.01
            message += f"""
💹 <b>MT5:</b>
• Units: {config['mt5_units']}
• Risk: {risk_pips:.1f} pips
• Approx Risk: ${risk_pips * config['mt5_units'] * 10:.2f}
"""
        else:
            risk_points = abs(rr['entry'] - rr['stop_loss'])
            message += f"""
💹 <b>MT5:</b>
• Units: {config['mt5_units']}
• Risk: {risk_points:.2f} points
"""
    
    message += f"""
━━━━━━━━━━━━━━━━━━━━━
💰 <b>Current Price:</b> ${data['price']:.2f}
⏰ <b>Time:</b> {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC

⚠️ <i>Disclaimer: For educational purposes only.</i>
"""
    return message

def main():
    log.info("=" * 70)
    log.info("🚀 EMA CROSSOVER TRADING BOT - 10-MIN TIMEFRAME")
    log.info("=" * 70)
    log.info("Monitoring: Gold, SPY, QQQ, ETH, ADA")
    log.info("Strategy: EMA200 crosses EMA50 on 10-min chart")
    log.info("=" * 70)
    
    signals_sent = 0
    
    for asset_name, config in ASSETS.items():
        try:
            result = analyze_asset(asset_name, config)
            
            if result:
                if check_signal_allowed(asset_name, result['signal_type']):
                    message = format_signal_message(asset_name, result)
                    if send_telegram_message(message):
                        save_signal_time(asset_name, result['signal_type'])
                        signals_sent += 1
                        log.info(f"✅ Signal SENT for {asset_name}")
                    else:
                        log.error(f"❌ Failed to send for {asset_name}")
                else:
                    log.info(f"⏭️ {asset_name} - in cooldown")
            else:
                log.info(f"📊 {asset_name} - No crossover")
            
            time.sleep(3)
            
        except Exception as e:
            log.error(f"❌ Error with {asset_name}: {e}")
    
    log.info(f"\n✅ Complete - Sent {signals_sent} signal(s)\n")

if __name__ == "__main__":
    main()
