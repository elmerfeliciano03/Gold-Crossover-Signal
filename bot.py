import os
import logging
import requests
import pandas as pd
import numpy as np
from datetime import datetime
import pytz
from telegram import Bot
import time
import sys
from typing import Dict, Optional, Tuple
import json
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
        'risk_percent': 0.5,  # 0.5% loss from entry
        'profit_percent': 1.0,  # 1% profit from entry
        'reward_ratio': 2,  # 1:2 reward:risk (1% profit / 0.5% loss = 2)
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
        'risk_percent': 2,  # 2% risk
        'profit_percent': 4,  # 4% profit (1:2 ratio)
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
        'risk_percent': 1,  # 1% risk
        'profit_percent': 2,  # 2% profit (1:2 movement)
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
        'risk_percent': 1,  # 1% risk
        'profit_percent': 2,  # 2% profit (1:2 movement)
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
        'risk_percent': 1,  # 1% risk
        'profit_percent': 2,  # 2% profit (1:2 movement)
        'reward_ratio': 2,
        'position_size': 2500,
        'currency': 'EUR',
        'broker_t212': 'Trading 212',
        'broker_mt5': None,
        'mt5_units': None,
        'asset_type': 'crypto'
    }
}

class AlphaVantageFetcher:
    """Fetch data from Alpha Vantage API"""
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://www.alphavantage.co/query"
        self.cache_dir = Path('/tmp/alpha_cache')
        self.cache_dir.mkdir(exist_ok=True)
    
    def get_cached_data(self, key: str, max_age_minutes: int = 10) -> Optional[pd.DataFrame]:
        """Get cached data to reduce API calls"""
        cache_file = self.cache_dir / f"{key}.json"
        if cache_file.exists():
            try:
                with open(cache_file, 'r') as f:
                    data = json.load(f)
                cache_time = datetime.fromisoformat(data['timestamp'])
                if datetime.now() - cache_time < timedelta(minutes=max_age_minutes):
                    df = pd.DataFrame(data['data'])
                    df.index = pd.to_datetime(df.index)
                    logger.info(f"Using cached data for {key}")
                    return df
            except Exception as e:
                logger.error(f"Cache read error for {key}: {e}")
        return None
    
    def cache_data(self, key: str, df: pd.DataFrame):
        """Cache fetched data"""
        try:
            cache_file = self.cache_dir / f"{key}.json"
            with open(cache_file, 'w') as f:
                json.dump({
                    'timestamp': datetime.now().isoformat(),
                    'data': df.reset_index().to_dict('records')
                }, f)
            logger.info(f"Cached data for {key}")
        except Exception as e:
            logger.error(f"Cache write error for {key}: {e}")
    
    def fetch_intraday(self, symbol: str, interval: str = '10min') -> Optional[pd.DataFrame]:
        """Fetch intraday data from Alpha Vantage with caching"""
        
        # Try cache first
        cache_key = f"{symbol}_{interval}"
        cached_df = self.get_cached_data(cache_key)
        if cached_df is not None:
            return cached_df
        
        try:
            interval_map = {
                '10min': '10min',
                '5min': '5min',
                '15min': '15min',
                '30min': '30min',
                '60min': '60min'
            }
            
            av_interval = interval_map.get(interval, '10min')
            
            # Handle different asset types
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
                    'interval': av_interval,
                    'outputsize': 'full',
                    'apikey': self.api_key
                }
            
            response = requests.get(self.base_url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            df = None
            
            # Parse response
            if 'Time Series' in data:
                time_series_key = [k for k in data.keys() if 'Time Series' in k][0]
                time_series = data[time_series_key]
                df = pd.DataFrame.from_dict(time_series, orient='index')
                df.index = pd.to_datetime(df.index)
                df.sort_index(inplace=True)
                df.columns = ['Open', 'High', 'Low', 'Close', 'Volume']
                df = df.astype(float)
                
            elif 'Digital Currency Intraday' in data:
                time_series = data['Digital Currency Intraday']
                df = pd.DataFrame.from_dict(time_series, orient='index')
                df.index = pd.to_datetime(df.index)
                df.sort_index(inplace=True)
                df['Open'] = df['1a. open (USD)'].astype(float)
                df['High'] = df['2a. high (USD)'].astype(float)
                df['Low'] = df['3a. low (USD)'].astype(float)
                df['Close'] = df['4a. close (USD)'].astype(float)
                df['Volume'] = df['5. volume'].astype(float)
            
            if df is not None and len(df) > 0:
                # Cache the data
                self.cache_data(cache_key, df)
                logger.info(f"Fetched {len(df)} bars for {symbol}")
                return df
            else:
                logger.error(f"No data returned for {symbol}")
                return None
                
        except Exception as e:
            logger.error(f"Error fetching {symbol}: {e}")
            return None

class TechnicalAnalyzer:
    """Technical analysis calculations"""
    
    @staticmethod
    def calculate_ema(df: pd.DataFrame, period: int) -> pd.Series:
        """Calculate Exponential Moving Average"""
        return df['Close'].ewm(span=period, adjust=False).mean()
    
    @staticmethod
    def calculate_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
        """Calculate ADX (Average Directional Index)"""
        high = df['High']
        low = df['Low']
        close = df['Close']
        
        # True Range
        tr1 = high - low
        tr2 = abs(high - close.shift())
        tr3 = abs(low - close.shift())
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        
        # Directional Movement
        up_move = high - high.shift()
        down_move = low.shift() - low
        
        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0)
        
        # Smooth with Wilder's smoothing
        atr = tr.ewm(span=period, adjust=False).mean()
        plus_di = 100 * (pd.Series(plus_dm).ewm(span=period, adjust=False).mean() / atr)
        minus_di = 100 * (pd.Series(minus_dm).ewm(span=period, adjust=False).mean() / atr)
        
        dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
        adx = dx.ewm(span=period, adjust=False).mean()
        
        return adx
    
    @staticmethod
    def check_crossover(df: pd.DataFrame, fast_period: int = 50, slow_period: int = 200) -> Tuple[bool, Optional[str]]:
        """Check for EMA crossover (EMA200 crosses EMA50 on 10-min timeframe)"""
        if len(df) < slow_period + 1:
            return False, None
        
        ema_fast = TechnicalAnalyzer.calculate_ema(df, fast_period)
        ema_slow = TechnicalAnalyzer.calculate_ema(df, slow_period)
        
        current_fast = ema_fast.iloc[-1]
        current_slow = ema_slow.iloc[-1]
        prev_fast = ema_fast.iloc[-2]
        prev_slow = ema_slow.iloc[-2]
        
        # Crossover detection (EMA200 crosses EMA50)
        # Bullish: EMA200 crosses ABOVE EMA50
        if prev_slow <= prev_fast and current_slow > current_fast:
            return True, "BULLISH 🟢"
        # Bearish: EMA200 crosses BELOW EMA50
        elif prev_slow >= prev_fast and current_slow < current_fast:
            return True, "BEARISH 🔴"
        
        return False, None
    
    @staticmethod
    def calculate_risk_reward(current_price: float, signal_type: str, risk_percent: float, profit_percent: float):
        """Calculate precise risk and reward levels"""
        if "BULLISH" in signal_type:
            entry = current_price
            stop_loss = entry * (1 - risk_percent / 100)
            take_profit = entry * (1 + profit_percent / 100)
            risk_amount = entry - stop_loss
            reward_amount = take_profit - entry
        else:  # BEARISH
            entry = current_price
            stop_loss = entry * (1 + risk_percent / 100)
            take_profit = entry * (1 - profit_percent / 100)
            risk_amount = stop_loss - entry
            reward_amount = entry - take_profit
        
        # Calculate actual ratio
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

class PositionCalculator:
    """Calculate position sizing for different brokers"""
    
    @staticmethod
    def calculate_t212_position(position_size: float, entry_price: float, stop_loss: float, currency: str = 'EUR') -> Dict:
        """Calculate Trading 212 position details"""
        risk_per_share = abs(entry_price - stop_loss)
        shares = int(position_size / entry_price) if entry_price > 0 else 0
        total_value = shares * entry_price
        total_risk = shares * risk_per_share
        
        return {
            'shares': shares,
            'total_value': round(total_value, 2),
            'total_risk': round(total_risk, 2),
            'currency': currency,
            'broker': 'Trading 212'
        }
    
    @staticmethod
    def calculate_mt5_position(entry_price: float, stop_loss: float, units: float, asset_type: str) -> Dict:
        """Calculate MT5 position details"""
        if asset_type == 'commodity':  # Gold
            pip_size = 0.01
            point_value = units * 100  # 1 unit = 100 oz for gold
            risk_pips = abs(entry_price - stop_loss) / pip_size
            total_risk = risk_pips * point_value
            
            return {
                'units': units,
                'risk_pips': round(risk_pips, 1),
                'total_risk': round(total_risk, 2),
                'broker': 'MT5',
                'asset_type': 'Gold (XAUUSD)'
            }
        elif asset_type == 'etf':  # SPY
            point_value = units * 100000  # Standard forex lot sizing
            risk_points = abs(entry_price - stop_loss)
            total_risk = risk_points * point_value
            
            return {
                'units': units,
                'risk_points': round(risk_points, 2),
                'total_risk': round(total_risk, 2),
                'broker': 'MT5',
                'asset_type': 'SPY ETF'
            }
        
        return {'broker': 'MT5', 'note': 'Standard position sizing'}

class SignalBot:
    """Main bot class for generating and sending signals"""
    
    def __init__(self, api_key: str, telegram_token: str, chat_id: str):
        self.fetcher = AlphaVantageFetcher(api_key)
        self.analyzer = TechnicalAnalyzer()
        self.position_calc = PositionCalculator()
        self.telegram_bot = Bot(token=telegram_token)
        self.chat_id = chat_id
        self.last_signals = {}  # Store last signal to avoid duplicates
        
    def analyze_asset(self, asset_name: str, config: Dict) -> Optional[Dict]:
        """Analyze single asset for signals"""
        logger.info(f"Analyzing {asset_name}...")
        
        # Fetch data
        df = self.fetcher.fetch_intraday(config['alpha_symbol'], config['timeframe'])
        if df is None or len(df) < 210:  # Need at least 200+ bars for EMA200
            logger.warning(f"Insufficient data for {asset_name} (only {len(df) if df is not None else 0} bars)")
            return None
        
        # Calculate indicators
        ema50 = self.analyzer.calculate_ema(df, 50)
        ema200 = self.analyzer.calculate_ema(df, 200)
        adx = self.analyzer.calculate_adx(df, 14)
        
        # Check for crossover
        has_crossover, signal_type = self.analyzer.check_crossover(df, 50, 200)
        
        if not has_crossover:
            logger.info(f"No EMA crossover for {asset_name}")
            return None
        
        current_price = df['Close'].iloc[-1]
        current_adx = adx.iloc[-1]
        current_ema50 = ema50.iloc[-1]
        current_ema200 = ema200.iloc[-1]
        
        # Check if ADX is valid
        if pd.isna(current_adx):
            current_adx = 0
        
        # ADX strength interpretation
        if current_adx > 40:
            adx_strength = "VERY STRONG 🔥"
            adx_comment = "Strong trending market"
        elif current_adx > 25:
            adx_strength = "STRONG ✅"
            adx_comment = "Good trend strength"
        elif current_adx > 20:
            adx_strength = "MODERATE 📊"
            adx_comment = "Trend developing"
        else:
            adx_strength = "WEAK ⚠️"
            adx_comment = "Range-bound market"
        
        # Calculate risk/reward using specific percentages
        rr = self.analyzer.calculate_risk_reward(
            current_price, 
            signal_type, 
            config['risk_percent'], 
            config['profit_percent']
        )
        
        # Calculate T212 position
        t212_position = self.position_calc.calculate_t212_position(
            config['position_size'], 
            rr['entry'], 
            rr['stop_loss'],
            config.get('currency', 'EUR')
        )
        
        # Calculate MT5 position if applicable
        mt5_position = None
        if config.get('mt5_units'):
            mt5_position = self.position_calc.calculate_mt5_position(
                rr['entry'],
                rr['stop_loss'],
                config['mt5_units'],
                config['asset_type']
            )
        
        return {
            'asset': asset_name,
            'symbol': config['symbol'],
            'signal_type': signal_type,
            'current_price': round(current_price, 4),
            'ema50': round(current_ema50, 4),
            'ema200': round(current_ema200, 4),
            'adx': round(current_adx, 2),
            'adx_strength': adx_strength,
            'adx_comment': adx_comment,
            'risk_reward': rr,
            'position_size': config['position_size'],
            'currency': config.get('currency', 'EUR'),
            't212_position': t212_position,
            'mt5_position': mt5_position,
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
"""
        
        # Custom message for Gold (specific .5% loss, 1% profit)
        if signal['asset'] == 'GOLD':
            message += f"""
• <b>Stop Loss:</b> {rr['risk_percent']}% from entry
• <b>Take Profit:</b> {rr['profit_percent']}% from entry
• <b>Risk:Reward Ratio:</b> 1:{rr['actual_ratio']}

📍 <b>Levels:</b>
• Entry: ${rr['entry']}
• Stop Loss: ${rr['stop_loss']} (-{rr['risk_percent']}%)
• Take Profit: ${rr['take_profit']} (+{rr['profit_percent']}%)

"""
        else:
            message += f"""
• <b>Risk per trade:</b> {rr['risk_percent']}% of capital
• <b>Target profit:</b> {rr['profit_percent']}% ({rr['profit_percent']/rr['risk_percent']}:1 ratio)
• <b>Risk:Reward Ratio:</b> 1:{rr['actual_ratio']}

📍 <b>Levels:</b>
• Entry: ${rr['entry']}
• Stop Loss: ${rr['stop_loss']} ({'-' + str(rr['risk_percent']) + '%' if 'BULLISH' in signal['signal_type'] else '+' + str(rr['risk_percent']) + '%'})
• Take Profit: ${rr['take_profit']} ({'+' + str(rr['profit_percent']) + '%' if 'BULLISH' in signal['signal_type'] else '-' + str(rr['profit_percent']) + '%'})

"""
        
        message += f"""
━━━━━━━━━━━━━━━━━━━━━
💼 <b>POSITION SIZING</b>
━━━━━━━━━━━━━━━━━━━━━

📱 <b>{signal['t212_position']['broker']}:</b>
• Capital: {signal['position_size']} {signal['currency']}
• Shares: {signal['t212_position']['shares']} units
• Position Value: {signal['t212_position']['total_value']} {signal['currency']}
• Total Risk: {signal['t212_position']['total_risk']} {signal['currency']}
"""
        
        # Add MT5 position if available
        if signal['mt5_position'] and 'units' in signal['mt5_position']:
            mt5 = signal['mt5_position']
            message += f"""
💹 <b>{mt5['broker']} ({mt5['asset_type']}):</b>
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
        
        # Add disclaimer
        message += f"""
━━━━━━━━━━━━━━━━━━━━━
⏰ <i>Signal Time: {signal['timestamp'].strftime('%Y-%m-%d %H:%M:%S UTC')}</i>

⚠️ <b>DISCLAIMER</b>
This signal is based on EMA200 crossing EMA50 on 10-minute timeframe.
Always conduct your own analysis before trading.
Past performance does not guarantee future results.

<i>🤖 Multi-Asset Trading Bot | 10-Minute EMA Crossover Strategy</i>
"""
        
        return message
    
    def is_duplicate_signal(self, asset: str, signal_type: str) -> bool:
        """Check if signal was sent recently (within last 12 hours)"""
        if asset in self.last_signals:
            last_time, last_type = self.last_signals[asset]
            time_diff = (datetime.now(pytz.UTC) - last_time).total_seconds() / 3600
            if time_diff < 12 and last_type == signal_type:
                logger.info(f"Duplicate {signal_type} signal for {asset} ignored (last sent {time_diff:.1f} hours ago)")
                return True
        return False
    
    def send_signal(self, signal: Dict):
        """Send signal to Telegram"""
        try:
            # Check for duplicate
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
            
            # Store last signal
            self.last_signals[signal['asset']] = (signal['timestamp'], signal['signal_type'])
            
        except Exception as e:
            logger.error(f"Failed to send signal for {signal['asset']}: {e}")
    
    def run_analysis(self):
        """Run analysis for all assets"""
        logger.info("=" * 70)
        logger.info("🚀 Starting Multi-Asset Signal Analysis")
        logger.info(f"📅 Time: {datetime.now(pytz.UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        logger.info("=" * 70)
        
        signals_found = []
        
        for asset_name, config in ASSETS.items():
            try:
                # Add delay between API calls to avoid rate limiting
                time.sleep(3)
                
                signal = self.analyze_asset(asset_name, config)
                if signal:
                    signals_found.append(signal)
                    self.send_signal(signal)
                    
            except Exception as e:
                logger.error(f"❌ Error analyzing {asset_name}: {e}")
        
        if signals_found:
            logger.info(f"✅ Found {len(signals_found)} signals: {[s['asset'] for s in signals_found]}")
        else:
            logger.info("📊 No signals detected for any asset")
        
        logger.info("=" * 70)
        logger.info("Analysis complete")
        logger.info("=" * 70)

# Import timedelta for caching
from datetime import timedelta

def main():
    """Main function - runs on cron trigger"""
    # Validate environment variables
    if not all([TELEGRAM_TOKEN, CHAT_ID, ALPHA_VANTAGE_API_KEY]):
        logger.error("❌ Missing required environment variables!")
        logger.error("Required: TELEGRAM_TOKEN, CHAT_ID, ALPHA_VANTAGE_API_KEY")
        sys.exit(1)
    
    logger.info("🤖 QQQ Trading Bot Starting...")
    logger.info(f"Bot Token: {'✓' if TELEGRAM_TOKEN else '✗'}")
    logger.info(f"Chat ID: {'✓' if CHAT_ID else '✗'}")
    logger.info(f"Alpha Vantage API: {'✓' if ALPHA_VANTAGE_API_KEY else '✗'}")
    
    # Create and run bot
    bot = SignalBot(ALPHA_VANTAGE_API_KEY, TELEGRAM_TOKEN, CHAT_ID)
    bot.run_analysis()

if __name__ == '__main__':
    main()
