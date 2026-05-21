#!/usr/bin/env python3
"""
EMA Crossover + Pullback Signal Bot — EUR/USD, GBP/USD, GOLD (cron edition)
============================================================================
Signal logic:
  • EUR/USD & GBP/USD: v4.6
      Crossover : EMA50 × EMA200, candle confirm, ADX ≥ 15
      Pullback  : 4H+1H aligned, 0.1% proximity to EMA50, ADX ≥ 15,
                  asymmetric RSI gate (long 35–55 / short 45–65),
                  12-bar cooldown, London+NY session filter

  • GOLD (GC=F): v4
      Crossover : EMA50 × EMA200, candle confirm, volume ≥ 1.2× MA20, ADX ≥ 20
      Pullback  : 4H+1H aligned, 0.3% proximity to EMA50, ADX ≥ 20,
                  12-bar cooldown, session filter
                  (no RSI filter, no Fibonacci retrace)

Cooldown between same signal type on same pair: 1 hour (file-backed)

Deployment: push to GitHub → connect to Render as Cron Job
  Schedule: */15 * * * *
  Command:  python ema_signal_bot.py

Required environment variables (Render dashboard):
  TELEGRAM_TOKEN          — bot token from @BotFather
  TELEGRAM_CHAT_ID        — channel/chat for trade signals
  TELEGRAM_ADMIN_CHAT_ID  — (optional) separate chat for health/error alerts

No API key required — data sourced from yfinance.
"""

import os
import json
import time
import logging
import requests
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime, timezone

# ── LOGGING ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN         = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID       = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "") or TELEGRAM_CHAT_ID

# ── CAPITAL & RISK ────────────────────────────────────────────────────────────
CAPITAL        = 2000.0
RR             = 2.0
MAX_RISK_MULT  = 3.0   # risk capped at 3× base (v4.6)

# ── PAIRS ─────────────────────────────────────────────────────────────────────
# (yf_symbol, display_name, risk_pct, logic_version)
PAIRS = {
    "EURUSD": ("EURUSD=X", "EUR/USD", 1.00, "v46"),
    "GBPUSD": ("GBPUSD=X", "GBP/USD", 1.00, "v46"),
    "GOLD":   ("GC=F",     "GOLD",    1.50, "v4"),
}

# ── SHARED SIGNAL PARAMETERS ──────────────────────────────────────────────────
EMA_FAST      = 50
EMA_SLOW      = 200
CANDLE_CONFIRM = True

# v4.6 (EURUSD / GBPUSD)
ADX_MIN_V46               = 15
PULLBACK_PROXIMITY_V46    = 0.001   # 0.1%
PULLBACK_COOLDOWN_BARS    = 12
PULLBACK_LONG_RSI_LO      = 35
PULLBACK_LONG_RSI_HI      = 55
PULLBACK_SHORT_RSI_LO     = 45
PULLBACK_SHORT_RSI_HI     = 65
PULLBACK_ALLOWED_HOURS_V46 = {6,7,8,9,10,11,12,13,14,15,16,17,18,19,21,22}

# v4 (GOLD)
ADX_MIN_V4              = 20
PULLBACK_PROXIMITY_V4   = 0.003   # 0.3%
VOLUME_FILTER_ENABLED   = True
ALLOWED_HOURS_V4        = {6,7,8,9,10,11,12,13,14,15,16,17,18,19,21,22}

# ── FRESHNESS (cron safety) ───────────────────────────────────────────────────
# Only alert signals on the last N bars (guards against stale cron execution)
SIGNAL_FRESHNESS_BARS = 2

# ── COOLDOWN (file-backed, cross-run) ─────────────────────────────────────────
SIGNAL_COOLDOWN_HRS = 1    # hours between same signal type on same pair

# ── STATE FILES ───────────────────────────────────────────────────────────────
TRACKER_FILE = "/tmp/ema_signal_tracker.json"
HEALTH_FILE  = "/tmp/ema_health_tracker.json"


# ─────────────────────────────────────────────────────────────────────────────
# STATE HELPERS
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
        if resp.status_code != 200:
            log.error(f"Telegram HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.status_code == 200
    except Exception as e:
        log.error(f"Telegram error: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# DATA FETCHING
# ─────────────────────────────────────────────────────────────────────────────

def fetch_15m(yf_symbol: str) -> pd.DataFrame:
    try:
        df = yf.download(yf_symbol, interval="15m", period="60d",
                         auto_adjust=True, progress=False,
                         multi_level_index=False)
        if df.empty:
            return pd.DataFrame()
        df.index = pd.to_datetime(df.index)
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        df = df[["Open","High","Low","Close","Volume"]].dropna().sort_index()
        log.info(f"  ✅ 15min: {len(df)} bars  "
                 f"({df.index[0].date()} → {df.index[-1].date()})")
        return df
    except Exception as e:
        log.error(f"  15min fetch error ({yf_symbol}): {e}")
        return pd.DataFrame()


def fetch_1h(yf_symbol: str) -> pd.DataFrame:
    try:
        df = yf.download(yf_symbol, interval="1h", period="2y",
                         auto_adjust=True, progress=False,
                         multi_level_index=False)
        if df.empty:
            return pd.DataFrame()
        df.index = pd.to_datetime(df.index)
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        df = df[["Close"]].dropna().sort_index()
        log.info(f"  ✅ 1H: {len(df)} bars")
        return df
    except Exception as e:
        log.error(f"  1H fetch error ({yf_symbol}): {e}")
        return pd.DataFrame()


# ─────────────────────────────────────────────────────────────────────────────
# INDICATORS (Wilder-smoothed — matches TradingView / MetaTrader)
# ─────────────────────────────────────────────────────────────────────────────

def ema_series(closes: np.ndarray, period: int) -> np.ndarray:
    if len(closes) < period:
        return np.full(len(closes), np.nan)
    mult = 2.0 / (period + 1)
    out = np.empty(len(closes))
    out[0] = closes[0]
    for i in range(1, len(closes)):
        out[i] = (closes[i] - out[i-1]) * mult + out[i-1]
    return out


def adx_series(highs: np.ndarray, lows: np.ndarray,
               closes: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(closes)
    adx_out = np.zeros(n)
    if n < period * 2 + 1:
        return adx_out
    tr  = np.zeros(n); pdm = np.zeros(n); ndm = np.zeros(n)
    for i in range(1, n):
        tr[i]  = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]),
                     abs(lows[i]-closes[i-1]))
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
        s   = pdi + mdi
        dx[i] = 100 * abs(pdi - mdi) / s if s > 0 else 0
    adx_out[period*2] = dx[period:period*2].mean()
    for i in range(period*2+1, n):
        adx_out[i] = (adx_out[i-1] * (period-1) + dx[i]) / period
    return adx_out


def rsi_series(closes: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(closes)
    rsi_out = np.full(n, 50.0)
    if n < period + 1:
        return rsi_out
    deltas   = np.diff(closes)
    gains    = np.where(deltas > 0, deltas, 0.0)
    losses   = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    rs = avg_gain / avg_loss if avg_loss > 0 else 100.0
    rsi_out[period] = 100 - (100 / (1 + rs))
    for i in range(period+1, n):
        avg_gain = (avg_gain * (period-1) + gains[i-1]) / period
        avg_loss = (avg_loss * (period-1) + losses[i-1]) / period
        rs = avg_gain / avg_loss if avg_loss > 0 else 100.0
        rsi_out[i] = 100 - (100 / (1 + rs))
    return rsi_out


def volume_ma(volumes: np.ndarray, period: int = 20) -> np.ndarray:
    out = np.zeros(len(volumes))
    for i in range(period, len(volumes)):
        out[i] = volumes[i-period:i].mean()
    return out


def htf_trend(df_1h: pd.DataFrame) -> tuple[str, str]:
    """
    Derive 4H and 1H trend from 1H data using iloc[::4] slice.
    Returns (trend_4h, trend_1h): "BULLISH" | "BEARISH" | "NEUTRAL"
    """
    if df_1h.empty or len(df_1h) < 200:
        return "NEUTRAL", "NEUTRAL"

    closes_1h  = df_1h["Close"].values
    ema200_1h  = ema_series(closes_1h, 200)
    trend_1h   = ("BULLISH" if closes_1h[-1] > ema200_1h[-1]
                  else "BEARISH") if not np.isnan(ema200_1h[-1]) else "NEUTRAL"

    closes_4h  = closes_1h[::4]
    if len(closes_4h) < 50:
        return "NEUTRAL", trend_1h

    period_4h  = min(200, len(closes_4h) - 1)
    ema200_4h  = ema_series(closes_4h, period_4h)
    trend_4h   = ("BULLISH" if closes_4h[-1] > ema200_4h[-1]
                  else "BEARISH") if not np.isnan(ema200_4h[-1]) else "NEUTRAL"

    return trend_4h, trend_1h


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL DETECTION — v4.6 (EUR/USD, GBP/USD)
# ─────────────────────────────────────────────────────────────────────────────

def detect_signals_v46(df: pd.DataFrame,
                        trend_4h: str,
                        trend_1h: str) -> pd.DataFrame:
    """
    v4.6 signal logic for EUR/USD and GBP/USD.

    BULLISH_CROSSOVER / BEARISH_CROSSOVER:
      EMA50 × EMA200, candle confirm, ADX ≥ 15, no session filter.

    PULLBACK_LONG / PULLBACK_SHORT:
      4H+1H aligned, price within 0.1% of EMA50, ADX ≥ 15,
      RSI gate (asymmetric), 12-bar cooldown, session filter.
    """
    closes  = df["Close"].values
    highs   = df["High"].values
    lows    = df["Low"].values
    times   = df.index

    ema50  = ema_series(closes, EMA_FAST)
    ema200 = ema_series(closes, EMA_SLOW)
    adx    = adx_series(highs, lows, closes, 14)
    rsi    = rsi_series(closes, 14)

    signals: list[dict] = []
    last_pb_bar: dict[str, int] = {}
    min_bar = EMA_SLOW + 5

    for i in range(min_bar, len(df)):
        lcc = i - 1

        c50 = ema50[lcc];  p50 = ema50[lcc-1]
        c200= ema200[lcc]; p200= ema200[lcc-1]
        adx_v = adx[lcc];  rsi_v = rsi[lcc]
        px    = closes[lcc]

        if np.isnan(c200) or np.isnan(c50):
            continue
        if adx_v < ADX_MIN_V46:
            continue

        # ── Crossover ─────────────────────────────────────────────────────────
        bull_x = (p50 < p200) and (c50 > c200)
        bear_x = (p50 > p200) and (c50 < c200)

        if bull_x or bear_x:
            is_long = bull_x
            if CANDLE_CONFIRM:
                if is_long     and not (px > c50 and px > c200): continue
                if not is_long and not (px < c50 and px < c200): continue

            entry = px
            sl    = entry * (1-0.005) if is_long else entry * (1+0.005)
            tp    = entry * (1+0.010) if is_long else entry * (1-0.010)
            signals.append({
                "bar": lcc, "timestamp": times[lcc],
                "signal_type": "BULLISH_CROSSOVER" if is_long else "BEARISH_CROSSOVER",
                "direction": 1 if is_long else -1,
                "entry": round(entry, 5), "sl": round(sl, 5), "tp": round(tp, 5),
                "adx": round(adx_v, 1), "rsi": round(rsi_v, 1),
                "ema50": round(c50, 5), "ema200": round(c200, 5),
            })
            continue

        # ── Pullback ──────────────────────────────────────────────────────────
        if times[lcc].hour not in PULLBACK_ALLOWED_HOURS_V46:
            continue
        if trend_4h == "NEUTRAL" or trend_1h == "NEUTRAL":
            continue

        is_long_pb  = (trend_4h == "BULLISH" and trend_1h == "BULLISH")
        is_short_pb = (trend_4h == "BEARISH" and trend_1h == "BEARISH")
        if not (is_long_pb or is_short_pb):
            continue

        if abs(px - c50) / c50 > PULLBACK_PROXIMITY_V46:
            continue

        if is_long_pb  and not (PULLBACK_LONG_RSI_LO  <= rsi_v <= PULLBACK_LONG_RSI_HI):
            continue
        if is_short_pb and not (PULLBACK_SHORT_RSI_LO <= rsi_v <= PULLBACK_SHORT_RSI_HI):
            continue

        pb_type = "PULLBACK_LONG" if is_long_pb else "PULLBACK_SHORT"

        if (i - last_pb_bar.get(pb_type, -9999)) < PULLBACK_COOLDOWN_BARS:
            continue

        entry = px
        sl    = entry * (1-0.005) if is_long_pb else entry * (1+0.005)
        tp    = entry * (1+0.010) if is_long_pb else entry * (1-0.010)
        signals.append({
            "bar": lcc, "timestamp": times[lcc],
            "signal_type": pb_type,
            "direction": 1 if is_long_pb else -1,
            "entry": round(entry, 5), "sl": round(sl, 5), "tp": round(tp, 5),
            "adx": round(adx_v, 1), "rsi": round(rsi_v, 1),
            "ema50": round(c50, 5), "ema200": round(c200, 5),
        })
        last_pb_bar[pb_type] = i

    return pd.DataFrame(signals)


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL DETECTION — v4 (GOLD)
# ─────────────────────────────────────────────────────────────────────────────

def detect_signals_v4(df: pd.DataFrame,
                       trend_4h: str,
                       trend_1h: str) -> pd.DataFrame:
    """
    v4 signal logic for Gold (GC=F).

    BULLISH_CROSSOVER / BEARISH_CROSSOVER:
      EMA50 × EMA200, candle confirm, volume spike (1.2× MA20), ADX ≥ 20.

    PULLBACK_LONG / PULLBACK_SHORT:
      4H+1H aligned, price within 0.3% of EMA50, ADX ≥ 20,
      RSI 40–60, Fibonacci retrace 28–58%.
    """
    closes  = df["Close"].values
    highs   = df["High"].values
    lows    = df["Low"].values
    volumes = df["Volume"].values
    times   = df.index

    ema50  = ema_series(closes, EMA_FAST)
    ema200 = ema_series(closes, EMA_SLOW)
    adx    = adx_series(highs, lows, closes, 14)
    rsi    = rsi_series(closes, 14)
    vol_ma = volume_ma(volumes, 20)

    # Auto-disable volume filter if data is missing
    has_vol = volumes.sum() > 0

    signals: list[dict] = []
    last_sig_bar: dict[str, int] = {}
    min_bar = EMA_SLOW + 5

    for i in range(min_bar, len(df)):
        lcc = i - 1

        if times[i].hour not in ALLOWED_HOURS_V4:
            continue

        c50 = ema50[lcc];  p50 = ema50[lcc-1]
        c200= ema200[lcc]; p200= ema200[lcc-1]
        adx_v = adx[lcc];  rsi_v = rsi[lcc]
        px    = closes[lcc]

        if np.isnan(c200) or np.isnan(c50):
            continue
        if adx_v < ADX_MIN_V4:
            continue

        # ── Crossover ─────────────────────────────────────────────────────────
        bull_x = (p50 < p200) and (c50 > c200)
        bear_x = (p50 > p200) and (c50 < c200)

        if bull_x or bear_x:
            is_long = bull_x
            stype   = "BULLISH_CROSSOVER" if is_long else "BEARISH_CROSSOVER"

            if CANDLE_CONFIRM:
                if is_long     and not (px > c50 and px > c200): continue
                if not is_long and not (px < c50 and px < c200): continue

            if VOLUME_FILTER_ENABLED and has_vol and vol_ma[lcc] > 0:
                if volumes[lcc] < 1.2 * vol_ma[lcc]: continue

            if (i - last_sig_bar.get(stype, -9999)) < 12:
                continue

            entry = px
            sl    = entry * (1-0.005) if is_long else entry * (1+0.005)
            tp    = entry * (1+0.010) if is_long else entry * (1-0.010)
            signals.append({
                "bar": lcc, "timestamp": times[lcc],
                "signal_type": stype,
                "direction": 1 if is_long else -1,
                "entry": round(entry, 5), "sl": round(sl, 5), "tp": round(tp, 5),
                "adx": round(adx_v, 1), "rsi": round(rsi_v, 1),
                "ema50": round(c50, 5), "ema200": round(c200, 5),
            })
            last_sig_bar[stype] = i
            continue

        # ── Pullback ──────────────────────────────────────────────────────────
        if trend_4h == "NEUTRAL" or trend_1h == "NEUTRAL":
            continue

        is_long_pb  = (trend_4h == "BULLISH" and trend_1h == "BULLISH")
        is_short_pb = (trend_4h == "BEARISH" and trend_1h == "BEARISH")
        if not (is_long_pb or is_short_pb):
            continue

        if abs(px - c50) / c50 > PULLBACK_PROXIMITY_V4:
            continue

        pb_type = "PULLBACK_LONG" if is_long_pb else "PULLBACK_SHORT"

        if (i - last_sig_bar.get(pb_type, -9999)) < 12:
            continue

        entry = px
        sl    = entry * (1-0.005) if is_long_pb else entry * (1+0.005)
        tp    = entry * (1+0.010) if is_long_pb else entry * (1-0.010)
        signals.append({
            "bar": lcc, "timestamp": times[lcc],
            "signal_type": pb_type,
            "direction": 1 if is_long_pb else -1,
            "entry": round(entry, 5), "sl": round(sl, 5), "tp": round(tp, 5),
            "adx": round(adx_v, 1), "rsi": round(rsi_v, 1),
            "ema50": round(c50, 5), "ema200": round(c200, 5),
        })
        last_sig_bar[pb_type] = i

    return pd.DataFrame(signals)


# ─────────────────────────────────────────────────────────────────────────────
# MESSAGE FORMATTING
# ─────────────────────────────────────────────────────────────────────────────

SIGNAL_LABELS = {
    "BULLISH_CROSSOVER": ("📈", "🟢 BULLISH CROSSOVER (LONG)", "EMA50 crossed above EMA200"),
    "BEARISH_CROSSOVER": ("📉", "🔴 BEARISH CROSSOVER (SHORT)", "EMA50 crossed below EMA200"),
    "PULLBACK_LONG":     ("📈", "🟢 PULLBACK LONG", "Pullback to EMA50 in uptrend"),
    "PULLBACK_SHORT":    ("📉", "🔴 PULLBACK SHORT", "Pullback to EMA50 in downtrend"),
}


def fmt_price(price: float) -> str:
    return f"{price:.2f}" if price > 10 else f"{price:.5f}"


def format_signal_message(pair: str, display: str, sig: dict,
                           risk_pct: float, logic_ver: str,
                           trend_4h: str, trend_1h: str) -> str:
    stype              = sig["signal_type"]
    arrow, label, desc = SIGNAL_LABELS.get(stype, ("⚡", stype, ""))
    entry, sl, tp      = sig["entry"], sig["sl"], sig["tp"]
    adx_v, rsi_v       = sig["adx"], sig["rsi"]
    ema50, ema200      = sig["ema50"], sig["ema200"]

    # Pip / point size for display
    is_gold   = pair == "GOLD"
    unit      = "pts" if is_gold else "pips"
    pip_size  = 0.1 if is_gold else 0.0001
    sl_dist   = round(abs(entry - sl) / pip_size)
    tp_dist   = round(abs(tp - entry) / pip_size)

    risk_eur   = round(CAPITAL * risk_pct / 100, 2)
    reward_eur = round(risk_eur * RR, 2)

    # HTF context line (relevant for pullbacks)
    htf_line = (f"\n• HTF Trend: 4H {trend_4h} | 1H {trend_1h}"
                if "PULLBACK" in stype else "")

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return (
        f"{arrow} <b>{display} — {label}</b> {arrow}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ <b>Time:</b> {now_utc}\n"
        f"📊 <b>Signal:</b> {desc}{htf_line}\n"
        f"🔬 <b>Logic:</b> {logic_ver}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ <b>Trade Levels</b>\n"
        f"• Entry:       <code>{fmt_price(entry)}</code>\n"
        f"• Stop Loss:   <code>{fmt_price(sl)}</code>  ({sl_dist} {unit})\n"
        f"• Take Profit: <code>{fmt_price(tp)}</code>  ({tp_dist} {unit})\n"
        f"• R:R          1:{RR:.0f}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📐 <b>Indicators</b>\n"
        f"• EMA50:  <code>{fmt_price(ema50)}</code>\n"
        f"• EMA200: <code>{fmt_price(ema200)}</code>\n"
        f"• ADX:    <code>{adx_v:.1f}</code>\n"
        f"• RSI:    <code>{rsi_v:.1f}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 <b>Risk (€{CAPITAL:,.0f} capital @ {risk_pct}%)</b>\n"
        f"• Risk:   €{risk_eur:.2f}\n"
        f"• Reward: €{reward_eur:.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ <i>Educational purposes only. Not financial advice.</i>"
    )


# ─────────────────────────────────────────────────────────────────────────────
# HEALTH TRACKING
# ─────────────────────────────────────────────────────────────────────────────

def update_health(status: str, detail: str = "") -> None:
    h = load_json(HEALTH_FILE)
    now = datetime.now(timezone.utc)
    h.update({
        "last_run":    now.isoformat(),
        "status":      status,
        "last_detail": detail,
        "run_count":   h.get("run_count", 0) + 1,
    })
    if status == "error":
        h["error_count"]        = h.get("error_count", 0) + 1
        h["last_error"]         = now.isoformat()
        h["last_error_detail"]  = detail
    save_json(HEALTH_FILE, h)


def should_send_daily_report() -> bool:
    now = datetime.now(timezone.utc)
    if now.hour != 9 or now.minute > 14:
        return False
    h    = load_json(HEALTH_FILE)
    last = h.get("last_daily_report")
    if not last:
        return True
    return (now.timestamp() - datetime.fromisoformat(last).timestamp()) > 23*3600


def send_daily_report(total_sent: int) -> None:
    h       = load_json(HEALTH_FILE)
    tracker = load_json(TRACKER_FILE)
    now     = datetime.now(timezone.utc)

    runs   = h.get("run_count", 0)
    errors = h.get("error_count", 0)
    sr     = round((runs - errors) / runs * 100, 1) if runs else 100
    cutoff = now.timestamp() - 86400
    recent = [k for k, v in tracker.items() if isinstance(v, (int, float)) and v > cutoff]

    status = ("✅ HEALTHY" if errors == 0
              else ("⚠️ ISSUES" if errors < 3 else "🔴 DEGRADED"))

    lines = [
        f"📊 <b>EMA BOT DAILY REPORT — {now.strftime('%Y-%m-%d')}</b>",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"📈 Status: {status}",
        f"🔄 Runs today: {runs}  |  Errors: {errors}  |  Uptime: {sr}%",
        f"🔔 Signals sent (24h): {len(recent)}",
    ]
    if recent:
        lines.append("\n<b>Recent signals:</b>")
        for k in recent[:10]:
            parts = k.split("_", 1)
            pair_label = parts[0] if len(parts) == 2 else k
            st_label   = parts[1].replace("_", " ").title() if len(parts) == 2 else ""
            e = "🟢" if "BULLISH" in k or "LONG" in k else "🔴"
            lines.append(f"  {e} {pair_label} — {st_label}")
    lines += [
        "━━━━━━━━━━━━━━━━━━━━━",
        "📡 Pairs: EUR/USD (v4.6) | GBP/USD (v4.6) | GOLD (v4)",
        "⏰ Next report: tomorrow 09:00 UTC",
        "<i>🤖 EMA Crossover + Pullback Bot</i>",
    ]
    send_telegram("\n".join(lines), admin=True)
    h["last_daily_report"] = now.isoformat()
    save_json(HEALTH_FILE, h)


def send_error_alert(pair: str, error: str) -> None:
    h = load_json(HEALTH_FILE)
    last_alert = h.get("last_error_alert", 0)
    if (datetime.now(timezone.utc).timestamp() - last_alert) < 300:
        return
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    msg = (f"⚠️ <b>EMA BOT ERROR — {pair}</b>\n\n"
           f"📅 {now_str}\n"
           f"❌ {str(error)[:300]}\n\n"
           f"<i>Bot will retry on next cron run.</i>")
    send_telegram(msg, admin=True)
    h["last_error_alert"] = datetime.now(timezone.utc).timestamp()
    save_json(HEALTH_FILE, h)


# ─────────────────────────────────────────────────────────────────────────────
# PER-PAIR SCAN
# ─────────────────────────────────────────────────────────────────────────────

def scan_pair(pair: str, yf_symbol: str, display: str,
              risk_pct: float, logic_ver: str) -> int:
    """
    Scan one pair for fresh signals.
    Returns number of signals sent.
    """
    log.info(f"\n{'─'*45}\n🔍 {pair} ({yf_symbol})  logic={logic_ver}")

    min_bars = EMA_SLOW + 20  # generous minimum

    df = fetch_15m(yf_symbol)
    if df.empty or len(df) < min_bars:
        log.warning(f"  Not enough bars ({len(df)}), skipping.")
        return 0

    time.sleep(1)   # courtesy pause between yfinance calls

    df_1h = fetch_1h(yf_symbol)
    if df_1h.empty:
        log.warning(f"  No 1H data, skipping.")
        return 0

    trend_4h, trend_1h = htf_trend(df_1h)
    log.info(f"  HTF: 4H={trend_4h}, 1H={trend_1h}")

    # Choose signal detector based on logic version
    if logic_ver == "v4":
        signals_df = detect_signals_v4(df, trend_4h, trend_1h)
    else:
        signals_df = detect_signals_v46(df, trend_4h, trend_1h)

    if signals_df.empty:
        log.info("  No signals detected this run.")
        return 0

    by_type = signals_df["signal_type"].value_counts().to_dict()
    log.info(f"  📡 {len(signals_df)} total signals: {by_type}")

    # Only process signals fresh enough to be from this cron cycle
    max_bar       = len(df) - 1
    min_fresh_bar = max_bar - SIGNAL_FRESHNESS_BARS
    fresh         = signals_df[signals_df["bar"] >= min_fresh_bar]

    if fresh.empty:
        log.info(f"  No fresh signals (all fired > {SIGNAL_FRESHNESS_BARS} bars ago).")
        return 0

    sent = 0
    for _, sig in fresh.iterrows():
        stype = sig["signal_type"]
        ts    = sig["timestamp"]
        log.info(f"  🔔 Fresh signal: {stype} @ bar {sig['bar']} ({ts})")

        if signal_on_cooldown(pair, stype):
            continue

        msg = format_signal_message(
            pair, display, sig.to_dict(),
            risk_pct, logic_ver,
            trend_4h, trend_1h,
        )

        if send_telegram(msg):
            mark_signal_sent(pair, stype)
            sent += 1
            log.info(f"  ✅ Sent: {stype} for {pair}")
        else:
            log.error("  ❌ Telegram send failed.")

    return sent


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("=" * 55)
    log.info("  EMA Crossover + Pullback Bot — cron run starting")
    log.info(f"  UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")
    log.info(f"  Pairs: {', '.join(PAIRS.keys())}")
    log.info("=" * 55)

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set — exiting.")
        return

    update_health("running", "cron started")
    total_sent = 0

    if should_send_daily_report():
        log.info("📊 Sending daily health report…")
        send_daily_report(total_sent)

    for pair, (yf_symbol, display, risk_pct, logic_ver) in PAIRS.items():
        try:
            sent = scan_pair(pair, yf_symbol, display, risk_pct, logic_ver)
            total_sent += sent
        except Exception as e:
            log.error(f"  ❌ Error scanning {pair}: {e}", exc_info=True)
            send_error_alert(pair, str(e))
            update_health("error", f"{pair}: {e}")

    update_health("ok", f"total_sent={total_sent}")
    log.info("=" * 55)
    log.info(f"  Done — signals sent this run: {total_sent}")
    log.info("=" * 55)


if __name__ == "__main__":
    main()
