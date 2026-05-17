import os
import logging
import requests
import json
from datetime import datetime, timedelta
import pytz
from telegram import Bot
import time
import sys
from typing import Dict, Optional, Tuple, List
from pathlib import Path

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration - Environment Variables
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHAT_ID = os.environ.get('CHAT_ID')
ALPHA_VANTAGE_API_KEY = os.environ.get('ALPHA_VANTAGE_API_KEY')

# Asset configurations with specific risk/reward rules
ASSETS = {
    'GOLD': {
        'symbol': 'XAUUSD',
        'alpha_symbol': 'XAUUSD',
        'timeframe': '10min',
        'risk_percent': 0.5,
        'profit_percent': 1.0,
        'reward_ratio': 2,
        'position_size': 10000,
        'currency': 'EUR',
        'broker_t212': 'Trading 212',
        'broker_mt5': 'MT5',
        'mt5_units': 0.03,
        'asset_type': 'commodity'
    },
    'SPY': {
        'symbol': 'SPY',
        'alpha_symbol': 'SPY',
        'timeframe': '10min',
        'risk_percent': 2,
        'profit_percent': 4,
        'reward_ratio': 2,
        'position_size': 2500,
        'currency': 'EUR',
        'broker_t212': 'Trading 212',
        'broker_mt5': 'MT5',
        'mt5_units': 0.03,
        'asset_type': 'etf'
    },
    'QQQ': {
        'symbol': 'QQQ',
        'alpha_symbol': 'QQQ',
        'timeframe': '10min',
        'risk_percent': 1,
        'profit_percent': 2,
        'reward_ratio': 2,
        'position_size': 2500,
        'currency': 'EUR',
        'broker_t212': 'Trading 212',
        'broker_mt5': None,
        'mt5_units': None,
        'asset_type': 'etf'
    },
    'ETH': {
        'symbol': 'ETHUSD',
        'alpha_symbol': 'ETH',
        'timeframe': '10min',
        'risk_percent': 1,
        'profit_percent': 2,
        'reward_ratio': 2,
        'position_size': 2500,
        'currency': 'EUR',
        'broker_t212': 'Trading 212',
        'broker_mt5': None,
        'mt5_units': None,
        'asset_type': 'crypto'
    },
    'ADA': {
        'symbol': 'ADAUSD',
        'alpha_symbol': 'ADA',
        'timeframe': '10min',
        'risk_percent': 1,
        'profit_percent': 2,
        'reward_ratio': 2,
        'position_size': 2500,
        'currency': 'EUR',
        'broker_t212': 'Trading 212',
        'broker_mt5': None,
        'mt5_units': None,
        'asset_type': 'crypto'
    }
}

class SimpleDataProcessor:
    """Simple data processing without pandas"""
    
    @staticmethod
    def calculate_ema(prices: List[float], period: int) -> List[float]:
        """Calculate EMA without pandas"""
        if len(prices) < period:
            return [0] * len(prices)
        
        ema = []
        multiplier = 2 / (period + 1)
        
        # Start with SMA for first value
        sma = sum(prices[:period]) / period
        ema.append(sma)
        
        # Calculate subsequent EMAs
        for i in range(period, len(prices)):
            current_ema = (prices[i] - ema[-1]) * multiplier + ema[-1]
            ema.append(current_ema)
        
        # Pad the beginning with zeros
        padding = [0] * (period - 1)
        return padding + ema
    
    @staticmethod
    def calculate_true_range(high: float, low: float, prev_close: float) -> float:
        """Calculate True Range"""
        tr1 = high - low
        tr2 = abs(high - prev_close)
        tr3 = abs(low - prev_close)
        return max(tr1, tr2, tr3)
    
    @staticmethod
    def calculate_adx(prices: List[Dict], period: int = 14) -> float:
        """Calculate ADX without pandas"""
        if len(prices) < period * 2:
            return 0
        
        tr_values = []
        plus_dm_values = []
        minus_dm_values = []
        
        # Calculate True Range and Directional Movements
        for i in range(1, len(prices)):
            high = prices[i]['high']
            low = prices[i]['low']
            close = prices[i-1]['close']
            
            tr = SimpleDataProcessor.calculate_true_range(high, low, close)
            tr_values.append(tr)
            
            up_move = high - prices[i-1]['high']
            down_move = prices[i-1]['low'] - low
            
            plus_dm = up_move if (up_move > down_move and up_move > 0) else 0
            minus_dm = down_move if (down_move > up_move and down_move > 0) else 0
            
            plus_dm_values.append(plus_dm)
            minus_dm_values.append(minus_dm)
        
        # Smooth with Wilder's method
        atr = SimpleDataProcessor.calculate_wilder_sma(tr_values, period)
        smoothed_plus_dm = SimpleDataProcessor.calculate_wilder_sma(plus_dm_values, period)
        smoothed_minus_dm = SimpleDataProcessor.calculate_wilder_sma(minus_dm_values, period)
        
        if atr == 0:
            return 0
        
        plus_di = 100 * (smoothed_plus_dm / atr)
        minus_di = 100 * (smoothed_minus_dm / atr)
        
        if plus_di + minus_di == 0:
            return 0
        
        dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
        
        # Calculate ADX (smoothed DX)
        dx_values = [dx]  # Simplified
        adx = SimpleDataProcessor.calculate_wilder_sma(dx_values, period)
        
        return adx
    
    @staticmethod
    def calculate_wilder_sma(values: List[float], period: int) -> float:
        """Calculate Wilder's Smoothing (similar to EMA)"""
        if len(values) < period:
            return 0
        
        # Start with SMA
        result = sum(values[:period]) / period
        
        # Wilder's smoothing
        for i in range(period, len(values)):
            result = (result * (period - 1) + values[i]) / period
        
        return result

class AlphaVantageFetcher:
    """Fetch data from Alpha Vantage API"""
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://www.alphavantage.co/query"
        self.cache_dir = Path('/tmp/alpha_cache')
        self.cache_dir.mkdir(exist_ok=True)
    
    def get_cached_data(self, key: str, max_age_minutes: int = 10) -> Optional[List[Dict]]:
        """Get cached data to reduce API calls"""
        cache_file = self.cache_dir / f"{key}.json"
        if cache_file.exists():
            try:
                with open(cache_file, 'r') as f:
                    data = json.load(f)
                cache_time = datetime.fromisoformat(data['timestamp'])
                if datetime.now() - cache_time < timedelta(minutes=max_age_minutes):
                    logger.info(f"Using cached data for {key}")
                    return data['data']
            except Exception as e:
                logger.error(f"Cache read error for {key}: {e}")
        return None
    
    def cache_data(self, key: str, data: List[Dict]):
        """Cache fetched data"""
        try:
            cache_file = self.cache_dir / f"{key}.json"
            with open(cache_file, 'w') as f:
                json.dump({
                    'timestamp': datetime.now().isoformat(),
                    'data': data
                }, f)
            logger.info(f"Cached data for {key}")
        except Exception as e:
            logger.error(f"Cache write error for {key}: {e}")
    
    def fetch_intraday(self, symbol: str, interval: str = '10min') -> Optional[List[Dict]]:
        """Fetch intraday data from Alpha Vantage with caching"""
        
        # Try cache first
        cache_key = f"{symbol}_{interval}"
        cached_data = self.get_cached_data(cache_key)
        if cached_data is not None:
            return cached_data
        
        try:
            # Handle crypto symbols
            if symbol in ['ETH', 'ADA']:
                params = {
                    'function': 'DIGITAL_CURRENCY_INTRADAY',
                    'symbol': symbol,
                    'market': 'USD',
                    'apikey': self.api_key
                }
            else:
                params = {
                    'function': 'TIME_SERIES_INTRADAY',
                    'symbol': symbol,
                    'interval': '10min',
                    'outputsize': 'full',
                    'apikey': self.api_key
                }
            
            response = requests.get(self.base_url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            price_data = []
            
            # Parse response
            if 'Time Series' in data:
                time_series_key = [k for k in data.keys() if 'Time Series' in k][0]
                time_series = data[time_series_key]
                
                for timestamp, values in sorted(time_series.items(), reverse=False):
                    price_data.append({
                        'timestamp': timestamp,
                        'open': float(values['1. open']),
                        'high': float(values['2. high']),
                        'low': float(values['3. low']),
                        'close': float(values['4. close']),
                        'volume': float(values['5. volume'])
                    })
                    
            elif 'Digital Currency Intraday' in data:
                time_series = data['Digital Currency Intraday']
                
                for timestamp, values in sorted(time_series.items(), reverse=False):
                    price_data.append({
                        'timestamp': timestamp,
                        'open': float(values['1a. open (USD)']),
                        'high': float(values['2a. high (USD)']),
                        'low': float(values['3a. low (USD)']),
                        'close': float(values['4a. close (USD)']),
                        'volume': float(values['5. volume'])
                    })
            
            if len(price_data) > 200:
                # Cache the data
                self.cache_data(cache_key, price_data)
                logger.info(f"Fetched {len(price_data)} bars for {symbol}")
                return price_data
            else:
                logger.error(f"Insufficient data for {symbol}: {len(price_data)} bars")
                return None
                
        except Exception as e:
            logger.error(f"Error fetching {symbol}: {e}")
            return None

class SignalBot:
    """Main bot class for generating and sending signals"""
    
    def __init__(self, api_key: str, telegram_token: str, chat_id: str):
        self.fetcher = AlphaVantageFetcher(api_key)
        self.processor = SimpleDataProcessor()
        self.telegram_bot = Bot(token=telegram_token)
        self.chat_id = chat_id
        self.last_signals = {}
    
    def calculate_indicators(self, price_data: List[Dict]):
        """Calculate EMAs from price data"""
        closes = [p['close'] for p in price_data]
        
        # Calculate EMAs
        ema50 = self.processor.calculate_ema(closes, 50)
        ema200 = self.processor.calculate_ema(closes, 200)
        
        return ema50, ema200
    
    def check_crossover(self, price_data: List[Dict]) -> Tuple[bool, Optional[str]]:
        """Check for EMA200 crossing EMA50"""
        if len(price_data) < 200:
            return False, None
        
        closes = [p['close'] for p in price_data]
        ema50 = self.processor.calculate_ema(closes, 50)
        ema200 = self.processor.calculate_ema(closes, 200)
        
        if len(ema50) < 2 or len(ema200) < 2:
            return False, None
        
        current_ema50 = ema50[-1]
        current_ema200 = ema200[-1]
        prev_ema50 = ema50[-2]
        prev_ema200 = ema200[-2]
        
        # Check for crossover (EMA200 crosses EMA50)
        if prev_ema200 <= prev_ema50 and current_ema200 > current_ema50:
            return True, "BULLISH 🟢"
        elif prev_ema200 >= prev_ema50 and current_ema200 < current_ema50:
            return True, "BEARISH 🔴"
        
        return False, None
    
    def calculate_risk_reward(self, current_price: float, signal_type: str, risk_percent: float, profit_percent: float) -> Dict:
        """Calculate precise risk and reward levels"""
        if "BULLISH" in signal_type:
            entry = current_price
            stop_loss = entry * (1 - risk_percent / 100)
            take_profit = entry * (1 + profit_percent / 100)
            risk_amount = entry - stop_loss
            reward_amount = take_profit - entry
        else:
            entry = current_price
            stop_loss = entry * (1 + risk_percent / 100)
            take_profit = entry * (1 - profit_percent / 100)
            risk_amount = stop_loss - entry
            reward_amount = entry - take_profit
        
        actual_ratio = round(reward_amount / risk_amount, 2) if risk_amount > 0 else 0
        
        return {
            'entry': round(entry, 4),
            'stop_loss': round(stop_loss, 4),
            'take_profit': round(take_profit, 4),
            'risk_amount': round(risk_amount, 4),
            'reward_amount': round(reward_amount, 4),
            'risk_percent': risk_percent,
            'profit_percent': profit_percent,
            'actual_ratio': actual_ratio
        }
    
    def calculate_position_t212(self, position_size: float, entry_price: float, stop_loss: float, currency: str = 'EUR') -> Dict:
        """Calculate Trading 212 position"""
        risk_per_share = abs(entry_price - stop_loss)
        shares = int(position_size / entry_price) if entry_price > 0 else 0
        total_value = shares * entry_price
        total_risk = shares * risk_per_share
        
        return {
            'shares': shares,
            'total_value': round(total_value, 2),
            'total_risk': round(total_risk, 2),
            'currency': currency
        }
    
    def calculate_position_mt5(self, entry_price: float, stop_loss: float, units: float, asset_type: str) -> Dict:
        """Calculate MT5 position"""
        if asset_type == 'commodity':
            pip_size = 0.01
            point_value = units * 100
            risk_pips = abs(entry_price - stop_loss) / pip_size
            total_risk = risk_pips * point_value
            
            return {
                'units': units,
                'risk_pips': round(risk_pips, 1),
                'total_risk': round(total_risk, 2)
            }
        elif asset_type == 'etf':
            point_value = units * 100000
            risk_points = abs(entry_price - stop_loss)
            total_risk = risk_points * point_value
            
            return {
                'units': units,
                'risk_points': round(risk_points, 2),
                'total_risk': round(total_risk, 2)
            }
        
        return {}
    
    def analyze_asset(self, asset_name: str, config: Dict) -> Optional[Dict]:
        """Analyze single asset for signals"""
        logger.info(f"Analyzing {asset_name}...")
        
        # Fetch data
        price_data = self.fetcher.fetch_intraday(config['alpha_symbol'], config['timeframe'])
        if price_data is None or len(price_data) < 210:
            logger.warning(f"Insufficient data for {asset_name}")
            return None
        
        # Check for crossover
        has_crossover, signal_type = self.check_crossover(price_data)
        
        if not has_crossover:
            logger.info(f"No EMA crossover for {asset_name}")
            return None
        
        current_price = price_data[-1]['close']
        
        # Get latest ADX
        adx = self.processor.calculate_adx(price_data, 14)
        
        # Calculate EMAs for display
        closes = [p['close'] for p in price_data]
        ema50_list = self.processor.calculate_ema(closes, 50)
        ema200_list = self.processor.calculate_ema(closes, 200)
        
        current_ema50 = ema50_list[-1] if ema50_list else 0
        current_ema200 = ema200_list[-1] if ema200_list else 0
        
        # ADX strength
        if adx > 40:
            adx_strength = "VERY STRONG 🔥"
            adx_comment = "Strong trending market"
        elif adx > 25:
            adx_strength = "STRONG ✅"
            adx_comment = "Good trend strength"
        elif adx > 20:
            adx_strength = "MODERATE 📊"
            adx_comment = "Trend developing"
        else:
            adx_strength = "WEAK ⚠️"
            adx_comment = "Range-bound market"
        
        # Calculate risk/reward
        rr = self.calculate_risk_reward(
            current_price,
            signal_type,
            config['risk_percent'],
            config['profit_percent']
        )
        
        # Calculate positions
        t212_pos = self.calculate_position_t212(
            config['position_size'],
            rr['entry'],
            rr['stop_loss'],
            config.get('currency', 'EUR')
        )
        
        mt5_pos = None
        if config.get('mt5_units'):
            mt5_pos = self.calculate_position_mt5(
                rr['entry'],
                rr['stop_loss'],
                config['mt5_units'],
                config['asset_type']
            )
        
        return {
            'asset': asset_name,
            'signal_type': signal_type,
            'current_price': round(current_price, 4),
            'ema50': round(current_ema50, 4),
            'ema200': round(current_ema200, 4),
            'adx': round(adx, 2),
            'adx_strength': adx_strength,
            'adx_comment': adx_comment,
            'risk_reward': rr,
            'position_size': config['position_size'],
            'currency': config.get('currency', 'EUR'),
            't212_position': t212_pos,
            'mt5_position': mt5_pos,
            'timestamp': datetime.now(pytz.UTC)
        }
    
    def format_message(self, signal: Dict) -> str:
        """Format signal as Telegram message"""
        rr = signal['risk_reward']
        
        message = f"""
🚨 <b>{signal['asset']} TRADING SIGNAL - 10-MIN TIMEFRAME</b> 🚨

📊 <b>Signal:</b> {signal['signal_type']} (EMA200 crosses EMA50)
💰 <b>Current Price:</b> ${signal['current_price']}
⏰ <b>Timeframe:</b> 10-Minute Chart

━━━━━━━━━━━━━━━━━━━━━
📈 <b>TECHNICAL INDICATORS</b>
━━━━━━━━━━━━━━━━━━━━━
• EMA50: ${signal['ema50']}
• EMA200: ${signal['ema200']}
• ADX: {signal['adx']} ({signal['adx_strength']})
• <i>{signal['adx_comment']}</i>

━━━━━━━━━━━━━━━━━━━━━
⚡ <b>RISK MANAGEMENT</b>
━━━━━━━━━━━━━━━━━━━━━
• Stop Loss: {rr['risk_percent']}% from entry
• Take Profit: {rr['profit_percent']}% from entry
• Risk:Reward Ratio: 1:{rr['actual_ratio']}

📍 <b>Levels:</b>
• Entry: ${rr['entry']}
• Stop Loss: ${rr['stop_loss']}
• Take Profit: ${rr['take_profit']}

━━━━━━━━━━━━━━━━━━━━━
💼 <b>POSITION SIZING</b>
━━━━━━━━━━━━━━━━━━━━━

📱 <b>Trading 212:</b>
• Capital: {signal['position_size']} {signal['currency']}
• Shares: {signal['t212_position']['shares']} units
• Position Value: {signal['t212_position']['total_value']} {signal['currency']}
• Total Risk: {signal['t212_position']['total_risk']} {signal['currency']}
"""
        
        if signal['mt5_position']:
            mt5 = signal['mt5_position']
            message += f"""
💹 <b>MT5:</b>
• Units: {mt5['units']}
"""
            if 'risk_pips' in mt5:
                message += f"""• Risk: {mt5['risk_pips']} pips
• Total Risk: ${mt5['total_risk']}
"""
            elif 'risk_points' in mt5:
                message += f"""• Risk: {mt5['risk_points']} points
• Total Risk: ${mt5['total_risk']}
"""
        
        message += f"""
━━━━━━━━━━━━━━━━━━━━━
⏰ <i>Signal Time: {signal['timestamp'].strftime('%Y-%m-%d %H:%M:%S UTC')}</i>

⚠️ <b>DISCLAIMER</b>
This signal is based on EMA200 crossing EMA50 on 10-minute timeframe.
Always conduct your own analysis before trading.

<i>🤖 Multi-Asset Trading Bot | 10-Minute EMA Crossover Strategy</i>
"""
        
        return message
    
    def is_duplicate_signal(self, asset: str, signal_type: str) -> bool:
        """Check if signal was sent recently (within last 12 hours)"""
        if asset in self.last_signals:
            last_time, last_type = self.last_signals[asset]
            time_diff = (datetime.now(pytz.UTC) - last_time).total_seconds() / 3600
            if time_diff < 12 and last_type == signal_type:
                logger.info(f"Duplicate {signal_type} signal for {asset} ignored")
                return True
        return False
    
    def send_signal(self, signal: Dict):
        """Send signal to Telegram"""
        try:
            if self.is_duplicate_signal(signal['asset'], signal['signal_type']):
                return
            
            message = self.format_message(signal)
            self.telegram_bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode='HTML',
                disable_web_page_preview=True
            )
            logger.info(f"✅ Signal sent for {signal['asset']}")
            
            self.last_signals[signal['asset']] = (signal['timestamp'], signal['signal_type'])
            
        except Exception as e:
            logger.error(f"Failed to send signal: {e}")
    
    def run_analysis(self):
        """Run analysis for all assets"""
        logger.info("=" * 70)
        logger.info("🚀 Starting Multi-Asset Signal Analysis")
        logger.info(f"📅 Time: {datetime.now(pytz.UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        logger.info("=" * 70)
        
        signals_found = []
        
        for asset_name, config in ASSETS.items():
            try:
                time.sleep(2)  # Rate limiting
                
                signal = self.analyze_asset(asset_name, config)
                if signal:
                    signals_found.append(signal)
                    self.send_signal(signal)
                    
            except Exception as e:
                logger.error(f"❌ Error analyzing {asset_name}: {e}")
        
        if signals_found:
            logger.info(f"✅ Found {len(signals_found)} signals")
        else:
            logger.info("📊 No signals detected")
        
        logger.info("=" * 70)

def main():
    """Main function"""
    if not all([TELEGRAM_TOKEN, CHAT_ID, ALPHA_VANTAGE_API_KEY]):
        logger.error("❌ Missing required environment variables!")
        sys.exit(1)
    
    logger.info("🤖 Multi-Asset Trading Bot Starting...")
    
    bot = SignalBot(ALPHA_VANTAGE_API_KEY, TELEGRAM_TOKEN, CHAT_ID)
    bot.run_analysis()

if __name__ == '__main__':
    main()
