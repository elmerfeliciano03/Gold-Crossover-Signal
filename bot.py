"""
QQQ/SPY/GOLD/ETH/ADA Trading Bot - EMA200/50 Crossover with ADX
10-Minute Timeframe with Risk/Reward Calculations
"""

import os
import logging
import requests
from datetime import datetime, timezone
import time
import numpy as np
import yfinance as yf
import json

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ============ CONFIGURATION ============
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Use persistent file for cooldown tracking
COOLDOWN_FILE = "/tmp/ema_signal_tracker.json"
SIGNAL_COOLDOWN = 43200  # 12 hours between same asset+signal

# Asset configurations
ASSETS = {
    "GOLD": {
        "symbol": "GC=F",
        "display_name": "💰 GOLD",
        "risk_percent": 0.5,
        "profit_percent": 1.0,
        "position_size": 10000,
        "currency": "EUR",
        "mt5_units": 0.03,
        "asset_type": "commodity"
    },
    "SPY": {
        "symbol": "SPY",
        "display_name": "📈 SPY",
        "risk_percent": 2.0,
        "profit_percent": 4.0,
        "position_size": 2500,
        "currency": "EUR",
        "mt5_units": 0.03,
        "asset_type": "etf"
    },
    "QQQ": {
        "symbol": "QQQ",
        "display_name": "🚀 QQQ",
        "risk_percent": 1.0,
        "profit_percent": 2.0,
        "position_size": 2500,
        "currency": "EUR",
        "mt5_units": None,
        "asset_type": "etf"
    },
    "ETH": {
        "symbol": "ETH-USD",
        "display_name": "🔷 ETHEREUM",
        "risk_percent": 1.0,
        "profit_percent": 2.0,
        "position_size": 2500,
        "currency": "EUR",
        "mt5_units": None,
        "asset_type": "crypto"
    },
    "ADA": {
        "symbol": "ADA-USD",
        "display_name": "📊 CARDANO",
        "risk_percent": 1.0,
        "profit_percent": 2.0,
        "position_size": 2500,
        "currency": "EUR",
        "mt5_units": None,
        "asset_type": "crypto"
    }
}

# ============ PERSISTENT TRACKING ============
def load_tracker(file_path):
    try:
        with open(file_path, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_tracker(file_path, data):
    try:
        with open(file_path, 'w') as f:
            json.dump(data, f)
    except Exception as e:
        log.debug(f"Failed to save tracker: {e}")

def check_signal_allowed(asset, signal_type):
    tracker = load_tracker(COOLDOWN_FILE)
    key = f"{asset}_{signal_type}"
    now = datetime.now(timezone.utc).timestamp()
    
    if key in tracker:
        last_time = tracker[key]
        if (now - last_time) < SIGNAL_COOLDOWN:
            hours_left = (SIGNAL_COOLDOWN - (now - last_time)) / 3600
            return False, f"cooldown ({hours_left:.1f} hours remaining)"
    return True, "allowed"

def save_signal_time(asset, signal_type):
    tracker = load_tracker(COOLDOWN_FILE)
    key = f"{asset}_{signal_type}"
    tracker[key] = datetime.now(timezone.utc).timestamp()
    save_tracker(COOLDOWN_FILE, tracker)

# ============ DATA FETCHING ============
def fetch_data(symbol, interval, period="7d"):
    """Fetch data from Yahoo Finance"""
    try:
        log.info(f"Fetching {symbol} with interval {interval}...")
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period, interval=interval)
        if df is not None and not df.empty:
            log.info(f"✅ Got {len(df)} bars for {symbol}")
            return df
        else:
            log.warning(f"⚠️ No data for {symbol}")
            return None
    except Exception as e:
        log.error(f"Error fetching {symbol}: {e}")
        return None

# ============ TECHNICAL INDICATORS ============
def calculate_ema(prices, period):
    """Calculate EMA"""
    if len(prices) < period:
        return None
    multiplier = 2 / (period + 1)
    ema = prices[0]
    for price in prices[1:]:
        ema = (price - ema) * multiplier + ema
    return ema

def get_ema_series(prices, period):
    """Get full EMA series"""
    if len(prices) < period:
        return []
    ema_values = []
    multiplier = 2 / (period + 1)
    ema = prices[0]
    for price in prices:
        ema = (price - ema) * multiplier + ema
        ema_values.append(ema)
    return ema_values

def calculate_adx(df, period=14):
    """Calculate ADX indicator"""
    try:
        high = df['High'].values
        low = df['Low'].values
        close = df['Close'].values
        
        if len(high) < period + 1:
            return 0
        
        # Calculate True Range
        tr = []
        for i in range(1, len(high)):
            tr1 = high[i] - low[i]
            tr2 = abs(high[i] - close[i-1])
            tr3 = abs(low[i] - close[i-1])
            tr.append(max(tr1, tr2, tr3))
        
        # Calculate Directional Movements
        plus_dm = []
        minus_dm = []
        for i in range(1, len(high)):
            up_move = high[i] - high[i-1]
            down_move = low[i-1] - low[i]
            
            if up_move > down_move and up_move > 0:
                plus_dm.append(up_move)
            else:
                plus_dm.append(0)
            
            if down_move > up_move and down_move > 0:
                minus_dm.append(down_move)
            else:
                minus_dm.append(0)
        
        # Smooth with Wilder's method
        atr = sum(tr[:period]) / period
        avg_plus_dm = sum(plus_dm[:period]) / period
        avg_minus_dm = sum(minus_dm[:period]) / period
        
        if atr == 0:
            return 0
        
        plus_di = 100 * (avg_plus_dm / atr)
        minus_di = 100 * (avg_minus_dm / atr)
        
        if plus_di + minus_di == 0:
            return 0
        
        dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
        return dx
        
    except Exception as e:
        log.error(f"ADX calculation error: {e}")
        return 0

def check_ema_crossover(df):
    """Check for EMA200 crossing EMA50 on last two bars"""
    if df is None or len(df) < 200:
        return False, None
    
    closes = df['Close'].values
    
    # Calculate EMAs
    ema50_series = get_ema_series(closes, 50)
    ema200_series = get_ema_series(closes, 200)
    
    if len(ema50_series) < 2 or len(ema200_series) < 2:
        return False, None
    
    current_ema50 = ema50_series[-1]
    current_ema200 = ema200_series[-1]
    prev_ema50 = ema50_series[-2]
    prev_ema200 = ema200_series[-2]
    
    # EMA200 crosses ABOVE EMA50 (Bullish)
    if prev_ema200 <= prev_ema50 and current_ema200 > current_ema50:
        log.info(f"📈 BULLISH crossover detected! EMA200: {current_ema200:.2f} > EMA50: {current_ema50:.2f}")
        return True, "BULLISH"
    
    # EMA200 crosses BELOW EMA50 (Bearish)
    elif prev_ema200 >= prev_ema50 and current_ema200 < current_ema50:
        log.info(f"📉 BEARISH crossover detected! EMA200: {current_ema200:.2f} < EMA50: {current_ema50:.2f}")
        return True, "BEARISH"
    
    return False, None

# ============ RISK REWARD CALCULATIONS ============
def calculate_risk_reward(current_price, signal_type, risk_percent, profit_percent):
    """Calculate entry, stop loss, and take profit levels"""
    if signal_type == "BULLISH":
        entry = current_price
        stop_loss = entry * (1 - risk_percent / 100)
        take_profit = entry * (1 + profit_percent / 100)
    else:  # BEARISH
        entry = current_price
        stop_loss = entry * (1 + risk_percent / 100)
        take_profit = entry * (1 - profit_percent / 100)
    
    risk_amount = abs(entry - stop_loss)
    reward_amount = abs(take_profit - entry)
    ratio = round(reward_amount / risk_amount, 2) if risk_amount > 0 else 0
    
    return {
        'entry': round(entry, 4),
        'stop_loss': round(stop_loss, 4),
        'take_profit': round(take_profit, 4),
        'risk_percent': risk_percent,
        'profit_percent': profit_percent,
        'ratio': ratio
    }

def calculate_position_t212(position_size, entry_price, stop_loss, currency='EUR'):
    """Calculate Trading 212 position sizing"""
    risk_per_share = abs(entry_price - stop_loss)
    shares = int(position_size / entry_price) if entry_price > 0 else 0
    total_value = shares * entry_price
    total_risk = shares * risk_per_share
    
    return {
        'shares': shares,
        'position_value': round(total_value, 2),
        'total_risk': round(total_risk, 2),
        'currency': currency
    }

# ============ TELEGRAM MESSAGE ============
def send_signal(asset_name, asset_config, signal_type, current_price, ema50, ema200, adx, rr, position):
    """Send formatted signal to Telegram"""
    
    direction = "🟢 BULLISH (LONG)" if signal_type == "BULLISH" else "🔴 BEARISH (SHORT)"
    arrow = "📈" if signal_type == "BULLISH" else "📉"
    
    # ADX interpretation
    if adx > 40:
        adx_text = f"{adx:.1f} 🔥 VERY STRONG"
    elif adx > 25:
        adx_text = f"{adx:.1f} ✅ STRONG"
    elif adx > 20:
        adx_text = f"{adx:.1f} 📊 MODERATE"
    else:
        adx_text = f"{adx:.1f} ⚠️ WEAK"
    
    message = f"""<b>{arrow} {asset_config['display_name']} - {direction} SIGNAL {arrow}</b>

━━━━━━━━━━━━━━━━━━━━━
📊 <b>EMA CROSSOVER (10-Min)</b>
━━━━━━━━━━━━━━━━━━━━━
• EMA50: ${ema50:.2f}
• EMA200: ${ema200:.2f}
• <b>EMA200 crossed {signal_type.lower()} EMA50</b>

━━━━━━━━━━━━━━━━━━━━━
📈 <b>ADX TREND STRENGTH</b>
━━━━━━━━━━━━━━━━━━━━━
• ADX: {adx_text}

━━━━━━━━━━━━━━━━━━━━━
⚡ <b>RISK MANAGEMENT</b>
━━━━━━━━━━━━━━━━━━━━━
• Risk: {rr['risk_percent']}% per trade
• Reward: {rr['profit_percent']}% target
• Risk:Reward: 1:{rr['ratio']}

📍 <b>Levels:</b>
• Entry: ${rr['entry']}
• Stop Loss: ${rr['stop_loss']}
• Take Profit: ${rr['take_profit']}

━━━━━━━━━━━━━━━━━━━━━
💼 <b>POSITION SIZING ({position['currency']})</b>
━━━━━━━━━━━━━━━━━━━━━
📱 <b>Trading 212:</b>
• Capital: {asset_config['position_size']:,}
• Shares: {position['shares']:,} units
• Position Value: {position['position_value']:,}
• Total Risk: {position['total_risk']:,}
"""
    
    # Add MT5 info for Gold and SPY
    if asset_config.get('mt5_units') and asset_name in ['GOLD', 'SPY']:
        if asset_name == 'GOLD':
            risk_pips = abs(rr['entry'] - rr['stop_loss']) / 0.01
            message += f"""
💹 <b>MT5:</b>
• Units: {asset_config['mt5_units']}
• Risk: {risk_pips:.1f} pips
• Approx Risk: ${risk_pips * asset_config['mt5_units'] * 10:.2f}
"""
        else:
            risk_points = abs(rr['entry'] - rr['stop_loss'])
            message += f"""
💹 <b>MT5:</b>
• Units: {asset_config['mt5_units']}
• Risk: {risk_points:.2f} points
"""
    
    message += f"""
━━━━━━━━━━━━━━━━━━━━━
💰 <b>Current Price:</b> ${current_price:.2f}
⏰ <b>Signal Time:</b> {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC

⚠️ <i>Disclaimer: For educational purposes only.
Always conduct your own research before trading.</i>
"""
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
        r.raise_for_status()
        log.info(f"✅ Signal sent for {asset_name}")
        return True
    except Exception as e:
        log.error(f"Telegram failed: {e}")
        return False

# ============ MAIN ANALYSIS ============
def analyze_asset(asset_name, asset_config):
    """Analyze single asset for EMA crossover signals"""
    log.info(f"\n{'='*50}")
    log.info(f"🔍 Analyzing {asset_name} ({asset_config['symbol']})...")
    log.info(f"{'='*50}")
    
    # Fetch 10-minute data
    df = fetch_data(asset_config['symbol'], "10m", "7d")
    if df is None or len(df) < 200:
        log.warning(f"⚠️ {asset_name} - Insufficient data (need 200+ bars, got {len(df) if df is not None else 0})")
        return None
    
    log.info(f"📊 Data points: {len(df)}")
    
    # Check for EMA crossover
    has_crossover, signal_type = check_ema_crossover(df)
    if not has_crossover:
        log.info(f"📉 No EMA crossover detected for {asset_name}")
        return None
    
    # Get current values
    current_price = df['Close'].iloc[-1]
    closes = df['Close'].values
    
    # Calculate current EMAs
    ema50_series = get_ema_series(closes, 50)
    ema200_series = get_ema_series(closes, 200)
    
    ema50 = ema50_series[-1] if ema50_series else 0
    ema200 = ema200_series[-1] if ema200_series else 0
    
    # Calculate ADX
    adx = calculate_adx(df, 14)
    
    # Calculate risk/reward
    rr = calculate_risk_reward(
        current_price,
        signal_type,
        asset_config['risk_percent'],
        asset_config['profit_percent']
    )
    
    # Calculate position sizing
    position = calculate_position_t212(
        asset_config['position_size'],
        rr['entry'],
        rr['stop_loss'],
        asset_config['currency']
    )
    
    log.info(f"✅ SIGNAL DETECTED!")
    log.info(f"   Signal: {signal_type}")
    log.info(f"   Price: ${current_price:.2f}")
    log.info(f"   EMA50: ${ema50:.2f}")
    log.info(f"   EMA200: ${ema200:.2f}")
    log.info(f"   ADX: {adx:.1f}")
    log.info(f"   Entry: ${rr['entry']}")
    log.info(f"   Stop: ${rr['stop_loss']}")
    log.info(f"   Target: ${rr['take_profit']}")
    
    return {
        'signal_type': signal_type,
        'current_price': current_price,
        'ema50': ema50,
        'ema200': ema200,
        'adx': adx,
        'rr': rr,
        'position': position
    }

def main():
    log.info("=" * 70)
    log.info("🚀 EMA CROSSOVER TRADING BOT - 10-MINUTE TIMEFRAME")
    log.info("=" * 70)
    log.info("Strategy: EMA200 crosses EMA50 on 10-minute chart")
    log.info("Filters: ADX trend strength + Risk/Reward calculations")
    log.info("=" * 70)
    log.info(f"Monitoring {len(ASSETS)} assets:")
    for name, config in ASSETS.items():
        log.info(f"  • {name} ({config['symbol']}) - Risk: {config['risk_percent']}%, Reward: {config['profit_percent']}%")
    log.info("=" * 70)
    
    signals_sent = 0
    
    for asset_name, asset_config in ASSETS.items():
        try:
            # Check if signal allowed (prevents duplicates)
            result = analyze_asset(asset_name, asset_config)
            
            if result:
                allowed, reason = check_signal_allowed(asset_name, result['signal_type'])
                if allowed:
                    log.info(f"\n🔔 SENDING {result['signal_type']} SIGNAL for {asset_name}!")
                    success = send_signal(
                        asset_name,
                        asset_config,
                        result['signal_type'],
                        result['current_price'],
                        result['ema50'],
                        result['ema200'],
                        result['adx'],
                        result['rr'],
                        result['position']
                    )
                    if success:
                        save_signal_time(asset_name, result['signal_type'])
                        signals_sent += 1
                else:
                    log.info(f"⏰ {asset_name} {result['signal_type']} - {reason}")
            else:
                log.info(f"📊 {asset_name} - No signal")
            
            # Small delay between assets
            time.sleep(1)
            
        except Exception as e:
            log.error(f"❌ Error processing {asset_name}: {e}")
    
    log.info(f"\n{'='*70}")
    log.info(f"✅ Cycle complete. Sent {signals_sent} signal(s).")
    log.info(f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    log.info(f"{'='*70}\n")

if __name__ == "__main__":
    main()
