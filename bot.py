#!/usr/bin/env python3
"""
EMA Crossover + Pullback Signal Bot — Telegram / Render Cron Job
================================================================
Signal logic: v4 (backtester-validated, Feb–May 2026)
  • EMA50×EMA200 crossover (fixed direction, candle confirm, volume spike)
  • Pullback to EMA50 (4H+1H trend confirm, RSI 40–60, Fib retrace 28–58%)
  • ADX ≥ 20 | Session filter (UTC) | 1‑hour cooldown per signal type

Deployment: push to GitHub → connect to Render as Cron Job
  Schedule: */15 * * * *  (every 15 minutes)
  Command:  python bot.py

Required environment variables (set in Render dashboard):
  TELEGRAM_TOKEN          — bot token from @BotFather
  TELEGRAM_CHAT_ID        — channel/chat to receive trade signals
  TELEGRAM_ADMIN_CHAT_ID  — (optional) separate chat for health/error alerts
  TWELVE_DATA_API_KEY     — main key (used for GOLD)
  TWELVE_DATA_API_KEY_2   — (optional) second key for forex pairs

State file: /tmp/signal_tracker.json
  Persists within a Render instance session. Resets on redeploy/restart
  (acceptable — cooldown is 1 hour so at worst one duplicate alert).

Fixes applied (v4.3):
  - FIX 1: RSI off-by-one corrected — last element now always computed
  - FIX 2: htf_trend uses -1 index on fully-closed resampled bars (not -2)
  - FIX 3: Signal timestamp now uses times[lcc] (last closed candle),
           not times[i] (forming candle) — removes phantom future-bar timestamps
  - FIX 4: Removed conflicting bar-gap de-dup inside detect_signals;
           external 1-hour cooldown (signal_on_cooldown) is the sole guard
  - FIX 5: Volume filter disabled per-pair for instruments with no real volume
           (GOLD); htf_trend returns NEUTRAL instead of clamping EMA period
"""

import os
import json
import time
import logging
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from typing import Dict, Tuple, List, Optional

# ── LOGGING ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ── ENVIRONMENT ───────────────────────────────────────────────────────────────
TELEGRAM_TOKEN         = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID       = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", TELEGRAM_CHAT_ID)
TWELVE_DATA_KEY_MAIN   = os.environ.get("TWELVE_DATA_API_KEY", "")
TWELVE_DATA_KEY_FOREX  = os.environ.get("TWELVE_DATA_API_KEY_2", TWELVE_DATA_KEY_MAIN)

# ── CAPITAL (for position sizing display only — no live trading) ──────────────
START_CAPITAL  = 2000.0   # €2,000
RR             = 2.0      # Risk:Reward ratio

# ── PAIRS — (symbol, api_key, risk_pct, volume_filter_enabled) ───────────────
# FIX 5: Added per-pair volume_filter flag.
# GOLD (XAU/USD) returns zero/meaningless volume from Twelve Data spot feed,
# so the filter is disabled for it. Enable only for instruments with real volume.
PAIRS = {
    "GOLD":   ("XAU/USD", TWELVE_DATA_KEY_MAIN,  1.50, False),
    "EURUSD": ("EUR/USD", TWELVE_DATA_KEY_FOREX, 1.00, True),
    "GBPUSD": ("GBP/USD", TWELVE_DATA_KEY_FOREX, 1.00, True),
}

# ── SIGNAL PARAMETERS (v4 — exact match to backtester) ───────────────────────
EMA_FAST             = 50
EMA_SLOW             = 200
ADX_MIN              = 20
SIGNAL_COOLDOWN_HRS  = 1           # hours between same signal type on same pair
CANDLE_CONFIRM       = True
# Global volume filter switch — overridden per pair via PAIRS tuple (FIX 5)
VOLUME_FILTER        = True

PULLBACK_PROXIMITY_PCT = 0.003   # price within 0.3% of EMA50
PULLBACK_RSI_LOW       = 40
PULLBACK_RSI_HIGH      = 60
PULLBACK_RETRACE_MIN   = 0.28
PULLBACK_RETRACE_MAX   = 0.58
PULLBACK_SWING_BARS    = 96

ALLOWED_HOURS = {6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 21, 22}

# Signal is only sent if it fired on the last closed candle, or at most
# SIGNAL_FRESHNESS_BARS bars ago (guards against late cron execution)
SIGNAL_FRESHNESS_BARS = 2

# ── STATE FILE ────────────────────────────────────────────────────────────────
TRACKER_FILE = "/tmp/signal_tracker.json"
HEALTH_FILE  = "/tmp/health_tracker.json"

# HTTP headers for API requests
API_HEADERS = {"User-Agent": "EMASignalBot/4.3 (https://github.com/your-repo)"}


# ─────────────────────────────────────────────────────────────────────────────
# TRACKER HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def load_json(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def save_json(path: str, data: dict) -> None:
    try:
        with open(path, "w") as f:
            json.dump(data, f)
    except Exception as e:
        log.warning(f"Could not save {path}: {e}")


def signal_on_cooldown(pair: str, signal_type: str) -> bool:
    """Return True if this pair+signal was sent within the last 1 hour."""
    tracker = load_json(TRACKER_FILE)
    key = f"{pair}_{signal_type}"
    last_ts = tracker.get(key)
    if not last_ts:
        return False
    elapsed_hrs = (datetime.now(timezone.utc).timestamp() - last_ts) / 3600
    if elapsed_hrs < SIGNAL_COOLDOWN_HRS:
        log.info(f"  ⏭  {key} on cooldown ({elapsed_hrs:.1f}h / {SIGNAL_COOLDOWN_HRS}h)")
        return True
    return False


def mark_signal_sent(pair: str, signal_type: str) -> None:
    tracker = load_json(TRACKER_FILE)
    tracker[f"{pair}_{signal_type}"] = datetime.now(timezone.utc).timestamp()
    save_json(TRACKER_FILE, tracker)


# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────────────────────────────────────

def send_telegram(message: str, admin: bool = False) -> bool:
    chat_id = TELEGRAM_ADMIN_CHAT_ID if admin else TELEGRAM_CHAT_ID
    if not TELEGRAM_TOKEN or not chat_id:
        log.warning("Telegram not configured — skipping send.")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception as e:
        log.error(f"Telegram error: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# DATA FETCHING
# ─────────────────────────────────────────────────────────────────────────────

def fetch_15min(symbol: str, api_key: str, outputsize: int = 500) -> pd.DataFrame:
    """
    Fetch the most recent 15-min bars for live signal detection.
    500 bars ≈ 5.2 days — enough for EMA200 warmup (200 bars) plus
    PULLBACK_SWING_BARS (96) with headroom. Falls back to 300 if 500 fails.
    """
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol":     symbol,
        "interval":   "15min",
        "outputsize": outputsize,
        "apikey":     api_key,
        "order":      "ASC",
    }
    try:
        resp = requests.get(url, params=params, headers=API_HEADERS, timeout=20)
        resp.raise_for_status()
        d = resp.json()
    except Exception as e:
        log.error(f"  15min fetch error ({symbol}): {e}")
        return pd.DataFrame()

    if "code" in d:
        log.error(f"  API error ({symbol}): {d.get('message')}")
        if outputsize > 300:
            log.info(f"  Retrying with outputsize=300")
            return fetch_15min(symbol, api_key, outputsize=300)
        return pd.DataFrame()

    values = d.get("values", [])
    if not values:
        return pd.DataFrame()

    rows = []
    for b in values:
        try:
            rows.append({
                "datetime": pd.Timestamp(b["datetime"]),
                "Open":  float(b["open"]),
                "High":  float(b["high"]),
                "Low":   float(b["low"]),
                "Close": float(b["close"]),
                "Volume": float(b.get("volume", 0)),
            })
        except (ValueError, KeyError):
            continue

    df = (pd.DataFrame(rows)
            .drop_duplicates("datetime")
            .sort_values("datetime")
            .set_index("datetime"))
    log.info(f"  ✅ {len(df)} bars  ({df.index[0].date()} → {df.index[-1].date()})")
    return df


def fetch_1h(symbol: str, api_key: str, outputsize: int = 300) -> pd.DataFrame:
    """Fetch 1H data for 4H+1H trend confirmation."""
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol":     symbol,
        "interval":   "1h",
        "outputsize": outputsize,
        "apikey":     api_key,
        "order":      "ASC",
    }
    try:
        resp = requests.get(url, params=params, headers=API_HEADERS, timeout=20)
        resp.raise_for_status()
        d = resp.json()
        values = d.get("values", [])
        if not values:
            return pd.DataFrame()
        rows = [{"datetime": pd.Timestamp(b["datetime"]),
                 "Close":    float(b["close"])} for b in values]
        df = (pd.DataFrame(rows)
                .drop_duplicates("datetime")
                .sort_values("datetime")
                .set_index("datetime"))
        return df
    except Exception as e:
        log.error(f"  1H fetch error ({symbol}): {e}")
        return pd.DataFrame()


# ─────────────────────────────────────────────────────────────────────────────
# INDICATORS
# ─────────────────────────────────────────────────────────────────────────────

def ema_series(closes: np.ndarray, period: int) -> np.ndarray:
    if len(closes) < period:
        return np.full(len(closes), np.nan)
    mult = 2.0 / (period + 1)
    out = np.empty(len(closes))
    out[0] = closes[0]
    for i in range(1, len(closes)):
        out[i] = (closes[i] - out[i - 1]) * mult + out[i - 1]
    return out


def adx_series(highs: np.ndarray, lows: np.ndarray,
               closes: np.ndarray, period: int = 14) -> np.ndarray:
    """Wilder-smoothed ADX."""
    n = len(closes)
    adx_out = np.zeros(n)
    if n < period * 2 + 1:
        return adx_out

    tr = np.zeros(n); pdm = np.zeros(n); ndm = np.zeros(n)
    for i in range(1, n):
        tr[i]  = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        up   = highs[i] - highs[i-1]
        down = lows[i-1] - lows[i]
        pdm[i] = up   if up > down and up > 0   else 0.0
        ndm[i] = down if down > up and down > 0 else 0.0

    atr_w = np.zeros(n); pdm_w = np.zeros(n); ndm_w = np.zeros(n)
    atr_w[period] = tr[1:period+1].sum()
    pdm_w[period] = pdm[1:period+1].sum()
    ndm_w[period] = ndm[1:period+1].sum()
    for i in range(period+1, n):
        atr_w[i] = atr_w[i-1] - atr_w[i-1]/period + tr[i]
        pdm_w[i] = pdm_w[i-1] - pdm_w[i-1]/period + pdm[i]
        ndm_w[i] = ndm_w[i-1] - ndm_w[i-1]/period + ndm[i]

    dx = np.zeros(n)
    for i in range(period, n):
        if atr_w[i] == 0: continue
        pdi = 100 * pdm_w[i] / atr_w[i]
        mdi = 100 * ndm_w[i] / atr_w[i]
        s = pdi + mdi
        dx[i] = 100 * abs(pdi - mdi) / s if s > 0 else 0

    adx_out[period*2] = dx[period:period*2].mean()
    for i in range(period*2+1, n):
        adx_out[i] = (adx_out[i-1] * (period-1) + dx[i]) / period
    return adx_out


def rsi_series(closes: np.ndarray, period: int = 14) -> np.ndarray:
    """
    Wilder RSI, oldest-first.

    FIX 1: Original loop ran to range(period, n-1), meaning the final
    element was never updated and stayed at the default 50.0.
    Loop now runs to range(period, n) and indexes directly with i,
    so every bar including the last closed candle gets a real RSI value.
    """
    n = len(closes)
    rsi_out = np.full(n, 50.0)
    if n < period + 1:
        return rsi_out

    deltas = np.diff(closes)                          # length n-1
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()

    # Seed the first computed RSI at index `period`
    rs = avg_gain / avg_loss if avg_loss > 0 else 100.0
    rsi_out[period] = 100 - (100 / (1 + rs))

    # FIX 1: iterate i = period..n-1 (deltas has n-1 elements, index i-1 is safe)
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        rs = avg_gain / avg_loss if avg_loss > 0 else 100.0
        rsi_out[i] = 100 - (100 / (1 + rs))

    return rsi_out


def volume_ma(volumes: np.ndarray, period: int = 20) -> np.ndarray:
    out = np.zeros(len(volumes))
    for i in range(period, len(volumes)):
        out[i] = volumes[i-period:i].mean()
    return out


def htf_trend(df_1h: pd.DataFrame) -> Tuple[str, str]:
    """
    Derive 4H and 1H trend from 1H data using resampled 4H closes.
    Returns (trend_4h, trend_1h): "BULLISH" | "BEARISH" | "NEUTRAL"

    FIX 2: Use index -1 (not -2) on resampled bars — these are already
    fully-closed historical bars, so the last element is safe to use.
    Using -2 discarded one full candle of information unnecessarily.

    FIX 5 (partial): Return NEUTRAL instead of clamping the EMA period.
    If we don't have 200 4H bars, an EMA computed with fewer periods is
    not a meaningful EMA-200 proxy and will produce incorrect trend reads.
    """
    if df_1h.empty or len(df_1h) < 200:
        return "NEUTRAL", "NEUTRAL"

    closes_1h = df_1h["Close"].values
    ema200_1h = ema_series(closes_1h, 200)

    # FIX 2: use -1 — the last 1H bar in the fetched history is closed
    last_1h = -1
    trend_1h = ("BULLISH" if closes_1h[last_1h] > ema200_1h[last_1h]
                else "BEARISH") if not np.isnan(ema200_1h[last_1h]) else "NEUTRAL"

    # Resample to 4-hour bars (last close of each 4H window)
    df_4h = df_1h.resample("4h").last().dropna()

    # FIX 5: Require a full 200 bars for EMA-200; return NEUTRAL if insufficient.
    # Previously the code clamped to min(200, len-1), silently computing e.g.
    # an EMA-50 and treating it as EMA-200 — producing wrong trend reads.
    if len(df_4h) < 201:
        log.warning(f"  htf_trend: only {len(df_4h)} 4H bars — need 201 for EMA-200; returning NEUTRAL")
        return "NEUTRAL", trend_1h

    closes_4h = df_4h["Close"].values
    ema200_4h = ema_series(closes_4h, 200)

    # FIX 2: use -1 — resampled 4H bars are all closed historical bars
    last_4h = -1
    trend_4h = ("BULLISH" if closes_4h[last_4h] > ema200_4h[last_4h]
                else "BEARISH") if not np.isnan(ema200_4h[last_4h]) else "NEUTRAL"

    return trend_4h, trend_1h


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL DETECTION  (v4.3)
# ─────────────────────────────────────────────────────────────────────────────

def detect_signals(df: pd.DataFrame,
                   trend_4h: str,
                   trend_1h: str,
                   volume_filter: bool = True) -> pd.DataFrame:
    """
    Full v4.3 signal detection over the supplied DataFrame.
    Returns all detected signals as a DataFrame. The caller then filters
    to only "fresh" signals (last SIGNAL_FRESHNESS_BARS bars).

    FIX 3: Signal timestamp now records times[lcc] — the last *closed*
    candle — rather than times[i] (the forming candle). This prevents
    signals appearing to belong to a candle that hasn't closed yet.

    FIX 4: Removed the internal bar-gap de-duplication (>= 12 bars check).
    That check (12 × 15min = 3 hours) conflicted with the external 1-hour
    cooldown enforced by signal_on_cooldown(), silently suppressing valid
    signals. The external cooldown is now the sole guard against duplicates.

    FIX 5: volume_filter parameter lets the caller disable the volume check
    for instruments that don't provide real volume data (e.g. spot gold).
    """
    closes  = df["Close"].values
    highs   = df["High"].values
    lows    = df["Low"].values
    volumes = df["Volume"].values
    times   = df.index

    ema50_arr  = ema_series(closes, EMA_FAST)
    ema200_arr = ema_series(closes, EMA_SLOW)
    adx_arr    = adx_series(highs, lows, closes, 14)
    rsi_arr    = rsi_series(closes, 14)
    vol_ma_arr = volume_ma(volumes, 20) if volume_filter else np.zeros(len(closes))

    signals = []
    min_bar = EMA_SLOW + PULLBACK_SWING_BARS + 5

    for i in range(min_bar, len(df)):
        lcc = i - 1   # last closed candle — no look-ahead

        if times[lcc].hour not in ALLOWED_HOURS:
            continue

        cur50  = ema50_arr[lcc];  prv50  = ema50_arr[lcc-1]
        cur200 = ema200_arr[lcc]; prv200 = ema200_arr[lcc-1]
        adx_val = adx_arr[lcc]
        rsi_val = rsi_arr[lcc]
        cur_px  = closes[lcc]

        if np.isnan(cur200) or np.isnan(cur50):
            continue
        if adx_val < ADX_MIN:
            continue

        # ── EMA Crossover ─────────────────────────────────────────────────────
        bullish_cross = (prv50 < prv200) and (cur50 > cur200)
        bearish_cross = (prv50 > prv200) and (cur50 < cur200)

        if bullish_cross or bearish_cross:
            signal_type = "BULLISH_CROSSOVER" if bullish_cross else "BEARISH_CROSSOVER"
            is_long = bullish_cross

            valid = True

            if CANDLE_CONFIRM:
                if is_long  and not (cur_px > cur50 and cur_px > cur200):
                    valid = False
                if not is_long and not (cur_px < cur50 and cur_px < cur200):
                    valid = False

            # FIX 5: only apply volume check when volume_filter is enabled
            if valid and volume_filter and vol_ma_arr[lcc] > 0:
                if volumes[lcc] < 1.2 * vol_ma_arr[lcc]:
                    valid = False

            if valid:
                entry = cur_px
                sl    = entry * (1 - 0.005) if is_long else entry * (1 + 0.005)
                tp    = entry * (1 + 0.010) if is_long else entry * (1 - 0.010)

                # FIX 3: timestamp from lcc (closed candle), not i (forming candle)
                signals.append({
                    "bar": lcc, "timestamp": str(times[lcc]),
                    "signal_type": signal_type, "direction": 1 if is_long else -1,
                    "entry": round(entry, 5), "sl": round(sl, 5), "tp": round(tp, 5),
                    "adx": round(adx_val, 1), "rsi": round(rsi_val, 1),
                    "ema50": round(cur50, 5), "ema200": round(cur200, 5),
                })

            # A crossover bar does not also trigger a pullback — move to next bar
            continue

        # ── Pullback to EMA50 ─────────────────────────────────────────────────
        if trend_4h == "NEUTRAL" or trend_1h == "NEUTRAL":
            continue

        is_long_pb  = (trend_4h == "BULLISH" and trend_1h == "BULLISH")
        is_short_pb = (trend_4h == "BEARISH" and trend_1h == "BEARISH")

        if not (is_long_pb or is_short_pb):
            continue

        if abs(cur_px - cur50) / cur50 > PULLBACK_PROXIMITY_PCT:
            continue

        if not (PULLBACK_RSI_LOW <= rsi_val <= PULLBACK_RSI_HIGH):
            continue

        swing_start = max(0, lcc - PULLBACK_SWING_BARS)
        swing_high  = highs[swing_start:lcc+1].max()
        swing_low   = lows[swing_start:lcc+1].min()
        swing_range = swing_high - swing_low

        if swing_range <= 0:
            continue

        retrace = (swing_high - cur_px) / swing_range if is_long_pb \
                  else (cur_px - swing_low) / swing_range

        if not (PULLBACK_RETRACE_MIN <= retrace <= PULLBACK_RETRACE_MAX):
            continue

        pb_type = "PULLBACK_LONG" if is_long_pb else "PULLBACK_SHORT"

        entry = cur_px
        sl    = entry * (1 - 0.005) if is_long_pb else entry * (1 + 0.005)
        tp    = entry * (1 + 0.010) if is_long_pb else entry * (1 - 0.010)

        # FIX 3: timestamp from lcc (closed candle), not i (forming candle)
        signals.append({
            "bar": lcc, "timestamp": str(times[lcc]),
            "signal_type": pb_type, "direction": 1 if is_long_pb else -1,
            "entry": round(entry, 5), "sl": round(sl, 5), "tp": round(tp, 5),
            "adx": round(adx_val, 1), "rsi": round(rsi_val, 1),
            "ema50": round(cur50, 5), "ema200": round(cur200, 5),
        })

    return pd.DataFrame(signals)


# ─────────────────────────────────────────────────────────────────────────────
# MESSAGE FORMATTING
# ─────────────────────────────────────────────────────────────────────────────

def fmt_price(price: float) -> str:
    return f"{price:.5f}" if price < 10 else f"{price:.2f}"


def format_signal_message(pair: str, sig: dict,
                           trend_4h: str, trend_1h: str,
                           risk_pct: float) -> str:
    stype     = sig["signal_type"]
    entry     = sig["entry"]
    sl        = sig["sl"]
    tp        = sig["tp"]
    adx       = sig["adx"]
    rsi       = sig["rsi"]
    ema50     = sig["ema50"]
    ema200    = sig["ema200"]

    risk_amt   = START_CAPITAL * risk_pct / 100
    reward_amt = risk_amt * RR

    if stype == "BULLISH_CROSSOVER":
        arrow = "📈"; dir_label = "🟢 BULLISH CROSSOVER (LONG)"
        desc = "EMA50 crossed above EMA200"
    elif stype == "BEARISH_CROSSOVER":
        arrow = "📉"; dir_label = "🔴 BEARISH CROSSOVER (SHORT)"
        desc = "EMA50 crossed below EMA200"
    elif stype == "PULLBACK_LONG":
        arrow = "📈"; dir_label = "🟢 PULLBACK LONG"
        desc = f"Pullback to EMA50 in uptrend (4H: {trend_4h} | 1H: {trend_1h})"
    else:
        arrow = "📉"; dir_label = "🔴 PULLBACK SHORT"
        desc = f"Pullback to EMA50 in downtrend (4H: {trend_4h} | 1H: {trend_1h})"

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return f"""{arrow} <b>{pair} — {dir_label}</b> {arrow}

━━━━━━━━━━━━━━━━━━━━━
⏰ <b>Timeframe:</b> 15-Min (last closed candle)
📊 <b>Signal:</b> {desc}
━━━━━━━━━━━━━━━━━━━━━

📊 <b>Indicators</b>
• EMA50:  {fmt_price(ema50)}
• EMA200: {fmt_price(ema200)}
• ADX:    {adx}
• RSI:    {rsi}
• HTF:    4H {trend_4h} | 1H {trend_1h}

━━━━━━━━━━━━━━━━━━━━━
⚡ <b>Levels</b>
• Entry:      {fmt_price(entry)}
• Stop Loss:  {fmt_price(sl)}  (-0.5%)
• Take Profit:{fmt_price(tp)}  (+1.0%)
• R:R         1:{RR:.0f}

━━━━━━━━━━━━━━━━━━━━━
💼 <b>Risk (€{START_CAPITAL:,.0f} capital @ {risk_pct}%)</b>
• Risk:   €{risk_amt:.2f}
• Reward: €{reward_amt:.2f}

⏰ {now_utc}
⚠️ <i>Educational purposes only. Not financial advice.</i>"""


# ─────────────────────────────────────────────────────────────────────────────
# HEALTH / DAILY REPORT
# ─────────────────────────────────────────────────────────────────────────────

def update_health(status: str, detail: str = "") -> None:
    h = load_json(HEALTH_FILE)
    now = datetime.now(timezone.utc)
    h["last_run"]       = now.isoformat()
    h["status"]         = status
    h["last_detail"]    = detail
    h["run_count"]      = h.get("run_count", 0) + 1
    if status == "error":
        h["error_count"]       = h.get("error_count", 0) + 1
        h["last_error"]        = now.isoformat()
        h["last_error_detail"] = detail
    save_json(HEALTH_FILE, h)


def should_send_daily_report() -> bool:
    """Send once per day at 09:00 UTC."""
    now = datetime.now(timezone.utc)
    if now.hour != 9 or now.minute > 14:
        return False
    h = load_json(HEALTH_FILE)
    last = h.get("last_daily_report")
    if not last:
        return True
    last_dt = datetime.fromisoformat(last)
    return (now - last_dt).total_seconds() > 23 * 3600


def send_daily_report() -> None:
    h = load_json(HEALTH_FILE)
    tracker = load_json(TRACKER_FILE)
    now = datetime.now(timezone.utc)

    runs    = h.get("run_count", 0)
    errors  = h.get("error_count", 0)
    sr      = round((runs - errors) / runs * 100, 1) if runs else 100

    cutoff = now.timestamp() - 86400
    recent = [k for k, v in tracker.items() if v > cutoff]

    status = "✅ HEALTHY" if errors == 0 else ("⚠️ ISSUES" if errors < 3 else "🔴 DEGRADED")

    lines = [f"📊 <b>BOT DAILY REPORT — {now.strftime('%Y-%m-%d')}</b>",
             "━━━━━━━━━━━━━━━━━━━━━",
             f"📈 Status: {status}",
             f"🔄 Runs today: {runs}  |  Errors: {errors}  |  Uptime: {sr}%",
             f"🔔 Signals sent (24h): {len(recent)}"]

    if recent:
        lines.append("\n<b>Recent signals:</b>")
        for k in recent[:8]:
            parts = k.rsplit("_", 1)
            if len(parts) == 2:
                p, st = parts
                e = "🟢" if "BULLISH" in st or "LONG" in st else "🔴"
                lines.append(f"  {e} {p}: {st.replace('_',' ').title()}")

    lines += ["━━━━━━━━━━━━━━━━━━━━━",
              f"📡 Pairs: GOLD · EUR/USD · GBP/USD",
              f"⏰ Next report: tomorrow 09:00 UTC",
              "<i>🤖 EMA Crossover + Pullback Bot (v4.3 logic)</i>"]

    send_telegram("\n".join(lines), admin=True)
    h["last_daily_report"] = now.isoformat()
    save_json(HEALTH_FILE, h)


def send_error_alert(pair: str, error: str) -> None:
    """Rate-limited error alert — max one per 5 minutes."""
    h = load_json(HEALTH_FILE)
    last_alert = h.get("last_error_alert", 0)
    if (datetime.now(timezone.utc).timestamp() - last_alert) < 300:
        return
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    msg = (f"⚠️ <b>BOT ERROR</b>\n\n"
           f"📅 {now_str}\n"
           f"📊 Pair: {pair}\n"
           f"❌ {error[:300]}\n\n"
           f"<i>Bot will retry on next cron run.</i>")
    send_telegram(msg, admin=True)
    h["last_error_alert"] = datetime.now(timezone.utc).timestamp()
    save_json(HEALTH_FILE, h)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN SCAN
# ─────────────────────────────────────────────────────────────────────────────

def scan_pair(pair: str, symbol: str, api_key: str,
              risk_pct: float, volume_filter: bool) -> int:
    """
    Scan one pair for fresh signals. Returns count of signals sent.

    FIX 5: volume_filter passed through from PAIRS config so that
    instruments without real volume data (GOLD) skip the volume check.
    """
    log.info(f"\n{'='*45}\n🔍 {pair} ({symbol})")

    df = fetch_15min(symbol, api_key, outputsize=500)
    if len(df) < EMA_SLOW + PULLBACK_SWING_BARS + 20:
        log.warning(f"  Not enough bars ({len(df)}), skipping.")
        return 0
    time.sleep(8)

    df_1h = fetch_1h(symbol, api_key, outputsize=300)
    trend_4h, trend_1h = htf_trend(df_1h)
    log.info(f"  HTF: 4H={trend_4h}, 1H={trend_1h}")
    time.sleep(8)

    # FIX 5: pass volume_filter flag per instrument
    signals_df = detect_signals(df, trend_4h, trend_1h, volume_filter=volume_filter)
    if signals_df.empty:
        log.info(f"  No signals detected.")
        return 0

    # FIX 3: bar column now holds lcc values; compare against len(df)-2
    # (last fully closed candle index) rather than len(df)-1 (forming candle).
    max_closed_bar = len(df) - 2
    min_fresh_bar  = max_closed_bar - SIGNAL_FRESHNESS_BARS
    fresh = signals_df[signals_df["bar"] >= min_fresh_bar]

    if fresh.empty:
        log.info(f"  No fresh signals (all fired > {SIGNAL_FRESHNESS_BARS} bars ago).")
        return 0

    sent = 0
    for _, sig in fresh.iterrows():
        stype = sig["signal_type"]
        log.info(f"  🔔 Fresh signal: {stype} at {sig['timestamp']}")

        if signal_on_cooldown(pair, stype):
            continue

        msg = format_signal_message(pair, sig.to_dict(), trend_4h, trend_1h, risk_pct)
        if send_telegram(msg):
            mark_signal_sent(pair, stype)
            sent += 1
            log.info(f"  ✅ Sent {stype} for {pair}")
        else:
            log.error(f"  ❌ Failed to send Telegram message.")

    return sent


def main() -> None:
    log.info("=" * 55)
    log.info("  EMA Signal Bot (v4.3 logic) — cron run starting")
    log.info(f"  UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("=" * 55)

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set. Exiting.")
        return

    if not TWELVE_DATA_KEY_MAIN:
        log.error("TWELVE_DATA_API_KEY not set. Exiting.")
        return

    update_health("running", "cron started")

    if should_send_daily_report():
        log.info("📊 Sending daily health report…")
        send_daily_report()

    total_sent = 0
    errors     = 0

    # FIX 5: unpack 4-tuple from PAIRS (added volume_filter field)
    for pair, (symbol, api_key, risk_pct, volume_filter) in PAIRS.items():
        try:
            sent = scan_pair(pair, symbol, api_key, risk_pct, volume_filter)
            total_sent += sent
        except Exception as e:
            errors += 1
            log.error(f"  ❌ {pair}: {e}", exc_info=True)
            send_error_alert(pair, str(e))
            update_health("error", str(e))
        time.sleep(5)

    status = "ok" if errors == 0 else "partial_error"
    update_health(status, f"sent={total_sent} errors={errors}")

    log.info("=" * 55)
    log.info(f"  Done — signals sent: {total_sent} | errors: {errors}")
    log.info("=" * 55)


if __name__ == "__main__":
    main()
