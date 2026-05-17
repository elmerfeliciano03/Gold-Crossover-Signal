"""
EMA Crossover Trading Bot - 15-Minute Timeframe
Supports Multiple API Keys for different asset groups
"""

import os
import logging
import requests
from datetime import datetime, timezone
import time
import json
from typing import List, Dict, Optional, Tuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ============ CONFIGURATION ============
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Multiple API Keys for different asset groups
TWELVE_DATA_KEY_MAIN = os.environ.get("TWELVE_DATA_API_KEY")  # For GOLD, SPY, QQQ
TWELVE_DATA_KEY_CRYPTO = os.environ.get("TWELVE_DATA_API_KEY_2")  # For ETH, ADA

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    log.error("❌ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID")
    exit(1)

if not TWELVE_DATA_KEY_MAIN:
    log.error("❌ Missing TWELVE_DATA_API_KEY (for GOLD, SPY, QQQ)")
    log.error("Get one free at: https://twelvedata.com/apikey")
    exit(1)

if not TWELVE_DATA_KEY_CRYPTO:
    log.warning("⚠️ TWELVE_DATA_API_KEY_2 not set - Crypto assets (ETH, ADA) will be skipped")
    log.warning("   Get a second free API key at: https://twelvedata.com/apikey")

# Cooldown file
COOLDOWN_FILE = "/tmp/ema_signal_tracker.json"
SIGNAL_COOLDOWN = 43200  # 12 hours

# Asset configurations with API key assignment
ASSETS = {
    "GOLD": {
        "symbol": "XAU/USD",
        "display_name": "💰 GOLD",
        "risk_percent": 0.5,
        "profit_percent": 1.0,
        "position_size": 10000,
        "currency": "EUR",
        "mt5_units": 0.03,
        "api_key": TWELVE_DATA_KEY_MAIN
    },
    "SPY": {
        "symbol": "SPY",
        "display_name": "📈 SPY",
        "risk_percent": 2.0,
        "profit_percent": 4.0,
        "position_size": 2500,
        "currency": "EUR",
        "mt5_units": 0.03,
        "api_key": TWELVE_DATA_KEY_MAIN
    },
    "QQQ": {
        "symbol": "QQQ",
        "display_name": "🚀 QQQ",
        "risk_percent": 1.0,
        "profit_percent": 2.0,
        "position_size": 2500,
        "currency": "EUR",
        "mt5_units": None,
        "api_key": TWELVE_DATA_KEY_MAIN
    },
    "ETH": {
        "symbol": "ETH/USD",
        "display_name": "🔷 ETHEREUM",
        "risk_percent": 1.0,
        "profit_percent": 2.0,
        "position_size": 2500,
        "currency": "EUR",
        "mt5_units": None,
        "api_key": TWELVE_DATA_KEY_CRYPTO
    },
    "ADA": {
        "symbol": "ADA/USD",
        "display_name": "📊 CARDANO",
        "risk_percent": 1.0,
        "profit_percent": 2.0,
        "position_size": 2500,
        "currency": "EUR",
        "mt5_units": None,
        "api_key": TWELVE_DATA_KEY_CRYPTO
    }
}

def load_tracker() -> Dict:
    try:
        with open(COOLDOWN_FILE, 'r') as f:
            return json.load(f)
    except:
        return {}

def save_tracker(data: Dict):
    try:
        with open(COOLDOWN_FILE, 'w') as f:
            json.dump(data, f)
    except:
        pass

def check_signal_allowed(asset: str, signal_type: str) -> bool:
    tracker = load_tracker()
    key = f"{asset}_{signal_type}"
    now = datetime.now(timezone.utc).timestamp()
    
    if key in tracker:
        last_time = tracker[key]
        if (now - last_time) < SIGNAL_COOLDOWN:
            hours_left = (SIGNAL_COOLDOWN - (now - last_time)) / 3600
            log.info(f"⏭️ {asset} - cooldown ({hours_left:.1f}h left)")
            return False
    return True

def save_signal_time(asset: str, signal_type: str):
    tracker = load_tracker()
    key = f"{asset}_{signal_type}"
    tracker[key] = datetime.now(timezone.utc).timestamp()
    save_tracker(tracker)

def fetch_twelvedata_data(symbol: str, api_key: str) -> Optional[List[Dict]]:
    """Fetch 15-minute data from Twelve Data API using specific key"""
    if not api_key:
        log.error(f"No API key available for {symbol}")
        return None
        
    try:
        url = f"https://api.twelvedata.com/time_series"
        params = {
            'symbol': symbol,
            'interval': '15min',
            'outputsize': '500',
            'apikey': api_key
        }
        
        log.info(f"Fetching {symbol} (15min)...")
        response = requests.get(url, params=params, timeout=15)
        data = response.json()
        
        if 'code' in data:
            log.error(f"API Error {data['code']}: {data.get('message', 'Unknown error')}")
            return None
        
        if 'values' not in data:
            log.error(f"No values in response for {symbol}")
            return None
        
        prices = []
        for item in data['values']:
            prices.append({
                'timestamp': item['datetime'],
                'open': float(item['open']),
                'high': float(item['high']),
                'low': float(item['low']),
                'close': float(item['close']),
                'volume': float(item.get('volume', 0))
            })
        
        # Reverse to chronological order (oldest first)
        prices.reverse()
        
        log.info(f"✅ Got {len(prices)} bars for {symbol}")
        return prices
        
    except Exception as e:
        log.error(f"Error fetching {symbol}: {e}")
        return None

def calculate_ema_series(prices: List[float], period: int) -> List[float]:
    """Calculate EMA series"""
    if len(prices) < period:
        return []
    
    multiplier = 2 / (period + 1)
    ema_values = []
    current_ema = prices[0]
    
    for price in prices:
        current_ema = (price - current_ema) * multiplier + current_ema
        ema_values.append(current_ema)
    
    return ema_values

def calculate_adx(prices: List[Dict], period: int = 14) -> float:
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
    
    # Averages
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

def check_ema_crossover(prices: List[Dict]) -> Tuple[bool, Optional[str]]:
    """Check if EMA200 crossed EMA50"""
    if len(prices) < 200:
        log.warning(f"Need 200 bars, have {len(prices)}")
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

def calculate_risk_reward(price: float, signal_type: str, risk_pct: float, profit_pct: float) -> Dict:
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

def send_telegram_message(message: str) -> bool:
    """Send message to Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        response = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
        if response.status_code == 200:
            log.info("✅ Telegram message sent")
            return True
        else:
            log.error(f"Telegram error: {response.status_code}")
            return False
    except Exception as e:
        log.error(f"Telegram error: {e}")
        return False

def analyze_asset(asset_name: str, config: Dict) -> Optional[Dict]:
    """Analyze single asset"""
    log.info(f"\n{'='*40}")
    log.info(f"🔍 Analyzing {asset_name}...")
    
    # Check if API key is available
    if not config['api_key']:
        log.warning(f"⚠️ No API key for {asset_name} - skipping")
        return None
    
    # Fetch data
    prices = fetch_twelvedata_data(config['symbol'], config['api_key'])
    
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
    
    log.info(f"✅ SIGNAL DETECTED: {signal_type} at ${current_price:.2f}")
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

def format_signal_message(data: Dict) -> str:
    """Format signal for Telegram"""
    config = data['config']
    rr = data['rr']
    asset_name = data['asset_name']
    
    arrow = "📈" if data['signal_type'] == "BULLISH" else "📉"
    direction = "🟢 BULLISH (LONG)" if data['signal_type'] == "BULLISH" else "🔴 BEARISH (SHORT)"
    
    message = f"""<b>{arrow} {config['display_name']} - {direction} SIGNAL {arrow}</b>

━━━━━━━━━━━━━━━━━━━━━
📊 <b>EMA CROSSOVER (15-Min)</b>
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

def test_api(api_key: str, name: str) -> bool:
    """Test a specific API key"""
    log.info(f"Testing API key {name}...")
    test_symbol = "SPY" if "MAIN" in name else "ETH/USD"
    result = fetch_twelvedata_data(test_symbol, api_key)
    if result and len(result) > 0:
        log.info(f"✅ API {name} test successful!")
        return True
    else:
        log.error(f"❌ API {name} test failed")
        return False

def main():
    log.info("=" * 70)
    log.info("🚀 EMA CROSSOVER TRADING BOT - 15-MIN TIMEFRAME")
    log.info("=" * 70)
    log.info("📊 Multi-API Configuration:")
    log.info(f"   Main API Key (GOLD, SPY, QQQ): {TWELVE_DATA_KEY_MAIN[:8] if TWELVE_DATA_KEY_MAIN else 'NOT SET'}...")
    log.info(f"   Crypto API Key (ETH, ADA): {TWELVE_DATA_KEY_CRYPTO[:8] if TWELVE_DATA_KEY_CRYPTO else 'NOT SET'}...")
    log.info("=" * 70)
    
    # Test APIs
    if TWELVE_DATA_KEY_MAIN:
        test_api(TWELVE_DATA_KEY_MAIN, "MAIN")
    if TWELVE_DATA_KEY_CRYPTO:
        test_api(TWELVE_DATA_KEY_CRYPTO, "CRYPTO")
    
    log.info("\n📊 Monitoring Assets:")
    for name, config in ASSETS.items():
        if config['api_key']:
            log.info(f"  • {config['display_name']} - Risk: {config['risk_percent']}% / Target: {config['profit_percent']}%")
        else:
            log.info(f"  • {config['display_name']} - ⚠️ SKIPPED (no API key)")
    log.info("=" * 70)
    
    signals_sent = 0
    
    for asset_name, config in ASSETS.items():
        try:
            # Skip if no API key
            if not config['api_key']:
                log.info(f"⏭️ {asset_name} - No API key, skipping")
                continue
                
            result = analyze_asset(asset_name, config)
            
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
                    log.info(f"⏭️ {asset_name} {result['signal_type']} - in cooldown")
            else:
                log.info(f"📊 {asset_name} - No crossover detected")
            
            # Small delay between API calls
            time.sleep(2)
            
        except Exception as e:
            log.error(f"❌ Error with {asset_name}: {e}")
    
    log.info(f"\n{'='*70}")
    log.info(f"✅ Scan complete - {signals_sent} new signal(s) sent")
    log.info(f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    log.info(f"{'='*70}\n")

if __name__ == "__main__":
    main()
