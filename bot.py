"""
EMA Crossover Trading Bot - 10-Minute Timeframe
Using Alpha Vantage API (works on Render)
"""

import os
import logging
import requests
from datetime import datetime, timezone
import time
import json

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ============ CONFIGURATION ============
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ALPHA_VANTAGE_KEY = os.environ.get("ALPHA_VANTAGE_API_KEY")

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    log.error("❌ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID")
    exit(1)

if not ALPHA_VANTAGE_KEY:
    log.error("❌ Missing ALPHA_VANTAGE_API_KEY - Get one free at https://www.alphavantage.co/support/#api-key")
    exit(1)

# Cooldown file
COOLDOWN_FILE = "/tmp/ema_signal_tracker.json"
SIGNAL_COOLDOWN = 43200  # 12 hours

# Asset configurations for Alpha Vantage
ASSETS = {
    "GOLD": {
        "symbol": "XAUUSD",
        "alpha_symbol": "XAUUSD",
        "display_name": "💰 GOLD",
        "risk_percent": 0.5,
        "profit_percent": 1.0,
        "position_size": 10000,
        "currency": "EUR",
        "mt5_units": 0.03
    },
    "SPY": {
        "symbol": "SPY",
        "alpha_symbol": "SPY",
        "display_name": "📈 SPY",
        "risk_percent": 2.0,
        "profit_percent": 4.0,
        "position_size": 2500,
        "currency": "EUR",
        "mt5_units": 0.03
    },
    "QQQ": {
        "symbol": "QQQ",
        "alpha_symbol": "QQQ",
        "display_name": "🚀 QQQ",
        "risk_percent": 1.0,
        "profit_percent": 2.0,
        "position_size": 2500,
        "currency": "EUR",
        "mt5_units": None
    }
}

# Crypto assets (different API endpoint)
CRYPTO_ASSETS = {
    "ETH": {
        "symbol": "ETH",
        "display_name": "🔷 ETHEREUM",
        "risk_percent": 1.0,
        "profit_percent": 2.0,
        "position_size": 2500,
        "currency": "EUR",
        "mt5_units": None
    },
    "ADA": {
        "symbol": "ADA",
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
            hours_left = (SIGNAL_COOLDOWN - (now - last_time)) / 3600
            log.info(f"⏭️ {asset} {signal_type} - cooldown ({hours_left:.1f}h left)")
            return False
    return True

def save_signal_time(asset, signal_type):
    tracker = load_tracker()
    key = f"{asset}_{signal_type}"
    tracker[key] = datetime.now(timezone.utc).timestamp()
    save_tracker(tracker)

def fetch_stock_data(symbol):
    """Fetch stock/ETF data from Alpha Vantage"""
    try:
        url = f"https://www.alphavantage.co/query?function=TIME_SERIES_INTRADAY&symbol={symbol}&interval=10min&outputsize=full&apikey={ALPHA_VANTAGE_KEY}"
        log.info(f"Fetching {symbol} from Alpha Vantage...")
        
        response = requests.get(url, timeout=15)
        data = response.json()
        
        if 'Error Message' in data:
            log.error(f"Alpha Vantage error for {symbol}: {data['Error Message']}")
            return None
        
        if 'Note' in data:
            log.warning(f"API rate limit: {data['Note']}")
            return None
        
        if 'Time Series (10min)' not in data:
            log.error(f"No time series data for {symbol}")
            return None
        
        time_series = data['Time Series (10min)']
        
        # Convert to list of dicts
        prices = []
        for timestamp, values in sorted(time_series.items()):
            prices.append({
                'timestamp': timestamp,
                'open': float(values['1. open']),
                'high': float(values['2. high']),
                'low': float(values['3. low']),
                'close': float(values['4. close']),
                'volume': float(values['5. volume'])
            })
        
        log.info(f"✅ Got {len(prices)} bars for {symbol}")
        return prices
        
    except Exception as e:
        log.error(f"Error fetching {symbol}: {e}")
        return None

def fetch_crypto_data(symbol):
    """Fetch crypto data from Alpha Vantage"""
    try:
        url = f"https://www.alphavantage.co/query?function=DIGITAL_CURRENCY_INTRADAY&symbol={symbol}&market=USD&apikey={ALPHA_VANTAGE_KEY}"
        log.info(f"Fetching crypto {symbol} from Alpha Vantage...")
        
        response = requests.get(url, timeout=15)
        data = response.json()
        
        if 'Error Message' in data:
            log.error(f"Alpha Vantage error for {symbol}: {data['Error Message']}")
            return None
        
        if 'Note' in data:
            log.warning(f"API rate limit: {data['Note']}")
            return None
        
        if 'Time Series Digital Currency Intraday' not in data:
            log.error(f"No crypto data for {symbol}")
            return None
        
        time_series = data['Time Series Digital Currency Intraday']
        
        prices = []
        for timestamp, values in sorted(time_series.items()):
            prices.append({
                'timestamp': timestamp,
                'open': float(values['1a. open (USD)']),
                'high': float(values['2a. high (USD)']),
                'low': float(values['3a. low (USD)']),
                'close': float(values['4a. close (USD)']),
                'volume': float(values['5. volume'])
            })
        
        log.info(f"✅ Got {len(prices)} bars for {symbol}")
        return prices
        
    except Exception as e:
        log.error(f"Error fetching crypto {symbol}: {e}")
        return None

def calculate_ema_series(prices, period):
    """Calculate EMA series from price list"""
    if len(prices) < period:
        return []
    
    multiplier = 2 / (period + 1)
    ema_values = []
    current_ema = prices[0]
    
    for price in prices:
        current_ema = (price - current_ema) * multiplier + current_ema
        ema_values.append(current_ema)
    
    return ema_values

def calculate_adx(prices, period=14):
    """Calculate ADX from price data"""
    if len(prices) < period * 2:
        return 0
    
    tr = []
    plus_dm = []
    minus_dm = []
    
    for i in range(1, len(prices)):
        high = prices[i]['high']
        low = prices[i]['low']
        prev_close = prices[i-1]['close']
        
        # True Range
        tr1 = high - low
        tr2 = abs(high - prev_close)
        tr3 = abs(low - prev_close)
        tr.append(max(tr1, tr2, tr3))
        
        # Directional Movement
        up_move = high - prices[i-1]['high']
        down_move = prices[i-1]['low'] - low
        
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

def check_ema_crossover(prices):
    """Check if EMA200 crossed EMA50"""
    if len(prices) < 200:
        return False, None
    
    closes = [p['close'] for p in prices]
    
    ema50 = calculate_ema_series(closes, 50)
    ema200 = calculate_ema_series(closes, 200)
    
    if len(ema50) < 2 or len(ema200) < 2:
        return False, None
    
    current_ema50 = ema50[-1]
    current_ema200 = ema200[-1]
    prev_ema50 = ema50[-2]
    prev_ema200 = ema200[-2]
    
    # Bullish: EMA200 crosses above EMA50
    if prev_ema200 <= prev_ema50 and current_ema200 > current_ema50:
        return True, "BULLISH"
    
    # Bearish: EMA200 crosses below EMA50
    if prev_ema200 >= prev_ema50 and current_ema200 < current_ema50:
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

def analyze_asset(asset_name, config, is_crypto=False):
    """Analyze single asset"""
    log.info(f"\n{'='*40}")
    log.info(f"🔍 Analyzing {asset_name}...")
    
    # Fetch data
    if is_crypto:
        prices = fetch_crypto_data(config['symbol'])
    else:
        prices = fetch_stock_data(config['symbol'])
    
    if not prices or len(prices) < 200:
        log.warning(f"⚠️ {asset_name}: Only {len(prices) if prices else 0} bars")
        return None
    
    # Check crossover
    has_cross, signal_type = check_ema_crossover(prices)
    if not has_cross:
        return None
    
    # Current values
    current_price = prices[-1]['close']
    closes = [p['close'] for p in prices]
    
    ema50_series = calculate_ema_series(closes, 50)
    ema200_series = calculate_ema_series(closes, 200)
    
    ema50 = ema50_series[-1] if ema50_series else 0
    ema200 = ema200_series[-1] if ema200_series else 0
    
    # Calculate ADX
    adx = calculate_adx(prices, 14)
    
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
    
    # Position sizing
    shares = int(config['position_size'] / rr['entry']) if rr['entry'] > 0 else 0
    position_value = shares * rr['entry']
    total_risk = shares * abs(rr['entry'] - rr['stop_loss'])
    
    log.info(f"✅ SIGNAL: {signal_type} at ${current_price:.2f}")
    log.info(f"   EMA50: ${ema50:.2f}, EMA200: ${ema200:.2f}")
    log.info(f"   ADX: {adx:.1f}")
    
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
        'config': config,
        'asset_name': asset_name
    }

def format_signal_message(data):
    """Format signal for Telegram"""
    config = data['config']
    rr = data['rr']
    asset_name = data['asset_name']
    
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
    log.info(f"Using Alpha Vantage API Key: {ALPHA_VANTAGE_KEY[:8]}...")
    log.info("Monitoring: Gold, SPY, QQQ, ETH, ADA")
    log.info("=" * 70)
    
    signals_sent = 0
    
    # Analyze stocks/ETFs
    for asset_name, config in ASSETS.items():
        try:
            result = analyze_asset(asset_name, config, is_crypto=False)
            
            if result:
                if check_signal_allowed(asset_name, result['signal_type']):
                    message = format_signal_message(result)
                    if send_telegram_message(message):
                        save_signal_time(asset_name, result['signal_type'])
                        signals_sent += 1
                        log.info(f"✅ Signal SENT for {asset_name}")
                    else:
                        log.error(f"❌ Failed to send for {asset_name}")
                else:
                    log.info(f"⏭️ {asset_name} - cooldown active")
            else:
                log.info(f"📊 {asset_name} - No crossover")
            
            time.sleep(12)  # Alpha Vantage free tier: 5 calls per minute
            
        except Exception as e:
            log.error(f"❌ Error with {asset_name}: {e}")
    
    # Analyze crypto
    for asset_name, config in CRYPTO_ASSETS.items():
        try:
            result = analyze_asset(asset_name, config, is_crypto=True)
            
            if result:
                if check_signal_allowed(asset_name, result['signal_type']):
                    message = format_signal_message(result)
                    if send_telegram_message(message):
                        save_signal_time(asset_name, result['signal_type'])
                        signals_sent += 1
                        log.info(f"✅ Signal SENT for {asset_name}")
                    else:
                        log.error(f"❌ Failed to send for {asset_name}")
                else:
                    log.info(f"⏭️ {asset_name} - cooldown active")
            else:
                log.info(f"📊 {asset_name} - No crossover")
            
            time.sleep(12)  # Rate limiting
            
        except Exception as e:
            log.error(f"❌ Error with {asset_name}: {e}")
    
    log.info(f"\n✅ Complete - Sent {signals_sent} signal(s)\n")

if __name__ == "__main__":
    main()
