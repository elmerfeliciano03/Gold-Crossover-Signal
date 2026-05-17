#!/usr/bin/env python3
"""
Multi-Asset Trading Signal Bot for Render Cron Jobs
Monitors EMA crossovers with ADX confirmation for multiple assets
Uses multiple data sources to avoid blocking
"""

import logging
import pandas as pd
import numpy as np
import requests
from datetime import datetime, timedelta
import os
import sys
import time
import json
from typing import Optional, Dict, Any, List
import yfinance as yf
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Configuration from environment variables
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')
POSITION_SIZE = float(os.getenv('POSITION_SIZE', '2500'))
RISK_PERCENT = float(os.getenv('RISK_PERCENT', '0.02'))
ADX_THRESHOLD = float(os.getenv('ADX_THRESHOLD', '25'))

# Asset configuration
ASSETS = {
    'GOLD': {
        'symbols': ['GC=F', 'GLD', 'XAUUSD=X'],
        'source': 'yfinance',
        'name': 'Gold'
    },
    'SPY': {
        'symbols': ['SPY'],
        'source': 'yfinance',
        'name': 'S&P 500 ETF'
    },
    'QQQ': {
        'symbols': ['QQQ'],
        'source': 'yfinance',
        'name': 'Nasdaq ETF'
    },
    'ETH': {
        'symbols': ['ETH-USD', 'ETHUSDT'],
        'source': 'crypto',
        'name': 'Ethereum'
    },
    'ADA': {
        'symbols': ['ADA-USD', 'ADAUSDT'],
        'source': 'crypto',
        'name': 'Cardano'
    }
}

class DataFetcher:
    """Handle data fetching from multiple sources with retry logic"""
    
    def __init__(self):
        self.session = self._create_session()
        
    def _create_session(self):
        """Create requests session with retries"""
        session = requests.Session()
        retry = Retry(
            total=3,
            read=3,
            connect=3,
            backoff_factor=0.5,
            status_forcelist=[500, 502, 503, 504]
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        
        # Add headers to avoid blocking
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json',
            'Accept-Language': 'en-US,en;q=0.9',
            'Cache-Control': 'no-cache'
        })
        return session
    
    def fetch_yfinance(self, symbol: str) -> Optional[pd.DataFrame]:
        """Fetch data from Yahoo Finance with error handling"""
        try:
            logger.info(f"Fetching {symbol} from Yahoo Finance...")
            
            # Configure yfinance to use different endpoint
            ticker = yf.Ticker(symbol)
            
            # Try different periods
            for period in ['5d', '1mo', '3mo']:
                try:
                    df = ticker.history(period=period, interval='1h')
                    if df is not None and len(df) > 50:
                        logger.info(f"✅ Yahoo Finance: Got {len(df)} bars for {symbol}")
                        return df
                except:
                    continue
                    
            logger.warning(f"⚠️ Yahoo Finance: Insufficient data for {symbol}")
            return None
            
        except Exception as e:
            logger.warning(f"⚠️ Yahoo Finance error for {symbol}: {str(e)}")
            return None
    
    def fetch_alphavantage(self, symbol: str) -> Optional[pd.DataFrame]:
        """Fetch data from Alpha Vantage (requires API key)"""
        api_key = os.getenv('ALPHA_VANTAGE_KEY')
        if not api_key:
            return None
            
        try:
            # Map symbols to Alpha Vantage format
            av_symbols = {
                'GOLD': 'XAUUSD',
                'SPY': 'SPY',
                'QQQ': 'QQQ'
            }
            
            if symbol not in av_symbols:
                return None
                
            url = f"https://www.alphavantage.co/query"
            params = {
                'function': 'TIME_SERIES_INTRADAY',
                'symbol': av_symbols[symbol],
                'interval': '60min',
                'apikey': api_key,
                'outputsize': 'compact'
            }
            
            response = self.session.get(url, params=params, timeout=10)
            data = response.json()
            
            if 'Time Series (60min)' in data:
                df = pd.DataFrame.from_dict(data['Time Series (60min)'], orient='index')
                df.index = pd.to_datetime(df.index)
                df = df.sort_index()
                
                # Rename columns
                df = df.rename(columns={
                    '1. open': 'Open',
                    '2. high': 'High',
                    '3. low': 'Low',
                    '4. close': 'Close',
                    '5. volume': 'Volume'
                })
                
                # Convert to numeric
                for col in ['Open', 'High', 'Low', 'Close', 'Volume']:
                    df[col] = pd.to_numeric(df[col])
                
                logger.info(f"✅ Alpha Vantage: Got {len(df)} bars for {symbol}")
                return df
                
        except Exception as e:
            logger.warning(f"⚠️ Alpha Vantage error: {str(e)}")
            
        return None
    
    def fetch_crypto(self, symbol: str) -> Optional[pd.DataFrame]:
        """Fetch crypto data from multiple exchanges"""
        crypto_symbols = {
            'ETH': 'ethereum',
            'ADA': 'cardano'
        }
        
        if symbol not in crypto_symbols:
            return None
            
        coin_id = crypto_symbols[symbol]
        
        # Try CoinGecko first
        df = self.fetch_coingecko(coin_id)
        if df is not None:
            return df
            
        # Try Binance API
        df = self.fetch_binance(symbol)
        if df is not None:
            return df
            
        return None
    
    def fetch_coingecko(self, coin_id: str) -> Optional[pd.DataFrame]:
        """Fetch crypto data from CoinGecko"""
        try:
            logger.info(f"Fetching {coin_id} from CoinGecko...")
            
            # Try with different timeframes
            for days in [7, 30, 90]:
                url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc"
                params = {
                    'vs_currency': 'usd',
                    'days': days
                }
                
                response = self.session.get(url, params=params, timeout=10)
                
                if response.status_code == 200:
                    data = response.json()
                    if data and len(data) > 50:
                        df = pd.DataFrame(data, columns=['timestamp', 'Open', 'High', 'Low', 'Close'])
                        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                        df.set_index('timestamp', inplace=True)
                        logger.info(f"✅ CoinGecko: Got {len(df)} bars for {coin_id}")
                        return df
                        
                elif response.status_code == 429:
                    logger.warning(f"⚠️ CoinGecko rate limit, waiting...")
                    time.sleep(2)
                    continue
                    
            return None
            
        except Exception as e:
            logger.warning(f"⚠️ CoinGecko error: {str(e)}")
            return None
    
    def fetch_binance(self, symbol: str) -> Optional[pd.DataFrame]:
        """Fetch crypto data from Binance API"""
        try:
            # Map to Binance symbols
            binance_symbols = {
                'ETH': 'ETHUSDT',
                'ADA': 'ADAUSDT'
            }
            
            if symbol not in binance_symbols:
                return None
                
            binance_symbol = binance_symbols[symbol]
            url = f"https://api.binance.com/api/v3/klines"
            params = {
                'symbol': binance_symbol,
                'interval': '1h',
                'limit': 200
            }
            
            response = self.session.get(url, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                df = pd.DataFrame(data, columns=[
                    'timestamp', 'Open', 'High', 'Low', 'Close', 'Volume',
                    'close_time', 'quote_asset_volume', 'number_of_trades',
                    'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'
                ])
                
                # Convert to numeric
                for col in ['Open', 'High', 'Low', 'Close']:
                    df[col] = pd.to_numeric(df[col])
                
                df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                df.set_index('timestamp', inplace=True)
                
                logger.info(f"✅ Binance: Got {len(df)} bars for {symbol}")
                return df
                
        except Exception as e:
            logger.warning(f"⚠️ Binance error: {str(e)}")
            
        return None
    
    def fetch_finnhub(self, symbol: str) -> Optional[pd.DataFrame]:
        """Fetch data from Finnhub (requires API key)"""
        api_key = os.getenv('FINNHUB_KEY')
        if not api_key:
            return None
            
        try:
            finnhub_symbols = {
                'GOLD': 'XAUUSD',
                'SPY': 'SPY',
                'QQQ': 'QQQ'
            }
            
            if symbol not in finnhub_symbols:
                return None
                
            # Finnhub doesn't have free OHLC, use alternative
            return None
            
        except Exception as e:
            logger.warning(f"⚠️ Finnhub error: {str(e)}")
            return None
    
    def get_asset_data(self, asset_name: str) -> Optional[pd.DataFrame]:
        """Get data for asset from multiple sources"""
        asset_config = ASSETS.get(asset_name, {})
        
        # Try different data sources based on asset type
        if asset_config.get('source') == 'crypto':
            # Try crypto sources
            df = self.fetch_crypto(asset_name)
            if df is not None:
                return df
                
        # Try Yahoo Finance for all assets
        for symbol in asset_config.get('symbols', []):
            df = self.fetch_yfinance(symbol)
            if df is not None and len(df) > 50:
                return df
                
        # Try Alpha Vantage as backup
        if os.getenv('ALPHA_VANTAGE_KEY'):
            df = self.fetch_alphavantage(asset_name)
            if df is not None and len(df) > 50:
                return df
                
        return None

class TechnicalAnalyzer:
    """Technical analysis calculations"""
    
    @staticmethod
    def calculate_ema(df: pd.DataFrame, period: int) -> pd.Series:
        """Calculate EMA"""
        return df['Close'].ewm(span=period, adjust=False).mean()
    
    @staticmethod
    def calculate_sma(df: pd.DataFrame, period: int) -> pd.Series:
        """Calculate SMA"""
        return df['Close'].rolling(window=period).mean()
    
    @staticmethod
    def calculate_rsi(df: pd.DataFrame, period: int = 14) -> float:
        """Calculate RSI"""
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        return rsi.iloc[-1] if len(rsi) > 0 else 50
    
    @staticmethod
    def calculate_adx(df: pd.DataFrame, period: int = 14) -> tuple:
        """Calculate ADX"""
        try:
            high = df['High']
            low = df['Low']
            close = df['Close']
            
            # True Range
            tr1 = high - low
            tr2 = abs(high - close.shift())
            tr3 = abs(low - close.shift())
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            atr = tr.rolling(window=period).mean()
            
            # Directional Movement
            up_move = high - high.shift()
            down_move = low.shift() - low
            
            plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0)
            minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0)
            
            # Smooth the DM
            plus_di = 100 * (pd.Series(plus_dm).rolling(window=period).mean() / atr)
            minus_di = 100 * (pd.Series(minus_dm).rolling(window=period).mean() / atr)
            
            # DX and ADX
            dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
            adx = dx.rolling(window=period).mean()
            
            if len(adx) > 0 and not pd.isna(adx.iloc[-1]):
                return round(adx.iloc[-1], 2), round(plus_di.iloc[-1], 2), round(minus_di.iloc[-1], 2)
            return 0, 0, 0
            
        except Exception as e:
            logger.error(f"Error calculating ADX: {str(e)}")
            return 0, 0, 0
    
    @staticmethod
    def check_ema_cross(df: pd.DataFrame, fast: int = 20, slow: int = 50) -> Dict[str, Any]:
        """Check EMA cross"""
        try:
            ema_fast = TechnicalAnalyzer.calculate_ema(df, fast)
            ema_slow = TechnicalAnalyzer.calculate_ema(df, slow)
            
            current_fast = ema_fast.iloc[-1]
            current_slow = ema_slow.iloc[-1]
            prev_fast = ema_fast.iloc[-2]
            prev_slow = ema_slow.iloc[-2]
            
            current_price = df['Close'].iloc[-1]
            
            # Check for cross
            golden_cross = (prev_fast <= prev_slow) and (current_fast > current_slow)
            death_cross = (prev_fast >= prev_slow) and (current_fast < current_slow)
            
            result = {
                'current_price': round(current_price, 2),
                f'ema{fast}': round(current_fast, 2),
                f'ema{slow}': round(current_slow, 2),
                'signal': None,
                'cross_type': None,
                'message': f"No {fast}/{slow} EMA cross detected"
            }
            
            if golden_cross:
                result['signal'] = "BULLISH"
                result['cross_type'] = f"Golden Cross (EMA{fast}↑EMA{slow})"
                result['message'] = f"EMA{fast} crossed ABOVE EMA{slow} - Bullish signal!"
            elif death_cross:
                result['signal'] = "BEARISH"
                result['cross_type'] = f"Death Cross (EMA{fast}↓EMA{slow})"
                result['message'] = f"EMA{fast} crossed BELOW EMA{slow} - Bearish signal!"
                
            return result
            
        except Exception as e:
            logger.error(f"Error checking EMA cross: {str(e)}")
            return None

class MultiAssetBot:
    """Main bot class"""
    
    def __init__(self):
        self.data_fetcher = DataFetcher()
        self.analyzer = TechnicalAnalyzer()
        self.position_size = POSITION_SIZE
        self.risk_percent = RISK_PERCENT
        self.adx_threshold = ADX_THRESHOLD
        
    def calculate_risk_reward(self, entry_price: float, signal_type: str, df: pd.DataFrame) -> Optional[Dict[str, Any]]:
        """Calculate risk-reward levels"""
        try:
            # Calculate ATR
            high_low = df['High'] - df['Low']
            high_close = abs(df['High'] - df['Close'].shift())
            low_close = abs(df['Low'] - df['Close'].shift())
            tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
            atr = tr.rolling(window=14).mean().iloc[-1]
            
            if pd.isna(atr) or atr == 0:
                return None
            
            # Risk amount
            risk_amount = self.position_size * self.risk_percent
            
            # Stop loss distance (1.5x ATR)
            stop_distance = atr * 1.5
            
            if signal_type == "BULLISH":
                stop_loss = entry_price - stop_distance
                take_profit = entry_price + (stop_distance * 2)
                direction = "LONG"
            else:
                stop_loss = entry_price + stop_distance
                take_profit = entry_price - (stop_distance * 2)
                direction = "SHORT"
            
            # Position sizing
            risk_per_share = abs(entry_price - stop_loss)
            if risk_per_share == 0:
                return None
                
            shares_to_trade = risk_amount / risk_per_share
            position_value = shares_to_trade * entry_price
            
            return {
                'direction': direction,
                'entry': round(entry_price, 2),
                'stop_loss': round(stop_loss, 2),
                'take_profit': round(take_profit, 2),
                'risk_amount': round(risk_amount, 2),
                'position_size': round(shares_to_trade, 2),
                'position_value': round(position_value, 2),
                'atr': round(atr, 2),
                'risk_reward_ratio': "1:2"
            }
            
        except Exception as e:
            logger.error(f"Error calculating risk-reward: {str(e)}")
            return None
    
    def analyze_asset(self, asset_name: str) -> Optional[Dict[str, Any]]:
        """Analyze single asset"""
        logger.info(f"🔍 Analysing {asset_name}...")
        
        # Fetch data
        df = self.data_fetcher.get_asset_data(asset_name)
        
        if df is None or len(df) < 100:
            logger.warning(f"⚠️ Insufficient data for {asset_name} ({len(df) if df is not None else 0} bars)")
            return None
        
        logger.info(f"✅ {asset_name}: Got {len(df)} bars")
        
        # Calculate indicators
        ema_data = self.analyzer.check_ema_cross(df, 20, 50)
        if ema_data is None:
            return None
            
        adx, plus_di, minus_di = self.analyzer.calculate_adx(df)
        rsi = self.analyzer.calculate_rsi(df)
        
        result = {
            'asset': asset_name,
            'name': ASSETS.get(asset_name, {}).get('name', asset_name),
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC'),
            'price': ema_data['current_price'],
            'ema20': ema_data['ema20'],
            'ema50': ema_data['ema50'],
            'adx': adx,
            'plus_di': plus_di,
            'minus_di': minus_di,
            'rsi': round(rsi, 2),
            'has_cross': ema_data['signal'] is not None,
            'signal': ema_data['signal'],
            'cross_type': ema_data['cross_type'],
            'cross_message': ema_data['message'],
            'risk_reward': None
        }
        
        # Calculate risk-reward if strong signal
        if result['has_cross'] and adx > self.adx_threshold:
            result['risk_reward'] = self.calculate_risk_reward(
                result['price'],
                result['signal'],
                df
            )
            
        return result

def send_telegram_message(message: str) -> bool:
    """Send message to Telegram"""
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.error("Telegram credentials missing!")
        return False
    
    # Split long messages
    if len(message) > 4000:
        chunks = [message[i:i+4000] for i in range(0, len(message), 4000)]
        success = True
        for chunk in chunks:
            if not send_single_message(chunk):
                success = False
        return success
    else:
        return send_single_message(message)

def send_single_message(message: str) -> bool:
    """Send single message to Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        'chat_id': CHAT_ID,
        'text': message,
        'parse_mode': 'Markdown',
        'disable_web_page_preview': True
    }
    
    try:
        response = requests.post(url, json=payload, timeout=15)
        if response.status_code == 200:
            return True
        else:
            logger.error(f"Failed: {response.status_code}")
            return False
    except Exception as e:
        logger.error(f"Error: {str(e)}")
        return False

def format_asset_message(data: Dict[str, Any]) -> str:
    """Format analysis for single asset"""
    
    # Get asset icon
    icons = {
        'GOLD': '🥇',
        'SPY': '📈',
        'QQQ': '💻',
        'ETH': '🔷',
        'ADA': '🟣'
    }
    icon = icons.get(data['asset'], '📊')
    
    message = f"{icon} *{data['name']} ({data['asset']})*\n"
    message += f"🕐 {data['timestamp']}\n\n"
    
    message += f"💰 *Price:* ${data['price']:,.2f}\n"
    message += f"📊 *EMA20:* ${data['ema20']:,.2f} | *EMA50:* ${data['ema50']:,.2f}\n"
    message += f"⚡ *ADX:* {data['adx']} "
    
    if data['adx'] > 40:
        message += "🔴 (Very Strong)\n"
    elif data['adx'] > 25:
        message += "🟢 (Strong)\n"
    elif data['adx'] > 20:
        message += "🟡 (Developing)\n"
    else:
        message += "🔵 (Weak)\n"
    
    message += f"📊 *DMI:* +DI {data['plus_di']} | -DI {data['minus_di']}\n"
    message += f"📈 *RSI:* {data['rsi']}"
    
    if data['rsi'] > 70:
        message += " (Overbought)\n"
    elif data['rsi'] < 30:
        message += " (Oversold)\n"
    else:
        message += " (Neutral)\n"
    
    message += f"\n🔄 *Signal:* {data['cross_message']}\n"
    
    # Trading setup
    if data['has_cross'] and data['adx'] > ADX_THRESHOLD and data['risk_reward']:
        rr = data['risk_reward']
        message += f"\n🎯 *🔥 TRADE SETUP - {data['signal']}* 🔥\n"
        message += f"└ *Direction:* {rr['direction']}\n"
        message += f"└ *Entry:* ${rr['entry']}\n"
        message += f"└ *Stop Loss:* ${rr['stop_loss']}\n"
        message += f"└ *Take Profit:* ${rr['take_profit']}\n"
        message += f"└ *Risk:Reward:* {rr['risk_reward_ratio']}\n"
        message += f"└ *Position Size:* {rr['position_size']} units\n"
        message += f"└ *Risk Amount:* ${rr['risk_amount']} (2% of ${POSITION_SIZE:,.0f})\n"
        
    elif data['has_cross']:
        message += f"\n⚠️ *Cross detected but ADX = {data['adx']} (<{ADX_THRESHOLD})*\n"
        message += f"└ Wait for trend strength to increase"
        
    elif data['adx'] > ADX_THRESHOLD:
        message += f"\n⚡ *Strong trend but no EMA cross*\n"
        message += f"└ Monitor for potential setup"
    
    return message

def main():
    """Main execution"""
    logger.info("=" * 70)
    logger.info("🤖 Multi-Asset Trading Bot Initialising...")
    logger.info(f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    logger.info(f"📊 Assets: {', '.join(ASSETS.keys())}")
    logger.info("=" * 70)
    
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.error("❌ Missing TELEGRAM_TOKEN or CHAT_ID environment variables")
        sys.exit(1)
    
    bot = MultiAssetBot()
    signals_found = 0
    all_messages = []
    
    for asset in ASSETS.keys():
        try:
            result = bot.analyze_asset(asset)
            
            if result:
                # Check if this is a valid signal
                is_signal = result['has_cross'] and result['adx'] > ADX_THRESHOLD
                
                if is_signal:
                    signals_found += 1
                    logger.info(f"🔥 SIGNAL found for {asset}!")
                
                # Format and store message
                message = format_asset_message(result)
                all_messages.append(message)
                
                # Send immediately for signals
                if is_signal:
                    send_telegram_message(message)
                    time.sleep(1)  # Avoid rate limiting
                    
        except Exception as e:
            logger.error(f"❌ Error analyzing {asset}: {str(e)}")
            continue
    
    # Send summary if no signals found
    if signals_found == 0:
        summary = "📊 *Market Summary*\n\n"
        summary += f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
        summary += f"🔍 Analysed {len(ASSETS)} assets\n"
        summary += f"❌ No strong trading signals found\n\n"
        summary += "*Conditions needed:*\n"
        summary += "• EMA20/EMA50 crossover\n"
        summary += f"• ADX > {ADX_THRESHOLD}\n"
        summary += "• 1:2 Risk-Reward ratio\n\n"
        summary += "📊 *Individual analysis:*\n"
        
        for msg in all_messages[:3]:  # Send first 3 analyses
            summary += "─" * 20 + "\n"
            summary += msg.split('\n')[0] + "\n"  # Just the header line
        
        send_telegram_message(summary)
    
    logger.info(f"✅ Analysis complete — {signals_found} signal(s) sent")
    logger.info("=" * 70)

if __name__ == '__main__':
    main()
