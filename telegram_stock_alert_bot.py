"""
US Stock Screener + Telegram Alert Bot — Intraday / Short-Term Breakout Edition
--------------------------------------------------------------------------------
Built for FAST in-and-out trades: buy near a short-term (last ~2 hour) breakout
level, take a quick profit, use a tight ATR-based stop. All indicators are
computed from 15-minute intraday candles (not daily), so the "kırılma" level
stays close to the current price and reflects the last couple of hours, not
weeks-old highs.

This is a technical screener, not a prediction engine. No co"""
US Stock Screener + Telegram Alert Bot — Intraday / Short-Term Breakout Edition
--------------------------------------------------------------------------------
Built for FAST in-and-out trades: buy near a short-term (last ~2 hour) breakout
level, take a quick profit, use a tight ATR-based stop. All indicators are
computed from 15-minute intraday candles (not daily), so the "kırılma" level
stays close to the current price and reflects the last couple of hours, not
weeks-old highs.

This is a technical screener, not a prediction engine. No combination of
indicators reliably predicts what a stock does in the next few minutes.
Fast, breakout-style trading is high-risk and can lose money quickly —
size positions accordingly. This is not financial advice.

IMPORTANT HONESTY NOTE
Free-tier data (Twelve Data free plan + GitHub Actions scheduling) has a
real-world latency of several minutes, not seconds. This bot is tuned for
"short-term, but still realistic" alerts — not literal second-by-second
scalping. True scalping needs paid real-time data, a much faster loop, and
usually automated order execution (this bot only sends Telegram messages;
you still act manually).

TWO RUN MODES
- RUN_MODE=loop (default): runs forever, checking every CYCLE_SECONDS.
  Use this on your own computer or a VPS that stays on.
- RUN_MODE=once: does a single pass then exits. Used by the included
  GitHub Actions workflow, which triggers this on a schedule. Alert
  cooldown state is saved to STATE_FILE (state.json) so repeat alerts
  are suppressed across separate runs.

FREE-PLAN CONSTRAINT (Twelve Data)
The free tier allows roughly 8 API credits/minute and 800/day. With an
8-ticker WATCHLIST queried every ~5 minutes during actual market hours
(~390 min/day), that's about 8 * (390/5) ≈ 624 requests/day — under the
800 quota, with some room to spare. If you grow WATCHLIST or shrink the
schedule interval, re-check this math. 429 errors or "run out of API
credits" in the log mean you need to back off.

SETUP
1. pip install -r requirements.txt
2. Telegram: message @BotFather -> /newbot -> copy the token. Message
   your new bot once, then visit
   https://api.telegram.org/bot<TOKEN>/getUpdates to find your chat_id.
3. Twelve Data: copy your API key from
   https://twelvedata.com/account/api-keys
4. Set TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, TWELVEDATA_API_KEY as
   environment variables (locally) or as GitHub Actions repo secrets —
   never hardcode them in this file.
5. Edit WATCHLIST below if you want different tickers.
6. Local/VPS use:      python telegram_stock_alert_bot.py
   GitHub Actions use: RUN_MODE=once python telegram_stock_alert_bot.py
   (the included workflow file sets RUN_MODE=once for you automatically)
"""

import os
import json
import time
import logging
from datetime import datetime, time as dtime

import numpy as np
import pandas as pd
import requests
import yfinance as yf
import pytz

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "PUT_YOUR_TELEGRAM_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "PUT_YOUR_CHAT_ID_HERE")
TWELVEDATA_API_KEY = os.environ.get("TWELVEDATA_API_KEY", "PUT_YOUR_TWELVEDATA_KEY_HERE")

RUN_MODE = os.environ.get("RUN_MODE", "loop")   # "loop" (VPS/local) or "once" (cron/GitHub Actions)
STATE_FILE = os.environ.get("STATE_FILE", "state.json")

# Keep this short on Twelve Data's free plan — see FREE-PLAN CONSTRAINT above
WATCHLIST = ["AAPL", "MSFT", "NVDA", "AMZN", "TSLA", "AMD", "META", "GOOGL"]

# --- Intraday indicator settings (all computed on 15-minute candles) ---
INTRADAY_INTERVAL = "15m"
INTRADAY_PERIOD = "5d"          # yfinance history window to pull (free tier allows up to 60d @ 15m)
SWING_LOOKBACK_BARS = 8         # ~2 hours — this defines the short-term "kırılma" (breakout) level
MA_SHORT_BARS = 20              # ~5 hours
MA_LONG_BARS = 50               # ~12.5 hours (roughly 2 sessions)
ATR_PERIOD = 14                 # ~3.5 hours of volatility
VOLUME_LOOKBACK_BARS = 20

NEAR_BREAKOUT_PCT = 0.02        # only alert if price is within 2% of the short-term swing high
STOP_ATR_MULT = 1.2             # tighter stop, suited to fast trades
TARGET_RR = 1.5                 # take-profit at 1.5x the risk (quicker, more achievable exit)

CYCLE_SECONDS = 120              # loop mode only: ~2 min per full pass
ONCE_MODE_PACING_SECONDS = 1.5   # once mode only: small gap between quote calls
INDICATOR_REFRESH_MINUTES = 10   # loop mode only: how often intraday indicators are recomputed
SCORE_ALERT_THRESHOLD = 3        # signals out of 5 needed to trigger an alert
ALERT_COOLDOWN_MINUTES = 10      # min gap between repeat alerts for the SAME ticker

MARKET_TZ = pytz.timezone("America/New_York")
MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)

_last_alerted = {}         # ticker -> unix timestamp of last alert (persisted to STATE_FILE)
_indicators = {}           # ticker -> dict of intraday RSI/MA/ATR/swing levels
_indicators_updated_at = 0.0


def load_state():
    global _last_alerted
    try:
        with open(STATE_FILE, "r") as f:
            _last_alerted = json.load(f)
        log.info("Loaded state for %d tickers from %s", len(_last_alerted), STATE_FILE)
    except (FileNotFoundError, json.JSONDecodeError):
        _last_alerted = {}


def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(_last_alerted, f)
    except Exception as e:
        log.error("Could not save state: %s", e)


def is_market_open() -> bool:
    now = datetime.now(MARKET_TZ)
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def send_telegram(message: str) -> None:
    if "PUT_YOUR" in TELEGRAM_TOKEN or "PUT_YOUR" in TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured -- printing instead:\n%s", message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        log.error("Telegram send failed: %s", e)


def compute_rsi(series, period: int = 14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def compute_atr(df, period: int = 14):
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.rolling(period).mean()


def refresh_indicators():
    """Recompute RSI/MA/ATR/swing levels from 15-min intraday candles (yfinance, free)."""
    global _indicators, _indicators_updated_at
    log.info("Refreshing intraday (%s) indicators for %d tickers...", INTRADAY_INTERVAL, len(WATCHLIST))
    for t in WATCHLIST:
        try:
            df = yf.download(
                t, period=INTRADAY_PERIOD, interval=INTRADAY_INTERVAL, progress=False, auto_adjust=True
            )
            min_bars = max(MA_LONG_BARS, ATR_PERIOD, VOLUME_LOOKBACK_BARS) + 2
            if df is None or len(df) < min_bars:
                log.warning("Not enough intraday bars for %s (%s)", t, None if df is None else len(df))
                continue

            close, volume = df["Close"], df["Volume"]
            rsi = float(compute_rsi(close, 14).iloc[-1])
            ma_short = float(close.rolling(MA_SHORT_BARS).mean().iloc[-1])
            ma_long = float(close.rolling(MA_LONG_BARS).mean().iloc[-1])
            atr = float(compute_atr(df, ATR_PERIOD).iloc[-1])
            swing_high = float(df["High"].rolling(SWING_LOOKBACK_BARS).max().iloc[-1])
            swing_low = float(df["Low"].rolling(SWING_LOOKBACK_BARS).min().iloc[-1])

            # Volume spike = latest completed bar's volume vs. recent average bar volume
            # (apples-to-apples, unlike comparing to a live cumulative day total).
            avg_bar_volume = float(volume.rolling(VOLUME_LOOKBACK_BARS).mean().iloc[-2])
            last_bar_volume = float(volume.iloc[-2])  # -2: last fully closed bar, not the forming one
            volume_ratio = (last_bar_volume / avg_bar_volume) if avg_bar_volume else 0.0

            _indicators[t] = {
                "rsi": rsi, "ma_short": ma_short, "ma_long": ma_long, "atr": atr,
                "swing_high": swing_high, "swing_low": swing_low, "volume_ratio": volume_ratio,
            }
        except Exception as e:
            log.error("Indicator refresh failed for %s: %s", t, e)
        time.sleep(0.3)
    _indicators_updated_at = time.time()


def get_live_quote(ticker: str):
    """One Twelve Data /quote call. Costs ~1 API credit on the free plan."""
    url = "https://api.twelvedata.com/quote"
    try:
        r = requests.get(url, params={"symbol": ticker, "apikey": TWELVEDATA_API_KEY}, timeout=10)
        data = r.json()
        price = data.get("close") or data.get("price")
        if price is None:
            log.warning("Twelve Data response for %s: %s", ticker, data)
            return None
        return {
            "price": float(price),
            "percent_change": float(data.get("percent_change", 0) or 0),
        }
    except Exception as e:
        log.error("Twelve Data quote failed for %s: %s", ticker, e)
        return None


def should_alert(ticker: str) -> bool:
    now = time.time()
    last = _last_alerted.get(ticker)
    if last and (now - last) < ALERT_COOLDOWN_MINUTES * 60:
        return False
    _last_alerted[ticker] = now
    return True


def evaluate(ticker: str, quote: dict):
    ind = _indicators.get(ticker)
    if not ind:
        return None

    price = quote["price"]
    signals = []
    score = 0

    if 30 < ind["rsi"] < 60:
        score += 1
        signals.append(f"RSI {ind['rsi']:.1f} (kısa vadede toparlanma)")

    if ind["volume_ratio"] > 1.5:
        score += 1
        signals.append(f"Son 15dk hacmi ortalamanın {ind['volume_ratio']:.1f}x üstünde")

    if price > ind["ma_short"]:
        score += 1
        signals.append("Fiyat kısa vadeli ortalamanın üstünde (~5 saat)")

    if not np.isnan(ind["ma_long"]) and ind["ma_short"] > ind["ma_long"]:
        score += 1
        signals.append("Kısa ortalama > uzun ortalama (yükseliş yönü)")

    if quote["percent_change"] > 1.0:
        score += 1
        signals.append(f"Bugün +%{quote['percent_change']:.1f}")

    # Short-term breakout level = the last ~2 hours' high. Only actionable if
    # price is actually close to it right now — otherwise skip entirely so we
    # never suggest an entry that's far from the current market price.
    distance_pct = abs(price - ind["swing_high"]) / ind["swing_high"]
    if distance_pct > NEAR_BREAKOUT_PCT:
        return None

    entry = max(price, ind["swing_high"])
    stop = round(entry - STOP_ATR_MULT * ind["atr"], 2)
    target = round(entry + TARGET_RR * (entry - stop), 2)

    return {
        "ticker": ticker, "score": score, "signals": signals, "price": price,
        "entry": round(entry, 2), "stop": stop, "target": target,
    }


def run_cycle(pacing_seconds=None):
    if not _indicators or (time.time() - _indicators_updated_at) > INDICATOR_REFRESH_MINUTES * 60:
        refresh_indicators()

    gap = pacing_seconds if pacing_seconds is not None else CYCLE_SECONDS / max(len(WATCHLIST), 1)
    for t in WATCHLIST:
        quote = get_live_quote(t)
        if quote:
            result = evaluate(t, quote)
            if result and result["score"] >= SCORE_ALERT_THRESHOLD and should_alert(t):
                msg = (
                    f"*{result['ticker']}* — {result['score']}/5 sinyal @ ${result['price']:.2f}\n"
                    + "\n".join(f"• {s}" for s in result["signals"]) + "\n\n"
                    f"📈 Giriş / kırılma (~2 saatlik tepe): *${result['entry']}*\n"
                    f"🛑 Stop-loss: *${result['stop']}*\n"
                    f"🎯 Hızlı hedef (1:{TARGET_RR}): *${result['target']}*\n\n"
                    "_Kısa vadeli teknik hesaplama. Yatırım tavsiyesi değildir; hızlı hareket ettiğin için riski küçük tut._"
                )
                send_telegram(msg)
                log.info("Alert: %s score=%d", t, result["score"])
        time.sleep(gap)


def main():
    load_state()

    if RUN_MODE == "once":
        if is_market_open():
            refresh_indicators()
            run_cycle(pacing_seconds=ONCE_MODE_PACING_SECONDS)
        else:
            log.info("Piyasa kapalı, bu çalıştırmada işlem yok.")
        save_state()
        return

    # loop mode (local / VPS)
    log.info("Starting intraday short-term alert bot (%d tickers).", len(WATCHLIST))
    send_telegram("🤖 Bot başladı — kısa vadeli kırılımlar için taranıyor.")
    refresh_indicators()
    while True:
        if is_market_open():
            run_cycle()
            save_state()
        else:
            log.info("Piyasa kapalı, bekleniyor.")
            time.sleep(300)


if __name__ == "__main__":
    main()
mbination of
indicators reliably predicts what a stock does in the next few minutes.
Fast, breakout-style trading is high-risk and can lose money quickly —
size positions accordingly. This is not financial advice.

IMPORTANT HONESTY NOTE
Free-tier data (Twelve Data free plan + GitHub Actions scheduling) has a
real-world latency of several minutes, not seconds. This bot is tuned for
"short-term, but still realistic" alerts — not literal second-by-second
scalping. True scalping needs paid real-time data, a much faster loop, and
usually automated order execution (this bot only sends Telegram messages;
you still act manually).

TWO RUN MODES
- RUN_MODE=loop (default): runs forever, checking every CYCLE_SECONDS.
  Use this on your own computer or a VPS that stays on.
- RUN_MODE=once: does a single pass then exits. Used by the included
  GitHub Actions workflow, which triggers this on a schedule. Alert
  cooldown state is saved to STATE_FILE (state.json) so repeat alerts
  are suppressed across separate runs.

FREE-PLAN CONSTRAINT (Twelve Data)
The free tier allows roughly 8 API credits/minute and 800/day. With an
8-ticker WATCHLIST queried every ~5 minutes during actual market hours
(~390 min/day), that's about 8 * (390/5) ≈ 624 requests/day — under the
800 quota, with some room to spare. If you grow WATCHLIST or shrink the
schedule interval, re-check this math. 429 errors or "run out of API
credits" in the log mean you need to back off.

SETUP
1. pip install -r requirements.txt
2. Telegram: message @BotFather -> /newbot -> copy the token. Message
   your new bot once, then visit
   https://api.telegram.org/bot<TOKEN>/getUpdates to find your chat_id.
3. Twelve Data: copy your API key from
   https://twelvedata.com/account/api-keys
4. Set TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, TWELVEDATA_API_KEY as
   environment variables (locally) or as GitHub Actions repo secrets —
   never hardcode them in this file.
5. Edit WATCHLIST below if you want different tickers.
6. Local/VPS use:      python telegram_stock_alert_bot.py
   GitHub Actions use: RUN_MODE=once python telegram_stock_alert_bot.py
   (the included workflow file sets RUN_MODE=once for you automatically)
"""

import os
import json
import time
import logging
from datetime import datetime, time as dtime

import numpy as np
import pandas as pd
import requests
import yfinance as yf
import pytz

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "PUT_YOUR_TELEGRAM_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "PUT_YOUR_CHAT_ID_HERE")
TWELVEDATA_API_KEY = os.environ.get("TWELVEDATA_API_KEY", "PUT_YOUR_TWELVEDATA_KEY_HERE")

RUN_MODE = os.environ.get("RUN_MODE", "loop")   # "loop" (VPS/local) or "once" (cron/GitHub Actions)
STATE_FILE = os.environ.get("STATE_FILE", "state.json")

# Keep this short on Twelve Data's free plan — see FREE-PLAN CONSTRAINT above
WATCHLIST = ["AAPL", "MSFT", "NVDA", "AMZN", "TSLA", "AMD", "META", "GOOGL"]

# --- Intraday indicator settings (all computed on 15-minute candles) ---
INTRADAY_INTERVAL = "15m"
INTRADAY_PERIOD = "5d"          # yfinance history window to pull (free tier allows up to 60d @ 15m)
SWING_LOOKBACK_BARS = 8         # ~2 hours — this defines the short-term "kırılma" (breakout) level
MA_SHORT_BARS = 20              # ~5 hours
MA_LONG_BARS = 50               # ~12.5 hours (roughly 2 sessions)
ATR_PERIOD = 14                 # ~3.5 hours of volatility
VOLUME_LOOKBACK_BARS = 20

NEAR_BREAKOUT_PCT = 0.02        # only alert if price is within 2% of the short-term swing high
STOP_ATR_MULT = 1.2             # tighter stop, suited to fast trades
TARGET_RR = 1.5                 # take-profit at 1.5x the risk (quicker, more achievable exit)

CYCLE_SECONDS = 120              # loop mode only: ~2 min per full pass
ONCE_MODE_PACING_SECONDS = 1.5   # once mode only: small gap between quote calls
INDICATOR_REFRESH_MINUTES = 10   # loop mode only: how often intraday indicators are recomputed
SCORE_ALERT_THRESHOLD = 3        # signals out of 5 needed to trigger an alert
ALERT_COOLDOWN_MINUTES = 10      # min gap between repeat alerts for the SAME ticker

MARKET_TZ = pytz.timezone("America/New_York")
MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)

_last_alerted = {}         # ticker -> unix timestamp of last alert (persisted to STATE_FILE)
_indicators = {}           # ticker -> dict of intraday RSI/MA/ATR/swing levels
_indicators_updated_at = 0.0


def load_state():
    global _last_alerted
    try:
        with open(STATE_FILE, "r") as f:
            _last_alerted = json.load(f)
        log.info("Loaded state for %d tickers from %s", len(_last_alerted), STATE_FILE)
    except (FileNotFoundError, json.JSONDecodeError):
        _last_alerted = {}


def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(_last_alerted, f)
    except Exception as e:
        log.error("Could not save state: %s", e)


def is_market_open() -> bool:
    now = datetime.now(MARKET_TZ)
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def send_telegram(message: str) -> None:
    if "PUT_YOUR" in TELEGRAM_TOKEN or "PUT_YOUR" in TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured -- printing instead:\n%s", message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        log.error("Telegram send failed: %s", e)


def compute_rsi(series, period: int = 14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def compute_atr(df, period: int = 14):
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.rolling(period).mean()


def refresh_indicators():
    """Recompute RSI/MA/ATR/swing levels from 15-min intraday candles (yfinance, free)."""
    global _indicators, _indicators_updated_at
    log.info("Refreshing intraday (%s) indicators for %d tickers...", INTRADAY_INTERVAL, len(WATCHLIST))
    for t in WATCHLIST:
        try:
            df = yf.download(
                t, period=INTRADAY_PERIOD, interval=INTRADAY_INTERVAL, progress=False, auto_adjust=True
            )
            min_bars = max(MA_LONG_BARS, ATR_PERIOD, VOLUME_LOOKBACK_BARS) + 2
            if df is None or len(df) < min_bars:
                log.warning("Not enough intraday bars for %s (%s)", t, None if df is None else len(df))
                continue

            close, volume = df["Close"], df["Volume"]
            rsi = float(compute_rsi(close, 14).iloc[-1])
            ma_short = float(close.rolling(MA_SHORT_BARS).mean().iloc[-1])
            ma_long = float(close.rolling(MA_LONG_BARS).mean().iloc[-1])
            atr = float(compute_atr(df, ATR_PERIOD).iloc[-1])
            swing_high = float(df["High"].rolling(SWING_LOOKBACK_BARS).max().iloc[-1])
            swing_low = float(df["Low"].rolling(SWING_LOOKBACK_BARS).min().iloc[-1])

            # Volume spike = latest completed bar's volume vs. recent average bar volume
            # (apples-to-apples, unlike comparing to a live cumulative day total).
            avg_bar_volume = float(volume.rolling(VOLUME_LOOKBACK_BARS).mean().iloc[-2])
            last_bar_volume = float(volume.iloc[-2])  # -2: last fully closed bar, not the forming one
            volume_ratio = (last_bar_volume / avg_bar_volume) if avg_bar_volume else 0.0

            _indicators[t] = {
                "rsi": rsi, "ma_short": ma_short, "ma_long": ma_long, "atr": atr,
                "swing_high": swing_high, "swing_low": swing_low, "volume_ratio": volume_ratio,
            }
        except Exception as e:
            log.error("Indicator refresh failed for %s: %s", t, e)
        time.sleep(0.3)
    _indicators_updated_at = time.time()


def get_live_quote(ticker: str):
    """One Twelve Data /quote call. Costs ~1 API credit on the free plan."""
    url = "https://api.twelvedata.com/quote"
    try:
        r = requests.get(url, params={"symbol": ticker, "apikey": TWELVEDATA_API_KEY}, timeout=10)
        data = r.json()
        price = data.get("close") or data.get("price")
        if price is None:
            log.warning("Twelve Data response for %s: %s", ticker, data)
            return None
        return {
            "price": float(price),
            "percent_change": float(data.get("percent_change", 0) or 0),
        }
    except Exception as e:
        log.error("Twelve Data quote failed for %s: %s", ticker, e)
        return None


def should_alert(ticker: str) -> bool:
    now = time.time()
    last = _last_alerted.get(ticker)
    if last and (now - last) < ALERT_COOLDOWN_MINUTES * 60:
        return False
    _last_alerted[ticker] = now
    return True


def evaluate(ticker: str, quote: dict):
    ind = _indicators.get(ticker)
    if not ind:
        return None

    price = quote["price"]
    signals = []
    score = 0

    if 30 < ind["rsi"] < 60:
        score += 1
        signals.append(f"RSI {ind['rsi']:.1f} (kısa vadede toparlanma)")

    if ind["volume_ratio"] > 1.5:
        score += 1
        signals.append(f"Son 15dk hacmi ortalamanın {ind['volume_ratio']:.1f}x üstünde")

    if price > ind["ma_short"]:
        score += 1
        signals.append("Fiyat kısa vadeli ortalamanın üstünde (~5 saat)")

    if not np.isnan(ind["ma_long"]) and ind["ma_short"] > ind["ma_long"]:
        score += 1
        signals.append("Kısa ortalama > uzun ortalama (yükseliş yönü)")

    if quote["percent_change"] > 1.0:
        score += 1
        signals.append(f"Bugün +%{quote['percent_change']:.1f}")

    # Short-term breakout level = the last ~2 hours' high. Only actionable if
    # price is actually close to it right now — otherwise skip entirely so we
    # never suggest an entry that's far from the current market price.
    distance_pct = abs(price - ind["swing_high"]) / ind["swing_high"]
    if distance_pct > NEAR_BREAKOUT_PCT:
        return None

    entry = max(price, ind["swing_high"])
    stop = round(entry - STOP_ATR_MULT * ind["atr"], 2)
    target = round(entry + TARGET_RR * (entry - stop), 2)

    return {
        "ticker": ticker, "score": score, "signals": signals, "price": price,
        "entry": round(entry, 2), "stop": stop, "target": target,
    }


def run_cycle(pacing_seconds=None):
    if not _indicators or (time.time() - _indicators_updated_at) > INDICATOR_REFRESH_MINUTES * 60:
        refresh_indicators()

    gap = pacing_seconds if pacing_seconds is not None else CYCLE_SECONDS / max(len(WATCHLIST), 1)
    for t in WATCHLIST:
        quote = get_live_quote(t)
        if quote:
            result = evaluate(t, quote)
            if result and result["score"] >= SCORE_ALERT_THRESHOLD and should_alert(t):
                msg = (
                    f"*{result['ticker']}* — {result['score']}/5 sinyal @ ${result['price']:.2f}\n"
                    + "\n".join(f"• {s}" for s in result["signals"]) + "\n\n"
                    f"📈 Giriş / kırılma (~2 saatlik tepe): *${result['entry']}*\n"
                    f"🛑 Stop-loss: *${result['stop']}*\n"
                    f"🎯 Hızlı hedef (1:{TARGET_RR}): *${result['target']}*\n\n"
                    "_Kısa vadeli teknik hesaplama. Yatırım tavsiyesi değildir; hızlı hareket ettiğin için riski küçük tut._"
                )
                send_telegram(msg)
                log.info("Alert: %s score=%d", t, result["score"])
        time.sleep(gap)


def main():
    load_state()

    if RUN_MODE == "once":
        if is_market_open():
            refresh_indicators()
            run_cycle(pacing_seconds=ONCE_MODE_PACING_SECONDS)
        else:
            log.info("Piyasa kapalı, bu çalıştırmada işlem yok.")
        save_state()
        return

    # loop mode (local / VPS)
    log.info("Starting intraday short-term alert bot (%d tickers).", len(WATCHLIST))
    send_telegram("🤖 Bot başladı — kısa vadeli kırılımlar için taranıyor.")
    refresh_indicators()
    while True:
        if is_market_open():
            run_cycle()
            save_state()
        else:
            log.info("Piyasa kapalı, bekleniyor.")
            time.sleep(300)


if __name__ == "__main__":
    main()
