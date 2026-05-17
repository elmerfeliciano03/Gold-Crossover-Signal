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
        'yahoo_symbol': 'GC=F',
        'risk_percent': 0.5,
        'profit_percent': 1.0,
        'position_size': 10000,
        'currency': 'EUR',
        'mt5_units': 0.03,
        'asset_type': 'commodity'
    },
    'SPY': {
        'symbol': 'SPY',
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
    """Fetch data from Yahoo Finance (free, no API key needed)"""
    
    def __init__(self):
        pass
    
    def fetch_from_yahoo(self, symbol: str) -> Optional[List[Dict]]:
        """Fetch data from Yahoo Finance"""
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            params = {
                'interval': '10m',
                'range': '7d',
                'includePrePost': 'false'
            }
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            
            logger.info(f"Fetching {symbol} from Yahoo...")
            response = requests.get(url, params=params, headers=headers, timeout=15)
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
                    if i < len(opens) and i < len(highs) and i < len(lows) and i < len(closes):
                        if opens[i] and highs[i] and lows[i] and closes[i]:
                            price_data.append({
                                'timestamp': datetime.fromtimestamp(timestamps[i]).isoformat(),
                                'open': float(opens[i]),
                                'high': float(highs[i]),
                                'low': float(lows[i]),
                                'close': float(closes[i]),
                                'volume': float(volumes[i]) if i < len(volumes) and volumes[i] else 0
                            })
                
                if len(price_data) >= 100:
                    logger.info(f"✅ Fetched {len(price_data)} bars for {symbol}")
                    return price_data[-210:]  # Return last 210 bars
                else:
                    logger.warning(f"⚠️ Only {len(price_data)} bars for {symbol}")
                    return None
            else:
                logger.warning(f"No data in response for {symbol}")
                return None
                
        except Exception as e:
            logger.error(f"Error fetching {symbol}: {e}")
            return None
    
    def fetch_intraday(self, asset_name: str, config: Dict) -> Optional[List[Dict]]:
        """Fetch data for asset"""
        yahoo_symbol = config.get('yahoo_symbol')
        if yahoo_symbol:
            data = self.fetch_from_yahoo(yahoo_symbol)
            if data:
                return data
        
        logger.error(f"❌ Could not fetch data for {asset_name}")
        return None

class TechnicalAnalyzer:
    """Technical calculations without pandas"""
    
    @staticmethod
    def calculate_ema(prices: List[float], period: int) -> List[float]:
        """Calculate EMA manually"""
        if len(prices) < period:
            return [0] * len(prices)
        
        ema = []
        multiplier = 2 / (period + 1)
        
        # Start with SMA for first value
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
            prev_close = prices[i-1]['close']
            
            tr = TechnicalAnalyzer.calculate_true_range(high, low, prev_close)
            tr_values.append(tr)
            
            up_move = high - prices[i-1]['high']
            down_move = prices[i-1]['low'] - low
            
            plus_dm = up_move if (up_move > down_move and up_move > 0) else 0
            minus_dm = down_move if (down_move > up_move and down_move > 0) else 0
            
            plus_dm_values.append(plus_dm)
            minus_dm_values.append(minus_dm)
        
        if len(tr_values) < period:
            return 0
        
        # Simple averages
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
    
    @staticmethod
    def check_crossover(price_data: List[Dict]) -> Tuple[bool, Optional[str]]:
        """Check for EMA200 crossing EMA50"""
        if len(price_data) < 200:
            return False, None
        
        closes = [p['close'] for p in price_data]
        
        ema50 = TechnicalAnalyzer.calculate_ema(closes, 50)
        ema200 = TechnicalAnalyzer.calculate_ema(closes, 200)
        
        if len(ema50) < 2 or len(ema200) < 2:
            return False, None
        
        current_ema50 = ema50[-1]
        current_ema200 = ema200[-1]
        prev_ema50 = ema50[-2]
        prev_ema200 = ema200[-2]
        
        # EMA200 crosses above EMA50 (Bullish)
        if prev_ema200 <= prev_ema50 and current_ema200 > current_ema50:
            logger.info(f"📈 BULLISH - EMA200: {current_ema200:.2f} > EMA50: {current_ema50:.2f}")
            return True, "BULLISH 🟢"
        
        # EMA200 crosses below EMA50 (Bearish)
        elif prev_ema200 >= prev_ema50 and current_ema200 < current_ema50:
            logger.info(f"📉 BEARISH - EMA200: {current_ema200:.2f} < EMA50: {current_ema50:.2f}")
            return True, "BEARISH 🔴"
        
        return False, None

class SignalBot:
    """Main bot class"""
    
    def __init__(self, telegram_token: str, chat_id: str):
        self.fetcher = DataFetcher()
        self.analyzer = TechnicalAnalyzer()
        self.bot = Bot(token=telegram_token)
        self.chat_id = chat_id
        self.last_signals = {}
    
    def calculate_risk_reward(self, current_price: float, signal_type: str, 
                             risk_percent: float, profit_percent: float) -> Dict:
        """Calculate risk/reward levels"""
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
        """Analyze single asset"""
        logger.info(f"🔍 Analyzing {asset_name}...")
        
        # Fetch data
        price_data = self.fetcher.fetch_intraday(asset_name, config)
        if not price_data or len(price_data) < 200:
            logger.warning(f"❌ Insufficient data for {asset_name}")
            return None
        
        # Check for crossover
        has_crossover, signal_type = self.analyzer.check_crossover(price_data)
        if not has_crossover:
            return None
        
        # Get current values
        current_price = price_data[-1]['close']
        closes = [p['close'] for p in price_data]
        
        ema50_list = self.analyzer.calculate_ema(closes, 50)
        ema200_list = self.analyzer.calculate_ema(closes, 200)
        adx = self.analyzer.calculate_adx(price_data, 14)
        
        # ADX strength
        if adx > 40:
            adx_strength = "VERY STRONG 🔥"
        elif adx > 25:
            adx_strength = "STRONG ✅"
        elif adx > 20:
            adx_strength = "MODERATE 📊"
        else:
            adx_strength = "WEAK ⚠️"
        
        # Risk/Reward
        rr = self.calculate_risk_reward(
            current_price, signal_type,
            config['risk_percent'], config['profit_percent']
        )
        
        # Position sizing
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
    
    def format_message(self, signal: Dict) -> str:
        """Format Telegram message"""
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
            # Prevent duplicates
            signal_key = f"{signal['asset']}_{signal['signal_type']}"
            if signal_key in self.last_signals:
                last_time = self.last_signals[signal_key]
                time_diff = (datetime.now(pytz.UTC) - last_time).total_seconds() / 3600
                if time_diff < 12:
                    logger.info(f"⏭️ Skipping duplicate for {signal['asset']}")
                    return
            
            message = self.format_message(signal)
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
    
    def run(self):
        """Run analysis"""
        logger.info("=" * 70)
        logger.info("🚀 Starting Multi-Asset Signal Analysis")
        logger.info(f"📅 {datetime.now(pytz.UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        logger.info(f"📊 Assets: {', '.join(ASSETS.keys())}")
        logger.info("=" * 70)
        
        signals_sent = 0
        
        for asset_name, config in ASSETS.items():
            try:
                time.sleep(1)
                signal = self.analyze_asset(asset_name, config)
                if signal:
                    self.send_signal(signal)
                    signals_sent += 1
            except Exception as e:
                logger.error(f"❌ Error with {asset_name}: {e}")
        
        logger.info("=" * 70)
        logger.info(f"✅ Complete - {signals_sent} signal(s) sent")
        logger.info("=" * 70)

def main():
    """Main entry point"""
    logger.info("🤖 Multi-Asset Trading Bot Starting...")
    
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.error("❌ Missing TELEGRAM_TOKEN or CHAT_ID environment variables")
        sys.exit(1)
    
    bot = SignalBot(TELEGRAM_TOKEN, CHAT_ID)
    bot.run()

if __name__ == '__main__':
    main()
