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
        'yahoo_symbol': 'GC=F',  # Gold futures on Yahoo
        'risk_percent': 0.5,
        'profit_percent': 1.0,
        'position_size': 10000,
        'currency': 'EUR',
        'mt5_units': 0.03,
        'asset_type': 'commodity'
    },
    'SPY': {
        'symbol': 'SPY',
        'alpha_symbol': 'SPY',
        'yahoo_symbol': 'SPY',
        'risk_percent': 2,
        'profit_percent': 4,
        'position_size': 2500,
        'currency': 'EUR',
        'mt5_units': 0.03,
        'asset_type': 'etf'
    },
    'QQQ': {
        'symbol': 'QQQ',
        'alpha_symbol': 'QQQ',
        'yahoo_symbol': 'QQQ',
        'risk_percent': 1,
        'profit_percent': 2,
        'position_size': 2500,
        'currency': 'EUR',
        'mt5_units': None,
        'asset_type': 'etf'
    },
    'ETH': {
        'symbol': 'ETHUSD',
        'alpha_symbol': 'ETH',
        'yahoo_symbol': 'ETH-USD',
        'risk_percent': 1,
        'profit_percent': 2,
        'position_size': 2500,
        'currency': 'EUR',
        'mt5_units': None,
        'asset_type': 'crypto'
    },
    'ADA': {
        'symbol': 'ADAUSD',
        'alpha_symbol': 'ADA',
        'yahoo_symbol': 'ADA-USD',
        'risk_percent': 1,
        'profit_percent': 2,
        'position_size': 2500,
        'currency': 'EUR',
        'mt5_units': None,
        'asset_type': 'crypto'
    }
}

class DataFetcher:
    """Fetch data from multiple sources"""
    
    def __init__(self, alpha_key: str = None):
        self.alpha_key = alpha_key
        self.alpha_base_url = "https://www.alphavantage.co/query"
    
    def fetch_from_yahoo(self, symbol: str) -> Optional[List[Dict]]:
        """Fetch data from Yahoo Finance (free, no API key needed)"""
        try:
            # Use Yahoo Finance's public API
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            params = {
                'interval': '10m',
                'range': '7d',
                'includePrePost': 'false'
            }
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            
            response = requests.get(url, params=params, headers=headers, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if 'chart' in data and 'result' in data['chart'] and data['chart']['result']:
                result = data['chart']['result'][0]
                timestamps = result.get('timestamp', [])
                quote = result.get('indicators', {}).get('quote', [{}])[0]
                
                opens = quote.get('open', [])
                highs = quote.get('high', [])
                lows = quote.get('low', [])
                closes = quote.get('close', [])
                volumes = quote.get('volume', [])
                
                price_data = []
                for i in range(len(timestamps)):
                    if all([opens[i], highs[i], lows[i], closes[i]]):
                        price_data.append({
                            'timestamp': datetime.fromtimestamp(timestamps[i]).isoformat(),
                            'open': float(opens[i]),
                            'high': float(highs[i]),
                            'low': float(lows[i]),
                            'close': float(closes[i]),
                            'volume': float(volumes[i]) if volumes[i] else 0
                        })
                
                if len(price_data) >= 200:
                    logger.info(f"✅ Yahoo: Fetched {len(price_data)} bars for {symbol}")
                    return price_data[-210:]
                else:
                    logger.warning(f"⚠️ Yahoo: Only {len(price_data)} bars for {symbol}")
                    return None
                    
        except Exception as e:
            logger.error(f"Yahoo fetch error for {symbol}: {e}")
            return None
    
    def fetch_from_alphavantage(self, symbol: str) -> Optional[List[Dict]]:
        """Fetch data from Alpha Vantage (requires API key)"""
        if not self.alpha_key:
            return None
            
        try:
            if symbol in ['ETH', 'ADA']:
                params = {
                    'function': 'DIGITAL_CURRENCY_INTRADAY',
                    'symbol': symbol,
                    'market': 'USD',
                    'apikey': self.alpha_key
                }
            else:
                params = {
                    'function': 'TIME_SERIES_INTRADAY',
                    'symbol': symbol,
                    'interval': '10min',
                    'outputsize': 'full',
                    'apikey': self.alpha_key
                }
            
            response = requests.get(self.alpha_base_url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if 'Error Message' in data or 'Note' in data:
                logger.warning(f"Alpha Vantage limitation for {symbol}: {data.get('Note', data.get('Error Message', 'Unknown'))}")
                return None
            
            price_data = []
            
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
            
            if len(price_data) >= 200:
                logger.info(f"✅ Alpha Vantage: Fetched {len(price_data)} bars for {symbol}")
                return price_data[-210:]
            else:
                logger.warning(f"⚠️ Alpha Vantage: Only {len(price_data)} bars for {symbol}")
                return None
                
        except Exception as e:
            logger.error(f"Alpha Vantage error for {symbol}: {e}")
            return None
    
    def fetch_intraday(self, asset_name: str, config: Dict) -> Optional[List[Dict]]:
        """Fetch data from best available source"""
        
        # Try Yahoo Finance first (more reliable for free tier)
        logger.info(f"Trying Yahoo Finance for {asset_name}...")
        yahoo_symbol = config.get('yahoo_symbol')
        if yahoo_symbol:
            data = self.fetch_from_yahoo(yahoo_symbol)
            if data:
                return data
        
        # Try Alpha Vantage as backup
        logger.info(f"Trying Alpha Vantage for {asset_name}...")
        alpha_symbol = config.get('alpha_symbol')
        if alpha_symbol:
            data = self.fetch_from_alphavantage(alpha_symbol)
            if data:
                return data
        
        logger.error(f"❌ No data source available for {asset_name}")
        return None

class SimpleDataProcessor:
    """Technical calculations without pandas"""
    
    @staticmethod
    def calculate_ema(prices: List[float], period: int) -> List[float]:
        """Calculate EMA manually"""
        if len(prices) < period:
            return [0] * len(prices)
        
        ema = []
        multiplier = 2 / (period + 1)
        
        # Start with SMA
        sma = sum(prices[:period]) / period
        ema.append(sma)
        
        # Calculate EMAs
        for i in range(period, len(prices)):
            current_ema = (prices[i] - ema[-1]) * multiplier + ema[-1]
            ema.append(current_ema)
        
        # Pad beginning with zeros
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
        
        if len(tr_values) < period:
            return 0
        
        # Calculate averages
        atr = sum(tr_values[:period]) / period
        avg_plus_dm = sum(plus_dm_values[:period]) / period
        avg_minus_dm = sum(minus_dm_values[:period]) / period
        
        if atr == 0:
            return 0
        
        plus_di = 100 * (avg_plus_dm / atr)
        minus_di = 100 * (avg_minus_dm / atr)
        
        if plus_di + minus_di == 0:
            return 0
        
        dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
        return dx

class SignalBot:
    """Main bot class"""
    
    def __init__(self, api_key: str, telegram_token: str, chat_id: str):
        self.fetcher = DataFetcher(api_key)
        self.processor = SimpleDataProcessor()
        self.bot = Bot(token=telegram_token)
        self.chat_id = chat_id
        self.last_signals = {}
    
    def check_crossover(self, price_data: List[Dict]) -> Tuple[bool, Optional[str]]:
        """Check for EMA200 crossing EMA50"""
        if len(price_data) < 200:
            return False, None
        
        # Extract closing prices
        closes = [p['close'] for p in price_data]
        
        # Calculate EMAs
        ema50 = self.processor.calculate_ema(closes, 50)
        ema200 = self.processor.calculate_ema(closes, 200)
        
        if len(ema50) < 2 or len(ema200) < 2:
            return False, None
        
        # Current values
        current_ema50 = ema50[-1]
        current_ema200 = ema200[-1]
        
        # Previous values
        prev_ema50 = ema50[-2]
        prev_ema200 = ema200[-2]
        
        # Check for crossover (EMA200 crossing EMA50)
        if prev_ema200 <= prev_ema50 and current_ema200 > current_ema50:
            logger.info(f"📈 BULLISH crossover! EMA200: {current_ema200:.2f} > EMA50: {current_ema50:.2f}")
            return True, "BULLISH 🟢"
        elif prev_ema200 >= prev_ema50 and current_ema200 < current_ema50:
            logger.info(f"📉 BEARISH crossover! EMA200: {current_ema200:.2f} < EMA50: {current_ema50:.2f}")
            return True, "BEARISH 🔴"
        
        return False, None
    
    def calculate_risk_reward(self, current_price: float, signal_type: str, 
                             risk_percent: float, profit_percent: float) -> Dict:
        """Calculate entry, stop loss, take profit levels"""
        if "BULLISH" in signal_type:
            entry = current_price
            stop_loss = entry * (1 - risk_percent / 100)
            take_profit = entry * (1 + profit_percent / 100)
        else:
            entry = current_price
            stop_loss = entry * (1 + risk_percent / 100)
            take_profit = entry * (1 - profit_percent / 100)
        
        risk_amount = abs(entry - stop_loss)
        reward_amount = abs(take_profit - entry)
        actual_ratio = round(reward_amount / risk_amount, 2) if risk_amount > 0 else 0
        
        return {
            'entry': round(entry, 4),
            'stop_loss': round(stop_loss, 4),
            'take_profit': round(take_profit, 4),
            'risk_percent': risk_percent,
            'profit_percent': profit_percent,
            'actual_ratio': actual_ratio
        }
    
    def analyze_asset(self, asset_name: str, config: Dict) -> Optional[Dict]:
        """Analyze single asset for trading signals"""
        logger.info(f"🔍 Analyzing {asset_name}...")
        
        # Fetch data
        price_data = self.fetcher.fetch_intraday(asset_name, config)
        if not price_data:
            logger.warning(f"❌ No data for {asset_name}")
            return None
        
        if len(price_data) < 200:
            logger.warning(f"⚠️ Only {len(price_data)} bars for {asset_name} (need 200+)")
            return None
        
        logger.info(f"✅ Got {len(price_data)} bars for {asset_name}")
        
        # Check for crossover
        has_crossover, signal_type = self.check_crossover(price_data)
        if not has_crossover:
            return None
        
        # Get current price and indicators
        current_price = price_data[-1]['close']
        closes = [p['close'] for p in price_data]
        
        # Calculate EMAs for display
        ema50_list = self.processor.calculate_ema(closes, 50)
        ema200_list = self.processor.calculate_ema(closes, 200)
        
        # Calculate ADX
        adx = self.processor.calculate_adx(price_data, 14)
        
        # Determine ADX strength
        if adx > 40:
            adx_strength = "VERY STRONG 🔥"
        elif adx > 25:
            adx_strength = "STRONG ✅"
        elif adx > 20:
            adx_strength = "MODERATE 📊"
        else:
            adx_strength = "WEAK ⚠️"
        
        # Calculate risk/reward levels
        rr = self.calculate_risk_reward(
            current_price, signal_type,
            config['risk_percent'], config['profit_percent']
        )
        
        # Calculate position sizing
        shares = int(config['position_size'] / rr['entry']) if rr['entry'] > 0 else 0
        position_value = shares * rr['entry']
        total_risk = shares * abs(rr['entry'] - rr['stop_loss'])
        
        return {
            'asset': asset_name,
            'signal_type': signal_type,
            'current_price': round(current_price, 4),
            'ema50': round(ema50_list[-1], 4) if ema50_list else 0,
            'ema200': round(ema200_list[-1], 4) if ema200_list else 0,
            'adx': round(adx, 2),
            'adx_strength': adx_strength,
            'risk_reward': rr,
            'position_size': config['position_size'],
            'currency': config['currency'],
            'shares': shares,
            'position_value': round(position_value, 2),
            'total_risk': round(total_risk, 2),
            'mt5_units': config.get('mt5_units'),
            'timestamp': datetime.now(pytz.UTC)
        }
    
    def format_telegram_message(self, signal: Dict) -> str:
        """Format signal as Telegram message"""
        rr = signal['risk_reward']
        
        mt5_text = ""
        if signal['mt5_units'] and signal['asset'] in ['GOLD', 'SPY']:
            if signal['asset'] == 'GOLD':
                risk_pips = abs(rr['entry'] - rr['stop_loss']) / 0.01
                mt5_text = f"""
💹 <b>MT5:</b>
• Units: {signal['mt5_units']}
• Risk: {risk_pips:.1f} pips
• Approx Risk: ${risk_pips * signal['mt5_units'] * 10:.2f}
"""
            else:
                risk_points = abs(rr['entry'] - rr['stop_loss'])
                mt5_text = f"""
💹 <b>MT5:</b>
• Units: {signal['mt5_units']}
• Risk: {risk_points:.2f} points
"""
        
        message = f"""
🚨 <b>{signal['asset']} TRADING SIGNAL - 10-MIN TIMEFRAME</b> 🚨

📊 <b>Signal:</b> {signal['signal_type']} (EMA200 crosses EMA50)
💰 <b>Current Price:</b> ${signal['current_price']}

━━━━━━━━━━━━━━━━━━━━━
📈 <b>TECHNICAL INDICATORS</b>
━━━━━━━━━━━━━━━━━━━━━
• EMA50: ${signal['ema50']}
• EMA200: ${signal['ema200']}
• ADX: {signal['adx']} ({signal['adx_strength']})

━━━━━━━━━━━━━━━━━━━━━
⚡ <b>RISK MANAGEMENT</b>
━━━━━━━━━━━━━━━━━━━━━
• Stop Loss: {rr['risk_percent']}% from entry
• Take Profit: {rr['profit_percent']}% from entry
• Risk:Reward: 1:{rr['actual_ratio']}

📍 <b>Levels:</b>
• Entry: ${rr['entry']}
• Stop Loss: ${rr['stop_loss']}
• Take Profit: ${rr['take_profit']}

━━━━━━━━━━━━━━━━━━━━━
💼 <b>POSITION SIZING ({signal['currency']})</b>
━━━━━━━━━━━━━━━━━━━━━

📱 <b>Trading 212:</b>
• Capital: {signal['position_size']:,}
• Shares: {signal['shares']:,} units
• Position Value: {signal['position_value']:,}
• Total Risk: {signal['total_risk']:,}
{mt5_text}
━━━━━━━━━━━━━━━━━━━━━
⏰ <i>{signal['timestamp'].strftime('%Y-%m-%d %H:%M:%S UTC')}</i>

⚠️ <b>DISCLAIMER:</b> For educational purposes only.
Always do your own research before trading.

<i>🤖 Multi-Asset Trading Bot | 10-Minute EMA Crossover Strategy</i>
"""
        return message
    
    def send_signal(self, signal: Dict):
        """Send signal to Telegram"""
        try:
            # Prevent duplicate signals
            signal_key = f"{signal['asset']}_{signal['signal_type']}"
            if signal_key in self.last_signals:
                last_time = self.last_signals[signal_key]
                time_diff = (datetime.now(pytz.UTC) - last_time).total_seconds() / 3600
                if time_diff < 12:
                    logger.info(f"⏭️ Skipping duplicate for {signal['asset']}")
                    return
            
            message = self.format_telegram_message(signal)
            self.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode='HTML',
                disable_web_page_preview=True
            )
            
            self.last_signals[signal_key] = datetime.now(pytz.UTC)
            logger.info(f"✅ Signal sent for {signal['asset']}")
            
        except Exception as e:
            logger.error(f"❌ Failed to send signal: {e}")
    
    def run_analysis(self):
        """Run complete analysis"""
        logger.info("=" * 70)
        logger.info("🚀 Starting Multi-Asset Signal Analysis")
        logger.info(f"📅 {datetime.now(pytz.UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        logger.info(f"📊 Assets: {', '.join(ASSETS.keys())}")
        logger.info("=" * 70)
        
        signals_sent = 0
        
        for asset_name, config in ASSETS.items():
            try:
                time.sleep(1)  # Rate limiting
                signal = self.analyze_asset(asset_name, config)
                if signal:
                    self.send_signal(signal)
                    signals_sent += 1
            except Exception as e:
                logger.error(f"❌ Error with {asset_name}: {e}")
        
        logger.info("=" * 70)
        logger.info(f"✅ Analysis complete - {signals_sent} signal(s) sent")
        logger.info("=" * 70)

def main():
    """Main entry point"""
    logger.info("🤖 Multi-Asset Trading Bot Initializing...")
    
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.error("❌ Missing TELEGRAM_TOKEN or CHAT_ID")
        sys.exit(1)
    
    bot = SignalBot(ALPHA_VANTAGE_API_KEY, TELEGRAM_TOKEN, CHAT_ID)
    bot.run_analysis()

if __name__ == '__main__':
    main()
