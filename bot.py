"""
EMA Crossover + Pullback Trading Bot - 15-Minute Timeframe
With Smart Health Monitoring (Daily Report at 9AM Irish Time)

Strategies:
1. EMA Crossover (EMA200 crosses EMA50) - Based on last closed candle
2. Pullback to EMA20 - Based on last closed candle
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

# Pullback configuration
PULLBACK_EMA = 20
PULLBACK_RETRACE_PERCENT = 0.382
PULLBACK_MIN_RSI = 40
PULLBACK_MAX_RSI = 60

def get_irish_time():
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
    health = load_tracker(HEALTH_FILE)
    last_report = health.get('last_daily_report')
    irish_now = get_irish_time()
    
    if irish_now.hour != 9 or irish_now.minute > 5:
        return False
    
    if not last_report:
        return True
    
    last_report_time = datetime.fromisoformat(last_report)
    last_report_irish = last_report_time.astimezone(IRISH_TZ)
    return last_report_irish.date() < irish_now.date()

def should_send_startup_message() -> bool:
    health = load_tracker(HEALTH_FILE)
    last_startup = health.get('last_startup_message')
    last_status = health.get('status', '')
    last_failure = health.get('last_failure')
    irish_now = get_irish_time()
    
    had_recent_failure = False
    if last_failure:
        failure_time = datetime.fromisoformat(last_failure)
        if (irish_now - failure_time.astimezone(IRISH_TZ)).total_seconds() < 86400:
            had_recent_failure = True
    
    if not last_startup:
        return had_recent_failure or last_status == 'failed'
    
    last_startup_time = datetime.fromisoformat(last_startup)
    last_startup_irish = last_startup_time.astimezone(IRISH_TZ)
    hours_since_startup = (irish_now - last_startup_irish).total_seconds() / 3600
    
    return had_recent_failure and hours_since_startup > 1

def send_daily_report():
    health = load_tracker(HEALTH_FILE)
    irish_now = get_irish_time()
    
    total_runs = health.get('run_count', 0)
    failures = health.get('failures', 0)
    success_rate = ((total_runs - failures) / total_runs * 100) if total_runs > 0 else 100
    
    last_run = health.get('last_run_irish', 'Never')
    last_failure = health.get('last_failure_irish', 'No failures')
    
    signals = load_tracker(COOLDOWN_FILE)
    today_signals = []
    yesterday = (irish_now - timedelta(days=1)).timestamp()
    
    for key, timestamp in signals.items():
        if timestamp > yesterday:
            today_signals.append(key)
    
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
            parts = sig.rsplit('_', 1)
            if len(parts) == 2:
                asset, signal_type = parts
                emoji = "🟢" if "BULLISH" in signal_type or "PULLBACK_LONG" in signal_type else "🔴"
                signal_name = signal_type.replace('_', ' ').title()
                message += f"  {emoji} {asset}: {signal_name}\n"
    else:
        message += "\n  • No signals detected in last 24h\n"
    
    message += f"""
━━━━━━━━━━━━━━━━━━━━━
⚠️ <b>Last Failure:</b> {last_failure}

🕐 <b>Next Report:</b> Tomorrow at 9:00 AM Irish Time

<i>🤖 Automated health report - Bot is monitoring 5 assets on 15-min timeframe</i>"""
    
    send_telegram_message(message, admin=True)
    health['last_daily_report'] = datetime.now(timezone.utc).isoformat()
    save_tracker(HEALTH_FILE, health)

def send_startup_message():
    health = load_tracker(HEALTH_FILE)
    irish_time = get_irish_time()
    last_failure = health.get('last_failure_irish', 'Unknown')
    last_status = health.get('last_status', 'Unknown')
    
    message = f"""✅ <b>BOT RECOVERED - NOW MONITORING</b>

━━━━━━━━━━━━━━━━━━━━━
📅 <b>Recovery Time:</b> {irish_time.strftime('%Y-%m-%d %H:%M:%S')} Irish Time
📊 <b>Last Status:</b> {last_status}
⚠️ <b>Last Failure:</b> {last_failure}

📈 <b>Currently Monitoring:</b> 5 assets on 15-min timeframe
⏰ <b>Schedule:</b> Every 6 minutes
🛡️ <b>Cooldown:</b> 12 hours between signals
📊 <b>Signal Logic:</b> Based on LAST CLOSED CANDLE (no repainting)

<i>Bot is back online and monitoring for signals</i>"""
    
    send_telegram_message(message, admin=True)
    health['last_startup_message'] = datetime.now(timezone.utc).isoformat()
    save_tracker(HEALTH_FILE, health)

def send_failure_alert(asset_name: str, error: str):
    irish_time = get_irish_time()
    health = load_tracker(HEALTH_FILE)
    failures_today = health.get('failures', 0)
    
    last_alert = health.get('last_failure_alert')
    if last_alert:
        last_alert_time = datetime.fromisoformat(last_alert)
        seconds_since_alert = (datetime.now(timezone.utc) - last_alert_time).total_seconds()
        if seconds_since_alert < 300:
            return
    
    message = f"""⚠️ <b>BOT ALERT - FAILURE DETECTED</b>

━━━━━━━━━━━━━━━━━━━━━
📅 <b>Time:</b> {irish_time.strftime('%Y-%m-%d %H:%M:%S')} Irish Time
📊 <b>Asset:</b> {asset_name}
❌ <b>Error:</b> {error[:200]}

📈 <b>Failures Today:</b> {failures_today + 1}

<i>Bot will auto-recover on next successful run</i>"""
    
    send_telegram_message(message, admin=True)
    health['last_failure_alert'] = datetime.now(timezone.utc).isoformat()
    save_tracker(HEALTH_FILE, health)

def fetch_twelvedata_data(symbol: str, api_key: str, lookback_bars: int = 500) -> Optional[List[Dict]]:
    """Fetch 15-minute data from Twelve Data API (supported interval)"""
    if not api_key:
        return None
        
    try:
        url = f"https://api.twelvedata.com/time_series"
        params = {
            'symbol': symbol,
            'interval': '15min',  # Changed from 10min to 15min (supported)
            'outputsize': str(lookback_bars),
            'apikey': api_key
        }
        
        log.info(f"Fetching {symbol} (15-min timeframe)...")
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
    if len(prices) < period:
        return []
    
    multiplier = 2 / (period + 1)
    ema_values = []
    current_ema = prices[0]
    
    for price in prices:
        current_ema = (price - current_ema) * multiplier + current_ema
        ema_values.append(current_ema)
    
    return ema_values

def calculate_rsi(closes: List[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50
    
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[-i] - closes[-i-1]
        if diff > 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))
    
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    
    if avg_loss == 0:
        return 100
    
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calculate_adx(prices: List[Dict], period: int = 14) -> float:
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
    if len(prices) < 200:
        return False, None
    
    closes = [p['close'] for p in prices]
    ema50 = calculate_ema_series(closes, 50)
    ema200 = calculate_ema_series(closes, 200)
    
    if len(ema50) < 3 or len(ema200) < 3:
        return False, None
    
    # Use LAST CLOSED CANDLE (index -2) to avoid repainting
    current_ema50 = ema50[-2]
    current_ema200 = ema200[-2]
    prev_ema50 = ema50[-3]
    prev_ema200 = ema200[-3]
    
    if prev_ema200 <= prev_ema50 and current_ema200 > current_ema50:
        return True, "BULLISH_CROSSOVER"
    elif prev_ema200 >= prev_ema50 and current_ema200 < current_ema50:
        return True, "BEARISH_CROSSOVER"
    
    return False, None

def check_pullback_signal(prices: List[Dict], trend_direction: str) -> Tuple[bool, Optional[str]]:
    if len(prices) < 50:
        return False, None
    
    closes = [p['close'] for p in prices]
    highs = [p['high'] for p in prices]
    lows = [p['low'] for p in prices]
    
    ema20 = calculate_ema_series(closes, PULLBACK_EMA)
    
    if len(ema20) < 10:
        return False, None
    
    # Use LAST CLOSED CANDLE (index -2)
    current_price = closes[-2]
    current_ema = ema20[-2]
    prev_price = closes[-3]
    prev_ema = ema20[-3]
    
    rsi = calculate_rsi(closes, 14)
    recent_high = max(highs[-21:-1])
    recent_low = min(lows[-21:-1])
    range_size = recent_high - recent_low
    
    if trend_direction == "BULLISH":
        is_pullback = (prev_price > prev_ema and current_price <= current_ema) or \
                      (abs(current_price - current_ema) / current_ema < 0.005)
        
        retracement = (recent_high - current_price) / range_size if range_size > 0 else 0
        is_healthy_retrace = PULLBACK_RETRACE_PERCENT - 0.1 <= retracement <= PULLBACK_RETRACE_PERCENT + 0.2
        rsi_ok = PULLBACK_MIN_RSI <= rsi <= PULLBACK_MAX_RSI
        
        if is_pullback and is_healthy_retrace and rsi_ok:
            return True, "PULLBACK_LONG"
    
    elif trend_direction == "BEARISH":
        is_pullback = (prev_price < prev_ema and current_price >= current_ema) or \
                      (abs(current_price - current_ema) / current_ema < 0.005)
        
        retracement = (current_price - recent_low) / range_size if range_size > 0 else 0
        is_healthy_retrace = PULLBACK_RETRACE_PERCENT - 0.1 <= retracement <= PULLBACK_RETRACE_PERCENT + 0.2
        rsi_ok = PULLBACK_MIN_RSI <= rsi <= PULLBACK_MAX_RSI
        
        if is_pullback and is_healthy_retrace and rsi_ok:
            return True, "PULLBACK_SHORT"
    
    return False, None

def detect_trend(prices: List[Dict]) -> str:
    if len(prices) < 200:
        return "NEUTRAL"
    
    closes = [p['close'] for p in prices]
    ema50 = calculate_ema_series(closes, 50)
    ema200 = calculate_ema_series(closes, 200)
    
    if len(ema50) < 3 or len(ema200) < 3:
        return "NEUTRAL"
    
    current_ema50 = ema50[-2]
    current_ema200 = ema200[-2]
    
    if current_ema50 > current_ema200:
        return "BULLISH"
    elif current_ema50 < current_ema200:
        return "BEARISH"
    
    return "NEUTRAL"

def calculate_risk_reward(price: float, signal_type: str, risk_pct: float, profit_pct: float) -> Dict:
    if signal_type in ["BULLISH_CROSSOVER", "PULLBACK_LONG"]:
        entry = price
        stop_loss = price * (1 - risk_pct / 100)
        take_profit = price * (1 + profit_pct / 100)
    else:
        entry = price
        stop_loss = price * (1 + risk_pct / 100)
        take_profit = price * (1 - profit_pct / 100)
    
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
    chat_id = TELEGRAM_ADMIN_CHAT_ID if admin else TELEGRAM_CHAT_ID
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    
    try:
        response = requests.post(url, json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
        return response.status_code == 200
    except Exception as e:
        log.error(f"Telegram error: {e}")
        return False

def analyze_asset(asset_name: str, config: Dict) -> Optional[Dict]:
    log.info(f"\n{'='*40}\n🔍 Analyzing {asset_name} (15-min)...")
    
    if not config['api_key']:
        log.warning(f"⚠️ No API key for {asset_name}")
        return None
    
    prices = fetch_twelvedata_data(config['symbol'], config['api_key'], lookback_bars=500)
    if not prices or len(prices) < 200:
        log.warning(f"⚠️ {asset_name}: Only {len(prices) if prices else 0} bars")
        return None
    
    last_closed_price = prices[-2]['close']
    closes = [p['close'] for p in prices]
    
    ema20_series = calculate_ema_series(closes, PULLBACK_EMA)
    ema50_series = calculate_ema_series(closes, 50)
    ema200_series = calculate_ema_series(closes, 200)
    
    ema20 = ema20_series[-2] if len(ema20_series) >= 2 else 0
    ema50 = ema50_series[-2] if len(ema50_series) >= 2 else 0
    ema200 = ema200_series[-2] if len(ema200_series) >= 2 else 0
    
    adx = calculate_adx(prices, 14)
    overall_trend = detect_trend(prices)
    
    has_cross, signal_type = check_ema_crossover(prices)
    
    if not has_cross and overall_trend != "NEUTRAL":
        has_pullback, pullback_type = check_pullback_signal(prices, overall_trend)
        if has_pullback:
            signal_type = pullback_type
            has_cross = True
    
    if not has_cross:
        return None
    
    if adx > 40:
        adx_text = f"{adx:.1f} 🔥 VERY STRONG"
    elif adx > 25:
        adx_text = f"{adx:.1f} ✅ STRONG"
    elif adx > 20:
        adx_text = f"{adx:.1f} 📊 MODERATE"
    else:
        adx_text = f"{adx:.1f} ⚠️ WEAK"
    
    rr = calculate_risk_reward(last_closed_price, signal_type,
                               config['risk_percent'], config['profit_percent'])
    
    shares = int(config['position_size'] / rr['entry']) if rr['entry'] > 0 else 0
    position_value = shares * rr['entry']
    total_risk = shares * abs(rr['entry'] - rr['stop_loss'])
    
    log.info(f"✅ {signal_type} at ${last_closed_price:.2f}")
    
    return {
        'signal_type': signal_type,
        'price': last_closed_price,
        'ema20': ema20,
        'ema50': ema50,
        'ema200': ema200,
        'adx': adx_text,
        'rr': rr,
        'shares': shares,
        'position_value': position_value,
        'total_risk': total_risk,
        'config': config,
        'asset_name': asset_name,
        'overall_trend': overall_trend,
        'candle_time': prices[-2]['timestamp']
    }

def format_signal_message(data: Dict) -> str:
    config, rr = data['config'], data['rr']
    irish_time = get_irish_time()
    signal_type = data['signal_type']
    
    if signal_type == "BULLISH_CROSSOVER":
        arrow = "📈"
        direction = "🟢 BULLISH CROSSOVER (LONG)"
        signal_desc = "EMA200 crossed ABOVE EMA50"
    elif signal_type == "BEARISH_CROSSOVER":
        arrow = "📉"
        direction = "🔴 BEARISH CROSSOVER (SHORT)"
        signal_desc = "EMA200 crossed BELOW EMA50"
    elif signal_type == "PULLBACK_LONG":
        arrow = "📈"
        direction = "🟢 PULLBACK LONG"
        signal_desc = f"Price pulled back to EMA{PULLBACK_EMA} within uptrend"
    elif signal_type == "PULLBACK_SHORT":
        arrow = "📉"
        direction = "🔴 PULLBACK SHORT"
        signal_desc = f"Price pulled back to EMA{PULLBACK_EMA} within downtrend"
    else:
        arrow = "📊"
        direction = f"📊 {signal_type}"
        signal_desc = "Signal detected"
    
    message = f"""<b>{arrow} {config['display_name']} - {direction} {arrow}</b>

━━━━━━━━━━━━━━━━━━━━━
⏰ <b>Timeframe:</b> 15-Minute (Last Closed Candle)
📊 <b>Signal:</b> {signal_desc}
━━━━━━━━━━━━━━━━━━━━━

📊 <b>EMAs (15-Min)</b>
• EMA20: ${data['ema20']:.2f}
• EMA50: ${data['ema50']:.2f}
• EMA200: ${data['ema200']:.2f}

📈 <b>ADX:</b> {data['adx']}
📈 <b>Trend:</b> {data['overall_trend']}

━━━━━━━━━━━━━━━━━━━━━
⚡ <b>RISK MANAGEMENT</b>
━━━━━━━━━━━━━━━━━━━━━
• Risk: {rr['risk_pct']}% | Target: {rr['profit_pct']}%
• Risk:Reward: 1:{rr['ratio']}

📍 <b>Levels:</b>
• Entry: ${rr['entry']}
• Stop Loss: ${rr['stop_loss']}
• Take Profit: ${rr['take_profit']}

━━━━━━━━━━━━━━━━━━━━━
💼 <b>POSITION SIZING</b>
━━━━━━━━━━━━━━━━━━━━━
• Capital: ${config['position_size']:,}
• Shares: {data['shares']:,} units
• Position Value: ${data['position_value']:,.2f}
• Total Risk: ${data['total_risk']:,.2f}
"""
    
    if config.get('mt5_units') and data['asset_name'] in ['GOLD', 'SPY']:
        if data['asset_name'] == 'GOLD':
            risk_pips = abs(rr['entry'] - rr['stop_loss']) / 0.01
            message += f"""
💹 <b>MT5:</b>
• Units: {config['mt5_units']}
• Risk: {risk_pips:.1f} pips
"""
        else:
            message += f"""
💹 <b>MT5:</b>
• Units: {config['mt5_units']}
"""
    
    message += f"""
━━━━━━━━━━━━━━━━━━━━━
💰 <b>Last Closed Candle:</b>
• Price: ${data['price']:.2f}
• Time: {data['candle_time']}

⏰ <b>Irish Time:</b> {irish_time.strftime('%Y-%m-%d %H:%M:%S')}

⚠️ <i>Educational purposes only. Based on last closed 15-min candle.</i>"""
    
    return message

def main():
    log.info("=" * 70)
    log.info("🚀 EMA CROSSOVER + PULLBACK BOT - 15-MIN TIMEFRAME")
    log.info("📊 Based on LAST CLOSED CANDLE (no repainting)")
    log.info("=" * 70)
    
    try:
        update_health('running', 'Bot started successfully')
        
        if should_send_daily_report():
            log.info("📊 Sending daily health report...")
            send_daily_report()
        
        if should_send_startup_message():
            log.info("✅ Sending recovery startup message...")
            send_startup_message()
        
        irish_now = get_irish_time()
        log.info(f"📊 Irish Time: {irish_now.strftime('%Y-%m-%d %H:%M:%S')}")
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
                    log.info(f"📊 {asset_name} - No signal")
                
                time.sleep(2)
                
            except Exception as e:
                failures += 1
                error_msg = str(e)
                log.error(f"❌ Error with {asset_name}: {error_msg}")
                send_failure_alert(asset_name, error_msg)
        
        if failures == 0:
            update_health('completed', f"Sent {signals_sent} signals")
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
