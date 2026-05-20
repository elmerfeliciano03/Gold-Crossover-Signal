#!/usr/bin/env python3
"""
EMA Crossover + Pullback Signal Bot — Yahoo Finance edition
============================================================
Signal logic: v4.5 (unchanged from Twelve Data version)

WHY YAHOO FINANCE:
  Twelve Data free tier = 800 credits/day. Each bot run uses:
    3 pairs × (800 15min bars + 1000 1H bars) → exhausted after GOLD.
  yfinance: unlimited, free, no API key, no rate limits.

SYMBOL MAPPING:
  GOLD   → GC=F      (Gold futures continuous contract)
  EURUSD → EURUSD=X  (EUR/USD spot)
  GBPUSD → GBPUSD=X  (GBP/USD spot)

DATA AVAILABILITY (yfinance):
  15min: up to 60 days  (~4,600 bars — far more than the 800 we need)
  1H:    up to 730 days (~17,500 bars — HTF EMA200 easily satisfied)

VOLUME NOTE:
  GC=F (gold futures) has real volume.
  EURUSD=X / GBPUSD=X volume is tick count, not traded volume — meaningless.
  Volume filter is disabled for all pairs (v4.5 already removed it).

UNCHANGED FROM v4.5:
  - Signal logic: EMA50×EMA200 crossover + pullback to EMA50
  - ADX > 15, candle confirm, session filter on pullbacks
  - No RSI / no Fibonacci / no volume filter
  - Breakeven stop, 1h cooldown, health report, error alerts
  - Separate Telegram message formats for crossover vs pullback

Deployment: push to GitHub → connect to Render as Cron Job
  Schedule:  */15 * * * *
  Command:   python bot.py
  Build cmd: pip install -r requirements.txt

Required env vars (set in Render dashboard):
  TELEGRAM_TOKEN
  TELEGRAM_CHAT_ID
  TELEGRAM_ADMIN_CHAT_ID  (optional — falls back to TELEGRAM_CHAT_ID)
"""

import os
import json
import time
import logging
import numpy as np
import pandas as pd
import yfinance as yf
import requests
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── ENVIRONMENT ───────────────────────────────────────────────────────────────
TELEGRAM_TOKEN         = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID       = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", TELEGRAM_CHAT_ID)

START_CAPITAL = 2000.0
RR            = 2.0

# ── PAIRS — (yf_symbol, display_name, risk_pct) ───────────────────────────────
PAIRS = {
    "GOLD":   ("GC=F",      "XAU/USD", 1.50),
    "EURUSD": ("EURUSD=X",  "EUR/USD", 1.00),
    "GBPUSD": ("GBPUSD=X",  "GBP/USD", 1.00),
}

# ── SIGNAL PARAMETERS (v4.5 — exact) ─────────────────────────────────────────
EMA_FAST               = 50
EMA_SLOW               = 200
ADX_MIN                = 15
CANDLE_CONFIRM         = True
PULLBACK_PROXIMITY_PCT = 0.003
SIGNAL_FRESHNESS_BARS  = 2
SIGNAL_COOLDOWN_HRS    = 1

PULLBACK_ALLOWED_HOURS = {6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 21, 22}

TRACKER_FILE = "/tmp/signal_tracker.json"
HEALTH_FILE  = "/tmp/health_tracker.json"


# ─────────────────────────────────────────────────────────────────────────────
# DATA FETCHING — Yahoo Finance
# ─────────────────────────────────────────────────────────────────────────────

def fetch_15min(yf_symbol: str) -> pd.DataFrame:
    """
    Fetch 15-min bars via yfinance.
    period="60d" gives the maximum available (~60 days, ~4,600 bars).
    Much better than the 800-bar Twelve Data free-tier window.
    """
    try:
        df = yf.download(
            yf_symbol,
            interval="15m",
            period="60d",
            auto_adjust=True,
            progress=False,
            multi_level_index=False,
        )
        if df.empty:
            log.error(f"  No 15min data returned for {yf_symbol}")
            return pd.DataFrame()

        df.index = pd.to_datetime(df.index)
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)

        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
        df = df.sort_index()
        log.info(f"  ✅ 15min: {len(df)} bars  "
                 f"({df.index[0].date()} → {df.index[-1].date()})")
        return df
    except Exception as e:
        log.error(f"  15min fetch error ({yf_symbol}): {e}")
        return pd.DataFrame()


def fetch_1h(yf_symbol: str) -> pd.DataFrame:
    """
    Fetch 1H bars via yfinance.
    period="2y" gives up to 730 days — EMA200 on 4H easily satisfied.
    """
    try:
        df = yf.download(
            yf_symbol,
            interval="1h",
            period="2y",
            auto_adjust=True,
            progress=False,
            multi_level_index=False,
        )
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
        s = pdi + mdi
        dx[i] = 100 * abs(pdi - mdi) / s if s > 0 else 0
    adx_out[period*2] = dx[period:period*2].mean()
    for i in range(period*2+1, n):
        adx_out[i] = (adx_out[i-1] * (period-1) + dx[i]) / period
    return adx_out


def rsi_series(closes: np.ndarray, period: int = 14) -> np.ndarray:
    """Display only — not used as a signal gate."""
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


def htf_trend(df_1h: pd.DataFrame) -> tuple[str, str]:
    """
    4H + 1H trend via iloc[::4] slice.
    yfinance 2y of 1H data gives ~17,500 bars → 4,375 4H bars.
    EMA200 on 4H has ~4,175 bars of warm signal — very reliable.
    """
    if df_1h.empty or len(df_1h) < 200:
        log.warning(f"  htf_trend: only {len(df_1h)} 1H bars, returning NEUTRAL")
        return "NEUTRAL", "NEUTRAL"

    closes_1h = df_1h["Close"].values
    ema200_1h = ema_series(closes_1h, 200)
    trend_1h = ("BULLISH" if closes_1h[-1] > ema200_1h[-1]
                else "BEARISH") if not np.isnan(ema200_1h[-1]) else "NEUTRAL"

    closes_4h = closes_1h[::4]
    if len(closes_4h) < 200:
        log.warning(f"  htf_trend: only {len(closes_4h)} 4H bars, 4H=NEUTRAL")
        return "NEUTRAL", trend_1h

    ema200_4h = ema_series(closes_4h, 200)
    trend_4h = ("BULLISH" if closes_4h[-1] > ema200_4h[-1]
                else "BEARISH") if not np.isnan(ema200_4h[-1]) else "NEUTRAL"

    return trend_4h, trend_1h


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL DETECTION  (v4.5 — unchanged)
# ─────────────────────────────────────────────────────────────────────────────

def detect_signals(df: pd.DataFrame,
                   trend_4h: str, trend_1h: str) -> pd.DataFrame:
    closes = df["Close"].values
    highs  = df["High"].values
    lows   = df["Low"].values
    times  = df.index

    ema50_arr  = ema_series(closes, EMA_FAST)
    ema200_arr = ema_series(closes, EMA_SLOW)
    adx_arr    = adx_series(highs, lows, closes, 14)
    rsi_arr    = rsi_series(closes, 14)

    signals = []
    min_bar = EMA_SLOW + 5   # 205

    for i in range(min_bar, len(df)):
        lcc = i - 1

        cur50  = ema50_arr[lcc];  prv50  = ema50_arr[lcc-1]
        cur200 = ema200_arr[lcc]; prv200 = ema200_arr[lcc-1]
        adx_val = adx_arr[lcc]
        rsi_val = rsi_arr[lcc]
        cur_px  = closes[lcc]

        if np.isnan(cur200) or np.isnan(cur50):
            continue
        if adx_val < ADX_MIN:
            continue

        # ── EMA Crossover (no session filter) ─────────────────────────────────
        bullish_cross = (prv50 < prv200) and (cur50 > cur200)
        bearish_cross = (prv50 > prv200) and (cur50 < cur200)

        if bullish_cross or bearish_cross:
            signal_type = "BULLISH_CROSSOVER" if bullish_cross else "BEARISH_CROSSOVER"
            is_long = bullish_cross

            if CANDLE_CONFIRM:
                if is_long  and not (cur_px > cur50 and cur_px > cur200): continue
                if not is_long and not (cur_px < cur50 and cur_px < cur200): continue

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
            continue

        # ── Pullback to EMA50 (session filter applied) ─────────────────────────
        if times[lcc].hour not in PULLBACK_ALLOWED_HOURS:
            continue
        if trend_4h == "NEUTRAL" or trend_1h == "NEUTRAL":
            continue

        is_long_pb  = (trend_4h == "BULLISH" and trend_1h == "BULLISH")
        is_short_pb = (trend_4h == "BEARISH" and trend_1h == "BEARISH")
        if not (is_long_pb or is_short_pb):
            continue

        if abs(cur_px - cur50) / cur50 > PULLBACK_PROXIMITY_PCT:
            continue

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
# MESSAGE FORMATTING
# ─────────────────────────────────────────────────────────────────────────────

def fmt_price(price: float) -> str:
    return f"{price:.5f}" if price < 10 else f"{price:.2f}"


def format_crossover_message(pair: str, display: str, sig: dict,
                              trend_4h: str, trend_1h: str,
                              risk_pct: float) -> str:
    is_long   = sig["signal_type"] == "BULLISH_CROSSOVER"
    arrow     = "📈" if is_long else "📉"
    colour    = "🟢" if is_long else "🔴"
    label     = "BULLISH CROSSOVER — LONG" if is_long else "BEARISH CROSSOVER — SHORT"
    cross     = "EMA50 crossed <b>above</b> EMA200 (Golden Cross)" if is_long \
                else "EMA50 crossed <b>below</b> EMA200 (Death Cross)"
    risk_amt  = START_CAPITAL * risk_pct / 100
    now_utc   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return f"""{arrow} <b>{pair} ({display}) — {colour} {label}</b> {arrow}

━━━━━━━━━━━━━━━━━━━━━
⏰ <b>Timeframe:</b> 15-Min (last closed candle)
📊 <b>Trigger:</b> {cross}
━━━━━━━━━━━━━━━━━━━━━

📊 <b>Indicators</b>
• EMA50:  {fmt_price(sig['ema50'])}
• EMA200: {fmt_price(sig['ema200'])}
• ADX:    {sig['adx']}
• RSI:    {sig['rsi']}
• HTF:    4H {trend_4h} | 1H {trend_1h}

━━━━━━━━━━━━━━━━━━━━━
⚡ <b>Levels</b>
• Entry:       {fmt_price(sig['entry'])}
• Stop Loss:   {fmt_price(sig['sl'])}  (-0.5%)
• Take Profit: {fmt_price(sig['tp'])}  (+1.0%)
• R:R          1:{RR:.0f}

━━━━━━━━━━━━━━━━━━━━━
💼 <b>Risk (€{START_CAPITAL:,.0f} @ {risk_pct}%)</b>
• Risk:   €{risk_amt:.2f}
• Reward: €{risk_amt * RR:.2f}

⏰ {now_utc}
⚠️ <i>Educational purposes only. Not financial advice.</i>"""


def format_pullback_message(pair: str, display: str, sig: dict,
                             trend_4h: str, trend_1h: str,
                             risk_pct: float) -> str:
    is_long  = sig["signal_type"] == "PULLBACK_LONG"
    arrow    = "📈" if is_long else "📉"
    colour   = "🟢" if is_long else "🔴"
    label    = "PULLBACK LONG" if is_long else "PULLBACK SHORT"
    trend_d  = "uptrend" if is_long else "downtrend"
    risk_amt = START_CAPITAL * risk_pct / 100
    now_utc  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return f"""{arrow} <b>{pair} ({display}) — {colour} {label}</b> {arrow}

━━━━━━━━━━━━━━━━━━━━━
⏰ <b>Timeframe:</b> 15-Min (last closed candle)
📊 <b>Trigger:</b> Price pulled back to EMA50 in {trend_d}
📈 <b>Trend:</b> 4H {trend_4h} | 1H {trend_1h}
━━━━━━━━━━━━━━━━━━━━━

📊 <b>Indicators</b>
• EMA50:  {fmt_price(sig['ema50'])}
• EMA200: {fmt_price(sig['ema200'])}
• ADX:    {sig['adx']}
• RSI:    {sig['rsi']}  (display only)

━━━━━━━━━━━━━━━━━━━━━
⚡ <b>Levels</b>
• Entry:       {fmt_price(sig['entry'])}
• Stop Loss:   {fmt_price(sig['sl'])}  (-0.5%)
• Take Profit: {fmt_price(sig['tp'])}  (+1.0%)
• R:R          1:{RR:.0f}

━━━━━━━━━━━━━━━━━━━━━
💼 <b>Risk (€{START_CAPITAL:,.0f} @ {risk_pct}%)</b>
• Risk:   €{risk_amt:.2f}
• Reward: €{risk_amt * RR:.2f}

⏰ {now_utc}
⚠️ <i>Educational purposes only. Not financial advice.</i>"""


def format_signal_message(pair: str, display: str, sig: dict,
                           trend_4h: str, trend_1h: str,
                           risk_pct: float) -> str:
    if sig["signal_type"] in ("BULLISH_CROSSOVER", "BEARISH_CROSSOVER"):
        return format_crossover_message(pair, display, sig, trend_4h, trend_1h, risk_pct)
    return format_pullback_message(pair, display, sig, trend_4h, trend_1h, risk_pct)


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
             f"🔔 Signals sent (24h): {len(recent)}",
             f"📡 Data: Yahoo Finance (free, unlimited)"]
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
              "<i>🤖 EMA Signal Bot v4.5 (Yahoo Finance)</i>"]

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

def scan_pair(pair: str, yf_symbol: str, display: str, risk_pct: float) -> int:
    log.info(f"\n{'='*45}\n🔍 {pair} ({yf_symbol} → {display})")

    df = fetch_15min(yf_symbol)
    if len(df) < EMA_SLOW + 20:
        log.warning(f"  Not enough 15min bars ({len(df)}), skipping.")
        return 0

    df_1h = fetch_1h(yf_symbol)
    trend_4h, trend_1h = htf_trend(df_1h)
    log.info(f"  HTF: 4H={trend_4h}, 1H={trend_1h}")

    signals_df = detect_signals(df, trend_4h, trend_1h)
    if signals_df.empty:
        log.info(f"  No signals detected.")
        return 0

    max_closed_bar = len(df) - 2
    min_fresh_bar  = max_closed_bar - SIGNAL_FRESHNESS_BARS
    fresh = signals_df[signals_df["bar"] >= min_fresh_bar]

    if fresh.empty:
        log.info(f"  {len(signals_df)} signals found but none fresh "
                 f"(last={signals_df['bar'].max()}, need>={min_fresh_bar}).")
        return 0

    sent = 0
    for _, sig in fresh.iterrows():
        stype = sig["signal_type"]
        log.info(f"  🔔 Fresh signal: {stype} at {sig['timestamp']}")
        if signal_on_cooldown(pair, stype):
            continue
        msg = format_signal_message(pair, display, sig.to_dict(),
                                    trend_4h, trend_1h, risk_pct)
        if send_telegram(msg):
            mark_signal_sent(pair, stype)
            sent += 1
            log.info(f"  ✅ Sent {stype} for {pair}")
        else:
            log.error("  ❌ Failed to send Telegram message.")
    return sent


def main() -> None:
    log.info("=" * 55)
    log.info("  EMA Signal Bot v4.5 (Yahoo Finance) — cron run")
    log.info(f"  UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("=" * 55)

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set. Exiting.")
        return

    update_health("running", "cron started")

    if should_send_daily_report():
        log.info("📊 Sending daily health report…")
        send_daily_report()

    total_sent = 0
    errors     = 0

    for pair, (yf_symbol, display, risk_pct) in PAIRS.items():
        try:
            sent = scan_pair(pair, yf_symbol, display, risk_pct)
            total_sent += sent
        except Exception as e:
            errors += 1
            log.error(f"  ❌ {pair}: {e}", exc_info=True)
            send_error_alert(pair, str(e))
            update_health("error", str(e))
        time.sleep(2)   # small courtesy delay between yfinance calls

    update_health("ok" if errors == 0 else "partial_error",
                  f"sent={total_sent} errors={errors}")
    log.info("=" * 55)
    log.info(f"  Done — signals sent: {total_sent} | errors: {errors}")
    log.info("=" * 55)


if __name__ == "__main__":
    main()
