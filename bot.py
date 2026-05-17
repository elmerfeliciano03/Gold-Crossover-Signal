"""
EMA Crossover Trading Bot - 15-Minute Timeframe
With Smart Health Monitoring (Daily Report at 9AM Irish Time)
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
TELEGRAM_ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", TELEGRAM_CHAT_ID)

# Multiple API Keys
TWELVE_DATA_KEY_MAIN = os.environ.get("TWELVE_DATA_API_KEY")
TWELVE_DATA_KEY_CRYPTO = os.environ.get("TWELVE_DATA_API_KEY_2")

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

# Irish Timezone
try:
    import pytz
    IRISH_TZ = pytz.timezone('Europe/Dublin')
except:
    IRISH_TZ = timezone.utc

# Asset configurations
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
    
    if status == 'failed':
        health['failures'] = health.get('failures', 0) + 1
        health['last_failure'] = datetime.now(timezone.utc).isoformat()
        health['last_failure_irish'] = get_irish_time().strftime('%Y-%m-%d %H:%M:%S')
    
    save_tracker(HEALTH_FILE, health)

def should_send_daily_report() -> bool:
    """Check if we should send the 9 AM daily report (ONCE per day)"""
    health = load_tracker(HEALTH_FILE)
    last_report = health.get('last_daily_report')
    irish_now = get_irish_time()
    
    # Only send between 9:00 AM and 9:05 AM Irish time
    if irish_now.hour != 9 or irish_now.minute > 5:
        return False
    
    if not last_report:
        return True
    
    last_report_time = datetime.fromisoformat(last_report)
    last_report_irish = last_report_time.astimezone(IRISH_TZ)
    
    # Send if last report was on a different day
    return last_report_irish.date() < irish_now.date()

def should_send_startup_message() -> bool:
    """Send startup message ONLY if there was a failure in the last run"""
    health = load_tracker(HEALTH_FILE)
    last_startup = health.get('last_startup_message')
    last_status = health.get('status', '')
    last_failure = health.get('last_failure')
    
    irish_now = get_irish_time()
    
    # Check if there was a recent failure (last 24 hours)
    had_recent_failure = False
    if last_failure:
        failure_time = datetime.fromisoformat(last_failure)
        if (irish_now - failure_time.astimezone(IRISH_TZ)).total_seconds() < 86400:  # 24 hours
            had_recent_failure = True
    
    # Send if:
    # 1. Last run failed, OR
    # 2. No startup message ever sent, OR
    # 3. Last startup was more than 24 hours ago AND we had a failure
    if not last_startup:
        return had_recent_failure or last_status == 'failed'
    
    last_startup_time = datetime.fromisoformat(last_startup)
    last_startup_irish = last_startup_time.astimezone(IRISH_TZ)
    hours_since_startup = (irish_now - last_startup_irish).total_seconds() / 3600
    
    return had_recent_failure and hours_since_startup > 1  # Don't spam, wait at least 1 hour

def send_daily_report():
    """Send daily health report at 9 AM Irish time (ONCE per day)"""
    health = load_tracker(HEALTH_FILE)
    irish_now = get_irish_time()
    
    # Calculate success rate
    total_runs = health.get('run_count', 0)
    failures = health.get('failures', 0)
    success_rate = ((total_runs - failures) / total_runs * 100) if total_runs > 0 else 100
    
    last_run = health.get('last_run_irish', 'Never')
    last_failure = health.get('last_failure_irish', 'No failures in last 24h')
    
    # Get last 24 hours signals
    signals = load_tracker(COOLDOWN_FILE)
    today_signals = []
    yesterday = (irish_now - timedelta(days=1)).timestamp()
    
    for key, timestamp in signals.items():
        if timestamp > yesterday:
            today_signals.append(key)
    
    # Get status emoji
    if failures == 0:
        status_emoji = "✅ HEALTHY"
    elif failures < 3:
        status_emoji = "⚠️ MINOR ISSUES"
    else:
        status_emoji = "🔴 ISSUES DETECTED"
    
    message = f"""📊 <b>BOT DAILY HEALTH REPORT</b>
━━━━━━━━━━━━━━━━━━━━━
📅 <b>Date:</b> {irish_now.strftime('%Y-%m-%d')}
⏰ <b>Time:</b> {irish_now.strftime('%H:%M:%S')} Irish Time
📊 <b>Status:</b> {status_emoji}

📈 <b>Statistics (Last 24h):</b>
• Total Runs: {total_runs}
• Failures: {failures}
• Success Rate: {success_rate:.1f}%
• Last Run: {last_run}

🔔 <b>Signals Detected:</b>
• Total Signals: {len(today_signals)}
"""
    
    if today_signals:
        message += f"\n📋 <b>Recent Signals:</b>\n"
        for sig in today_signals[:5]:
            asset, signal_type = sig.rsplit('_', 1)
            emoji = "🟢" if signal_type == "BULLISH" else "🔴"
            message += f"  {emoji} {asset}: {signal_type}\n"
    else:
        message += "\n  • No signals detected in last 24h\n"
    
    message += f"""
━━━━━━━━━━━━━━━━━━━━━
⚠️ <b>Last Failure:</b> {last_failure}

🕐 <b>Next Report:</b> Tomorrow at 9:00 AM Irish Time

<i>🤖 Automated health report - Bot is monitoring 5 assets</i>"""
    
    send_telegram_message(message, admin=True)
    
    # Mark report as sent
    health['last_daily_report'] = datetime.now(timezone.utc).isoformat()
    save_tracker(HEALTH_FILE, health)

def send_startup_message():
    """Send startup message ONLY after failures"""
    health = load_tracker(HEALTH_FILE)
    irish_time = get_irish_time()
    last_failure = health.get('last_failure_irish', 'Unknown')
    last_status = health.get('last_status', 'Unknown')
    
    message = f"""✅ <b>BOT RECOVERED - NOW MONITORING</b>

━━━━━━━━━━━━━━━━━━━━━
📅 <b>Recovery Time:</b> {irish_time.strftime('%Y-%m-%d %H:%M:%S')} Irish Time
📊 <b>Last Status:</b> {last_status}
⚠️ <b>Last Failure:</b> {last_failure}

📈 <b>Currently Monitoring:</b>
• GOLD (0.5% risk)
• SPY (2% risk)
• QQQ (1% risk)
• ETH (1% risk)
• ADA (1% risk)

⏰ <b>Schedule:</b> Every 6 minutes
🛡️ <b>Cooldown:</b> 12 hours between signals

<i>Bot is back online and monitoring for signals</i>"""
    
    send_telegram_message(message, admin=True)
    
    # Mark startup as sent
    health['last_startup_message'] = datetime.now(timezone.utc).isoformat()
    save_tracker(HEALTH_FILE, health)

def send_failure_alert(asset_name: str, error: str):
    """Send immediate alert when a failure occurs"""
    irish_time = get_irish_time()
    health = load_tracker(HEALTH_FILE)
    failures_today = health.get('failures', 0)
    
    # Don't spam if too many failures (throttle)
    last_alert = health.get('last_failure_alert')
    if last_alert:
        last_alert_time = datetime.fromisoformat(last_alert)
        seconds_since_alert = (datetime.now(timezone.utc) - last_alert_time).total_seconds()
        if seconds_since_alert < 300:  # 5 minutes cooldown on alerts
            log.info("⏭️ Skipping duplicate failure alert (throttled)")
            return
    
    message = f"""⚠️ <b>BOT ALERT - FAILURE DETECTED</b>

━━━━━━━━━━━━━━━━━━━━━
📅 <b>Time:</b> {irish_time.strftime('%Y-%m-%d %H:%M:%S')} Irish Time
📊 <b>Asset:</b> {asset_name}
❌ <b>Error:</b> {error[:200]}

📈 <b>Failures Today:</b> {failures_today + 1}

🔄 <b>Action:</b> Bot continues monitoring other assets
🚀 <b>Next Run:</b> In 6 minutes

<i>Bot will auto-recover on next successful run</i>"""
    
    send_telegram_message(message, admin=True)
    health['last_failure_alert'] = datetime.now(timezone.utc).isoformat()
    save_tracker(HEALTH_FILE, health)

def fetch_twelvedata_data(symbol: str, api_key: str) -> Optional[List[Dict]]:
    """Fetch 15-minute data from Twelve Data API"""
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
        
        log.info(f"Fetching {symbol}...")
        response = requests.get(url, params=params, timeout=15)
        data = response.json()
        
        if 'code' in data:
            log.error(f"API Error: {data.get('message', 'Unknown')}")
            return None
        
        if 'values' not in data:
            log.error(f"No values for {symbol}")
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
    """Calculate ADX"""
    if len(prices) < period * 2:
        return 0
    
    tr, plus_dm, minus_dm = [], [], []
    
    for i in range(1, len(prices)):
        high, low = prices[i]['high'], prices[i]['low']
        prev_close = prices[i-1]['close']
        
        tr1, tr2, tr3 = high - low, abs(high - prev_close), abs(low - prev_close)
        tr.append(max(tr1, tr2, tr3))
        
        up_move, down_move = high - prices[i-1]['high'], prices[i-1]['low'] - low
        
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
    
    return 100 * abs(plus_di - minus_di) / (plus_di + minus_di)

def check_ema_crossover(prices: List[Dict]) -> Tuple[bool, Optional[str]]:
    """Check for EMA200 crossing EMA50"""
    if len(prices) < 200:
        return False, None
    
    closes = [p['close'] for p in prices]
    ema50 = calculate_ema_series(closes, 50)
    ema200 = calculate_ema_series(closes, 200)
    
    if len(ema50) < 2 or len(ema200) < 2:
        return False, None
    
    current_ema50, current_ema200 = ema50[-1], ema200[-1]
    prev_ema50, prev_ema200 = ema50[-2], ema200[-2]
    
    if prev_ema200 <= prev_ema50 and current_ema200 > current_ema50:
        return True, "BULLISH"
    elif prev_ema200 >= prev_ema50 and current_ema200 < current_ema50:
        return True, "BEARISH"
    
    return False, None

def calculate_risk_reward(price: float, signal_type: str, risk_pct: float, profit_pct: float) -> Dict:
    """Calculate levels"""
    if signal_type == "BULLISH":
        entry, stop_loss, take_profit = price, price * (1 - risk_pct / 100), price * (1 + profit_pct / 100)
    else:
        entry, stop_loss, take_profit = price, price * (1 + risk_pct / 100), price * (1 - profit_pct / 100)
    
    risk_amount, reward_amount = abs(entry - stop_loss), abs(take_profit - entry)
    ratio = round(reward_amount / risk_amount, 2) if risk_amount > 0 else 0
    
    return {
        'entry': round(entry, 4), 'stop_loss': round(stop_loss, 4),
        'take_profit': round(take_profit, 4), 'risk_pct': risk_pct,
        'profit_pct': profit_pct, 'ratio': ratio
    }

def send_telegram_message(message: str, admin: bool = False) -> bool:
    """Send message to Telegram"""
    chat_id = TELEGRAM_ADMIN_CHAT_ID if admin else TELEGRAM_CHAT_ID
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    
    try:
        response = requests.post(url, json={
            "chat_id": chat_id, "text": message, "parse_mode": "HTML"
        }, timeout=10)
        return response.status_code == 200
    except Exception as e:
        log.error(f"Telegram error: {e}")
        return False

def analyze_asset(asset_name: str, config: Dict) -> Optional[Dict]:
    """Analyze single asset"""
    log.info(f"\n{'='*40}\n🔍 Analyzing {asset_name}...")
    
    if not config['api_key']:
        log.warning(f"⚠️ No API key for {asset_name}")
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
    
    ema50, ema200 = ema50_series[-1], ema200_series[-1]
    adx = calculate_adx(prices, 14)
    
    if adx > 40:
        adx_text = f"{adx:.1f} 🔥 VERY STRONG"
    elif adx > 25:
        adx_text = f"{adx:.1f} ✅ STRONG"
    elif adx > 20:
        adx_text = f"{adx:.1f} 📊 MODERATE"
    else:
        adx_text = f"{adx:.1f} ⚠️ WEAK"
    
    rr = calculate_risk_reward(current_price, signal_type,
                               config['risk_percent'], config['profit_percent'])
    
    shares = int(config['position_size'] / rr['entry']) if rr['entry'] > 0 else 0
    position_value, total_risk = shares * rr['entry'], shares * abs(rr['entry'] - rr['stop_loss'])
    
    log.info(f"✅ SIGNAL: {signal_type} at ${current_price:.2f}")
    return {
        'signal_type': signal_type, 'price': current_price, 'ema50': ema50,
        'ema200': ema200, 'adx': adx_text, 'rr': rr, 'shares': shares,
        'position_value': position_value, 'total_risk': total_risk,
        'config': config, 'asset_name': asset_name
    }

def format_signal_message(data: Dict) -> str:
    """Format signal for Telegram"""
    config, rr, asset_name = data['config'], data['rr'], data['asset_name']
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
            message += f"""
💹 <b>MT5:</b>
• Units: {config['mt5_units']}
• Risk: {abs(rr['entry'] - rr['stop_loss']):.2f} points
"""
    
    message += f"""
━━━━━━━━━━━━━━━━━━━━━
💰 <b>Current Price:</b> ${data['price']:.2f}
⏰ <b>Irish Time:</b> {irish_time.strftime('%Y-%m-%d %H:%M:%S')}

⚠️ <i>Disclaimer: For educational purposes only.</i>"""
    
    return message

def main():
    log.info("=" * 70)
    log.info("🚀 EMA CROSSOVER TRADING BOT - WITH SMART NOTIFICATIONS")
    log.info("=" * 70)
    
    try:
        # Update health
        update_health('running', 'Bot started successfully')
        
        # Send daily report at 9 AM (ONCE per day)
        if should_send_daily_report():
            log.info("📊 Sending daily health report...")
            send_daily_report()
        
        # Send startup message ONLY if there was a failure
        if should_send_startup_message():
            log.info("✅ Sending recovery startup message...")
            send_startup_message()
        
        irish_now = get_irish_time()
        log.info(f"📊 Monitoring 5 assets | Irish Time: {irish_now.strftime('%Y-%m-%d %H:%M:%S')}")
        log.info("=" * 70)
        
        signals_sent = 0
        failures = 0
        
        for asset_name, config in ASSETS.items():
            try:
                if not config['api_key']:
                    log.info(f"⏭️ {asset_name} - No API key")
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
                            failures += 1
                    else:
                        log.info(f"⏭️ {asset_name} - cooldown")
                else:
                    log.info(f"📊 {asset_name} - No crossover")
                
                time.sleep(2)
                
            except Exception as e:
                failures += 1
                error_msg = str(e)
                log.error(f"❌ Error with {asset_name}: {error_msg}")
                send_failure_alert(asset_name, error_msg)
        
        # Update final health
        if failures == 0:
            update_health('completed', f"Sent {signals_sent} signals successfully")
        else:
            update_health('completed_with_errors', f"Sent {signals_sent} signals, {failures} failures")
        
        log.info(f"\n{'='*70}")
        log.info(f"✅ Scan complete - {signals_sent} signals sent, {failures} failures")
        log.info(f"🕐 Irish Time: {get_irish_time().strftime('%Y-%m-%d %H:%M:%S')}")
        log.info(f"{'='*70}\n")
        
    except Exception as e:
        error_msg = str(e)
        log.error(f"❌ FATAL ERROR: {error_msg}")
        update_health('failed', error_msg)
        send_failure_alert("SYSTEM", error_msg)

if __name__ == "__main__":
    main()
