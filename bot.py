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

# Per-source rate limits (seconds between calls)
RATE_LIMITS = {
    'yahoo':        2.0,
    'alphavantage': 12.0,   # Free tier: 5 calls/min
    'coingecko':    6.0,    # Free tier: 10 calls/min
}

# CoinGecko coin IDs for crypto assets
COINGECKO_IDS = {
    'ETH': 'ethereum',
    'ADA': 'cardano',
}


class DataFetcher:
    """
    Fetch OHLC data from multiple sources with per-source rate limiting.

    Fallback chain per asset type:
      Equities/ETFs/Commodities: Yahoo (crumb auth) → Alpha Vantage
      Crypto:                    CoinGecko           → Yahoo → Alpha Vantage
    """

    # Realistic browser headers Yahoo now requires
    _YAHOO_HEADERS = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/124.0.0.0 Safari/537.36'
        ),
        'Accept':          'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'Origin':          'https://finance.yahoo.com',
        'Referer':         'https://finance.yahoo.com/',
    }

    def __init__(self, alpha_key: str = None):
        self.alpha_key      = alpha_key
        self.alpha_base_url = "https://www.alphavantage.co/query"
        self._last_call: Dict[str, float] = {}

        # Shared session so cookies persist across Yahoo calls (needed for crumb)
        self._session = requests.Session()
        self._session.headers.update(self._YAHOO_HEADERS)
        self._yahoo_crumb: Optional[str] = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _rate_limit(self, source: str):
        """Block until the minimum inter-call gap for a source has elapsed."""
        min_gap = RATE_LIMITS.get(source, 1.0)
        last    = self._last_call.get(source, 0.0)
        elapsed = time.monotonic() - last
        if elapsed < min_gap:
            time.sleep(min_gap - elapsed)
        self._last_call[source] = time.monotonic()

    def _parse_yahoo_chart(self, data: dict, symbol: str) -> Optional[List[Dict]]:
        """Parse a Yahoo Finance chart API response into a list of bar dicts."""
        result = data.get('chart', {}).get('result', [None])[0]
        if not result:
            logger.warning(f"Yahoo: empty result for {symbol}")
            return None

        timestamps = result.get('timestamp', [])
        quote      = result.get('indicators', {}).get('quote', [{}])[0]

        opens   = quote.get('open',   [])
        highs   = quote.get('high',   [])
        lows    = quote.get('low',    [])
        closes  = quote.get('close',  [])
        volumes = quote.get('volume', [])

        price_data = []
        for i in range(len(timestamps)):
            if None in (opens[i], highs[i], lows[i], closes[i]):
                continue
            price_data.append({
                'timestamp': datetime.fromtimestamp(timestamps[i]).isoformat(),
                'open':      float(opens[i]),
                'high':      float(highs[i]),
                'low':       float(lows[i]),
                'close':     float(closes[i]),
                'volume':    float(volumes[i]) if volumes[i] is not None else 0.0,
            })

        return price_data

    # ------------------------------------------------------------------
    # Yahoo Finance (crumb-authenticated)
    # ------------------------------------------------------------------
    def _get_yahoo_crumb(self) -> Optional[str]:
        """
        Obtain a crumb token from Yahoo Finance.
        Yahoo now requires: visit the consent/main page first (sets cookies),
        then call /v1/test/getcrumb to receive the crumb string.
        """
        if self._yahoo_crumb:
            return self._yahoo_crumb
        try:
            # Step 1: hit the main page so Yahoo sets its session cookies
            self._rate_limit('yahoo')
            self._session.get('https://finance.yahoo.com', timeout=10)

            # Step 2: fetch the crumb
            self._rate_limit('yahoo')
            resp = self._session.get(
                'https://query1.finance.yahoo.com/v1/test/getcrumb',
                timeout=10
            )
            if resp.status_code == 200 and resp.text:
                self._yahoo_crumb = resp.text.strip()
                logger.info(f"✅ Yahoo crumb obtained: {self._yahoo_crumb[:6]}...")
                return self._yahoo_crumb

            logger.warning(f"Yahoo crumb fetch returned {resp.status_code}")
            return None

        except Exception as e:
            logger.warning(f"Yahoo crumb fetch failed: {e}")
            return None

    def fetch_from_yahoo(self, symbol: str, retries: int = 3) -> Optional[List[Dict]]:
        """Fetch 10-minute bars from Yahoo Finance using crumb authentication."""
        crumb = self._get_yahoo_crumb()
        if not crumb:
            logger.warning("Yahoo: could not obtain crumb — skipping")
            return None

        url    = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {
            'interval':      '10m',
            'range':         '7d',
            'includePrePost':'false',
            'crumb':         crumb,
        }

        for attempt in range(1, retries + 1):
            try:
                self._rate_limit('yahoo')
                response = self._session.get(url, params=params, timeout=10)

                # If crumb expired, clear it and retry once with a fresh one
                if response.status_code in (401, 403) and attempt == 1:
                    logger.warning("Yahoo: crumb rejected — refreshing")
                    self._yahoo_crumb = None
                    crumb = self._get_yahoo_crumb()
                    if crumb:
                        params['crumb'] = crumb
                    continue

                response.raise_for_status()
                data       = response.json()
                price_data = self._parse_yahoo_chart(data, symbol)

                if price_data and len(price_data) >= 200:
                    logger.info(f"✅ Yahoo: {len(price_data)} bars for {symbol}")
                    return price_data[-210:]

                if price_data:
                    logger.warning(f"⚠️ Yahoo: only {len(price_data)} clean bars for {symbol}")
                return None

            except requests.RequestException as e:
                logger.warning(f"Yahoo attempt {attempt}/{retries} failed for {symbol}: {e}")
                if attempt < retries:
                    time.sleep(2 ** attempt)

        logger.error(f"❌ Yahoo: all retries exhausted for {symbol}")
        return None

    # ------------------------------------------------------------------
    # CoinGecko (free, no API key, good for crypto OHLC)
    # ------------------------------------------------------------------
    def fetch_from_coingecko(self, coin_id: str, retries: int = 3) -> Optional[List[Dict]]:
        """
        Fetch ~10-minute OHLC bars from CoinGecko (free tier, no key needed).

        CoinGecko's /coins/{id}/ohlc endpoint returns up to 7 days of data
        at the finest granularity available (~30-min for free tier). We
        interpolate nothing — just use what we get and check bar count.
        """
        url    = f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc"
        params = {'vs_currency': 'usd', 'days': '7'}

        for attempt in range(1, retries + 1):
            try:
                self._rate_limit('coingecko')
                response = requests.get(url, params=params, timeout=10)
                response.raise_for_status()
                raw = response.json()  # [[timestamp_ms, o, h, l, c], ...]

                price_data = [
                    {
                        'timestamp': datetime.fromtimestamp(row[0] / 1000).isoformat(),
                        'open':      float(row[1]),
                        'high':      float(row[2]),
                        'low':       float(row[3]),
                        'close':     float(row[4]),
                        'volume':    0.0,  # OHLC endpoint doesn't include volume
                    }
                    for row in raw
                    if len(row) == 5
                ]

                if len(price_data) >= 200:
                    logger.info(f"✅ CoinGecko: {len(price_data)} bars for {coin_id}")
                    return price_data[-210:]

                logger.warning(f"⚠️ CoinGecko: only {len(price_data)} bars for {coin_id}")
                return None

            except requests.RequestException as e:
                logger.warning(f"CoinGecko attempt {attempt}/{retries} for {coin_id}: {e}")
                if attempt < retries:
                    time.sleep(2 ** attempt)

        logger.error(f"❌ CoinGecko: all retries exhausted for {coin_id}")
        return None

    # ------------------------------------------------------------------
    # Alpha Vantage (fallback)
    # ------------------------------------------------------------------
    def fetch_from_alphavantage(self, symbol: str, retries: int = 3) -> Optional[List[Dict]]:
        """Fetch 10-minute bars from Alpha Vantage."""
        if not self.alpha_key:
            return None

        is_crypto = symbol in COINGECKO_IDS

        params = (
            {
                'function': 'DIGITAL_CURRENCY_INTRADAY',
                'symbol':   symbol,
                'market':   'USD',
                'apikey':   self.alpha_key,
            }
            if is_crypto else
            {
                'function':   'TIME_SERIES_INTRADAY',
                'symbol':     symbol,
                'interval':   '10min',
                'outputsize': 'full',
                'apikey':     self.alpha_key,
            }
        )

        for attempt in range(1, retries + 1):
            try:
                self._rate_limit('alphavantage')
                response = requests.get(self.alpha_base_url, params=params, timeout=10)
                response.raise_for_status()
                data = response.json()

                if 'Error Message' in data or 'Note' in data:
                    logger.warning(
                        f"Alpha Vantage limit for {symbol}: "
                        f"{data.get('Note', data.get('Error Message'))}"
                    )
                    return None

                price_data = []

                if not is_crypto:
                    ts_key = next((k for k in data if 'Time Series' in k), None)
                    if not ts_key:
                        return None
                    for ts, vals in sorted(data[ts_key].items()):
                        price_data.append({
                            'timestamp': ts,
                            'open':      float(vals['1. open']),
                            'high':      float(vals['2. high']),
                            'low':       float(vals['3. low']),
                            'close':     float(vals['4. close']),
                            'volume':    float(vals['5. volume']),
                        })
                else:
                    ts_key = 'Digital Currency Intraday'
                    if ts_key not in data:
                        return None
                    for ts, vals in sorted(data[ts_key].items()):
                        price_data.append({
                            'timestamp': ts,
                            'open':      float(vals['1a. open (USD)']),
                            'high':      float(vals['2a. high (USD)']),
                            'low':       float(vals['3a. low (USD)']),
                            'close':     float(vals['4a. close (USD)']),
                            'volume':    float(vals['5. volume']),
                        })

                if len(price_data) >= 200:
                    logger.info(f"✅ Alpha Vantage: {len(price_data)} bars for {symbol}")
                    return price_data[-210:]

                logger.warning(f"⚠️ Alpha Vantage: only {len(price_data)} bars for {symbol}")
                return None

            except requests.RequestException as e:
                logger.warning(f"Alpha Vantage attempt {attempt}/{retries} for {symbol}: {e}")
                if attempt < retries:
                    time.sleep(2 ** attempt)

        logger.error(f"❌ Alpha Vantage: all retries exhausted for {symbol}")
        return None

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def fetch_intraday(self, asset_name: str, config: Dict) -> Optional[List[Dict]]:
        """
        Fetch from the best available source for this asset type.

        Crypto:  CoinGecko → Yahoo → Alpha Vantage
        Others:  Yahoo     → Alpha Vantage
        """
        asset_type   = config.get('asset_type', '')
        yahoo_symbol = config.get('yahoo_symbol')
        alpha_symbol = config.get('alpha_symbol')

        if asset_type == 'crypto':
            coin_id = COINGECKO_IDS.get(asset_name)
            if coin_id:
                logger.info(f"Trying CoinGecko for {asset_name}...")
                data = self.fetch_from_coingecko(coin_id)
                if data:
                    return data

        if yahoo_symbol:
            logger.info(f"Trying Yahoo Finance for {asset_name}...")
            data = self.fetch_from_yahoo(yahoo_symbol)
            if data:
                return data

        if alpha_symbol:
            logger.info(f"Trying Alpha Vantage for {asset_name}...")
            data = self.fetch_from_alphavantage(alpha_symbol)
            if data:
                return data

        logger.error(f"❌ No data source available for {asset_name}")
        return None


class SimpleDataProcessor:
    """Technical indicator calculations without pandas."""

    @staticmethod
    def calculate_ema(prices: List[float], period: int) -> List[Optional[float]]:
        """
        Calculate EMA using Wilder-style exponential smoothing.

        Returns a list of the same length as `prices`. The first (period-1)
        values are None (insufficient history); subsequent values are valid EMAs.
        """
        if len(prices) < period:
            return [None] * len(prices)

        result: List[Optional[float]] = [None] * (period - 1)
        multiplier = 2 / (period + 1)

        # Seed with simple average of the first `period` bars
        sma = sum(prices[:period]) / period
        result.append(sma)

        for price in prices[period:]:
            ema = (price - result[-1]) * multiplier + result[-1]
            result.append(ema)

        return result

    @staticmethod
    def calculate_true_range(high: float, low: float, prev_close: float) -> float:
        return max(high - low, abs(high - prev_close), abs(low - prev_close))

    @staticmethod
    def calculate_adx(prices: List[Dict], period: int = 14) -> float:
        """
        Calculate ADX using Wilder's smoothing method.

        Requires at least 2*period + 1 bars for a meaningful result.
        Returns 0.0 on insufficient data.
        """
        if len(prices) < period * 2 + 1:
            return 0.0

        tr_list, plus_dm_list, minus_dm_list = [], [], []

        for i in range(1, len(prices)):
            high      = prices[i]['high']
            low       = prices[i]['low']
            prev_high = prices[i - 1]['high']
            prev_low  = prices[i - 1]['low']
            prev_close= prices[i - 1]['close']

            tr       = SimpleDataProcessor.calculate_true_range(high, low, prev_close)
            up_move  = high - prev_high
            down_move= prev_low - low

            plus_dm  = up_move   if (up_move  > down_move and up_move  > 0) else 0.0
            minus_dm = down_move if (down_move > up_move  and down_move > 0) else 0.0

            tr_list.append(tr)
            plus_dm_list.append(plus_dm)
            minus_dm_list.append(minus_dm)

        # Wilder smoothing: seed with simple sum of first `period` values
        atr       = sum(tr_list[:period])
        avg_plus  = sum(plus_dm_list[:period])
        avg_minus = sum(minus_dm_list[:period])

        dx_values = []

        for i in range(period, len(tr_list)):
            # Wilder's smoothing: prev_smoothed - prev_smoothed/period + current
            atr       = atr       - atr / period       + tr_list[i]
            avg_plus  = avg_plus  - avg_plus / period  + plus_dm_list[i]
            avg_minus = avg_minus - avg_minus / period + minus_dm_list[i]

            if atr == 0:
                dx_values.append(0.0)
                continue

            plus_di  = 100 * avg_plus  / atr
            minus_di = 100 * avg_minus / atr
            denom    = plus_di + minus_di

            dx = 100 * abs(plus_di - minus_di) / denom if denom else 0.0
            dx_values.append(dx)

        if not dx_values:
            return 0.0

        # ADX = Wilder-smoothed DX over the final `period` dx values
        adx = sum(dx_values[-period:]) / period
        return adx


class SignalBot:
    """Main bot class."""

    def __init__(self, api_key: str, telegram_token: str, chat_id: str):
        self.fetcher  = DataFetcher(api_key)
        self.processor= SimpleDataProcessor()
        self.bot      = Bot(token=telegram_token)
        self.chat_id  = chat_id
        self.last_signals: Dict[str, datetime] = self._load_signal_state()

    # ------------------------------------------------------------------
    # Signal-state persistence (survives restarts)
    # ------------------------------------------------------------------
    STATE_FILE = 'signal_state.json'

    def _load_signal_state(self) -> Dict[str, datetime]:
        """Load last-signal timestamps from disk."""
        if not os.path.exists(self.STATE_FILE):
            return {}
        try:
            with open(self.STATE_FILE) as f:
                raw = json.load(f)
            return {k: datetime.fromisoformat(v) for k, v in raw.items()}
        except Exception as e:
            logger.warning(f"Could not load signal state: {e}")
            return {}

    def _save_signal_state(self):
        """Persist last-signal timestamps to disk."""
        try:
            with open(self.STATE_FILE, 'w') as f:
                json.dump(
                    {k: v.isoformat() for k, v in self.last_signals.items()},
                    f
                )
        except Exception as e:
            logger.warning(f"Could not save signal state: {e}")

    # ------------------------------------------------------------------
    # Crossover detection
    # ------------------------------------------------------------------
    def check_crossover(self, price_data: List[Dict]) -> Tuple[bool, Optional[str]]:
        """
        Detect EMA50 crossing EMA200 (golden/death cross).

        BULLISH (golden cross): EMA50 crosses *above* EMA200.
        BEARISH (death cross):  EMA50 crosses *below* EMA200.
        """
        if len(price_data) < 210:
            return False, None

        closes = [p['close'] for p in price_data]

        ema50  = self.processor.calculate_ema(closes, 50)
        ema200 = self.processor.calculate_ema(closes, 200)

        # Only compare positions where both EMAs are valid
        valid_pairs = [
            (e50, e200)
            for e50, e200 in zip(ema50, ema200)
            if e50 is not None and e200 is not None
        ]

        if len(valid_pairs) < 2:
            return False, None

        prev_ema50,    prev_ema200    = valid_pairs[-2]
        current_ema50, current_ema200 = valid_pairs[-1]

        # Golden cross: EMA50 was below EMA200, now above
        if prev_ema50 <= prev_ema200 and current_ema50 > current_ema200:
            logger.info(
                f"📈 BULLISH (golden cross) — EMA50: {current_ema50:.4f} "
                f"> EMA200: {current_ema200:.4f}"
            )
            return True, "BULLISH 🟢"

        # Death cross: EMA50 was above EMA200, now below
        if prev_ema50 >= prev_ema200 and current_ema50 < current_ema200:
            logger.info(
                f"📉 BEARISH (death cross) — EMA50: {current_ema50:.4f} "
                f"< EMA200: {current_ema200:.4f}"
            )
            return True, "BEARISH 🔴"

        return False, None

    # ------------------------------------------------------------------
    # Risk / reward
    # ------------------------------------------------------------------
    def calculate_risk_reward(
        self,
        current_price: float,
        signal_type: str,
        risk_percent: float,
        profit_percent: float
    ) -> Dict:
        """Calculate entry, stop-loss, take-profit, and R:R ratio."""
        entry = current_price

        if "BULLISH" in signal_type:
            stop_loss   = entry * (1 - risk_percent   / 100)
            take_profit = entry * (1 + profit_percent / 100)
        else:
            stop_loss   = entry * (1 + risk_percent   / 100)
            take_profit = entry * (1 - profit_percent / 100)

        risk_amount   = abs(entry - stop_loss)
        reward_amount = abs(take_profit - entry)
        actual_ratio  = round(reward_amount / risk_amount, 2) if risk_amount > 0 else 0

        return {
            'entry':          round(entry, 4),
            'stop_loss':      round(stop_loss, 4),
            'take_profit':    round(take_profit, 4),
            'risk_percent':   risk_percent,
            'profit_percent': profit_percent,
            'actual_ratio':   actual_ratio
        }

    def calculate_position_size(
        self,
        capital: float,
        risk_percent: float,
        entry: float,
        stop_loss: float
    ) -> Dict:
        """
        Risk-based position sizing.

        shares = (capital * risk_fraction) / risk_per_share
        This ensures the *monetary* risk never exceeds `risk_percent` of capital.
        """
        risk_per_share = abs(entry - stop_loss)
        if risk_per_share == 0:
            return {'shares': 0, 'position_value': 0.0, 'total_risk': 0.0}

        max_risk_amount = capital * (risk_percent / 100)
        shares          = int(max_risk_amount / risk_per_share)
        position_value  = round(shares * entry, 2)
        total_risk      = round(shares * risk_per_share, 2)

        return {
            'shares':         shares,
            'position_value': position_value,
            'total_risk':     total_risk
        }

    # ------------------------------------------------------------------
    # Asset analysis
    # ------------------------------------------------------------------
    def analyze_asset(self, asset_name: str, config: Dict) -> Optional[Dict]:
        """Analyse a single asset and return a signal dict, or None."""
        logger.info(f"🔍 Analysing {asset_name}...")

        price_data = self.fetcher.fetch_intraday(asset_name, config)
        if not price_data or len(price_data) < 210:
            logger.warning(
                f"⚠️ Insufficient data for {asset_name} "
                f"({len(price_data) if price_data else 0} bars)"
            )
            return None

        logger.info(f"✅ {len(price_data)} bars for {asset_name}")

        has_crossover, signal_type = self.check_crossover(price_data)
        if not has_crossover:
            return None

        current_price = price_data[-1]['close']
        closes        = [p['close'] for p in price_data]

        ema50_list  = self.processor.calculate_ema(closes, 50)
        ema200_list = self.processor.calculate_ema(closes, 200)

        # Use last valid values
        ema50_val  = next((v for v in reversed(ema50_list)  if v is not None), 0.0)
        ema200_val = next((v for v in reversed(ema200_list) if v is not None), 0.0)

        adx = self.processor.calculate_adx(price_data, 14)

        if adx > 40:
            adx_strength = "VERY STRONG 🔥"
        elif adx > 25:
            adx_strength = "STRONG ✅"
        elif adx > 20:
            adx_strength = "MODERATE 📊"
        else:
            adx_strength = "WEAK ⚠️"

        rr = self.calculate_risk_reward(
            current_price, signal_type,
            config['risk_percent'], config['profit_percent']
        )

        sizing = self.calculate_position_size(
            capital      = config['position_size'],
            risk_percent = config['risk_percent'],
            entry        = rr['entry'],
            stop_loss    = rr['stop_loss']
        )

        return {
            'asset':        asset_name,
            'signal_type':  signal_type,
            'current_price':round(current_price, 4),
            'ema50':        round(ema50_val, 4),
            'ema200':       round(ema200_val, 4),
            'adx':          round(adx, 2),
            'adx_strength': adx_strength,
            'risk_reward':  rr,
            'position_size':config['position_size'],
            'currency':     config['currency'],
            'shares':       sizing['shares'],
            'position_value':sizing['position_value'],
            'total_risk':   sizing['total_risk'],
            'mt5_units':    config.get('mt5_units'),
            'timestamp':    datetime.now(pytz.UTC)
        }

    # ------------------------------------------------------------------
    # Telegram messaging
    # ------------------------------------------------------------------
    def _build_mt5_block(self, signal: Dict) -> str:
        rr         = signal['risk_reward']
        asset      = signal['asset']
        mt5_units  = signal['mt5_units']

        if not mt5_units or asset not in ('GOLD', 'SPY'):
            return ""

        if asset == 'GOLD':
            risk_pips   = abs(rr['entry'] - rr['stop_loss']) / 0.01
            approx_risk = risk_pips * mt5_units * 10
            return (
                f"\n💹 <b>MT5:</b>\n"
                f"• Units: {mt5_units}\n"
                f"• Risk: {risk_pips:.1f} pips\n"
                f"• Approx Risk: ${approx_risk:.2f}\n"
            )
        else:  # SPY
            risk_points = abs(rr['entry'] - rr['stop_loss'])
            return (
                f"\n💹 <b>MT5:</b>\n"
                f"• Units: {mt5_units}\n"
                f"• Risk: {risk_points:.2f} points\n"
            )

    def format_telegram_message(self, signal: Dict) -> str:
        rr       = signal['risk_reward']
        mt5_text = self._build_mt5_block(signal)

        return (
            f"🚨 <b>{signal['asset']} TRADING SIGNAL — 10-MIN TIMEFRAME</b> 🚨\n\n"
            f"📊 <b>Signal:</b> {signal['signal_type']} (EMA50 crosses EMA200)\n"
            f"💰 <b>Current Price:</b> ${signal['current_price']}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 <b>TECHNICAL INDICATORS</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"• EMA50:  ${signal['ema50']}\n"
            f"• EMA200: ${signal['ema200']}\n"
            f"• ADX: {signal['adx']} ({signal['adx_strength']})\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ <b>RISK MANAGEMENT</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"• Stop Loss: {rr['risk_percent']}% from entry\n"
            f"• Take Profit: {rr['profit_percent']}% from entry\n"
            f"• Risk:Reward: 1:{rr['actual_ratio']}\n\n"
            f"📍 <b>Levels:</b>\n"
            f"• Entry:       ${rr['entry']}\n"
            f"• Stop Loss:   ${rr['stop_loss']}\n"
            f"• Take Profit: ${rr['take_profit']}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"💼 <b>POSITION SIZING ({signal['currency']}) — Risk-Based</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📱 <b>Trading 212:</b>\n"
            f"• Capital: {signal['position_size']:,}\n"
            f"• Shares: {signal['shares']:,} units\n"
            f"• Position Value: {signal['position_value']:,}\n"
            f"• Total Risk: {signal['total_risk']:,}\n"
            f"{mt5_text}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ <i>{signal['timestamp'].strftime('%Y-%m-%d %H:%M:%S UTC')}</i>\n\n"
            f"⚠️ <b>DISCLAIMER:</b> For educational purposes only. "
            f"Always do your own research before trading.\n\n"
            f"<i>🤖 Multi-Asset Trading Bot | 10-Minute EMA Crossover Strategy</i>"
        )

    def send_signal(self, signal: Dict):
        """Send signal to Telegram, skipping duplicates within 12 hours."""
        signal_key = f"{signal['asset']}_{signal['signal_type']}"
        last_sent  = self.last_signals.get(signal_key)

        if last_sent is not None:
            # Make last_sent timezone-aware if it isn't already
            if last_sent.tzinfo is None:
                last_sent = last_sent.replace(tzinfo=pytz.UTC)
            hours_ago = (datetime.now(pytz.UTC) - last_sent).total_seconds() / 3600
            if hours_ago < 12:
                logger.info(
                    f"⏭️ Skipping duplicate for {signal['asset']} "
                    f"(sent {hours_ago:.1f}h ago)"
                )
                return

        try:
            message = self.format_telegram_message(signal)
            self.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode='HTML',
                disable_web_page_preview=True
            )
            self.last_signals[signal_key] = datetime.now(pytz.UTC)
            self._save_signal_state()
            logger.info(f"✅ Signal sent for {signal['asset']}")

        except Exception as e:
            logger.error(f"❌ Failed to send signal for {signal['asset']}: {e}")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def run_analysis(self):
        """Run a full analysis pass across all configured assets."""
        logger.info("=" * 70)
        logger.info("🚀 Starting Multi-Asset Signal Analysis")
        logger.info(f"📅 {datetime.now(pytz.UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        logger.info(f"📊 Assets: {', '.join(ASSETS.keys())}")
        logger.info("=" * 70)

        signals_sent = 0

        for asset_name, config in ASSETS.items():
            try:
                signal = self.analyze_asset(asset_name, config)
                if signal:
                    self.send_signal(signal)
                    signals_sent += 1
            except Exception as e:
                logger.error(f"❌ Unhandled error with {asset_name}: {e}")

        logger.info("=" * 70)
        logger.info(f"✅ Analysis complete — {signals_sent} signal(s) sent")
        logger.info("=" * 70)


def main():
    logger.info("🤖 Multi-Asset Trading Bot Initialising...")

    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.error("❌ Missing TELEGRAM_TOKEN or CHAT_ID environment variables")
        sys.exit(1)

    bot = SignalBot(ALPHA_VANTAGE_API_KEY, TELEGRAM_TOKEN, CHAT_ID)
    bot.run_analysis()


if __name__ == '__main__':
    main()
