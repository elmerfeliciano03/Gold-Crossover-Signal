import os
import logging
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import pytz
from telegram import Bot
import time
import sys
from typing import Dict, Optional, Tuple

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

# Asset configurations
ASSETS = {
    'GOLD': {
        'symbol': 'XAUUSD',
        'alpha_symbol': 'XAUUSD',
        'timeframe': '10min',
        'risk_percent': 0.5,  # 0.5%
        'reward_ratio': 2,  # 1:2 means reward is 2x risk (1% move)
        'position_size': 10000,
        'broker': 'Trading 212',
        'mt5_lot_size': 0.03,
        'asset_type': 'commodity'
    },
    'SPY': {
        'symbol': 'SPY',
        'alpha_symbol': 'SPY',
        'timeframe': '10min',
        'risk_percent': 2,  # 2%
        'reward_ratio': 2,  # 1:2
        'position_size': 2500,
        'broker': 'T212',
        'mt5_lot_size': 0.03,
        'asset_type': 'etf'
    },
    'QQQ': {
        'symbol': 'QQQ',
        'alpha_symbol': 'QQQ',
        'timeframe': '10min',
        'risk_percent': 1,  # 1%
        'reward_ratio': 2,  # 1%:2% movement
        'position_size': 2500,
        'broker': 'T212',
        'mt5_lot_size': None,
        'asset_type': 'etf'
    },
    'ETH': {
        'symbol': 'ETHUSD',
        'alpha_symbol': 'ETH',
        'timeframe': '10min',
        'risk_percent': 1,  # 1%
        'reward_ratio': 2,  # 1%:2% movement
        'position_size': 2500,
        'broker': 'T212',
        'mt5_lot_size': None,
        'asset_type': 'crypto'
    },
    'ADA': {
        'symbol': 'ADAUSD',
        'alpha_symbol': 'ADA',
        'timeframe': '10min',
        'risk_percent': 1,  # 1%
        'reward_ratio': 2,  # 1%:2% movement
        'position_size': 2500,
        'broker': 'T212',
        'mt5_lot_size': None,
        'asset_type': 'crypto'
    }
}

class AlphaVantageFetcher:
    """Fetch data from Alpha Vantage API"""
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://www.alphavantage.co/query"
    
    def fetch_intraday(self, symbol: str, interval: str = '10min') -> Optional[pd.DataFrame]:
        """Fetch intraday data from Alpha Vantage"""
        try:
            # Map intervals
            interval_map = {
                '10min': '10min',
                '5min': '5min',
                '15min': '15min',
                '30min': '30min',
                '60min': '60min'
            }
            
            av_interval = interval_map.get(interval, '10min')
            
            params = {
                'function': 'TIME_SERIES_INTRADAY',
                'symbol': symbol,
                'interval': av_interval,
                'outputsize': 'full',
                'apikey': self.api_key
            }
            
            # Special handling for crypto
            if symbol in ['ETH', 'ADA', 'BTC']:
                params['function'] = 'DIGITAL_CURRENCY_INTRADAY'
                params['market'] = 'USD'
                del params['symbol']
                params['symbol'] = symbol
            
            response = requests.get(self.base_url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            # Parse response based on function type
            if 'Time Series' in data:
                time_series_key = [k for k in data.keys() if 'Time Series' in k][0]
                time_series = data[time_series_key]
                
                df = pd.DataFrame.from_dict(time_series, orient='index')
                df.index = pd.to_datetime(df.index)
                df.sort_index(inplace=True)
                
                # Rename columns
                df.columns = ['Open', 'High', 'Low', 'Close', 'Volume']
                df = df.astype(float)
                
                logger.info(f"Fetched {len(df)} bars for {symbol}")
                return df
            
            elif 'Digital Currency Intraday' in data:
                time_series = data['Digital Currency Intraday']
                df = pd.DataFrame.from_dict(time_series, orient='index')
                df.index = pd.to_datetime(df.index)
                df.sort_index(inplace=True)
                
                # For crypto data
                df['Open'] = df['1a. open (USD)'].astype(float)
                df['High'] = df['2a. high (USD)'].astype(float)
                df['Low'] = df['3a. low (USD)'].astype(float)
                df['Close'] = df['4a. close (USD)'].astype(float)
                df['Volume'] = df['5. volume'].astype(float)
                
                logger.info(f"Fetched {len(df)} crypto bars for {symbol}")
                return df
            
            else:
                logger.error(f"Unexpected API response for {symbol}: {data.get('Error Message', 'Unknown error')}")
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
        
        # Smooth with Wilder's smoothing (similar to EMA with alpha=1/period)
        atr = tr.ewm(span=period, adjust=False).mean()
        plus_di = 100 * (pd.Series(plus_dm).ewm(span=period, adjust=False).mean() / atr)
        minus_di = 100 * (pd.Series(minus_dm).ewm(span=period, adjust=False).mean() / atr)
        
        dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
        adx = dx.ewm(span=period, adjust=False).mean()
        
        return adx
    
    @staticmethod
    def check_crossover(df: pd.DataFrame, fast_period: int = 50, slow_period: int = 200) -> Tuple[bool, Optional[str]]:
        """Check for EMA crossover"""
        if len(df) < slow_period + 1:
            return False, None
        
        ema_fast = TechnicalAnalyzer.calculate_ema(df, fast_period)
        ema_slow = TechnicalAnalyzer.calculate_ema(df, slow_period)
        
        current_fast = ema_fast.iloc[-1]
        current_slow = ema_slow.iloc[-1]
        prev_fast = ema_fast.iloc[-2]
        prev_slow = ema_slow.iloc[-2]
        
        # Bullish crossover: fast crosses above slow
        if prev_fast <= prev_slow and current_fast > current_slow:
            return True, "BULLISH 🟢"
        # Bearish crossover: fast crosses below slow
        elif prev_fast >= prev_slow and current_fast < current_slow:
            return True, "BEARISH 🔴"
        
        return False, None
    
    @staticmethod
    def calculate_risk_reward(current_price: float, signal_type: str, risk_percent: float, reward_ratio: float):
        """Calculate risk and reward levels"""
        if "BULLISH" in signal_type:
            entry = current_price
            stop_loss = entry * (1 - risk_percent / 100)
            take_profit = entry * (1 + (risk_percent * reward_ratio) / 100)
            risk_amount = entry - stop_loss
            reward_amount = take_profit - entry
        else:  # BEARISH
            entry = current_price
            stop_loss = entry * (1 + risk_percent / 100)
            take_profit = entry * (1 - (risk_percent * reward_ratio) / 100)
            risk_amount = stop_loss - entry
            reward_amount = entry - take_profit
        
        return {
            'entry': round(entry, 4),
            'stop_loss': round(stop_loss, 4),
            'take_profit': round(take_profit, 4),
            'risk_amount': round(risk_amount, 4),
            'reward_amount': round(reward_amount, 4),
            'risk_reward_ratio': reward_ratio
        }

class PositionCalculator:
    """Calculate position sizing for different brokers"""
    
    @staticmethod
    def calculate_t212_position(position_size: float, entry_price: float, stop_loss: float) -> Dict:
        """Calculate Trading 212 position details"""
        risk_per_share = abs(entry_price - stop_loss)
        shares = int(position_size / entry_price) if entry_price > 0 else 0
        total_value = shares * entry_price
        total_risk = shares * risk_per_share
        
        return {
            'shares': shares,
            'total_value': round(total_value, 2),
            'total_risk': round(total_risk, 2),
            'broker': 'Trading 212'
        }
    
    @staticmethod
    def calculate_mt5_position(entry_price: float, stop_loss: float, lot_size: float, asset_type: str) -> Dict:
        """Calculate MT5 position details"""
        if asset_type == 'commodity':  # Gold
            pip_size = 0.01
            point_value = lot_size * 100  # 1 lot = 100 oz
            risk_pips = abs(entry_price - stop_loss) / pip_size
            total_risk = risk_pips * point_value
            
            return {
                'lots': lot_size,
                'risk_pips': round(risk_pips, 1),
                'total_risk': round(total_risk, 2),
                'broker': 'MT5'
            }
        elif asset_type == 'etf':  # SPY, QQQ
            point_value = lot_size * 100000  # Standard forex lot sizing
            risk_points = abs(entry_price - stop_loss)
            total_risk = risk_points * point_value
            
            return {
                'lots': lot_size,
                'risk_points': round(risk_points, 2),
                'total_risk': round(total_risk, 2),
                'broker': 'MT5'
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
        self.last_signals = {}  # Store last signal time to avoid duplicates
    
    def analyze_asset(self, asset_name: str, config: Dict) -> Optional[Dict]:
        """Analyze single asset for signals"""
        logger.info(f"Analyzing {asset_name}...")
        
        # Fetch data
        df = self.fetcher.fetch_intraday(config['alpha_symbol'], config['timeframe'])
        if df is None or len(df) < 210:  # Need at least 200+ bars for EMA200
            logger.warning(f"Insufficient data for {asset_name}")
            return None
        
        # Calculate indicators
        ema50 = self.analyzer.calculate_ema(df, 50)
        ema200 = self.analyzer.calculate_ema(df, 200)
        adx = self.analyzer.calculate_adx(df, 14)
        
        # Check for crossover
        has_crossover, signal_type = self.analyzer.check_crossover(df, 50, 200)
        
        if not has_crossover:
            logger.info(f"No crossover for {asset_name}")
            return None
        
        current_price = df['Close'].iloc[-1]
        current_adx = adx.iloc[-1]
        current_ema50 = ema50.iloc[-1]
        current_ema200 = ema200.iloc[-1]
        
        # Check ADX strength
        adx_strength = "VERY STRONG 🔥" if current_adx > 40 else "STRONG ✅" if current_adx > 25 else "WEAK ⚠️"
        
        # Calculate risk/reward
        rr = self.analyzer.calculate_risk_reward(
            current_price, 
            signal_type, 
            config['risk_percent'], 
            config['reward_ratio']
        )
        
        # Calculate positions
        t212_position = self.position_calc.calculate_t212_position(
            config['position_size'], 
            rr['entry'], 
            rr['stop_loss']
        )
        
        mt5_position = None
        if config.get('mt5_lot_size'):
            mt5_position = self.position_calc.calculate_mt5_position(
                rr['entry'],
                rr['stop_loss'],
                config['mt5_lot_size'],
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
            'risk_reward': rr,
            'risk_percent': config['risk_percent'],
            'position_size': config['position_size'],
            't212_position': t212_position,
            'mt5_position': mt5_position,
            'timestamp': datetime.now(pytz.UTC)
        }
    
    def format_message(self, signal: Dict) -> str:
        """Format signal as Telegram message"""
        rr = signal['risk_reward']
        
        message = f"""
🚨 <b>{signal['asset']} TRADING SIGNAL</b> 🚨

📊 <b>Signal:</b> {signal['signal_type']} EMA Crossover
💰 <b>Current Price:</b> ${signal['current_price']}
⏰ <b>Timeframe:</b> 10-Minute

━━━━━━━━━━━━━━━━━━━━━
📈 <b>TECHNICAL INDICATORS</b>
━━━━━━━━━━━━━━━━━━━━━
• EMA50: ${signal['ema50']}
• EMA200: ${signal['ema200']}
• ADX: {signal['adx']} ({signal['adx_strength']})

━━━━━━━━━━━━━━━━━━━━━
⚡ <b>RISK MANAGEMENT</b>
━━━━━━━━━━━━━━━━━━━━━
• Risk: {signal['risk_percent']}% per trade
• Reward:Risk Ratio: 1:{signal['risk_reward']['risk_reward_ratio']}
• Entry: ${rr['entry']}
• Stop Loss: ${rr['stop_loss']}
• Take Profit: ${rr['take_profit']}
• Risk per Unit: ${rr['risk_amount']}
• Reward per Unit: ${rr['reward_amount']}

━━━━━━━━━━━━━━━━━━━━━
💼 <b>POSITION SIZING</b>
━━━━━━━━━━━━━━━━━━━━━
"""
        
        # Add T212 position
        t212 = signal['t212_position']
        message += f"""
📱 <b>Trading 212:</b>
• Shares: {t212['shares']} shares
• Position Value: ${t212['total_value']}
• Total Risk: ${t212['total_risk']}
"""
        
        # Add MT5 position if available
        if signal['mt5_position']:
            mt5 = signal['mt5_position']
            if 'lots' in mt5:
                message += f"""
💹 <b>MT5 ({signal['asset']}):</b>
• Lots: {mt5['lots']}
• Risk: {mt5.get('risk_pips', mt5.get('risk_points', 0))} {'pips' if 'pips' in mt5 else 'points'}
• Total Risk: ${mt5['total_risk']}
"""
        
        message += f"""
━━━━━━━━━━━━━━━━━━━━━
⏰ <i>Signal Time: {signal['timestamp'].strftime('%Y-%m-%d %H:%M:%S UTC')}</i>

⚠️ <b>DISCLAIMER</b>
This is for educational purposes only.
Always do your own research before trading.
━━━━━━━━━━━━━━━━━━━━━

<i>🤖 Multi-Asset Trading Bot | 10-Minute Timeframe</i>
"""
        
        return message
    
    def send_signal(self, signal: Dict):
        """Send signal to Telegram"""
        try:
            message = self.format_message(signal)
            self.telegram_bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode='HTML',
                disable_web_page_preview=True
            )
            logger.info(f"Signal sent for {signal['asset']}")
            
            # Store last signal time
            self.last_signals[signal['asset']] = signal['timestamp']
            
        except Exception as e:
            logger.error(f"Failed to send signal for {signal['asset']}: {e}")
    
    def run_analysis(self):
        """Run analysis for all assets"""
        logger.info("=" * 60)
        logger.info("Starting multi-asset signal analysis")
        logger.info(f"Time: {datetime.now(pytz.UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        
        signals_found = []
        
        for asset_name, config in ASSETS.items():
            try:
                # Add small delay between API calls to avoid rate limiting
                time.sleep(2)
                
                signal = self.analyze_asset(asset_name, config)
                if signal:
                    signals_found.append(signal)
                    self.send_signal(signal)
                    
            except Exception as e:
                logger.error(f"Error analyzing {asset_name}: {e}")
        
        if signals_found:
            logger.info(f"Found {len(signals_found)} signals")
        else:
            logger.info("No signals detected for any asset")
        
        logger.info("Analysis complete")
        logger.info("=" * 60)

def main():
    """Main function - runs on cron trigger"""
    # Validate environment variables
    if not all([TELEGRAM_TOKEN, CHAT_ID, ALPHA_VANTAGE_API_KEY]):
        logger.error("Missing required environment variables!")
        logger.error("Required: TELEGRAM_TOKEN, CHAT_ID, ALPHA_VANTAGE_API_KEY")
        sys.exit(1)
    
    # Create and run bot
    bot = SignalBot(ALPHA_VANTAGE_API_KEY, TELEGRAM_TOKEN, CHAT_ID)
    bot.run_analysis()

if __name__ == '__main__':
    main()
