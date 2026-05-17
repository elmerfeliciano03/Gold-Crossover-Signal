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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ============ CONFIGURATION ============
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    log.error("❌ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID")
    exit(1)

# Cooldown file (prevents duplicate signals)
COOLDOWN_FILE = "/tmp/ema_signal_tracker.json"
SIGNAL_COOLDOWN = 43200  # 12 hours

# Asset configurations
ASSETS = {
    "GOLD": {
        "symbol": "GC=F",
        "display_name": "💰 GOLD",
        "risk_percent": 0.5,
        "profit_percent": 1.0,
        "position_size": 10000,
        "currency": "EUR",
        "mt5_units": 0.03
    },
    "SPY": {
        "symbol": "SPY",
        "display_name": "📈 SPY",
        "risk_percent": 2.0,
        "profit_percent": 4.0,
        "position_size": 2500,
        "currency": "EUR",
        "mt5_units": 0.03
    },
    "QQQ": {
        "symbol": "QQQ",
        "display_name": "🚀 QQQ",
        "risk_percent": 1.0,
        "profit_percent": 2.0,
        "position_size": 2500,
        "currency": "EUR",
        "mt5_units": None
    },
    "ETH": {
        "symbol": "ETH-USD",
        "display_name": "🔷 ETHEREUM",
        "risk_percent": 1.0,
        "profit_percent": 2.0,
        "position_size": 2500,
        "currency": "EUR",
        "mt5_units": None
    },
    "ADA": {
        "symbol": "ADA-USD",
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

def fetch_data(symbol):
    """Fetch 10-minute data from Yahoo Finance"""
    try:
        log.info(f"Fetching {symbol}...")
        ticker = yf.Ticker(symbol)
        # Get 7 days of 10-minute data
        df = ticker.history(period="7d", interval="10m")
        if df is not None and not df.empty:
            log.info(f"✅ Got {len(df)} bars for {symbol}")
            return df
        return None
    except Exception as e:
        log.error(f"Error fetching {symbol}: {e}")
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
        high = df['High'].values
        low = df['Low'].values
        close = df['Close'].values
        
        if len(high) < period + 1:
            return 0
        
        tr = []
        plus_dm = []
        minus_dm = []
        
        for i in range(1, len(high)):
            # True Range
            tr1 = high[i] - low[i]
            tr2 = abs(high[i] - close[i-1])
            tr3 = abs(low[i] - close[i-1])
            tr.append(max(tr1, tr2, tr3))
            
            # Directional Movement
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
    
    closes = df['Close'].values
    
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
    df = fetch_data(config['symbol'])
    if df is None or len(df) < 200:
        log.warning(f"⚠️ {asset_name}: Only {len(df) if df is not None else 0} bars")
        return None
    
    # Check crossover
    has_cross, signal_type = check_ema_crossover(df)
    if not has_cross:
        return None
    
    # Current values
    current_price = df['Close'].iloc[-1]
    closes = df['Close'].values
    
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
                # Check cooldown
                if check_signal_allowed(asset_name, result['signal_type']):
                    message = format_signal_message(asset_name, result)
                    if send_telegram_message(message):
                        save_signal_time(asset_name, result['signal_type'])
                        signals_sent += 1
                        log.info(f"✅ Signal SENT for {asset_name}")
                    else:
                        log.error(f"❌ Failed to send for {asset_name}")
                else:
                    log.info(f"⏭️ {asset_name} - in cooldown (12h)")
            else:
                log.info(f"📊 {asset_name} - No crossover")
            
            time.sleep(2)  # Rate limit
            
        except Exception as e:
            log.error(f"❌ Error with {asset_name}: {e}")
    
    log.info(f"\n✅ Complete - Sent {signals_sent} signal(s)\n")

if __name__ == "__main__":
    main()
