"""
US Stock Screener + Telegram Alert Bot — Session-Aware Intraday Edition
--------------------------------------------------------------------------------
Covers three trading sessions: pre-market (4:00-9:30 ET), regular market hours
(9:30-16:00 ET), and after-hours (16:00-20:00 ET). All indicators are computed
from 15-minute intraday candles (yfinance, including pre/post market bars).

WHY EXTENDED HOURS GET STRICTER RULES
Pre-market and after-hours trading has much thinner volume and wider spreads
than the regular session — the same signal is less trustworthy there. So this
bot deliberately requires MORE confirmation outside regular hours:
  - Higher score threshold (4/5 instead of 3/5)
  - Tighter proximity to the breakout level (1.5% instead of 2%)
  - Higher volume-spike bar (2.0x instead of 1.5x average)
  - Checked less often (every ~30 min instead of every ~5 min, to stay inside
    Twelve Data's free daily quota — see FREE-PLAN CONSTRAINT below)
Every alert message says which session it fired in, so you know which
reliability level you're looking at.

This is a technical screener, not a prediction engine. No combination of
indicators reliably predicts what a stock does in the next few minutes,
especially in thin extended-hours trading. This is not financial advice;
size positions accordingly.

TWO RUN MODES
- RUN_MODE=loop (default): runs forever, checking every CYCLE_SECONDS.
  Use this on your own computer or a VPS that stays on.
- RUN_MODE=once: does a single pass then exits. Used by the included
  GitHub Actions workflow, which triggers this on a schedule. Alert
  cooldown state is saved to STATE_FILE (state.json) so repeat alerts
  are suppressed across separate runs.

FREE-PLAN CONSTRAINT (Twelve Data)
The free tier allows roughly 8 API credits/minute and 800/day. Regular
hours (~390 min/day) at every 5 min with 8 tickers ≈ 624 credits/day.
That leaves only ~176 credits for the ~570 minutes of combined pre/after
hours, hence the 30-minute throttle there (≈152 credits/day). Total ≈
776/day, just under quota. If you change WATCHLIST size or intervals,
redo this math, or you'll start seeing 429 / "out of credits" errors.

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
# Optional: get a free key at https://www.alphavantage.co/support/#api-key — used for
# news/sentiment, only fetched right before an actual alert (see NEWS-QUOTA note below).
ALPHAVANTAGE_API_KEY = os.environ.get("ALPHAVANTAGE_API_KEY", "")

RUN_MODE = os.environ.get("RUN_MODE", "loop")   # "loop" (VPS/local) or "once" (cron/GitHub Actions)
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
FORCE_RUN = os.environ.get("FORCE_RUN", "false").lower() == "true"  # bypass the extended-hours
                                                                       # throttle for manual test runs

# NEWS-QUOTA NOTE: Alpha Vantage's free plan allows only 25 requests/day. News is
# therefore fetched ONLY right before an alert actually fires (never during the
# regular scan of all tickers), which should stay well under that limit unless
# you get an unusually large number of alerts in one day.

# Keep the ACTIVE watchlist short on Twelve Data's free plan — see FREE-PLAN
# CONSTRAINT above. Instead of a fixed list, it's chosen fresh each trading
# day from CANDIDATE_UNIVERSE below (see rank_candidates()/load_or_build_watchlist()).
WATCHLIST_SIZE = 8
DAILY_WATCHLIST_FILE = os.environ.get("DAILY_WATCHLIST_FILE", "daily_watchlist.json")
WATCHLIST = ["AAPL", "MSFT", "NVDA", "AMZN", "TSLA", "AMD", "META", "GOOGL"]  # fallback if ranking fails

# Broader pool the bot picks TODAY's 8 from. Mixes stable mega-caps with more
# volatile/higher-beta names — the latter tend to produce more frequent
# short-term breakouts, which fits "fast in-and-out" trading better than
# mega-caps alone (which move less often).
CANDIDATE_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "NFLX", "AVGO",
    "CRM", "ORCL", "ADBE", "INTC", "QCOM", "PYPL", "SBUX", "DIS", "BA", "UBER",
    "PLTR", "COIN", "SOFI", "RIVN", "SNAP", "ABNB", "SHOP", "MARA", "RIOT", "MSTR",
]

# --- Intraday indicator settings (all computed on 15-minute candles) ---
INTRADAY_INTERVAL = "15m"
INTRADAY_PERIOD = "5d"          # yfinance history window (free tier allows up to 60d @ 15m)
SWING_LOOKBACK_BARS = 8         # ~2 hours — defines the short-term "kırılma" (breakout) level
MA_SHORT_BARS = 20              # ~5 hours
MA_LONG_BARS = 50               # ~12.5 hours (roughly 2 sessions)
ATR_PERIOD = 14                 # ~3.5 hours of volatility
VOLUME_LOOKBACK_BARS = 20

# --- Daily watchlist ranking settings (all computed on DAILY candles, free/unlimited) ---
RANK_VOLATILITY_SWEET_SPOT = 3.0   # ATR/price % that scores highest (too low=dead, too high=chaotic)
RANK_VOLATILITY_TOLERANCE = 5.0    # how quickly the score falls off away from the sweet spot
RANK_MAX_EXTENSION_PCT = 15.0      # beyond this %-distance from the 20-day high, proximity score = 0

STOP_ATR_MULT = 1.2
TARGET_RR = 1.5                 # take-profit at 1.5x the risk (quick, achievable exit)

# --- Session windows (America/New_York) ---
PREMARKET_OPEN = dtime(4, 0)
MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)
AFTERHOURS_CLOSE = dtime(20, 0)

# --- Per-session thresholds: regular hours vs. thin extended hours ---
SESSION_SETTINGS = {
    "regular":    {"score_threshold": 3, "near_breakout_pct": 0.020, "volume_mult": 1.5},
    "premarket":  {"score_threshold": 4, "near_breakout_pct": 0.015, "volume_mult": 2.0},
    "afterhours": {"score_threshold": 4, "near_breakout_pct": 0.015, "volume_mult": 2.0},
}
SESSION_LABELS = {"regular": "🕒 Normal seans", "premarket": "🌅 Pre-market", "afterhours": "🌙 After-hours"}

REGULAR_CYCLE_MINUTES = 5        # how often we actually act during regular hours
EXTENDED_CYCLE_MINUTES = 30      # how often we actually act during pre/after hours (quota-limited)

CYCLE_SECONDS = 120               # loop mode only: pacing between full passes
ONCE_MODE_PACING_SECONDS = 1.5    # once mode only: small gap between quote calls
INDICATOR_REFRESH_MINUTES = 10    # loop mode only: how often intraday indicators are recomputed
ALERT_COOLDOWN_MINUTES = 10       # min gap between repeat alerts for the SAME ticker

MARKET_TZ = pytz.timezone("America/New_York")

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


def get_session(now=None) -> str:
    """Returns 'premarket', 'regular', 'afterhours', or 'closed' based on ET time."""
    now = now or datetime.now(MARKET_TZ)
    if now.weekday() >= 5:
        return "closed"
    t = now.time()
    if PREMARKET_OPEN <= t < MARKET_OPEN:
        return "premarket"
    if MARKET_OPEN <= t <= MARKET_CLOSE:
        return "regular"
    if MARKET_CLOSE < t <= AFTERHOURS_CLOSE:
        return "afterhours"
    return "closed"


def should_act_this_cycle(session: str, now=None) -> bool:
    """Throttles extended-hours activity to stay inside the free API quota."""
    if session == "regular":
        return True
    if session in ("premarket", "afterhours"):
        now = now or datetime.now(MARKET_TZ)
        return (now.minute % EXTENDED_CYCLE_MINUTES) < REGULAR_CYCLE_MINUTES
    return False


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


def rank_candidates():
    """Scores CANDIDATE_UNIVERSE on DAILY candles (yfinance, free/unlimited) and
    returns the top WATCHLIST_SIZE tickers for today's intraday monitoring.

    Composite score (0-4, higher = better fast-breakout candidate today):
      - trend:      +1 if price > 20-day MA and 20-day MA > 50-day MA
      - volatility: 0-1, peaks when ATR/price %% is near RANK_VOLATILITY_SWEET_SPOT
                    (too calm = no setups; too wild = unreliable stops)
      - proximity:  0-1, higher the closer price is to its 20-day high
                    (room to break out, not already way overextended)
      - volume:     0-1, higher when the last 5 days' volume is running above
                    the prior 20 days' (rising interest = more likely to move)
    """
    scored = []
    for t in CANDIDATE_UNIVERSE:
        try:
            df = yf.download(t, period="4mo", interval="1d", progress=False, auto_adjust=True)
            if df is None or len(df) < 30:
                continue
            close, volume, high = df["Close"], df["Volume"], df["High"]

            price = float(close.iloc[-1])
            ma20 = float(close.rolling(20).mean().iloc[-1])
            ma50 = float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else float("nan")
            atr = float(compute_atr(df, 14).iloc[-1])
            high20 = float(high.rolling(20).max().iloc[-1])

            trend_score = 1.0 if (price > ma20 and (np.isnan(ma50) or ma20 > ma50)) else 0.0

            volatility_pct = (atr / price * 100) if price else 0.0
            vola_score = max(0.0, 1 - abs(volatility_pct - RANK_VOLATILITY_SWEET_SPOT) / RANK_VOLATILITY_TOLERANCE)

            extension_pct = abs(price - high20) / high20 * 100 if high20 else 100.0
            proximity_score = max(0.0, 1 - extension_pct / RANK_MAX_EXTENSION_PCT)

            recent_vol = float(volume.iloc[-5:].mean())
            base_vol = float(volume.iloc[-25:-5].mean()) if len(volume) >= 25 else float(volume.mean())
            volume_score = min(1.0, (recent_vol / base_vol) / 1.5) if base_vol else 0.0

            composite = trend_score + vola_score + proximity_score + volume_score
            scored.append((t, composite))
        except Exception as e:
            log.error("Ranking failed for %s: %s", t, e)
        time.sleep(0.2)

    scored.sort(key=lambda x: x[1], reverse=True)
    top = [t for t, _ in scored[:WATCHLIST_SIZE]]
    log.info("Bugünün en iyi %d adayı: %s", WATCHLIST_SIZE, top)
    return top if top else list(WATCHLIST)  # fall back to the static default if the scan fully fails


def load_or_build_watchlist():
    """Reuses today's already-picked watchlist if we've already ranked once
    today (persisted to DAILY_WATCHLIST_FILE); otherwise re-ranks."""
    global WATCHLIST
    today_str = datetime.now(MARKET_TZ).strftime("%Y-%m-%d")
    try:
        with open(DAILY_WATCHLIST_FILE, "r") as f:
            data = json.load(f)
        if data.get("date") == today_str and data.get("tickers"):
            WATCHLIST = data["tickers"]
            log.info("Bugünün watchlist'i dosyadan yüklendi: %s", WATCHLIST)
            return
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    WATCHLIST = rank_candidates()
    try:
        with open(DAILY_WATCHLIST_FILE, "w") as f:
            json.dump({"date": today_str, "tickers": WATCHLIST}, f)
    except Exception as e:
        log.error("Could not save daily watchlist: %s", e)


def assess_risk(session: str, score: int, volatility_pct: float) -> str:
    """Best-effort, rule-based risk label — not a guarantee. Combines: how thin
    the current session's liquidity is, how many of the 5 signals agreed, and
    how volatile the stock currently is (ATR as % of price)."""
    points = 0
    if session != "regular":
        points += 1                    # thinner liquidity outside regular hours
    if volatility_pct >= 1.5:
        points += 1                    # unusually choppy right now
    elif volatility_pct <= 0.6:
        points -= 1                    # calmer than usual
    if score >= 5:
        points -= 1                    # every signal agreed

    if points <= -1:
        return "🟢 Düşük"
    if points == 0:
        return "🟡 Orta"
    return "🔴 Yüksek"


def refresh_indicators():
    """Recompute RSI/MA/ATR/swing levels from 15-min intraday candles, INCLUDING
    pre-market and after-hours bars (prepost=True) so extended-session signals
    are based on real extended-session price action."""
    global _indicators, _indicators_updated_at
    log.info("Refreshing intraday (%s) indicators for %d tickers...", INTRADAY_INTERVAL, len(WATCHLIST))
    for t in WATCHLIST:
        try:
            df = yf.download(
                t, period=INTRADAY_PERIOD, interval=INTRADAY_INTERVAL,
                progress=False, auto_adjust=True, prepost=True,
            )
            min_bars = max(MA_LONG_BARS, ATR_PERIOD, VOLUME_LOOKBACK_BARS) + 2
            if df is None or len(df) < min_bars:
                log.warning("Not enough intraday bars for %s", t)
                continue

            close, volume = df["Close"], df["Volume"]
            rsi = float(compute_rsi(close, 14).iloc[-1])
            ma_short = float(close.rolling(MA_SHORT_BARS).mean().iloc[-1])
            ma_long = float(close.rolling(MA_LONG_BARS).mean().iloc[-1])
            atr = float(compute_atr(df, ATR_PERIOD).iloc[-1])
            swing_high = float(df["High"].rolling(SWING_LOOKBACK_BARS).max().iloc[-1])
            swing_low = float(df["Low"].rolling(SWING_LOOKBACK_BARS).min().iloc[-1])

            avg_bar_volume = float(volume.rolling(VOLUME_LOOKBACK_BARS).mean().iloc[-2])
            last_bar_volume = float(volume.iloc[-2])  # -2: last fully closed bar
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
    """One Twelve Data /quote call. Costs ~1 API credit on the free plan.
    Note: extended-hours quote data quality/availability on the free plan can
    be limited compared to regular session data."""
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


def get_news_sentiment(ticker: str):
    """One Alpha Vantage NEWS_SENTIMENT call — only used right before an alert
    fires (see NEWS-QUOTA note above). Returns the most recent article's
    sentiment label/score plus its headline+source, or None if unavailable."""
    if not ALPHAVANTAGE_API_KEY:
        return None
    url = "https://www.alphavantage.co/query"
    try:
        r = requests.get(
            url,
            params={"function": "NEWS_SENTIMENT", "tickers": ticker, "limit": 5, "apikey": ALPHAVANTAGE_API_KEY},
            timeout=10,
        )
        data = r.json()
        feed = data.get("feed")
        if not feed:
            return None
        latest = feed[0]
        # Find this ticker's specific sentiment within the article (falls back
        # to the article's overall sentiment if the per-ticker figure is missing).
        ticker_sentiment = next(
            (ts for ts in latest.get("ticker_sentiment", []) if ts.get("ticker") == ticker), None
        )
        label = (ticker_sentiment or {}).get("ticker_sentiment_label") or latest.get("overall_sentiment_label", "Bilinmiyor")
        score = float((ticker_sentiment or {}).get("ticker_sentiment_score", latest.get("overall_sentiment_score", 0)) or 0)
        return {
            "label": label,
            "score": score,
            "headline": latest.get("title", "")[:120],
            "source": latest.get("source", "bilinmeyen kaynak"),
        }
    except Exception as e:
        log.error("News sentiment failed for %s: %s", ticker, e)
        return None


def should_alert(ticker: str) -> bool:
    now = time.time()
    last = _last_alerted.get(ticker)
    if last and (now - last) < ALERT_COOLDOWN_MINUTES * 60:
        return False
    _last_alerted[ticker] = now
    return True


def evaluate(ticker: str, quote: dict, session: str):
    ind = _indicators.get(ticker)
    if not ind:
        return None

    settings = SESSION_SETTINGS[session]
    price = quote["price"]
    signals = []
    score = 0

    if 30 < ind["rsi"] < 60:
        score += 1
        signals.append(f"RSI {ind['rsi']:.1f} (kısa vadede toparlanma)")

    if ind["volume_ratio"] > settings["volume_mult"]:
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

    if score < settings["score_threshold"]:
        log.info(
            "%s: skor %d/5 (eşik %d) — sinyal yetersiz. Detay: %s",
            ticker, score, settings["score_threshold"], "; ".join(signals) or "hiçbiri",
        )
        return None

    # Short-term breakout level = the last ~2 hours' high. Only actionable if
    # price is genuinely close to it (threshold tightens in extended hours).
    distance_pct = abs(price - ind["swing_high"]) / ind["swing_high"]
    if distance_pct > settings["near_breakout_pct"]:
        log.info(
            "%s: skor %d/5 yeterli AMA kırılmaya %%%.1f uzak (limit %%%.1f, tepe $%.2f, fiyat $%.2f)",
            ticker, score, distance_pct * 100, settings["near_breakout_pct"] * 100,
            ind["swing_high"], price,
        )
        return None

    entry = max(price, ind["swing_high"])
    stop = round(entry - STOP_ATR_MULT * ind["atr"], 2)
    target = round(entry + TARGET_RR * (entry - stop), 2)

    risk_pct = (entry - stop) / entry * 100
    reward_pct = (target - entry) / entry * 100
    volatility_pct = (ind["atr"] / price * 100) if price else 0.0
    risk_label = assess_risk(session, score, volatility_pct)

    return {
        "ticker": ticker, "score": score, "signals": signals, "price": price,
        "entry": round(entry, 2), "stop": stop, "target": target,
        "risk_pct": round(risk_pct, 2), "reward_pct": round(reward_pct, 2),
        "volatility_pct": round(volatility_pct, 2), "risk_label": risk_label,
    }


def run_cycle(session: str, pacing_seconds=None):
    if not _indicators or (time.time() - _indicators_updated_at) > INDICATOR_REFRESH_MINUTES * 60:
        refresh_indicators()

    alerts_sent = 0
    gap = pacing_seconds if pacing_seconds is not None else CYCLE_SECONDS / max(len(WATCHLIST), 1)
    for t in WATCHLIST:
        quote = get_live_quote(t)
        if quote:
            result = evaluate(t, quote, session)
            if result:
                if should_alert(t):
                    news = get_news_sentiment(t)
                    # A clearly negative news backdrop makes an otherwise-clean
                    # technical breakout less trustworthy — nudge the risk label up.
                    if news and news["label"] in ("Bearish", "Somewhat-Bearish"):
                        if result["risk_label"] == "🟢 Düşük":
                            result["risk_label"] = "🟡 Orta"
                        elif result["risk_label"] == "🟡 Orta":
                            result["risk_label"] = "🔴 Yüksek"

                    news_section = ""
                    if news:
                        news_section = (
                            f"\n📰 Haber duyarlılığı: *{news['label']}* (skor {news['score']:.2f})\n"
                            f"   • \"{news['headline']}\" — {news['source']}\n"
                        )

                    msg = (
                        f"{SESSION_LABELS[session]}\n"
                        f"*{result['ticker']}* — {result['score']}/5 sinyal @ ${result['price']:.2f}\n"
                        + "\n".join(f"• {s}" for s in result["signals"]) + "\n\n"
                        f"📈 Giriş / kırılma (~2 saatlik tepe): *${result['entry']}*\n"
                        f"🛑 Stop-loss: *${result['stop']}*\n"
                        f"🎯 Hızlı hedef (1:{TARGET_RR}): *${result['target']}*\n\n"
                        f"⚖️ Risk seviyesi: *{result['risk_label']}*\n"
                        f"   • Stop'a kadar risk: %{result['risk_pct']:.2f}\n"
                        f"   • Hedefe kadar potansiyel: %{result['reward_pct']:.2f}\n"
                        f"   • Şu anki oynaklık (ATR/fiyat): %{result['volatility_pct']:.2f}\n"
                        + news_section + "\n"
                        "_Kısa vadeli teknik hesaplama, kural-bazlı risk etiketi. Yatırım tavsiyesi değildir; "
                        "hızlı hareket ettiğin için pozisyonu küçük tut._"
                    )
                    send_telegram(msg)
                    alerts_sent += 1
                    log.info("Alert: %s score=%d session=%s risk=%s", t, result["score"], session, result["risk_label"])
                else:
                    log.info("%s: kriterleri karşıladı ama cooldown nedeniyle atlandı.", t)
        time.sleep(gap)
    return alerts_sent


def main():
    load_state()
    session = get_session()

    if RUN_MODE == "once":
        if session != "closed" and (FORCE_RUN or should_act_this_cycle(session)):
            load_or_build_watchlist()
            refresh_indicators()
            alerts_sent = run_cycle(session, pacing_seconds=ONCE_MODE_PACING_SECONDS)
            if FORCE_RUN:
                # Manual trigger: always confirm the bot is alive and working,
                # regardless of whether a tradeable setup was found this time.
                send_telegram(
                    f"✅ Bot çalışıyor — {SESSION_LABELS[session]}\n"
                    f"Bugünün listesi: {', '.join(WATCHLIST)}\n"
                    f"{len(WATCHLIST)} hisse tarandı, {alerts_sent} sinyal bulundu."
                )
        elif session == "closed":
            log.info("Piyasa kapalı (session=closed).")
            if FORCE_RUN:
                send_telegram("✅ Bot çalışıyor — ama piyasa şu an tamamen kapalı (pre-market/regular/after-hours dışında).")
        else:
            log.info("Session=%s ama throttle penceresi dışında, bu döngü atlanıyor.", session)
        save_state()
        return

    # loop mode (local / VPS)
    log.info("Starting session-aware intraday alert bot.")
    load_or_build_watchlist()
    send_telegram(f"🤖 Bot başladı — bugünün listesi: {', '.join(WATCHLIST)}")
    refresh_indicators()
    while True:
        load_or_build_watchlist()  # cheap once cached; re-ranks automatically at day rollover
        session = get_session()
        if session != "closed" and should_act_this_cycle(session):
            run_cycle(session)
            save_state()
        else:
            log.info("Bekleniyor (session=%s).", session)
        time.sleep(60)  # check session every minute; run_cycle's own throttle governs actual work


if __name__ == "__main__":
    main()
