"""
ABD Hisse Kırılım Botu (Telegram) — v3
======================================

Ne yapar
--------
1. Günün "hareketli" hisselerini bulur: Yahoo'nun günün yükselenleri / en
   aktifleri / küçük-orta ölçek yükselenleri listeleri + sabit bir oynak hisse
   havuzu. Bunları günlük oynaklık, göreli hacim, bugünkü hareket, trend ve
   tepeye yakınlığa göre puanlayıp en iyi WATCHLIST_SIZE tanesini seçer.
   Liste saatte bir ve açılıştan 15 dk sonra yeniden hesaplanır.
2. Her çalıştırmada bu hisselerin 15 dakikalık mumlarını TEK istekte çeker ve
   son ~2 saatin tepesinin kırılıp kırılmadığına bakar.
3. Sinyal verirken giriş / stop / hedef, pozisyon büyüklüğü, gerekçeli risk
   seviyesi, bu hissede aynı kurulumun son 60 gündeki sonucu, haber duyarlılığı
   ve yaklaşan bilanço tarihini gönderir.
4. Sinyali takip eder: kırılım gerçekleşti / stop'u başa-baş çek / hedef /
   stop / gün sonu kapat / emri iptal et mesajları atar.
5. Kapanıştan sonra günlük rapor atar (sinyaller + son 20 günün performansı).

Bu bir teknik tarayıcıdır, yatırım tavsiyesi değildir. Hiçbir gösterge
kombinasyonu kârı garanti etmez; özellikle %5+ gün içi hedefler nadiren tutar.

Veri kaynakları
---------------
- Fiyat ve göstergeler: yfinance (ücretsiz, anahtar gerekmez). Çalıştırma başına
  1-3 istek yapıldığı için Twelve Data'nın 8 istek/dk sınırına takılmaz.
- Haber duyarlılığı (opsiyonel): Alpha Vantage, sadece sinyal gönderilirken.

Ortam değişkenleri (GitHub > Settings > Secrets / Variables)
------------------------------------------------------------
TELEGRAM_TOKEN, TELEGRAM_CHAT_ID        (zorunlu, secret)
ALPHAVANTAGE_API_KEY                    (opsiyonel, secret)
ACCOUNT_SIZE        (opsiyonel variable, varsayılan 80 — $ cinsinden alım bütçesi)
MAX_STOCK_PRICE     (opsiyonel variable — boşsa bütçeden otomatik hesaplanır)
COMMISSION_PER_SIDE (opsiyonel variable, varsayılan 1.5 — alış ve satışta ayrı ayrı ödenen ücret $)
MIN_NET_PROFIT      (opsiyonel variable, varsayılan 1 — hedefte komisyon sonrası en az net kâr $)
RISK_PER_TRADE_PCT  (opsiyonel variable, varsayılan 1 — işlem başına riske edilen %)
RUN_MODE=once       (GitHub Actions için; workflow dosyası ayarlar)
"""

import os
import re
import json
import html
import time
import logging
from datetime import datetime, timedelta, date, time as dtime

import numpy as np
import pandas as pd
import requests
import yfinance as yf
import pytz

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("stockbot")
logging.getLogger("yfinance").setLevel(logging.CRITICAL)  # delisted/eksik semboller için gürültülü loglar


def _env_float(name, default):
    raw = os.environ.get(name, "")
    try:
        return float(raw) if raw.strip() else default
    except ValueError:
        return default


# ---------------------------------------------------------------- AYARLAR
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
ALPHAVANTAGE_API_KEY = os.environ.get("ALPHAVANTAGE_API_KEY", "")

RUN_MODE = os.environ.get("RUN_MODE", "loop")            # "once" = GitHub Actions, "loop" = kendi bilgisayarın/VPS,
                                                         # "backtest" = son 2 ayın testi (Telegram'a rapor)
FORCE_RUN = os.environ.get("FORCE_RUN", "false").lower() == "true"   # elle "Run workflow" = durum mesajı gönder
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
DAILY_WATCHLIST_FILE = os.environ.get("DAILY_WATCHLIST_FILE", "daily_watchlist.json")

ACCOUNT_SIZE = _env_float("ACCOUNT_SIZE", 80.0)          # alım bütçen ($)
RISK_PER_TRADE_PCT = _env_float("RISK_PER_TRADE_PCT", 1.0)
MAX_STOCK_PRICE = _env_float("MAX_STOCK_PRICE", 0.0)       # 0 = bütçeden hesapla
TYPICAL_STOP_PCT = 1.5                                      # bütçe filtresi için ortalama stop mesafesi
COMMISSION_PER_SIDE = _env_float("COMMISSION_PER_SIDE", 1.5)  # aracı kurum ücreti: alışta ve satışta ayrı ayrı ($)
MIN_NET_PROFIT = _env_float("MIN_NET_PROFIT", 1.0)            # hedefte komisyon sonrası en az bu kadar net kâr ($)
ROUND_TRIP_FEES = 2 * COMMISSION_PER_SIDE
MAX_FEE_PCT = 1.0          # komisyon pozisyonun en fazla bu yüzdesi olsun: gerekirse pozisyon bu kadar büyütülür

MARKET_TZ = pytz.timezone("America/New_York")
LOCAL_TZ = pytz.timezone(os.environ.get("LOCAL_TZ") or "Europe/Budapest")

# --- Hisse seçimi (günlük mumlar) ---
WATCHLIST_SIZE = 15
WATCHLIST_REFRESH_MINUTES = 60
POST_OPEN_RERANK = dtime(9, 45)        # açılıştan 15 dk sonra bugünkü verilerle yeniden sırala
DISCOVERY_SCREENS = ("day_gainers", "most_actives", "small_cap_gainers", "aggressive_small_caps")
DISCOVERY_COUNT = 50
MAX_DISCOVERED = 60
MIN_PRICE = 3.0                        # altındakiler: penny stock, spread çok geniş
MIN_MARKET_CAP = 300_000_000
MIN_AVG_DOLLAR_VOLUME = 20_000_000     # günlük ortalama işlem hacmi ($) — likidite
MIN_DAILY_ATR_PCT = 4.0                # günde ortalama %4'ten az oynayan hisse %5 hedefe uygun değil
MAX_DAY_CHANGE_PCT = 60.0              # bugün %60+ uçmuş hisse: durdurma (halt) ve çöküş riski

# Kalıcı aday havuzu: likit, yüksek oynaklığa sahip isimler. Sakin olanlar
# MIN_DAILY_ATR_PCT filtresiyle zaten elenir; listeden çıkan semboller sorun çıkarmaz.
BASE_UNIVERSE = [
    "TSLA", "NVDA", "AMD", "PLTR", "COIN", "MSTR", "MARA", "RIOT", "CLSK", "HOOD",
    "SOFI", "UPST", "AFRM", "RIVN", "LCID", "SNAP", "ROKU", "CVNA", "SMCI", "ARM",
    "IONQ", "RGTI", "QBTS", "SOUN", "RKLB", "ASTS", "HIMS", "APP", "SHOP", "DKNG",
    "U", "NET", "CRWD", "TTD", "ENPH", "CELH", "OKLO", "SMR", "RDDT", "NBIS",
    # daha düşük fiyatlı, hareketli isimler (küçük bütçe için)
    "NIO", "PLUG", "ACHR", "JOBY", "BBAI", "OPEN", "RUN", "GRAB", "RXRX", "LUNR",
    "SERV", "RR", "APLD", "WULF", "CIFR", "HUT", "BTDR", "IREN", "TLRY", "DNA",
]
FALLBACK_WATCHLIST = ["TSLA", "NVDA", "AMD", "PLTR", "COIN", "MSTR", "MARA", "HOOD", "SOFI", "SMCI"]
MARKET_PROXY = "SPY"

# --- Kırılım sinyali (15 dakikalık mumlar) ---
INTRADAY_INTERVAL = "15m"
BREAKOUT_LOOKBACK_BARS = 8     # ~2 saat: kırılacak direnç = önceki 8 kapanmış mumun en yükseği
BASE_LOW_BARS = 4              # stop için son ~1 saatin dibi
ATR_PERIOD = 14
RSI_MIN, RSI_MAX = 55, 78      # kırılımda momentum olmalı ama aşırı şişmemiş
MIN_DAY_CHANGE_PCT = 2.0       # bugün en az %2 artıda = "oyunda" olan hisse
MAX_CHASE_PCT = 1.0            # kırılımın %1'inden fazla üstündeyse kovalama
STOP_ATR_MULT = 1.5
MIN_STOP_ATR = 1.0             # stop girişten en az 1 ATR uzakta (normal dalgalanmada patlamasın)
MAX_RISK_PCT = 2.5             # stop girişten %2.5'ten uzaksa işlem kötü risk/ödül — atla
MIN_REWARD_PCT = 5.0           # hedef en az %5
TARGET_RR = 2.0                # ... ve en az riskin 2 katı
MAX_TARGET_VS_DAILY_ATR = 1.3  # hedef, hissenin ortalama günlük hareketinin 1.3 katından büyükse gerçekçi değil

SESSION_SETTINGS = {
    "regular":    {"min_score": 3, "near_pct": 1.0, "volume_mult": 1.5},
    "premarket":  {"min_score": 4, "near_pct": 0.7, "volume_mult": 2.0},
    "afterhours": {"min_score": 4, "near_pct": 0.7, "volume_mult": 2.0},
}
SESSION_LABELS = {"regular": "🕒 Normal seans", "premarket": "🌅 Pre-market",
                  "afterhours": "🌙 After-hours", "closed": "💤 Piyasa kapalı"}

MAX_ALERTS_PER_RUN = 3
ALERT_COOLDOWN_MINUTES = 30
PENDING_EXPIRY_MINUTES = 60    # "kırılıma yakın" sinyali 60 dk içinde tetiklenmezse iptal
DATA_STALE_MINUTES = 35        # son mum bundan eskiyse veri bayat (tatil / işlem yok)
REGULAR_EXIT = dtime(15, 55)   # gün içi işlemler kapanıştan 5 dk önce kapatılır
AFTERHOURS_EXIT = dtime(19, 50)
SUMMARY_TIME = dtime(16, 5)
BACKTEST_PERIOD = "60d"        # yfinance 15m verisi en fazla 60 gün geriye gider
LOOP_SECONDS = 120

PREMARKET_OPEN = dtime(4, 0)
MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)
AFTERHOURS_CLOSE = dtime(20, 0)

REASON_LABELS = {
    "low_score": "sinyal skoru düşük",
    "far": "kırılıma uzak",
    "chased": "kırılım kaçmış (kovalama)",
    "risk_wide": "stop çok uzak",
    "target_unrealistic": "%5 hedef bu hisse için gerçekçi değil",
    "no_data": "veri yetersiz",
    "stale": "veri bayat",
    "cooldown": "yakın zamanda sinyal verildi",
    "open_signal": "zaten takipte",
    "too_expensive": "bütçene göre pahalı",
    "fees_too_high": "komisyonu çıkarmak için gereken hareket çok büyük",
}

RESULT_LABELS = {"target": "🎯", "stop": "🛑", "breakeven": "⚪", "eod": "🔔",
                 "expired": "⌛", "cancelled": "❌"}

# ---------------------------------------------------------------- yardımcılar
def e(x):
    return html.escape(str(x), quote=False)


def money(x):
    return f"${x:,.2f}"


def smoney(x):
    return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


def now_et():
    return datetime.now(MARKET_TZ)


def local_hm(dt):
    return dt.astimezone(LOCAL_TZ).strftime("%H:%M")


def et_at(d, t):
    return MARKET_TZ.localize(datetime.combine(d, t))


def read_json(path, default):
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def write_json(path, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=1, default=str)
    except OSError as ex:
        log.error("%s yazılamadı: %s", path, ex)


def session_of(ts) -> str:
    t = ts.time()
    if PREMARKET_OPEN <= t < MARKET_OPEN:
        return "premarket"
    if MARKET_OPEN <= t < MARKET_CLOSE:
        return "regular"
    if MARKET_CLOSE <= t < AFTERHOURS_CLOSE:
        return "afterhours"
    return "closed"


def get_session(now) -> str:
    if now.weekday() >= 5:
        return "closed"
    return session_of(now)


# ---------------------------------------------------------------- Telegram
def _strip_tags(text):
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def send_telegram(text: str) -> bool:
    """HTML modunda gönderir; Telegram reddederse (ör. başlıktaki özel karakter)
    düz metinle tekrar dener. Başarısızlık artık sessizce yutulmuyor."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or "PUT_YOUR" in TELEGRAM_TOKEN:
        log.warning("Telegram ayarlı değil — mesaj yalnızca loga yazılıyor:\n%s", _strip_tags(text))
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [text]
    for chunk in chunks:
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": chunk,
                   "parse_mode": "HTML", "disable_web_page_preview": True}
        try:
            r = requests.post(url, data=payload, timeout=15)
            ok = r.ok and r.json().get("ok", False)
            if not ok:
                log.error("Telegram HTML'i reddetti (%s) — düz metin deneniyor", r.text[:200])
                payload.pop("parse_mode")
                payload["text"] = _strip_tags(chunk)
                r = requests.post(url, data=payload, timeout=15)
                ok = r.ok and r.json().get("ok", False)
            if not ok:
                log.error("Telegram gönderimi başarısız: %s", r.text[:300])
                return False
        except Exception as ex:
            log.error("Telegram gönderim hatası: %s", ex)
            return False
    return True


# ---------------------------------------------------------------- veri
def _extract(raw, ticker):
    """yfinance 1.x tek sembolde bile çok seviyeli (MultiIndex) sütun döndürür.
    Eski kod df["Close"]'u Series sandığı için pandas 3'te her hisse için
    çöküyordu. Burada her durumda düz OHLCV tablosu elde ediyoruz."""
    if raw is None or raw.empty:
        return None
    df = None
    if isinstance(raw.columns, pd.MultiIndex):
        for lvl in range(raw.columns.nlevels):
            if ticker in raw.columns.get_level_values(lvl):
                df = raw.xs(ticker, axis=1, level=lvl)
                break
        if df is None:
            return None
    else:
        df = raw
    if not {"High", "Low", "Close"}.issubset(df.columns):
        return None
    df = df.loc[:, [c for c in ("Open", "High", "Low", "Close", "Volume") if c in df.columns]].copy()
    df = df.dropna(subset=["High", "Low", "Close"])
    if df.empty:
        return None
    df["Volume"] = df["Volume"].fillna(0).astype(float) if "Volume" in df.columns else 0.0
    idx = pd.DatetimeIndex(df.index)
    df.index = idx.tz_localize(MARKET_TZ) if idx.tz is None else idx.tz_convert(MARKET_TZ)
    return df.sort_index()


def yf_download(tickers, **kwargs):
    tickers = sorted({t for t in tickers if t})
    if not tickers:
        return {}
    try:
        raw = yf.download(tickers=tickers, group_by="ticker", auto_adjust=True, progress=False,
                          threads=True, multi_level_index=True, timeout=20, **kwargs)
    except Exception as ex:
        log.error("yfinance indirme hatası (%s): %s", kwargs.get("interval"), ex)
        return {}
    out = {}
    for t in tickers:
        try:
            df = _extract(raw, t)
        except Exception as ex:
            log.error("%s verisi ayrıştırılamadı: %s", t, ex)
            df = None
        if df is not None and len(df):
            out[t] = df
    return out


# ---------------------------------------------------------------- göstergeler
def compute_rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    return rsi.where(loss != 0, 100.0)


def compute_atr(df, period=14):
    prev_close = df["Close"].shift(1)
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - prev_close).abs(),
                    (df["Low"] - prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def compute_intraday(df):
    """Son (henüz kapanmamış) mum = anlık fiyat. Direnç, stop tabanı, ATR ve
    hacim karşılaştırması KAPANMIŞ mumlardan hesaplanır. (Eski kod direnci
    oluşan mumu da katarak hesaplıyordu; bu yüzden fiyat dirence asla
    geçemiyor, gerçek bir kırılım hiç görülmüyordu.)"""
    if df is None or len(df) < max(BREAKOUT_LOOKBACK_BARS, ATR_PERIOD, 21) + 3:
        return None
    closed = df.iloc[:-1]
    last_ts = df.index[-1]
    price = float(df["Close"].iloc[-1])

    dates = np.array([ts.date() for ts in df.index])
    times = np.array([ts.time() for ts in df.index])
    today_mask = dates == last_ts.date()
    tdf = df[today_mask]
    tp = (tdf["High"] + tdf["Low"] + tdf["Close"]) / 3
    vol_sum = float(tdf["Volume"].sum())
    vwap = float((tp * tdf["Volume"]).sum() / vol_sum) if vol_sum > 0 else float("nan")

    prev_reg = df[(dates < last_ts.date()) & (times >= MARKET_OPEN) & (times < MARKET_CLOSE)]
    prev_close = float(prev_reg["Close"].iloc[-1]) if len(prev_reg) else float("nan")
    day_change = (price / prev_close - 1) * 100 if prev_close == prev_close and prev_close > 0 else 0.0

    last_closed_ts = closed.index[-1]
    kind = session_of(last_closed_ts)
    earlier = closed.iloc[:-1]
    same_kind = earlier[[session_of(ts) == kind for ts in earlier.index]]["Volume"].tail(20)
    avg_vol = float(same_kind.mean()) if len(same_kind) else 0.0
    vol_ratio = float(closed["Volume"].iloc[-1]) / avg_vol if avg_vol > 0 else 0.0

    return {
        "price": price,
        "last_ts": last_ts,
        "prior_high": float(closed["High"].iloc[-BREAKOUT_LOOKBACK_BARS:].max()),
        "base_low": float(closed["Low"].iloc[-BASE_LOW_BARS:].min()),
        "atr15": float(compute_atr(closed, ATR_PERIOD).iloc[-1]),
        "rsi": float(compute_rsi(df["Close"]).iloc[-1]),
        "ema_fast": float(df["Close"].ewm(span=9, adjust=False).mean().iloc[-1]),
        "ema_slow": float(df["Close"].ewm(span=21, adjust=False).mean().iloc[-1]),
        "vwap": vwap,
        "day_change": day_change,
        "vol_ratio": vol_ratio,
        "seen_high": float(df["High"].iloc[-1]),
        "seen_low": float(df["Low"].iloc[-1]),
    }


def trade_levels(entry, base_low, atr15, min_reward=MIN_REWARD_PCT):
    """Canlı sinyal ve geçmiş testi için ortak giriş/stop/hedef kuralı.
    (levels, hata_kodu) döndürür."""
    if not (np.isfinite(atr15) and atr15 > 0 and entry > 0):
        return None, "no_data"
    atr_stop = entry - STOP_ATR_MULT * atr15
    struct_stop = base_low * 0.999 if np.isfinite(base_low) else atr_stop
    stop = max(atr_stop, struct_stop)                      # yapısal dip daha yakınsa onu kullan
    if entry - stop < MIN_STOP_ATR * atr15:
        stop = entry - MIN_STOP_ATR * atr15
    risk_pct = (entry - stop) / entry * 100
    if risk_pct > MAX_RISK_PCT:
        return None, "risk_wide"
    reward_pct = max(min_reward, TARGET_RR * risk_pct)
    return {"entry": entry, "stop": stop, "target": entry * (1 + reward_pct / 100),
            "risk_pct": risk_pct, "reward_pct": reward_pct}, None


# ---------------------------------------------------------------- hisse seçimi
def discover_candidates():
    """Yahoo'nun hazır tarama listelerinden günün hareketli hisselerini toplar."""
    found = []
    for query in DISCOVERY_SCREENS:
        try:
            try:
                res = yf.screen(query, count=DISCOVERY_COUNT)
            except TypeError:
                res = yf.screen(query)
        except Exception as ex:
            log.warning("Tarama listesi alınamadı (%s): %s", query, ex)
            continue
        quotes = res.get("quotes", []) if isinstance(res, dict) else []
        for q in quotes:
            sym = str(q.get("symbol", ""))
            if not sym.isalpha() or len(sym) > 5:              # warrant/unit/yabancı sınıflar
                continue
            if q.get("quoteType") not in (None, "EQUITY"):
                continue
            price = q.get("regularMarketPrice") or 0
            cap = q.get("marketCap") or 0
            avg_vol = q.get("averageDailyVolume3Month") or 0
            chg = q.get("regularMarketChangePercent") or 0
            if price < MIN_PRICE or price > max_affordable_price() or (cap and cap < MIN_MARKET_CAP):
                continue
            if avg_vol and avg_vol * price < MIN_AVG_DOLLAR_VOLUME:
                continue
            if chg > MAX_DAY_CHANGE_PCT:
                continue
            if sym not in found:
                found.append(sym)
    log.info("Taramadan %d aday bulundu: %s", len(found), found[:MAX_DISCOVERED])
    return found[:MAX_DISCOVERED]


def rank_candidates(candidates, now):
    """Günlük mumlarla puanlar (0-5.5): göreli hacim (0-2), bugünkü hareket (0-1),
    oynaklık (0-1), trend (0-0.75), 20 günlük tepeye yakınlık (0-0.75)."""
    data = yf_download(candidates, period="6mo", interval="1d")
    scored, meta = [], {}
    for t in candidates:
        d = data.get(t)
        if d is None or len(d) < 60:
            continue
        try:
            if d.index[-1].date() == now.date():
                today_bar, hist = d.iloc[-1], d.iloc[:-1]
            else:
                today_bar, hist = None, d
            price = float(d["Close"].iloc[-1])
            prev_close = float(hist["Close"].iloc[-1] if today_bar is not None else hist["Close"].iloc[-2])
            atr_pct = float(compute_atr(hist).iloc[-1]) / price * 100
            avg_vol = float(hist["Volume"].tail(20).mean())
            if price < MIN_PRICE or price > max_affordable_price() or avg_vol * price < MIN_AVG_DOLLAR_VOLUME:
                continue
            if atr_pct < MIN_DAILY_ATR_PCT:
                log.info("%s elendi: günlük oynaklık %%%.1f < %%%.1f", t, atr_pct, MIN_DAILY_ATR_PCT)
                continue
            change = (price / prev_close - 1) * 100
            if change > MAX_DAY_CHANGE_PCT:
                continue
            if today_bar is not None:
                start = et_at(now.date(), MARKET_OPEN)
                frac = min(max((now - start).total_seconds() / (390 * 60), 0.05), 1.0)
                rvol = float(today_bar["Volume"]) / (avg_vol * frac) if avg_vol > 0 else 0.0
            else:
                base = float(hist["Volume"].iloc[-21:-1].mean())
                rvol = float(hist["Volume"].iloc[-1]) / base if base > 0 else 0.0
            ma20 = float(hist["Close"].tail(20).mean())
            ma50 = float(hist["Close"].tail(50).mean())
            high20 = float(hist["High"].tail(20).max())
            extension = max(0.0, (high20 - price) / high20 * 100)

            score = (min(rvol, 4.0) / 2
                     + min(max(change / 5, 0.0), 1.0)
                     + max(0.0, 1 - abs(atr_pct - 6.0) / 6.0)
                     + (0.75 if price > ma20 > ma50 else 0.0)
                     + 0.75 * max(0.0, 1 - extension / 15))
            scored.append((t, score))
            meta[t] = {"atr_pct": round(atr_pct, 2), "rvol": round(rvol, 2), "change": round(change, 2),
                       "price": round(price, 2), "score": round(score, 2)}
        except Exception as ex:
            log.error("%s puanlanamadı: %s", t, ex)
    scored.sort(key=lambda x: x[1], reverse=True)
    top = [t for t, _ in scored[:WATCHLIST_SIZE]]
    log.info("Takip listesi (%d): %s", len(top), top)
    return top, {t: meta[t] for t in top}


def load_or_build_watchlist(session, now):
    data = read_json(DAILY_WATCHLIST_FILE, {})
    today = now.strftime("%Y-%m-%d")
    same_day = data.get("date") == today
    need = (not same_day or not data.get("tickers")
            or time.time() - float(data.get("built_at", 0)) > WATCHLIST_REFRESH_MINUTES * 60
            or (session == "regular" and now.time() >= POST_OPEN_RERANK and not data.get("post_open_ranked")))
    if not need:
        return data["tickers"], data.get("meta", {}), None

    candidates = list(dict.fromkeys(BASE_UNIVERSE + discover_candidates()))
    top, meta = rank_candidates(candidates, now)
    if not top:
        if same_day and data.get("tickers"):
            log.warning("Sıralama başarısız — önceki liste kullanılıyor, 10 dk sonra tekrar denenecek")
            data["built_at"] = time.time() - WATCHLIST_REFRESH_MINUTES * 60 + 600
            write_json(DAILY_WATCHLIST_FILE, data)
            return data["tickers"], data.get("meta", {}), None
        log.warning("Sıralama başarısız — yedek liste kullanılıyor")
        top, meta = list(FALLBACK_WATCHLIST), {}

    new = {"date": today, "built_at": time.time(), "tickers": top, "meta": meta,
           "post_open_ranked": bool(same_day and data.get("post_open_ranked")),
           "announced": list(data.get("announced", [])) if same_day else []}
    if session == "regular" and now.time() >= POST_OPEN_RERANK:
        new["post_open_ranked"] = True
    kind = "open" if new["post_open_ranked"] else "pre"
    announce = None
    if kind not in new["announced"] and meta:
        announce = kind
        new["announced"].append(kind)
    write_json(DAILY_WATCHLIST_FILE, new)
    return top, meta, announce


def watchlist_message(tickers, meta, kind, now):
    title = "Açılıştan sonra güncel liste" if kind == "open" else "Bugün izlediğim hisseler"
    return f"📋 <b>{title}</b> ({local_hm(now)})\n{e(', '.join(tickers))}"


# ---------------------------------------------------------------- sinyal
def market_context(spy_df):
    ind = compute_intraday(spy_df) if spy_df is not None else None
    if not ind:
        return {"known": False, "weak": False, "strong": False, "text": "bilinmiyor"}
    above = ind["vwap"] == ind["vwap"] and ind["price"] > ind["vwap"]
    chg = ind["day_change"]
    weak = chg < -1.0 or (not above and chg < -0.3)
    strong = chg > 0.5 and above
    state = "güçlü" if strong else "zayıf" if weak else "nötr"
    return {"known": True, "weak": weak, "strong": strong, "change": chg,
            "text": f"%{chg:+.2f} · VWAP {'üstü' if above else 'altı'} → {state}"}


def evaluate(ticker, ind, meta, session):
    s = SESSION_SETTINGS[session]
    price = ind["price"]
    signals = []
    if ind["vwap"] == ind["vwap"] and price > ind["vwap"]:
        signals.append(f"Fiyat günün VWAP'ının ({money(ind['vwap'])}) üstünde — alıcılar kontrolde")
    if ind["ema_fast"] > ind["ema_slow"]:
        signals.append("Kısa vadeli trend yukarı (EMA9 > EMA21)")
    if RSI_MIN <= ind["rsi"] <= RSI_MAX:
        signals.append(f"RSI {ind['rsi']:.0f} — güçlü momentum, aşırı şişmemiş")
    if ind["vol_ratio"] >= s["volume_mult"]:
        signals.append(f"Son 15 dk hacmi ortalamanın {ind['vol_ratio']:.1f} katı")
    if ind["day_change"] >= MIN_DAY_CHANGE_PCT:
        signals.append(f"Bugün %{ind['day_change']:+.1f} — hisse hareketli")
    score = len(signals)
    if score < s["min_score"]:
        return None, "low_score", f"{ticker}: skor {score}/5 < {s['min_score']}"

    ext = (price - ind["prior_high"]) / ind["prior_high"] * 100
    if ext > MAX_CHASE_PCT:
        return None, "chased", f"{ticker}: kırılımın %{ext:.1f} üstünde, kovalanmaz"
    if ext >= 0:
        state, entry = "broken", price
    elif -ext <= s["near_pct"]:
        state, entry = "near", ind["prior_high"] * 1.001
    else:
        return None, "far", f"{ticker}: skor {score}/5 ama dirence %{-ext:.1f} uzak"

    lv, err = trade_levels(entry, ind["base_low"], ind["atr15"])
    if lv is None:
        return None, err, f"{ticker}: {REASON_LABELS.get(err, err)}"
    if lv["entry"] > max_affordable_price():
        return None, "too_expensive", f"{ticker}: fiyat {money(price)} bütçeye göre pahalı"
    shares, value = position_size(lv["entry"], lv["stop"])
    if shares < 1:
        return None, "too_expensive", f"{ticker}: bütçeyle 1 adet bile alınamıyor (fiyat {money(price)})"
    # Hedef, komisyonu çıkardıktan sonra da en az MIN_NET_PROFIT kazandırmalı
    fee_driven = False
    if ROUND_TRIP_FEES > 0:
        need_pct = (ROUND_TRIP_FEES + MIN_NET_PROFIT) / value * 100
        if need_pct > lv["reward_pct"]:
            lv["reward_pct"], fee_driven = need_pct, True
            lv["target"] = lv["entry"] * (1 + need_pct / 100)
    daily_atr = meta.get("atr_pct")
    if daily_atr and lv["reward_pct"] > MAX_TARGET_VS_DAILY_ATR * daily_atr:
        code = "fees_too_high" if fee_driven else "target_unrealistic"
        return None, code, (f"{ticker}: hedef %{lv['reward_pct']:.1f} gerekiyor, günlük oynaklık "
                            f"sadece %{daily_atr:.1f}")
    net_win = value * lv["reward_pct"] / 100 - ROUND_TRIP_FEES
    net_loss = value * lv["risk_pct"] / 100 + ROUND_TRIP_FEES
    return {**lv, "ticker": ticker, "state": state, "score": score, "signals": signals,
            "shares": shares, "value": value, "net_win": net_win, "net_loss": net_loss,
            "be_winrate": net_loss / (net_win + net_loss) * 100 if net_win > 0 else 100.0,
            "price": price, "ext": ext, "prior_high": ind["prior_high"], "rsi": ind["rsi"],
            "rr": lv["reward_pct"] / lv["risk_pct"], "daily_atr": daily_atr,
            "rvol": meta.get("rvol", 0), "seen_high": ind["seen_high"], "seen_low": ind["seen_low"],
            "max_buy": ind["prior_high"] * (1 + MAX_CHASE_PCT / 100)}, "ok", f"{ticker}: SİNYAL ({state})"


def max_affordable_price():
    """Bütçeyle alınabilecek en pahalı hisse: hem 1 adedi alabilmeli hem de
    tipik bir stopta kayıp, işlem başı risk sınırını aşmamalı."""
    if MAX_STOCK_PRICE > 0:
        return MAX_STOCK_PRICE
    risk_money = ACCOUNT_SIZE * RISK_PER_TRADE_PCT / 100 - ROUND_TRIP_FEES
    by_risk = risk_money / (TYPICAL_STOP_PCT / 100) if risk_money > 0 else ACCOUNT_SIZE
    return max(MIN_PRICE, min(ACCOUNT_SIZE, by_risk))


def position_size(entry, stop):
    """Normalde işlem başı risk (komisyon dahil) bütçenin RISK_PER_TRADE_PCT'si kadar.
    Sabit komisyon yüzünden bu kural küçük bütçede çok küçük pozisyon üretir; o
    durumda komisyonun payı MAX_FEE_PCT'yi aşmayacak kadar büyük (ama bütçeyi
    geçmeyen) bir pozisyon kullanılır. Böylece bütçe arttıkça pozisyon hiç küçülmez."""
    per_share = entry - stop
    if per_share <= 0 or entry <= 0:
        return 0, 0.0
    by_budget = int(ACCOUNT_SIZE // entry)
    risk_money = ACCOUNT_SIZE * RISK_PER_TRADE_PCT / 100 - ROUND_TRIP_FEES
    by_risk = int(risk_money // per_share) if risk_money > 0 else 0
    by_fees = int((ROUND_TRIP_FEES / (MAX_FEE_PCT / 100)) // entry) if ROUND_TRIP_FEES > 0 else 0
    shares = min(by_budget, max(by_risk, by_fees))
    return shares, shares * entry


# ---------------------------------------------------------------- zenginleştirme
def sentiment_label(avg):
    if avg <= -0.35:
        return "Olumsuz"
    if avg <= -0.15:
        return "Hafif olumsuz"
    if avg < 0.15:
        return "Nötr"
    if avg < 0.35:
        return "Hafif olumlu"
    return "Olumlu"


def get_news_sentiment(ticker, now):
    """Alpha Vantage (günde 25 istek) — sadece sinyal gönderilirken çağrılır.
    Son 48 saatteki ilgili haberlerin ortalama duyarlılığını döndürür."""
    if not ALPHAVANTAGE_API_KEY:
        return None
    since = (now - timedelta(hours=48)).astimezone(pytz.utc).strftime("%Y%m%dT%H%M")
    try:
        r = requests.get("https://www.alphavantage.co/query", timeout=15, params={
            "function": "NEWS_SENTIMENT", "tickers": ticker, "time_from": since,
            "sort": "LATEST", "limit": 50, "apikey": ALPHAVANTAGE_API_KEY})
        data = r.json()
    except Exception as ex:
        log.error("Haber alınamadı (%s): %s", ticker, ex)
        return None
    feed = data.get("feed") or []
    if not feed:
        if data.get("Information") or data.get("Note"):
            log.warning("Alpha Vantage limiti: %s", data.get("Information") or data.get("Note"))
            return None
        return {"count": 0}
    scores, latest = [], None
    for art in feed:
        ts = next((x for x in art.get("ticker_sentiment", []) if x.get("ticker") == ticker), None)
        if not ts:
            continue
        try:
            if float(ts.get("relevance_score", 0)) < 0.25:
                continue
            scores.append(float(ts.get("ticker_sentiment_score", 0)))
        except (TypeError, ValueError):
            continue
        latest = latest or art
    if not scores:
        return {"count": 0}
    avg = sum(scores) / len(scores)
    return {"count": len(scores), "avg": avg, "label": sentiment_label(avg),
            "headline": (latest.get("title") or "")[:140], "source": latest.get("source") or ""}


def earnings_in_days(ticker, now):
    try:
        cal = yf.Ticker(ticker).calendar
        dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if not dates:
            return None
        if not isinstance(dates, (list, tuple)):
            dates = [dates]
        days = []
        for d in dates:
            d = d.date() if isinstance(d, datetime) else d if isinstance(d, date) else pd.Timestamp(d).date()
            days.append((d - now.date()).days)
        future = [x for x in days if x >= 0]
        return min(future) if future else None
    except Exception as ex:
        log.info("Bilanço tarihi alınamadı (%s): %s", ticker, ex)
        return None


def simulate_setups(df, daily_atr_pct, levels_fn):
    """Aynı kurulum (VWAP üstü, trend yukarı, RSI 55-78, hacim ≥1.5x, gün +%2,
    2 saatlik tepenin kapanışla kırılması) geçmişte oluştuğunda, levels_fn'in
    verdiği giriş/stop/hedefle ne olurdu? Yaklaşık bir simülasyondur: giriş
    kırılım mumunun kapanışında, aynı mumda hem stop hem hedef olmaz varsayılır."""
    if df is None or len(df) < 300:
        return None
    reg = np.array([MARKET_OPEN <= ts.time() < MARKET_CLOSE for ts in df.index])
    df = df[reg]
    if len(df) < 300:
        return None
    idx = df.index
    dates = np.array([ts.date() for ts in idx])
    times = np.array([ts.time() for ts in idx])
    c, h, l, v = (df[k].to_numpy(float) for k in ("Close", "High", "Low", "Volume"))
    ph = df["High"].shift(1).rolling(BREAKOUT_LOOKBACK_BARS).max().to_numpy()
    bl = df["Low"].shift(1).rolling(BASE_LOW_BARS).min().to_numpy()
    atr = compute_atr(df, ATR_PERIOD).shift(1).to_numpy()
    e9 = df["Close"].ewm(span=9, adjust=False).mean().to_numpy()
    e21 = df["Close"].ewm(span=21, adjust=False).mean().to_numpy()
    rsi = compute_rsi(df["Close"]).to_numpy()
    vavg = df["Volume"].shift(1).rolling(20).mean().to_numpy()
    tp = (h + l + c) / 3
    cum_pv = pd.Series(tp * v).groupby(dates).cumsum().to_numpy()
    cum_v = pd.Series(v).groupby(dates).cumsum().to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        vwap = np.where(cum_v > 0, cum_pv / cum_v, np.nan)
    last_close = pd.Series(c).groupby(dates).last()
    day_list = list(last_close.index)
    prev_map = {day_list[k]: float(last_close.iloc[k - 1]) for k in range(1, len(day_list))}
    prevc = np.array([prev_map.get(d, np.nan) for d in dates])

    trades, busy, n = [], -1, len(c)
    for i in range(n):
        if i <= busy or not (dtime(9, 45) <= times[i] <= dtime(15, 0)):
            continue
        if not all(np.isfinite(x) for x in (ph[i], atr[i], prevc[i], vavg[i], vwap[i])) or vavg[i] <= 0:
            continue
        if c[i] <= ph[i] or (c[i] - ph[i]) / ph[i] * 100 > MAX_CHASE_PCT:
            continue
        if not (c[i] > vwap[i] and e9[i] > e21[i] and RSI_MIN <= rsi[i] <= RSI_MAX and v[i] >= 1.5 * vavg[i]):
            continue
        if (c[i] / prevc[i] - 1) * 100 < MIN_DAY_CHANGE_PCT:
            continue
        lv = levels_fn(c[i], bl[i], atr[i])
        if lv is None or (daily_atr_pct and lv["reward_pct"] > MAX_TARGET_VS_DAILY_ATR * daily_atr_pct):
            continue
        entry, stop, target = lv["entry"], lv["stop"], lv["target"]
        one_r = entry - stop
        cur_stop, be, result, j, last_j = stop, False, None, i + 1, i
        while j < n and dates[j] == dates[i] and times[j] < REGULAR_EXIT:
            last_j = j
            if l[j] <= cur_stop:
                result = ("breakeven", 0.0) if be else ("stop", -lv["risk_pct"])
                break
            if h[j] >= target:
                result = ("target", lv["reward_pct"])
                break
            if not be and h[j] >= entry + one_r:
                cur_stop, be = entry, True
            j += 1
        if result is None:
            if j >= n:          # veri bugün bitiyor, işlem henüz sonuçlanmadı
                break
            result = ("eod", (c[last_j] - entry) / entry * 100)
        trades.append({**lv, "result": result[0], "pnl": float(result[1]),
                       "entry_ts": idx[i], "exit_ts": idx[last_j]})
        busy = last_j
    return trades


def backtest_setup(df, daily_atr_pct, min_reward=MIN_REWARD_PCT, fee_pct=0.0):
    """Sinyal mesajındaki 'Geçmiş' satırı için bu hissenin özeti (% cinsinden, komisyon sonrası)."""
    trades = simulate_setups(df, daily_atr_pct,
                             lambda entry, base, atr: trade_levels(entry, base, atr, min_reward)[0])
    if trades is None:
        return None
    if not trades:
        return {"n": 0}
    pnls = [t["pnl"] - fee_pct for t in trades]      # komisyon sonrası net %
    counts = {k: sum(1 for t in trades if t["result"] == k) for k in ("target", "stop", "breakeven", "eod")}
    return {"n": len(trades), **counts, "win_rate": sum(1 for p in pnls if p > 0) / len(pnls) * 100,
            "avg": sum(pnls) / len(pnls)}


def live_levels(entry, base_low, atr15):
    """Canlı sinyaldeki kuralların aynısı: bütçe, adet, komisyonu karşılayan hedef."""
    lv, _ = trade_levels(entry, base_low, atr15)
    if lv is None or lv["entry"] > max_affordable_price():
        return None
    shares, value = position_size(lv["entry"], lv["stop"])
    if shares < 1:
        return None
    if ROUND_TRIP_FEES > 0:
        need = (ROUND_TRIP_FEES + MIN_NET_PROFIT) / value * 100
        if need > lv["reward_pct"]:
            lv["reward_pct"] = need
            lv["target"] = lv["entry"] * (1 + need / 100)
    return {**lv, "shares": shares, "value": value}


def run_backtest(budgets=None):
    """Son ~2 ayın 15 dakikalık gerçek verisiyle stratejiyi test eder ve sonucu
    Telegram'a gönderir. Hafta sonu da çalışır. Sonuç yaklaşıktır: hisse listesi
    bugünkü fiyat ve oynaklığa göre seçilir, kayma (slippage) hesaba katılmaz."""
    global ACCOUNT_SIZE
    now = now_et()
    user_budget = ACCOUNT_SIZE
    budgets = budgets or sorted({user_budget, 300.0, 1000.0})
    candidates = list(dict.fromkeys(BASE_UNIVERSE + discover_candidates()))
    daily = yf_download(candidates, period="6mo", interval="1d")
    meta = {}
    for t in candidates:
        d = daily.get(t)
        if d is None or len(d) < 60:
            continue
        hist = d.iloc[:-1] if d.index[-1].date() == now.date() else d
        price = float(hist["Close"].iloc[-1])
        atr_pct = float(compute_atr(hist).iloc[-1]) / price * 100
        dollar_vol = float(hist["Volume"].tail(20).mean()) * price
        if price >= MIN_PRICE and atr_pct >= MIN_DAILY_ATR_PCT and dollar_vol >= MIN_AVG_DOLLAR_VOLUME:
            meta[t] = {"price": price, "atr_pct": atr_pct}
    if not meta:
        send_telegram("🧪 Test yapılamadı: fiyat verisi alınamadı. Birazdan tekrar dene.")
        return
    max_budget = max(budgets)
    try:
        ACCOUNT_SIZE = max_budget
        cap = max_affordable_price()
    finally:
        ACCOUNT_SIZE = user_budget
    tested = [t for t, m in meta.items() if m["price"] <= cap]
    intraday = yf_download(tested, period=BACKTEST_PERIOD, interval=INTRADAY_INTERVAL, prepost=False)
    days = sorted({ts.date() for df in intraday.values() for ts in df.index})

    results = {}
    try:
        for b in budgets:
            ACCOUNT_SIZE = b
            trades = []
            for t in tested:
                if meta[t]["price"] > max_affordable_price():
                    continue
                for tr in simulate_setups(intraday.get(t), meta[t]["atr_pct"], live_levels) or []:
                    tr["ticker"] = t
                    tr["net"] = tr["value"] * tr["pnl"] / 100 - ROUND_TRIP_FEES
                    tr["gross"] = tr["value"] * tr["pnl"] / 100
                    trades.append(tr)
            trades.sort(key=lambda x: x["entry_ts"])
            single, free_at = [], None                   # tek pozisyon: öncekisi kapanmadan yenisine girilmez
            for tr in trades:
                if free_at is None or tr["entry_ts"] > free_at:
                    single.append(tr)
                    free_at = tr["exit_ts"]
            results[b] = {"all": trades, "single": single,
                          "n_tickers": sum(1 for t in tested if meta[t]["price"] <= max_affordable_price())}
    finally:
        ACCOUNT_SIZE = user_budget

    def line(trs):
        if not trs:
            return "işlem yok"
        tg = sum(1 for x in trs if x["result"] == "target")
        return (f"{len(trs)} işlem · 🎯{tg} · 🛑{sum(1 for x in trs if x['result'] == 'stop')} · "
                f"⚪{sum(1 for x in trs if x['result'] == 'breakeven')} · 🔔{sum(1 for x in trs if x['result'] == 'eod')}"
                f" → net {smoney(sum(x['net'] for x in trs))}")

    mine = results[user_budget]
    allt = mine["all"]
    lines = [f"🧪 <b>Geriye dönük test</b> — son {len(days)} işlem günü, gerçek 15 dk verisi",
             f"Bütçene ({money(user_budget)}) uyan {mine['n_tickers']} hisse test edildi.", ""]
    if allt:
        tg_rate = sum(1 for x in allt if x["result"] == "target") / len(allt) * 100
        be_rate = sum((x["value"] * x["risk_pct"] / 100 + ROUND_TRIP_FEES) /
                      max(x["value"] * x["reward_pct"] / 100 + x["value"] * x["risk_pct"] / 100, 1e-9)
                      for x in allt) / len(allt) * 100
        lines.append(f"Tüm sinyaller: {line(allt)}")
        lines.append(f"Hedefe gitme oranı: %{tg_rate:.0f} (kâra geçmek için gereken ~%{be_rate:.0f})")
        lines.append(f"Tek seferde tek işlemle (gerçekçi): {line(mine['single'])}")
        lines.append(f"Aynı işlemler komisyonsuz olsaydı: net {smoney(sum(x['gross'] for x in mine['single']))}")
        by_t = {}
        for x in allt:
            by_t[x["ticker"]] = by_t.get(x["ticker"], 0.0) + x["net"]
        ranked = sorted(by_t.items(), key=lambda kv: kv[1], reverse=True)
        if len(ranked) <= 6:
            lines.append("Hisse bazında: " + ", ".join(f"{e(t)} {smoney(v)}" for t, v in ranked))
        else:
            lines.append("En iyi: " + ", ".join(f"{e(t)} {smoney(v)}" for t, v in ranked[:3]))
            lines.append("En kötü: " + ", ".join(f"{e(t)} {smoney(v)}" for t, v in ranked[-3:][::-1]))
    else:
        lines.append("Bu dönemde bütçene uyan hiçbir hissede kurulum tamamlanmadı.")
    others = [b for b in budgets if b != user_budget]
    if others:
        lines.append("")
        lines.append("<b>Aynı dönem, farklı bütçelerle (tek işlemle):</b>")
        for b in others:
            lines.append(f"{money(b)}: {line(results[b]['single'])}")
    lines.append("")
    lines.append(f"<i>Komisyon işlem başı {money(COMMISSION_PER_SIDE)} × 2 dahil. Yaklaşık simülasyon: kayma yok, "
                 f"hisse listesi bugünkü verilere göre seçildi. Geçmiş sonuç geleceği garanti etmez.</i>")
    send_telegram("\n".join(lines))
    log.info("Test tamamlandı: %d işlem (bütçe %s)", len(allt), user_budget)

def assess_risk(c, session, market, news, earn_days, bt, now):
    """Ağırlıklı risk puanı. Olumsuzlar tam ağırlıkla, olumlular yarım ağırlıkla
    sayılır: iyi haber, yarınki bilançonun riskini silmez. Mesajda sadece
    olumsuzların kısa adları gösterilir."""
    bad, good = [], []            # (kısa ad, ağırlık)
    if earn_days is not None and earn_days <= 2:
        bad.append(("bilanço yakın", 2.0))
    if market.get("weak"):
        bad.append(("piyasa zayıf", 1.5))
    elif market.get("strong"):
        good.append(("piyasa güçlü", 0.5))
    if c["rsi"] > 80:
        bad.append(("aşırı alım", 1.5))
    elif c["rsi"] > 72:
        bad.append(("aşırı alıma yakın", 1.0))
    if session != "regular":
        bad.append(("seans dışı, hacim az", 1.0))
    if c["state"] == "broken" and c["ext"] > 0.5:
        bad.append(("geç giriş", 1.0))
    if news and news.get("count"):
        if news["avg"] <= -0.15:
            bad.append(("haberler olumsuz", 1.0))
        elif news["avg"] >= 0.15:
            good.append(("haberler olumlu", 0.5))
    if c["price"] < 10:
        bad.append(("$10 altı hisse", 1.0))
    t = now.time()
    if session == "regular" and t < dtime(9, 45):
        bad.append(("açılış dalgalı", 1.0))
    if session == "regular" and t >= dtime(15, 0):
        bad.append(("kapanışa az kaldı", 1.0))
    if c.get("daily_atr") and c["reward_pct"] > c["daily_atr"]:
        bad.append(("hedef iddialı", 1.0))
    if bt and bt.get("n", 0) >= 5:
        if bt["avg"] > 0 and bt["win_rate"] >= 45:
            good.append(("geçmişte iyi çalışmış", 0.5))
        elif bt["avg"] < 0:
            bad.append(("geçmişte zarar ettirmiş", 1.5))    # en somut kanıt: daha ağır say
    if ROUND_TRIP_FEES > 0 and c.get("value"):
        gross_win = c["value"] * c["reward_pct"] / 100
        if ROUND_TRIP_FEES >= 0.5 * gross_win:
            bad.append(("komisyon kârın yarısından fazlası", 1.0))
    if c["score"] >= 5:
        good.append(("5/5 sinyal", 0.5))
    points = sum(w for _, w in bad) - sum(w for _, w in good)
    label = "🟢 Düşük" if points <= 0.5 else "🟡 Orta" if points <= 2.0 else "🔴 Yüksek"
    return label, [x for x, _ in bad], [x for x, _ in good]


def signal_message(c, session, market, news, earn_days, bt, risk, now):
    label, bad, _good = risk
    shares, value = c.get("shares"), c.get("value")
    if shares is None:
        shares, value = position_size(c["entry"], c["stop"])
    exit_t = AFTERHOURS_EXIT if session == "afterhours" else REGULAR_EXIT
    exit_local = local_hm(et_at(now.date(), exit_t))
    tag = "" if session == "regular" else f" · {SESSION_LABELS[session]}"
    lines = []
    if c["state"] == "broken":
        lines.append(f"🟢 <b>{e(c['ticker'])} — ŞİMDİ AL</b> (kırılım oldu){tag}")
    else:
        lines.append(f"🟡 <b>{e(c['ticker'])} — ALIŞ EMRİ KOY</b> (kırılıma yakın){tag}")
    lines.append(f"Fiyat: {money(c['price'])} (bugün %{c.get('day_change', 0):+.1f})")
    lines.append("")
    if c["state"] == "broken":
        lines.append(f"Al: şimdi (en fazla {money(c['max_buy'])})")
    else:
        lines.append(f"Al: {money(c['entry'])} geçerse")
    lines.append(f"Stop: {money(c['stop'])} (-%{c['risk_pct']:.1f})")
    lines.append(f"Hedef: {money(c['target'])} (+%{c['reward_pct']:.1f})")
    if shares > 0:
        lines.append(f"Adet: {shares} ≈ {money(value)}")
        if ROUND_TRIP_FEES > 0 and "net_win" in c:
            lines.append(f"Net (komisyon {money(ROUND_TRIP_FEES)} dahil): hedefte {smoney(c['net_win'])}, "
                         f"stopta {smoney(-c['net_loss'])}")
            lines.append(f"Kâra geçmek için bu tür işlemlerin %{c['be_winrate']:.0f}'i hedefe gitmeli")
        else:
            lines.append(f"En fazla kayıp: {money(shares * (c['entry'] - c['stop']))}")
    else:
        lines.append("Adet: sermayene göre çok riskli, bu işlemi geç")
    lines.append("")
    lines.append(f"Risk: {label}" + (f" — {e(', '.join(bad))}" if bad else ""))
    if bt and bt.get("n", 0) >= 5:
        lines.append(f"Geçmiş: benzer {bt['n']} işlem → {bt['target']} hedef, {bt['stop']} stop")
    elif bt and bt.get("n", 0) > 0:
        lines.append(f"Geçmiş: sadece {bt['n']} benzer işlem var, fikir vermez")
    if news and news.get("count"):
        lines.append(f"Haber: {e(news['label'].lower())}")
    lines.append(f"Hedef/stop gelmezse sat: {exit_local}")
    return "\n".join(lines)


# ---------------------------------------------------------------- sinyal takibi
EVENT_TEXT = {
    "triggered": "✅ <b>{t}</b> kırıldı — alış gerçekleşti: {entry} ({time})",
    "breakeven_move": "🔒 <b>{t}</b> yükseliyor — stop'u girişe çek: {entry} ({time})",
    "target": "🎯 <b>{t}</b> hedefe geldi → SAT {target} (+%{pnl:.1f}, net {net}) ({time})",
    "stop": "🛑 <b>{t}</b> stop oldu {stop} (%{pnl:.1f}, net {net}) ({time})",
    "breakeven": "⚪ <b>{t}</b> girişten çıktı (net {net}) ({time})",
    "eod": "🔔 <b>{t}</b> gün bitiyor → SAT {price} (%{pnl:+.1f}, net {net}) ({time})",
    "cancelled": "❌ <b>{t}</b> kırılmadan düştü → alış emrini iptal et ({time})",
    "expired": "⌛ <b>{t}</b> kırılmadı → alış emrini iptal et ({time})",
}

def simulate_signal(sig, df, now):
    """Sinyali, oluşturulduğu andan itibaren mumlar üzerinde baştan oynatır.
    Her çalıştırmada aynı sonucu verir; yalnızca yeni olaylar bildirilir."""
    created = datetime.fromtimestamp(sig["created"], MARKET_TZ)
    created_bar = created.replace(minute=created.minute - created.minute % 15, second=0, microsecond=0)
    exit_t = AFTERHOURS_EXIT if sig.get("session") == "afterhours" else REGULAR_EXIT
    exit_at = et_at(created.date(), exit_t)
    expiry = created + timedelta(minutes=PENDING_EXPIRY_MINUTES)
    entry, target, one_r = sig["entry"], sig["target"], sig["entry"] - sig["stop0"]
    status, stop, be = sig["kind"], sig["stop0"], False
    events, last_close = [], None

    bars = df[(df.index >= created_bar) & (df.index < exit_at)] if df is not None else df
    for ts, bar in (bars.iterrows() if bars is not None else []):
        hi, lo = float(bar["High"]), float(bar["Low"])
        last_close = float(bar["Close"])
        if ts == created_bar:                       # sinyalden önce görülen uçları sayma
            hi = hi if hi > sig["seen_high"] else float("-inf")
            lo = lo if lo < sig["seen_low"] else float("inf")
        if status == "pending":
            if lo <= stop:
                events.append({"type": "cancelled", "ts": ts, "price": stop})
                status = "closed"
                break
            if hi >= entry:
                events.append({"type": "triggered", "ts": ts, "price": entry})
                status = "active"
                continue
            if ts >= expiry:
                events.append({"type": "expired", "ts": ts, "price": None})
                status = "closed"
                break
            continue
        if lo <= stop:
            kind = "breakeven" if be else "stop"
            events.append({"type": kind, "ts": ts, "price": stop,
                           "pnl": 0.0 if be else (stop - entry) / entry * 100})
            status = "closed"
            break
        if hi >= target:
            events.append({"type": "target", "ts": ts, "price": target, "pnl": (target - entry) / entry * 100})
            status = "closed"
            break
        if not be and hi >= entry + one_r:
            events.append({"type": "breakeven_move", "ts": ts, "price": entry + one_r})
            stop, be = entry, True

    if status == "pending" and (now >= expiry or now >= exit_at):
        events.append({"type": "expired", "ts": min(max(expiry, created), exit_at), "price": None})
        status = "closed"
    elif status == "active" and now >= exit_at:
        px = last_close if last_close is not None else entry
        events.append({"type": "eod", "ts": exit_at, "price": px, "pnl": (px - entry) / entry * 100})
        status = "closed"
    return events, status


def update_open_signals(state, data, now):
    closed_ids = []
    for sid, sig in state["signals"].items():
        df = data.get(sig["ticker"])
        created = datetime.fromtimestamp(sig["created"], MARKET_TZ)
        if df is None and now < et_at(created.date(), AFTERHOURS_EXIT):
            continue                                 # bu turda veri yok, sonra tekrar dene
        events, status = simulate_signal(sig, df, now)
        for ev in events[sig.get("sent", 0):]:
            ts = ev["ts"] if isinstance(ev["ts"], datetime) else pd.Timestamp(ev["ts"]).to_pydatetime()
            msg = EVENT_TEXT[ev["type"]].format(
                t=e(sig["ticker"]), time=local_hm(ts), entry=money(sig["entry"]), stop=money(sig["stop0"]),
                target=money(sig["target"]), price=money(ev["price"] or 0), pnl=ev.get("pnl", 0.0),
                net=smoney(net_usd(sig, ev.get("pnl", 0.0))))
            send_telegram(msg)
        sig["sent"] = max(sig.get("sent", 0), len(events))
        if status == "closed":
            final = events[-1] if events else {"type": "expired"}
            state["history"].append({
                "date": created.strftime("%Y-%m-%d"), "ticker": sig["ticker"], "session": sig.get("session"),
                "result": final["type"], "pnl": None if final.get("pnl") is None else round(float(final["pnl"]), 3),
                "net_usd": None if final.get("pnl") is None else round(net_usd(sig, final["pnl"]), 2),
                "triggered": any(x["type"] == "triggered" for x in events) or sig["kind"] == "active"})
            closed_ids.append(sid)
    for sid in closed_ids:
        state["signals"].pop(sid, None)
    state["history"] = state["history"][-400:]


def net_usd(sig, pnl_pct):
    """Komisyon sonrası dolar kâr/zarar (eski kayıtlarda adet yoksa bugünkü ayarla hesaplanır)."""
    value = sig.get("value") or position_size(sig["entry"], sig["stop0"])[1]
    return value * pnl_pct / 100 - sig.get("fees", ROUND_TRIP_FEES)


def register_signal(state, c, session, now):
    sid = f"{c['ticker']}-{int(now.timestamp())}"
    state["signals"][sid] = {
        "ticker": c["ticker"], "created": now.timestamp(), "session": session,
        "kind": "active" if c["state"] == "broken" else "pending",
        "entry": round(float(c["entry"]), 4), "stop0": round(float(c["stop"]), 4),
        "target": round(float(c["target"]), 4), "seen_high": float(c["seen_high"]),
        "seen_low": float(c["seen_low"]), "sent": 0,
        "shares": int(c.get("shares", 0)), "value": round(float(c.get("value", 0.0)), 2), "fees": ROUND_TRIP_FEES}
    state["last_alerted"][c["ticker"]] = now.timestamp()


# ---------------------------------------------------------------- raporlar
def perf_block(history, days=20):
    dates = sorted({h["date"] for h in history})[-days:]
    trades = [h for h in history if h["date"] in dates and h.get("pnl") is not None]
    if not trades:
        return f"Son {days} gün: henüz işlem yok"
    pnls = [h["pnl"] for h in trades]
    nets = [h["net_usd"] for h in trades if h.get("net_usd") is not None]
    wins = sum(1 for h in trades if (h["net_usd"] if h.get("net_usd") is not None else h["pnl"]) > 0)
    net_txt = f", komisyon sonrası net {smoney(sum(nets))}" if nets else ""
    return (f"Son {len(dates)} gün: {len(trades)} işlem, %{wins / len(trades) * 100:.0f} kârla kapandı, "
            f"toplam %{sum(pnls):+.1f}{net_txt}")


def maybe_daily_summary(state, now):
    today = now.strftime("%Y-%m-%d")
    if now.time() < SUMMARY_TIME or state["meta"].get("summary_date") == today:
        return
    todays = [h for h in state["history"] if h["date"] == today]
    lines = ["📊 <b>Günlük rapor</b>"]
    if not todays:
        lines.append("Bugün: sinyal çıkmadı")
    else:
        parts = [f"{e(h['ticker'])} {RESULT_LABELS.get(h['result'], h['result'])}"
                 + (f" %{h['pnl']:+.1f}" if h.get("pnl") is not None else "") for h in todays]
        total = sum(h["pnl"] for h in todays if h.get("pnl") is not None)
        lines.append("Bugün: " + ", ".join(parts))
        nets = [h["net_usd"] for h in todays if h.get("net_usd") is not None]
        lines.append(f"Bugünün toplamı: %{total:+.1f}" + (f" · net {smoney(sum(nets))}" if nets else ""))
    lines.append(perf_block(state["history"]))
    if send_telegram("\n".join(lines)):
        state["meta"]["summary_date"] = today


# ---------------------------------------------------------------- durum
def load_state():
    s = read_json(STATE_FILE, {})
    if s and not any(k in s for k in ("signals", "last_alerted", "history")):
        s = {"last_alerted": {k: v for k, v in s.items() if isinstance(v, (int, float))}}   # eski format
    for key, default in (("last_alerted", {}), ("signals", {}), ("history", []), ("meta", {})):
        if not isinstance(s.get(key), type(default)):
            s[key] = default
    return s


def save_state(state):
    cutoff = time.time() - 3 * 86400
    state["last_alerted"] = {k: v for k, v in state["last_alerted"].items() if v > cutoff}
    write_json(STATE_FILE, state)


# ---------------------------------------------------------------- ana akış
def scan(watch, data, meta, session, market, state, now):
    reasons, cands = {}, []
    open_tickers = {s["ticker"] for s in state["signals"].values()}
    for t in watch:
        df = data.get(t)
        if df is None:
            reasons["no_data"] = reasons.get("no_data", 0) + 1
            continue
        if now - df.index[-1].to_pydatetime() > timedelta(minutes=DATA_STALE_MINUTES + 10):
            reasons["stale"] = reasons.get("stale", 0) + 1
            continue
        if t in open_tickers:
            reasons["open_signal"] = reasons.get("open_signal", 0) + 1
            continue
        last = state["last_alerted"].get(t)
        if last and now.timestamp() - last < ALERT_COOLDOWN_MINUTES * 60:
            reasons["cooldown"] = reasons.get("cooldown", 0) + 1
            continue
        ind = compute_intraday(df)
        if not ind:
            reasons["no_data"] = reasons.get("no_data", 0) + 1
            continue
        c, code, why = evaluate(t, ind, meta.get(t, {}), session)
        log.info(why)
        if c:
            c["day_change"] = ind["day_change"]
            cands.append(c)
        else:
            reasons[code] = reasons.get(code, 0) + 1

    cands.sort(key=lambda c: (c["score"], c["state"] == "broken", c["rr"], c["rvol"]), reverse=True)
    sent = 0
    for c in cands[:MAX_ALERTS_PER_RUN]:
        news = get_news_sentiment(c["ticker"], now)
        earn = earnings_in_days(c["ticker"], now)
        hist = yf_download([c["ticker"]], period=BACKTEST_PERIOD, interval=INTRADAY_INTERVAL, prepost=False)
        bt = backtest_setup(hist.get(c["ticker"]), c.get("daily_atr"), min_reward=c["reward_pct"],
                            fee_pct=ROUND_TRIP_FEES / c["value"] * 100)
        risk = assess_risk(c, session, market, news, earn, bt, now)
        if send_telegram(signal_message(c, session, market, news, earn, bt, risk, now)):
            register_signal(state, c, session, now)
            sent += 1
            log.info("Sinyal gönderildi: %s (%s, risk %s)", c["ticker"], c["state"], risk[0])
    return sent, len(cands), reasons


def run_once(state, force=False):
    now = now_et()
    session = get_session(now)
    head = f"{SESSION_LABELS[session]} (New York {now.strftime('%H:%M')} · senin saatin {local_hm(now)})"
    if session == "closed":
        log.info("Piyasa kapalı.")
        if force:
            send_telegram(f"✅ Bot çalışıyor — {head}\nHafta içi 04:00-20:00 New York saatinde "
                          f"({local_hm(et_at(now.date(), PREMARKET_OPEN))}-{local_hm(et_at(now.date(), AFTERHOURS_CLOSE))}"
                          f" senin saatinle) tarama yapıyorum.")
        return

    watch, meta, announce = load_or_build_watchlist(session, now)
    tickers = set(watch) | {s["ticker"] for s in state["signals"].values()} | {MARKET_PROXY}
    data = yf_download(tickers, period="5d", interval=INTRADAY_INTERVAL, prepost=True)
    today = now.strftime("%Y-%m-%d")

    if not data:
        state["meta"]["fail_count"] = state["meta"].get("fail_count", 0) + 1
        log.error("Fiyat verisi alınamadı (üst üste %d kez)", state["meta"]["fail_count"])
        if force or (state["meta"]["fail_count"] >= 3 and session == "regular"
                     and state["meta"].get("fail_warned") != today):
            send_telegram(f"⚠️ {head}\nFiyat verisi alınamıyor (Yahoo erişimi). Bot bir sonraki turda "
                          f"tekrar deneyecek; sürerse GitHub Actions loguna bak.")
            state["meta"]["fail_warned"] = today
        return
    state["meta"]["fail_count"] = 0

    newest = max(df.index[-1].to_pydatetime() for df in data.values())
    if now - newest > timedelta(minutes=DATA_STALE_MINUTES):
        log.info("Veri bayat (son mum %s) — tatil veya işlem yok.", newest)
        if force:
            send_telegram(f"✅ Bot çalışıyor — {head}\nAma son fiyat verisi {local_hm(newest)} tarihli: "
                          f"bugün borsa tatili olabilir ya da bu saatte işlem yok.")
        return

    update_open_signals(state, data, now)
    if announce:
        send_telegram(watchlist_message(watch, meta, announce, now))
    market = market_context(data.get(MARKET_PROXY))
    sent, n_cands, reasons = scan(watch, data, meta, session, market, state, now)
    if session == "afterhours":
        maybe_daily_summary(state, now)

    if force:
        why = " · ".join(f"{REASON_LABELS.get(k, k)} {v}" for k, v in sorted(reasons.items(), key=lambda x: -x[1]))
        pending = sum(1 for s in state["signals"].values() if s["kind"] == "pending")
        send_telegram("\n".join([
            f"✅ <b>Bot çalışıyor</b> — {head}",
            f"📋 Takip listesi ({len(watch)}): {e(', '.join(watch))}",
            f"💰 Bütçe: {money(ACCOUNT_SIZE)} · komisyon al+sat {money(ROUND_TRIP_FEES)} · "
            f"hisse fiyatı en fazla {money(max_affordable_price())}",
            f"🌎 Piyasa (SPY): {e(market['text'])}",
            f"🔎 {len(watch)} hisse tarandı → {n_cands} uygun kurulum, {sent} sinyal gönderildi",
            f"Elenme nedenleri: {e(why) if why else '—'}",
            f"📂 Takipteki sinyal: {len(state['signals'])} ({pending} bekleyen)",
            perf_block(state["history"]),
        ]))


def main():
    if RUN_MODE == "backtest":
        run_backtest()
        return
    state = load_state()
    if RUN_MODE == "once":
        try:
            run_once(state, force=FORCE_RUN)
        finally:
            save_state(state)
        return

    log.info("Döngü modu başladı.")
    send_telegram("🤖 Bot başladı (döngü modu).")
    while True:
        try:
            run_once(state)
        except Exception as ex:
            log.exception("Tur hatası: %s", ex)
        save_state(state)
        time.sleep(LOOP_SECONDS if get_session(now_et()) != "closed" else 300)


if __name__ == "__main__":
    main()
