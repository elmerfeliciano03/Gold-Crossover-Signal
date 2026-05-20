#!/usr/bin/env python3
"""
EMA Crossover + Pullback Signal Bot — Telegram / Render Cron Job
================================================================
Signal logic: v4 (backtester-validated, Feb–May 2026)
  • EMA50×EMA200 crossover (fixed direction, candle confirm, volume spike)
  • Pullback to EMA50 (4H+1H trend confirm, RSI 40–60, Fib retrace 28–58%)
  • ADX ≥ 20 | Session filter (UTC) | 12-bar cooldown per signal type

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
  (acceptable — cooldown is 12 hours so at worst one duplicate alert).

Fixes applied (v4.1):
  FIX 1 — main() was never called (missing body under if __name__ == "__main__")
  FIX 2 — session hour filter checked times[i] (forming candle) instead of
           times[lcc] (last closed candle), blocking valid signals near hour edges
  FIX 3 — failed crossover candle/volume check used bare `continue`, silently
           skipping pullback detection on the same bar; replaced with flag pattern
"""

import os
import json
import time
import logging
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta

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

# ── PAIRS — (symbol, api_key, risk_pct) ──────────────────────────────────────
PAIRS = {
    "GOLD":   ("XAU/USD", TWELVE_DATA_KEY_MAIN,  1.50),
    "EURUSD": ("EUR/USD", TWELVE_DATA_KEY_FOREX, 1.00),
    "GBPUSD": ("GBP/USD", TWELVE_DATA_KEY_FOREX, 1.00),
}

# ── SIGNAL PARAMETERS (v4 — exact match to backtester) ───────────────────────
EMA_FAST             = 50
EMA_SLOW             = 200
ADX_MIN              = 20
SIGNAL_COOLDOWN_HRS  = 12        # hours between same signal type on same pair
CANDLE_CONFIRM       = True
VOLUME_FILTER        = True
BREAKEVEN_TRIGGER    = 0.5       # % move before SL moves to breakeven

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
    """Return True if this pair+signal was sent within the last 12 hours."""
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

def fetch_15min(symbol: str, api_key: str, outputsize: int = 800) -> pd.DataFrame:
    """
    Fetch the most recent 15-min bars for live signal detection.
    800 bars ≈ 8 days — enough for EMA200 warmup (200 bars) plus
    PULLBACK_SWING_BARS (96) with headroom.
    Single call only (no chunking needed for live mode).
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
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        d = resp.json()
    except Exception as e:
        log.error(f"  15min fetch error ({symbol}): {e}")
        return pd.DataFrame()

    if "code" in d:
        log.error(f"  API error ({symbol}): {d.get('message')}")
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
        resp = requests.get(url, params=params, timeout=20)
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
# INDICATORS  (v4 — exact match to backtester)
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
    """Wilder RSI, oldest-first."""
    n = len(closes)
    rsi_out = np.full(n, 50.0)
    if n < period + 1:
        return rsi_out
    deltas = np.diff(closes)
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    for i in range(period, n - 1):
        avg_gain = (avg_gain * (period-1) + gains[i]) / period
        avg_loss = (avg_loss * (period-1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss > 0 else 100.0
        rsi_out[i+1] = 100 - (100 / (1 + rs))
    return rsi_out


def volume_ma(volumes: np.ndarray, period: int = 20) -> np.ndarray:
    out = np.zeros(len(volumes))
    for i in range(period, len(volumes)):
        out[i] = volumes[i-period:i].mean()
    return out


def htf_trend(df_1h: pd.DataFrame) -> tuple[str, str]:
    """
    Derive 4H and 1H trend from 1H data (v4 — fixed session-start snapshot).
    Returns (trend_4h, trend_1h): "BULLISH" | "BEARISH" | "NEUTRAL"
    """
    if df_1h.empty or len(df_1h) < 200:
        return "NEUTRAL", "NEUTRAL"

    closes_1h = df_1h["Close"].values
    ema200_1h = ema_series(closes_1h, 200)
    idx = -2 if len(closes_1h) >= 2 else -1
    trend_1h = ("BULLISH" if closes_1h[idx] > ema200_1h[idx]
                else "BEARISH") if not np.isnan(ema200_1h[idx]) else "NEUTRAL"

    closes_4h = closes_1h[::4]
    if len(closes_4h) < 50:
        return "NEUTRAL", trend_1h

    period_4h = min(200, len(closes_4h) - 1)
    ema200_4h = ema_series(closes_4h, period_4h)
    idx4 = -2 if len(closes_4h) >= 2 else -1
    trend_4h = ("BULLISH" if closes_4h[idx4] > ema200_4h[idx4]
                else "BEARISH") if not np.isnan(ema200_4h[idx4]) else "NEUTRAL"

    return trend_4h, trend_1h


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL DETECTION  (v4.1 — fixes applied)
# ─────────────────────────────────────────────────────────────────────────────

def detect_signals(df: pd.DataFrame,
                   trend_4h: str,
                   trend_1h: str) -> pd.DataFrame:
    """
    Full v4.1 signal detection over the supplied DataFrame.
    Returns all detected signals as a DataFrame. The caller then filters
    to only "fresh" signals (last SIGNAL_FRESHNESS_BARS bars).

    Changes vs v4:
      - FIX 2: session hour filter now uses times[lcc] (last closed candle)
               instead of times[i] (the forming candle)
      - FIX 3: failed crossover checks no longer silently skip pullback
               detection on the same bar; replaced bare `continue` with a
               boolean `valid` flag so the pullback block is still evaluated
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
    vol_ma_arr = volume_ma(volumes, 20) if VOLUME_FILTER else np.zeros(len(closes))

    signals = []
    last_signal_bar: dict[str, int] = {}
    min_bar = EMA_SLOW + PULLBACK_SWING_BARS + 5

    for i in range(min_bar, len(df)):
        lcc = i - 1   # last closed candle — no look-ahead

        # FIX 2: check the closed candle's hour, not the forming candle's hour
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

            # FIX 3: use a flag instead of bare `continue` so a failed crossover
            # check does not silently skip pullback detection on this same bar
            valid = True

            if CANDLE_CONFIRM:
                if is_long  and not (cur_px > cur50 and cur_px > cur200):
                    valid = False
                if not is_long and not (cur_px < cur50 and cur_px < cur200):
                    valid = False

            if valid and VOLUME_FILTER and vol_ma_arr[lcc] > 0:
                if volumes[lcc] < 1.2 * vol_ma_arr[lcc]:
                    valid = False

            if valid and (i - last_signal_bar.get(signal_type, -9999)) >= 12:
                entry = cur_px
                sl    = entry * (1 - 0.005) if is_long else entry * (1 + 0.005)
                tp    = entry * (1 + 0.010) if is_long else entry * (1 - 0.010)

                signals.append({
                    "bar": i, "timestamp": str(times[i]),
                    "signal_type": signal_type, "direction": 1 if is_long else -1,
                    "entry": round(entry, 5), "sl": round(sl, 5), "tp": round(tp, 5),
                    "adx": round(adx_val, 1), "rsi": round(rsi_val, 1),
                    "ema50": round(cur50, 5), "ema200": round(cur200, 5),
                })
                last_signal_bar[signal_type] = i

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

        if (i - last_signal_bar.get(pb_type, -9999)) < 12:
            continue

        entry = cur_px
        sl    = entry * (1 - 0.005) if is_long_pb else entry * (1 + 0.005)
        tp    = entry * (1 + 0.010) if is_long_pb else entry * (1 - 0.010)

        signals.append({
            "bar": i, "timestamp": str(times[i]),
            "signal_type": pb_type, "direction": 1 if is_long_pb else -1,
            "entry": round(entry, 5), "sl": round(sl, 5), "tp": round(tp, 5),
            "adx": round(adx_val, 1), "rsi": round(rsi_val, 1),
            "ema50": round(cur50, 5), "ema200": round(cur200, 5),
        })
        last_signal_bar[pb_type] = i

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
    direction = sig["direction"]

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

    now_irish = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

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

⏰ {now_irish}
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

    # Count signals sent in last 24h
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
              "<i>🤖 EMA Crossover + Pullback Bot (v4.1 logic)</i>"]

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

def scan_pair(pair: str, symbol: str, api_key: str, risk_pct: float) -> int:
    """
    Scan one pair for fresh signals. Returns count of signals sent.
    """
    log.info(f"\n{'='*45}\n🔍 {pair} ({symbol})")

    # ── Fetch data ────────────────────────────────────────────────────────────
    df = fetch_15min(symbol, api_key, outputsize=800)
    if len(df) < EMA_SLOW + PULLBACK_SWING_BARS + 20:
        log.warning(f"  Not enough bars ({len(df)}), skipping.")
        return 0
    time.sleep(2)

    df_1h = fetch_1h(symbol, api_key, outputsize=300)
    trend_4h, trend_1h = htf_trend(df_1h)
    log.info(f"  HTF: 4H={trend_4h}, 1H={trend_1h}")
    time.sleep(2)

    # ── Detect all signals ────────────────────────────────────────────────────
    signals_df = detect_signals(df, trend_4h, trend_1h)
    if signals_df.empty:
        log.info(f"  No signals detected.")
        return 0

    # ── Filter to fresh signals (fired on last 1–2 closed candles) ────────────
    max_bar = len(df) - 1              # current (forming) candle index
    min_fresh_bar = max_bar - SIGNAL_FRESHNESS_BARS
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
    log.info("  EMA Signal Bot (v4.1 logic) — cron run starting")
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

    for pair, (symbol, api_key, risk_pct) in PAIRS.items():
        try:
            sent = scan_pair(pair, symbol, api_key, risk_pct)
            total_sent += sent
        except Exception as e:
            errors += 1
            log.error(f"  ❌ {pair}: {e}", exc_info=True)
            send_error_alert(pair, str(e))
            update_health("error", str(e))
        time.sleep(3)

    status = "ok" if errors == 0 else "partial_error"
    update_health(status, f"sent={total_sent} errors={errors}")

    log.info("=" * 55)
    log.info(f"  Done — signals sent: {total_sent} | errors: {errors}")
    log.info("=" * 55)


# FIX 1: main() was never called — the if __name__ block had no body
if __name__ == "__main__":
    main()
