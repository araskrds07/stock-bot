"""
US Stock Screener + Telegram Alert Bot (Twelve Data + yfinance hybrid)
------------------------------------------------------------------------
Live-ish price/volume tracking via Twelve Data (free plan, rate-limited)
combined with slower-moving technical indicators (RSI, SMA, ATR, swing
high/low) computed from daily candles via yfinance (free, no key needed).

Each alert includes a technical entry ("kırılma" / breakout) level, a
stop-loss level (ATR-based), and a take-profit level (2x the risk). These
are formulaic outputs of a standard technical-analysis method (breakout +
ATR stop + risk/reward target) — not personal recommendations, and no
system can guarantee they will work out. This is not financial advice;
you are responsible for your own trades.

TWO RUN MODES
- RUN_MODE=loop (default): runs forever, checking every CYCLE_SECONDS.
  Use this on your own computer or a VPS that stays on.
- RUN_MODE=once: does a single pass then exits. Use this with a
  scheduler that starts the process periodically (e.g. GitHub Actions
  cron), since those don't keep a process running between triggers.
  Alert cooldown state is saved to STATE_FILE (state.json) so repeat
  alerts are still suppressed across separate runs.

FREE-PLAN CONSTRAINT (Twelve Data)
The free tier allows roughly 8 API credits/minute and 800/day. With an
8-ticker WATCHLIST queried once every ~10 minutes during market hours
(the GitHub Actions schedule below), that's well under both limits. If
you shrink/grow WATCHLIST or the schedule, scale accordingly. If you see
429 errors or "run out of API credits" in the log, back off.

SETUP (see the chat for the full walkthrough of each of these)
1. pip install -r requirements.txt
2. Telegram: message @BotFather -> /newbot -> copy the token. Message
   your new bot once, then visit
   https://api.telegram.org/bot<TOKEN>/getUpdates to find your chat_id.
3. Twelve Data: copy your API key from
   https://twelvedata.com/account/api-keys
4. Set TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, TWELVEDATA_API_KEY as
   environment variables (locally) or as GitHub Actions repo secrets
   (for the scheduled/cron setup) — never hardcode them in this file.
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

CYCLE_SECONDS = 240              # only used in "loop" mode: ~4 min per full pass
ONCE_MODE_PACING_SECONDS = 1.5   # only used in "once" mode: small gap between quote calls
INDICATOR_REFRESH_MINUTES = 30   # how often RSI/SMA/ATR/swing levels are recomputed
SCORE_ALERT_THRESHOLD = 3        # signals out of 5 needed to trigger an alert
ALERT_COOLDOWN_MINUTES = 20      # min gap between repeat alerts for the SAME ticker

MARKET_TZ = pytz.timezone("America/New_York")
MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)

_last_alerted = {}         # ticker -> unix timestamp of last alert (persisted to STATE_FILE)
_indicators = {}           # ticker -> dict of RSI/SMA/ATR/swing levels
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
    """Recompute RSI/SMA/ATR/swing levels from daily candles (yfinance, free/unlimited)."""
    global _indicators, _indicators_updated_at
    log.info("Refreshing daily indicators for %d tickers...", len(WATCHLIST))
    for t in WATCHLIST:
        try:
            df = yf.download(t, period="4mo", interval="1d", progress=False, auto_adjust=True)
            if df is None or len(df) < 30:
                continue
            close, volume = df["Close"], df["Volume"]
            rsi = float(compute_rsi(close).iloc[-1])
            sma20 = float(close.rolling(20).mean().iloc[-1])
            sma50 = float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else float("nan")
            atr = float(compute_atr(df).iloc[-1])
            swing_high = float(df["High"].rolling(20).max().iloc[-1])
            swing_low = float(df["Low"].rolling(20).min().iloc[-1])
            avg_volume = float(volume.rolling(20).mean().iloc[-1])
            _indicators[t] = {
                "rsi": rsi, "sma20": sma20, "sma50": sma50, "atr": atr,
                "swing_high": swing_high, "swing_low": swing_low, "avg_volume": avg_volume,
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
            "volume": float(data.get("volume", 0) or 0),
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

    if 30 < ind["rsi"] < 55:
        score += 1
        signals.append(f"RSI {ind['rsi']:.1f} (toparlanma bölgesi)")

    # NOTE: comparing intraday cumulative volume to the FULL daily average is an
    # approximation (volume builds up through the day) — good enough for a screener,
    # not a precise pace-of-volume calculation.
    if ind["avg_volume"] and quote["volume"] > 0.3 * ind["avg_volume"]:
        score += 1
        signals.append("Hacim ortalamaya göre yüksek")

    if price > ind["sma20"]:
        score += 1
        signals.append("Fiyat 20 günlük ortalamanın üstünde")

    if not np.isnan(ind["sma50"]) and ind["sma20"] > ind["sma50"]:
        score += 1
        signals.append("20MA > 50MA (yükseliş trendi)")

    if quote["percent_change"] > 1.5:
        score += 1
        signals.append(f"Bugün +%{quote['percent_change']:.1f}")

    # Breakout / entry level: current swing-high resistance, or live price if
    # it has already broken above it.
    entry = max(price, ind["swing_high"])
    stop = round(entry - 1.5 * ind["atr"], 2)          # ATR-based stop
    target = round(entry + 2 * (entry - stop), 2)      # 1:2 risk/reward

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
                    f"📈 Giriş / kırılma seviyesi: *${result['entry']}*\n"
                    f"🛑 Stop-loss: *${result['stop']}*\n"
                    f"🎯 Hedef (1:2 risk/ödül): *${result['target']}*\n\n"
                    "_ATR + kırılma seviyesine dayalı teknik hesaplama. Yatırım tavsiyesi değildir._"
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
    log.info("Starting hybrid Twelve Data + yfinance alert bot (%d tickers).", len(WATCHLIST))
    send_telegram("🤖 Bot başladı — piyasa açık olduğu sürece gün boyu aktif taranıyor.")
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
