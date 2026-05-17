"""
EMA Crossover Trading Bot - 15-Minute Timeframe
With Health Monitoring & Daily Status Reports
"""

import os
import logging
import requests
from datetime import datetime, timezone, timedelta
import time
import json
from typing import List, Dict, Optional, Tuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ============ CONFIGURATION ============
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TELEGRAM_ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", TELEGRAM_CHAT_ID)  # For alerts

# Multiple API Keys for different asset groups
TWELVE_DATA_KEY_MAIN = os.environ.get("TWELVE_DATA_API_KEY")  # For GOLD, SPY, QQQ
TWELVE_DATA_KEY_CRYPTO = os.environ.get("TWELVE_DATA_API_KEY_2")  # For ETH, ADA

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    log.error("❌ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID")
    exit(1)

if not TWELVE_DATA_KEY_MAIN:
    log.error("❌ Missing TWELVE_DATA_API_KEY (for GOLD, SPY, QQQ)")
    exit(1)

# Cooldown files
COOLDOWN_FILE = "/tmp/ema_signal_tracker.json"
HEALTH_FILE = "/tmp/bot_health.json"
SIGNAL_COOLDOWN = 43200  # 12 hours

# Irish Timezone (UTC+0 in winter, UTC+1 in summer)
try:
    import pytz
    IRISH_TZ = pytz.timezone('Europe/Dublin')
except:
    IRISH_TZ = timezone.utc

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

def get_irish_time():
    """Get current Irish time"""
    return datetime.now(IRISH_TZ)

def load_tracker(file_path: str) -> Dict:
    try:
        with open(file_path, 'r') as f:
            return json.load(f)
    except:
        return {}

def save_tracker(file_path: str, data: Dict):
    try:
        with open(file_path, 'w') as f:
            json.dump(data, f)
    except:
        pass

def check_signal_allowed(asset: str, signal_type: str) -> bool:
    tracker = load_tracker(COOLDOWN_FILE)
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
    tracker = load_tracker(COOLDOWN_FILE)
    key = f"{asset}_{signal_type}"
    tracker[key] = datetime.now(timezone.utc).timestamp()
    save_tracker(COOLDOWN_FILE, tracker)

def update_health(status: str, details: str = ""):
    """Update bot health status"""
    health = load_tracker(HEALTH_FILE)
    health['last_run'] = datetime.now(timezone.utc).isoformat()
    health['last_run_irish'] = get_irish_time().strftime('%Y-%m-%d %H:%M:%S')
    health['status'] = status
    health['last_status'] = details
    health['run_count'] = health.get('run_count', 0) + 1
    
    # Track failures
    if status == 'failed':
        health['failures'] = health.get('failures', 0) + 1
        health['last_failure'] = datetime.now(timezone.utc).isoformat()
        health['last_failure_irish'] = get_irish_time().strftime('%Y-%m-%d %H:%M:%S')
    
    save_tracker(HEALTH_FILE, health)

def should_send_daily_report() -> bool:
    """Check if we should send the 9 AM daily report"""
    health = load_tracker(HEALTH_FILE)
    last_report = health.get('last_daily_report')
    
    if not last_report:
        return True
    
    last_report_time = datetime.fromisoformat(last_report)
    now = get_irish_time()
    
    # Check if it's after 9 AM and last report was before today 9 AM
    if now.hour >= 9 and last_report_time.date() < now.date():
        return True
    
    return False

def send_daily_report():
    """Send daily health report at 9 AM Irish time"""
    health = load_tracker(HEALTH_FILE)
    irish_now = get_irish_time()
    
    # Calculate success rate
    total_runs = health.get('run_count', 0)
    failures = health.get('failures', 0)
    success_rate = ((total_runs - failures) / total_runs * 100) if total_runs > 0 else 100
    
    last_run = health.get('last_run_irish', 'Never')
    last_failure = health.get('last_failure_irish', 'No failures')
    
    # Get last 24 hours signals
    signals = load_tracker(COOLDOWN_FILE)
    today_signals = []
    yesterday = (irish_now - timedelta(days=1)).timestamp()
    
    for key, timestamp in signals.items():
        if timestamp > yesterday:
            today_signals.append(key)
    
    message = f"""📊 <b>BOT DAILY HEALTH REPORT</b>
━━━━━━━━━━━━━━━━━━━━━
📅 <b>Date:</b> {irish_now.strftime('%Y-%m-%d')}
⏰ <b>Time:</b> {irish_now.strftime('%H:%M:%S')} Irish Time

📈 <b>Statistics:</b>
• Total Runs: {total_runs}
• Failures: {failures}
• Success Rate: {success_rate:.1f}%
• Last Run: {last_run}

🔔 <b>Signals (last 24h):</b>
• Signals Sent: {len(today_signals)}
"""
    
    if today_signals:
        message += f"\n📋 <b>Recent Signals:</b>\n"
        for sig in today_signals[:5]:
            message += f"  • {sig}\n"
    else:
        message += "\n  • No signals in last 24h\n"
    
    message += f"""
━━━━━━━━━━━━━━━━━━━━━
⚠️ <b>Last Failure:</b> {last_failure}

✅ <b>Bot Status:</b> Active
🕐 <i>Next scheduled run: Every 6 minutes</i>

<i>🤖 This is an automated health report</i>"""
    
    send_telegram_message(message, admin=True)

def send_failure_alert(asset_name: str, error: str):
    """Send immediate alert when a failure occurs"""
    irish_time = get_irish_time()
    
    message = f"""⚠️ <b>BOT ALERT - FAILURE DETECTED</b>

━━━━━━━━━━━━━━━━━━━━━
📅 <b>Time:</b> {irish_time.strftime('%Y-%m-%d %H:%M:%S')} Irish Time
📊 <b>Asset:</b> {asset_name}
❌ <b>Error:</b> {error[:200]}

🔄 <b>Action:</b> Bot continues monitoring other assets

<i>Check logs for more details</i>"""
    
    send_telegram_message(message, admin=True)

def send_startup_message():
    """Send message when bot starts"""
    irish_time = get_irish_time()
    
    message = f"""✅ <b>BOT STARTED SUCCESSFULLY</b>

━━━━━━━━━━━━━━━━━━━━━
📅 <b>Time:</b> {irish_time.strftime('%Y-%m-%d %H:%M:%S')} Irish Time
📊 <b>Monitoring:</b> 5 Assets (GOLD, SPY, QQQ, ETH, ADA)
⏰ <b>Schedule:</b> Every 6 minutes
🛡️ <b>Cooldown:</b> 12 hours between same signals

📈 <b>Risk Levels:</b>
• GOLD: 0.5% risk / 1% target
• SPY: 2% risk / 4% target  
• QQQ/ETH/ADA: 1% risk / 2% target

<i>Daily reports will be sent at 9:00 AM Irish time</i>"""
    
    send_telegram_message(message, admin=True)

def fetch_twelvedata_data(symbol: str, api_key: str) -> Optional[List[Dict]]:
    """Fetch 15-minute data from Twelve Data API using specific key"""
    if not api_key:
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
        
        prices.reverse()  # Oldest first
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
        
        tr1 = high - low
        tr2 = abs(high - prev_close)
        tr3 = abs(low - prev_close)
        tr.append(max(tr1, tr2, tr3))
        
        up_move = high - prices[i-1]['high']
        down_move = prices[i-1]['low'] - low
        
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0)
    
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
    
    if prev_ema200 <= prev_ema50 and current_ema200 > current_ema50:
        return True, "BULLISH"
    elif prev_ema200 >= prev_ema50 and current_ema200 < current_ema50:
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

def send_telegram_message(message: str, admin: bool = False) -> bool:
    """Send message to Telegram"""
    chat_id = TELEGRAM_ADMIN_CHAT_ID if admin else TELEGRAM_CHAT_ID
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    
    try:
        response = requests.post(url, json={
            "chat_id": chat_id,
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
    
    if not config['api_key']:
        log.warning(f"⚠️ No API key for {asset_name} - skipping")
        return None
    
    prices = fetch_twelvedata_data(config['symbol'], config['api_key'])
    
    if not prices or len(prices) < 200:
        log.warning(f"⚠️ {asset_name}: Only {len(prices) if prices else 0} bars")
        return None
    
    has_cross, signal_type = check_ema_crossover(prices)
    if not has_cross:
        return None
    
    current_price = prices[-1]['close']
    closes = [p['close'] for p in prices]
    
    ema50_series = calculate_ema_series(closes, 50)
    ema200_series = calculate_ema_series(closes, 200)
    
    ema50 = ema50_series[-1] if ema50_series else 0
    ema200 = ema200_series[-1] if ema200_series else 0
    adx = calculate_adx(prices, 14)
    
    if adx > 40:
        adx_text = f"{adx:.1f} 🔥 VERY STRONG"
    elif adx > 25:
        adx_text = f"{adx:.1f} ✅ STRONG"
    elif adx > 20:
        adx_text = f"{adx:.1f} 📊 MODERATE"
    else:
        adx_text = f"{adx:.1f} ⚠️ WEAK"
    
    rr = calculate_risk_reward(
        current_price, signal_type,
        config['risk_percent'],
        config['profit_percent']
    )
    
    shares = int(config['position_size'] / rr['entry']) if rr['entry'] > 0 else 0
    position_value = shares * rr['entry']
    total_risk = shares * abs(rr['entry'] - rr['stop_loss'])
    
    log.info(f"✅ SIGNAL: {signal_type} at ${current_price:.2f}")
    log.info(f"   EMA50: ${ema50:.2f}, EMA200: ${ema200:.2f}, ADX: {adx:.1f}")
    
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
    irish_time = get_irish_time()
    
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
⏰ <b>Irish Time:</b> {irish_time.strftime('%Y-%m-%d %H:%M:%S')}

⚠️ <i>Disclaimer: For educational purposes only.</i>"""
    
    return message

def main():
    log.info("=" * 70)
    log.info("🚀 EMA CROSSOVER TRADING BOT - WITH HEALTH MONITORING")
    log.info("=" * 70)
    
    try:
        # Update health - started
        update_health('running', 'Bot started successfully')
        
        # Send startup message (only once per day)
        health = load_tracker(HEALTH_FILE)
        last_startup = health.get('last_startup_message')
        irish_now = get_irish_time()
        
        if not last_startup or (irish_now - datetime.fromisoformat(last_startup)).days >= 1:
            send_startup_message()
            health['last_startup_message'] = irish_now.isoformat()
            save_tracker(HEALTH_FILE, health)
        
        # Send daily report at 9 AM Irish time
        if should_send_daily_report():
            send_daily_report()
            health = load_tracker(HEALTH_FILE)
            health['last_daily_report'] = datetime.now(timezone.utc).isoformat()
            save_tracker(HEALTH_FILE, health)
        
        log.info(f"📊 Monitoring 5 assets | Irish Time: {irish_now.strftime('%Y-%m-%d %H:%M:%S')}")
        log.info("=" * 70)
        
        signals_sent = 0
        failures = 0
        
        for asset_name, config in ASSETS.items():
            try:
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
                            failures += 1
                    else:
                        log.info(f"⏭️ {asset_name} - in cooldown")
                else:
                    log.info(f"📊 {asset_name} - No crossover")
                
                time.sleep(2)
                
            except Exception as e:
                failures += 1
                error_msg = str(e)
                log.error(f"❌ Error with {asset_name}: {error_msg}")
                send_failure_alert(asset_name, error_msg)
        
        # Update final health
        update_health('completed', f"Sent {signals_sent} signals, {failures} failures")
        
        log.info(f"\n{'='*70}")
        log.info(f"✅ Scan complete - {signals_sent} signals sent, {failures} failures")
        log.info(f"🕐 Irish Time: {get_irish_time().strftime('%Y-%m-%d %H:%M:%S')}")
        log.info(f"{'='*70}\n")
        
    except Exception as e:
        error_msg = str(e)
        log.error(f"❌ FATAL ERROR: {error_msg}")
        update_health('failed', error_msg)
        send_failure_alert("SYSTEM", error_msg)
        raise

if __name__ == "__main__":
    main()
