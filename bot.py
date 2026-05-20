#!/usr/bin/env python3
"""
EMA Crossover + Pullback Signal Bot — Telegram / Render Cron Job
================================================================
Signal logic: v4.5

CHANGES IN v4.5:
────────────────
1. Signals separated — crossover and pullback sent as distinct
   message types with different Telegram chat sections. Each has
   its own cooldown key so a crossover never blocks a pullback
   and vice versa on the same pair.

2. Volume filter removed entirely. No volume check on any signal.
   Per-pair volume_filter flag removed from PAIRS config.

3. ADX minimum lowered from 20 to 15. Catches earlier-stage
   trend moves before ADX fully confirms.

4. RSI filter removed. Pullback signals no longer require RSI
   to be in the 40–60 neutral zone.

5. Fibonacci retracement check removed. Pullback fires purely
   on price proximity to EMA50 (within 0.3%) + HTF alignment.
   No swing-high/low calculation needed.

   Consequence: PULLBACK_SWING_BARS no longer used for signal
   filtering; min_bar reduced from 301 (200+96+5) to 205 (200+5),
   giving ~595 scannable bars from the 800-bar fetch.

RETAINED FROM v4.4:
───────────────────
- fetch_1h 1000 bars (4H EMA200 needs 800 1H bars)
- fetch_15min 800 bars (enough scannable window)
- iloc[::4] for 4H trend (no resample anchor issues)
- Session filter on pullbacks only (crossovers fire 24h)
- Candle confirmation on crossovers (close beyond both EMAs)
- 1-hour external cooldown per pair+signal type
- ADX still displayed in messages (informational)
"""

import os
import json
import time
import logging
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from typing import Dict, Tuple, Optional

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── ENVIRONMENT ───────────────────────────────────────────────────────────────
TELEGRAM_TOKEN         = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID       = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", TELEGRAM_CHAT_ID)
TWELVE_DATA_KEY_MAIN   = os.environ.get("TWELVE_DATA_API_KEY", "")
TWELVE_DATA_KEY_FOREX  = os.environ.get("TWELVE_DATA_API_KEY_2", TWELVE_DATA_KEY_MAIN)

START_CAPITAL = 2000.0
RR            = 2.0

# ── PAIRS — (symbol, api_key, risk_pct) ──────────────────────────────────────
# volume_filter removed (change 2) — no longer a per-pair flag
PAIRS = {
    "GOLD":   ("XAU/USD", TWELVE_DATA_KEY_MAIN,  1.50),
    "EURUSD": ("EUR/USD", TWELVE_DATA_KEY_FOREX, 1.00),
    "GBPUSD": ("GBP/USD", TWELVE_DATA_KEY_FOREX, 1.00),
}

EMA_FAST              = 50
EMA_SLOW              = 200
ADX_MIN               = 15        # change 3: lowered from 20
SIGNAL_COOLDOWN_HRS   = 1
CANDLE_CONFIRM        = True      # crossover bar must close beyond both EMAs
PULLBACK_PROXIMITY_PCT = 0.003    # price within 0.3% of EMA50
SIGNAL_FRESHNESS_BARS = 2

# RSI constants removed (change 4)
# Fibonacci/swing constants removed (change 5)
# min_bar now = EMA_SLOW + 5 = 205 (no longer needs PULLBACK_SWING_BARS)

# Session filter — applied to PULLBACK signals only
PULLBACK_ALLOWED_HOURS = {6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 21, 22}

TRACKER_FILE = "/tmp/signal_tracker.json"
HEALTH_FILE  = "/tmp/health_tracker.json"


# ─────────────────────────────────────────────────────────────────────────────
# TRACKER / STATE
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
        log.warning("Telegram not configured — skipping.")
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
    FIX B: 500 → 800 bars.
    min_bar = 301, so 800 bars gives 499 scannable bars (≈125h ≈ 5 days).
    Previously 500 bars gave only 199 scannable bars (≈50h ≈ 2 days) —
    not enough to catch a crossover which happens ~once per 2–4 weeks.
    """
    url = "https://api.twelvedata.com/time_series"
    params = {"symbol": symbol, "interval": "15min",
              "outputsize": outputsize, "apikey": api_key, "order": "ASC"}
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
                "Open":  float(b["open"]),  "High": float(b["high"]),
                "Low":   float(b["low"]),   "Close": float(b["close"]),
                "Volume": float(b.get("volume", 0)),
            })
        except (ValueError, KeyError):
            continue

    df = (pd.DataFrame(rows).drop_duplicates("datetime")
            .sort_values("datetime").set_index("datetime"))
    log.info(f"  ✅ 15min: {len(df)} bars  ({df.index[0].date()} → {df.index[-1].date()})")
    return df


def fetch_1h(symbol: str, api_key: str, outputsize: int = 1000) -> pd.DataFrame:
    """
    FIX A: 300 → 1000 bars.
    htf_trend (backtester method, iloc[::4]) needs 200 4H bars = 800 1H bars.
    1000 gives comfortable headroom after EMA warmup.
    Previously 300 1H bars → 75 4H bars → EMA200 impossible → always NEUTRAL.
    """
    url = "https://api.twelvedata.com/time_series"
    params = {"symbol": symbol, "interval": "1h",
              "outputsize": outputsize, "apikey": api_key, "order": "ASC"}
    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        d = resp.json()
        values = d.get("values", [])
        if not values:
            return pd.DataFrame()
        rows = [{"datetime": pd.Timestamp(b["datetime"]),
                 "Close": float(b["close"])} for b in values]
        df = (pd.DataFrame(rows).drop_duplicates("datetime")
                .sort_values("datetime").set_index("datetime"))
        log.info(f"  ✅ 1H: {len(df)} bars")
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
        out[i] = (closes[i] - out[i-1]) * mult + out[i-1]
    return out


def adx_series(highs: np.ndarray, lows: np.ndarray,
               closes: np.ndarray, period: int = 14) -> np.ndarray:
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
    """Kept for display in messages only — no longer used as a signal gate."""
    n = len(closes)
    rsi_out = np.full(n, 50.0)
    if n < period + 1:
        return rsi_out
    deltas = np.diff(closes)
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    rs = avg_gain / avg_loss if avg_loss > 0 else 100.0
    rsi_out[period] = 100 - (100 / (1 + rs))
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period-1) + gains[i-1]) / period
        avg_loss = (avg_loss * (period-1) + losses[i-1]) / period
        rs = avg_gain / avg_loss if avg_loss > 0 else 100.0
        rsi_out[i] = 100 - (100 / (1 + rs))
    return rsi_out


def htf_trend(df_1h: pd.DataFrame) -> Tuple[str, str]:
    """
    FIX A + FIX D: Use iloc[::4] slice for 4H (matches backtester exactly).
    Requires 1000 1H bars (fetched above) so EMA200 on 4H is valid.

    FIX D: Removed resample('4h') — it had midnight-UTC anchor issues
    causing non-standard candle boundaries. iloc[::4] is deterministic.
    """
    if df_1h.empty or len(df_1h) < 200:
        log.warning(f"  htf_trend: insufficient 1H bars ({len(df_1h)}), returning NEUTRAL")
        return "NEUTRAL", "NEUTRAL"

    closes_1h = df_1h["Close"].values
    ema200_1h = ema_series(closes_1h, 200)
    trend_1h = ("BULLISH" if closes_1h[-1] > ema200_1h[-1]
                else "BEARISH") if not np.isnan(ema200_1h[-1]) else "NEUTRAL"

    # FIX D: iloc[::4] — identical to backtester (no anchor issues)
    closes_4h = closes_1h[::4]
    if len(closes_4h) < 200:
        log.warning(f"  htf_trend: only {len(closes_4h)} 4H bars (need 200); 4H=NEUTRAL")
        return "NEUTRAL", trend_1h

    ema200_4h = ema_series(closes_4h, 200)
    trend_4h = ("BULLISH" if closes_4h[-1] > ema200_4h[-1]
                else "BEARISH") if not np.isnan(ema200_4h[-1]) else "NEUTRAL"

    return trend_4h, trend_1h


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL DETECTION  (v4.4)
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL DETECTION  (v4.5)
# ─────────────────────────────────────────────────────────────────────────────

def detect_signals(df: pd.DataFrame,
                   trend_4h: str, trend_1h: str) -> pd.DataFrame:
    """
    Two fully separated signal types — each has its own section,
    its own cooldown key, and its own Telegram message format.

    BULLISH_CROSSOVER / BEARISH_CROSSOVER:
      • EMA50 crosses EMA200 (correct direction)
      • Candle close confirms beyond both EMAs
      • ADX > 15
      • No session filter (fires 24h)
      • No volume check (removed)

    PULLBACK_LONG / PULLBACK_SHORT:
      • 4H + 1H trend aligned (both BULLISH or both BEARISH)
      • Price within 0.3% of EMA50
      • ADX > 15
      • Session filter applied (London + NY hours)
      • No RSI filter (removed)
      • No Fibonacci retracement check (removed)

    min_bar reduced from 301 to 205 (no longer needs PULLBACK_SWING_BARS=96),
    giving ~595 scannable bars from the 800-bar fetch.
    """
    closes = df["Close"].values
    highs  = df["High"].values
    lows   = df["Low"].values
    times  = df.index

    ema50_arr  = ema_series(closes, EMA_FAST)
    ema200_arr = ema_series(closes, EMA_SLOW)
    adx_arr    = adx_series(highs, lows, closes, 14)
    rsi_arr    = rsi_series(closes, 14)   # display only

    signals = []
    min_bar = EMA_SLOW + 5   # 205 — no swing bars needed any more

    for i in range(min_bar, len(df)):
        lcc = i - 1   # last closed candle — no look-ahead

        cur50  = ema50_arr[lcc];  prv50  = ema50_arr[lcc-1]
        cur200 = ema200_arr[lcc]; prv200 = ema200_arr[lcc-1]
        adx_val = adx_arr[lcc]
        rsi_val = rsi_arr[lcc]
        cur_px  = closes[lcc]

        if np.isnan(cur200) or np.isnan(cur50):
            continue

        # ADX gate — applies to both signal types (change 3: threshold = 15)
        if adx_val < ADX_MIN:
            continue

        # ── SIGNAL TYPE 1: EMA CROSSOVER ──────────────────────────────────────
        # No session filter — crossovers are momentum events, fire 24h
        bullish_cross = (prv50 < prv200) and (cur50 > cur200)
        bearish_cross = (prv50 > prv200) and (cur50 < cur200)

        if bullish_cross or bearish_cross:
            signal_type = "BULLISH_CROSSOVER" if bullish_cross else "BEARISH_CROSSOVER"
            is_long = bullish_cross

            # Candle must close beyond both EMAs to confirm the cross
            if CANDLE_CONFIRM:
                if is_long  and not (cur_px > cur50 and cur_px > cur200): continue
                if not is_long and not (cur_px < cur50 and cur_px < cur200): continue

            # Volume filter removed (change 2)

            entry = cur_px
            sl    = entry * (1 - 0.005) if is_long else entry * (1 + 0.005)
            tp    = entry * (1 + 0.010) if is_long else entry * (1 - 0.010)

            signals.append({
                "bar": lcc, "timestamp": str(times[lcc]),
                "signal_type": signal_type, "direction": 1 if is_long else -1,
                "entry": round(entry, 5), "sl": round(sl, 5), "tp": round(tp, 5),
                "adx": round(adx_val, 1), "rsi": round(rsi_val, 1),
                "ema50": round(cur50, 5), "ema200": round(cur200, 5),
            })
            # crossover bar does NOT also check for pullback
            continue

        # ── SIGNAL TYPE 2: PULLBACK TO EMA50 ──────────────────────────────────
        # Session filter kept — timing matters for mean-reversion entries
        if times[lcc].hour not in PULLBACK_ALLOWED_HOURS:
            continue

        # Requires confirmed HTF trend in both directions
        if trend_4h == "NEUTRAL" or trend_1h == "NEUTRAL":
            continue

        is_long_pb  = (trend_4h == "BULLISH" and trend_1h == "BULLISH")
        is_short_pb = (trend_4h == "BEARISH" and trend_1h == "BEARISH")

        if not (is_long_pb or is_short_pb):
            continue

        # Price must be near EMA50 (within 0.3%)
        if abs(cur_px - cur50) / cur50 > PULLBACK_PROXIMITY_PCT:
            continue

        # RSI filter removed (change 4)
        # Fibonacci retracement check removed (change 5)

        pb_type = "PULLBACK_LONG" if is_long_pb else "PULLBACK_SHORT"
        entry = cur_px
        sl    = entry * (1 - 0.005) if is_long_pb else entry * (1 + 0.005)
        tp    = entry * (1 + 0.010) if is_long_pb else entry * (1 - 0.010)

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


def format_crossover_message(pair: str, sig: dict,
                              trend_4h: str, trend_1h: str,
                              risk_pct: float) -> str:
    """Telegram message for EMA crossover signals."""
    stype  = sig["signal_type"]
    is_long = stype == "BULLISH_CROSSOVER"
    arrow  = "📈" if is_long else "📉"
    colour = "🟢" if is_long else "🔴"
    label  = "BULLISH CROSSOVER — LONG" if is_long else "BEARISH CROSSOVER — SHORT"
    cross  = "EMA50 crossed <b>above</b> EMA200 (Golden Cross)" if is_long \
             else "EMA50 crossed <b>below</b> EMA200 (Death Cross)"

    risk_amt   = START_CAPITAL * risk_pct / 100
    reward_amt = risk_amt * RR
    now_utc    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return f"""{arrow} <b>{pair} — {colour} {label}</b> {arrow}

━━━━━━━━━━━━━━━━━━━━━
⏰ <b>Timeframe:</b> 15-Min (last closed candle)
📊 <b>Trigger:</b> {cross}
━━━━━━━━━━━━━━━━━━━━━

📊 <b>Indicators</b>
• EMA50:  {fmt_price(sig['ema50'])}
• EMA200: {fmt_price(sig['ema200'])}
• ADX:    {sig['adx']}  (threshold: >{ADX_MIN})
• RSI:    {sig['rsi']}
• HTF:    4H {trend_4h} | 1H {trend_1h}

━━━━━━━━━━━━━━━━━━━━━
⚡ <b>Levels</b>
• Entry:       {fmt_price(sig['entry'])}
• Stop Loss:   {fmt_price(sig['sl'])}  (-0.5%)
• Take Profit: {fmt_price(sig['tp'])}  (+1.0%)
• R:R          1:{RR:.0f}

━━━━━━━━━━━━━━━━━━━━━
💼 <b>Risk (€{START_CAPITAL:,.0f} capital @ {risk_pct}%)</b>
• Risk:   €{risk_amt:.2f}
• Reward: €{reward_amt:.2f}

⏰ {now_utc}
⚠️ <i>Educational purposes only. Not financial advice.</i>"""


def format_pullback_message(pair: str, sig: dict,
                             trend_4h: str, trend_1h: str,
                             risk_pct: float) -> str:
    """Telegram message for pullback-to-EMA50 signals."""
    stype   = sig["signal_type"]
    is_long = stype == "PULLBACK_LONG"
    arrow   = "📈" if is_long else "📉"
    colour  = "🟢" if is_long else "🔴"
    label   = "PULLBACK LONG" if is_long else "PULLBACK SHORT"
    trend_d = "uptrend" if is_long else "downtrend"

    risk_amt   = START_CAPITAL * risk_pct / 100
    reward_amt = risk_amt * RR
    now_utc    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return f"""{arrow} <b>{pair} — {colour} {label}</b> {arrow}

━━━━━━━━━━━━━━━━━━━━━
⏰ <b>Timeframe:</b> 15-Min (last closed candle)
📊 <b>Trigger:</b> Price pulled back to EMA50 in {trend_d}
📈 <b>Trend:</b> 4H {trend_4h} | 1H {trend_1h}
━━━━━━━━━━━━━━━━━━━━━

📊 <b>Indicators</b>
• EMA50:  {fmt_price(sig['ema50'])}
• EMA200: {fmt_price(sig['ema200'])}
• ADX:    {sig['adx']}  (threshold: >{ADX_MIN})
• RSI:    {sig['rsi']}  (display only)

━━━━━━━━━━━━━━━━━━━━━
⚡ <b>Levels</b>
• Entry:       {fmt_price(sig['entry'])}
• Stop Loss:   {fmt_price(sig['sl'])}  (-0.5%)
• Take Profit: {fmt_price(sig['tp'])}  (+1.0%)
• R:R          1:{RR:.0f}

━━━━━━━━━━━━━━━━━━━━━
💼 <b>Risk (€{START_CAPITAL:,.0f} capital @ {risk_pct}%)</b>
• Risk:   €{risk_amt:.2f}
• Reward: €{reward_amt:.2f}

⏰ {now_utc}
⚠️ <i>Educational purposes only. Not financial advice.</i>"""


def format_signal_message(pair: str, sig: dict,
                           trend_4h: str, trend_1h: str,
                           risk_pct: float) -> str:
    """Route to the correct formatter based on signal type."""
    if sig["signal_type"] in ("BULLISH_CROSSOVER", "BEARISH_CROSSOVER"):
        return format_crossover_message(pair, sig, trend_4h, trend_1h, risk_pct)
    return format_pullback_message(pair, sig, trend_4h, trend_1h, risk_pct)


# ─────────────────────────────────────────────────────────────────────────────
# HEALTH
# ─────────────────────────────────────────────────────────────────────────────

def update_health(status: str, detail: str = "") -> None:
    h = load_json(HEALTH_FILE)
    now = datetime.now(timezone.utc)
    h["last_run"]    = now.isoformat()
    h["status"]      = status
    h["last_detail"] = detail
    h["run_count"]   = h.get("run_count", 0) + 1
    if status == "error":
        h["error_count"]       = h.get("error_count", 0) + 1
        h["last_error"]        = now.isoformat()
        h["last_error_detail"] = detail
    save_json(HEALTH_FILE, h)


def should_send_daily_report() -> bool:
    now = datetime.now(timezone.utc)
    if now.hour != 9 or now.minute > 14:
        return False
    h = load_json(HEALTH_FILE)
    last = h.get("last_daily_report")
    if not last:
        return True
    return (now - datetime.fromisoformat(last)).total_seconds() > 23 * 3600


def send_daily_report() -> None:
    h = load_json(HEALTH_FILE)
    tracker = load_json(TRACKER_FILE)
    now = datetime.now(timezone.utc)
    runs   = h.get("run_count", 0)
    errors = h.get("error_count", 0)
    sr     = round((runs - errors) / runs * 100, 1) if runs else 100
    cutoff = now.timestamp() - 86400
    recent = [k for k, v in tracker.items() if v > cutoff]
    status = "✅ HEALTHY" if errors == 0 else ("⚠️ ISSUES" if errors < 3 else "🔴 DEGRADED")

    lines = [f"📊 <b>BOT DAILY REPORT — {now.strftime('%Y-%m-%d')}</b>",
             "━━━━━━━━━━━━━━━━━━━━━",
             f"📈 Status: {status}",
             f"🔄 Runs: {runs}  |  Errors: {errors}  |  Uptime: {sr}%",
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
              "📡 Pairs: GOLD · EUR/USD · GBP/USD",
              "⏰ Next report: tomorrow 09:00 UTC",
              "<i>🤖 EMA Signal Bot v4.4</i>"]

    send_telegram("\n".join(lines), admin=True)
    h["last_daily_report"] = now.isoformat()
    save_json(HEALTH_FILE, h)


def send_error_alert(pair: str, error: str) -> None:
    h = load_json(HEALTH_FILE)
    if (datetime.now(timezone.utc).timestamp() - h.get("last_error_alert", 0)) < 300:
        return
    msg = (f"⚠️ <b>BOT ERROR</b>\n\n"
           f"📅 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
           f"📊 Pair: {pair}\n❌ {error[:300]}\n\n"
           f"<i>Bot will retry on next cron run.</i>")
    send_telegram(msg, admin=True)
    h["last_error_alert"] = datetime.now(timezone.utc).timestamp()
    save_json(HEALTH_FILE, h)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN SCAN
# ─────────────────────────────────────────────────────────────────────────────

def scan_pair(pair: str, symbol: str, api_key: str, risk_pct: float) -> int:
    log.info(f"\n{'='*45}\n🔍 {pair} ({symbol})")

    df = fetch_15min(symbol, api_key, outputsize=800)
    if len(df) < EMA_SLOW + 20:
        log.warning(f"  Not enough bars ({len(df)}), skipping.")
        return 0
    time.sleep(8)

    df_1h = fetch_1h(symbol, api_key, outputsize=1000)
    trend_4h, trend_1h = htf_trend(df_1h)
    log.info(f"  HTF: 4H={trend_4h}, 1H={trend_1h}")
    time.sleep(8)

    signals_df = detect_signals(df, trend_4h, trend_1h)
    if signals_df.empty:
        log.info(f"  No signals detected.")
        return 0

    max_closed_bar = len(df) - 2
    min_fresh_bar  = max_closed_bar - SIGNAL_FRESHNESS_BARS
    fresh = signals_df[signals_df["bar"] >= min_fresh_bar]

    if fresh.empty:
        log.info(f"  {len(signals_df)} signals found but none fresh "
                 f"(last bar={signals_df['bar'].max()}, need >={min_fresh_bar}).")
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
            log.error("  ❌ Failed to send Telegram message.")
    return sent


def main() -> None:
    log.info("=" * 55)
    log.info("  EMA Signal Bot v4.5 — cron run starting")
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
        time.sleep(5)

    update_health("ok" if errors == 0 else "partial_error",
                  f"sent={total_sent} errors={errors}")
    log.info("=" * 55)
    log.info(f"  Done — signals sent: {total_sent} | errors: {errors}")
    log.info("=" * 55)


if __name__ == "__main__":
    main()
