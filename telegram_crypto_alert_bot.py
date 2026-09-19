"""
Kripto Kırılım Botu (Telegram) — Midas Kripto için
==================================================

Ne yapar
--------
1. Midas Kripto'da işlem gören coinlerden, Coinbase'de de USD paritesi olanları
   saatte bir günlük oynaklık ve işlem hacmine göre sıralar, en likit
   WATCHLIST_SIZE tanesini takip eder.
2. Her çalıştırmada bu coinlerin 15 dakikalık mumlarını çeker ve son ~2 saatin
   tepesinin kırılıp kırılmadığına bakar (7/24, hafta sonu dahil).
3. Kırılım olduğunda: şimdi al / hedef (limit satış) / stop / tutar / komisyon
   sonrası net kâr-zarar / gerekçeli risk / aynı kurulumun bu coinde son 60
   gündeki sonucu.
4. Sinyali takip eder: stop'u başa-başa çek / hedef / stop / süre doldu mesajları.
   Midas Kripto'da stop emri olmadığı için stop'u bot takip eder ve haber verir.
5. Her gün 23:00'te (yerel saat) günlük rapor gönderir.
6. RUN_MODE=backtest: son 60 günün gerçek verisiyle 3 farklı hedef ayarını test
   eder. Verinin ilk yarısı ile ikinci yarısını ayrı raporlar; bir ayarın
   güvenilir sayılması için İKİ yarıda da kâr etmesi gerekir.

Veri: Coinbase Exchange açık API (anahtar gerekmez, gerçek zamanlı). Fiyatlar
USD cinsinden; Midas'taki USDT fiyatı genelde çok yakındır.

Teknik tarayıcıdır, yatırım tavsiyesi değildir. Kripto çok oynaktır; kaybetmeyi
göze alamayacağın parayla işlem yapma.

Ortam değişkenleri (GitHub > Settings > Secrets / Variables)
------------------------------------------------------------
TELEGRAM_TOKEN, TELEGRAM_CHAT_ID   (zorunlu, secret — hisse botuyla aynı)
CRYPTO_BUDGET        (opsiyonel variable, varsayılan 80 $)
CRYPTO_FEE_PCT       (opsiyonel variable, varsayılan 0.15 — alış ve satışta ayrı ayrı %)
CRYPTO_MIN_REWARD    (opsiyonel variable, varsayılan 5 — hedef en az yüzde kaç)
RUN_MODE             ("once" = GitHub Actions, "loop" = kendi bilgisayarın, "backtest", "swing", "plans")
"""

import os
import re
import json
import html
import time
import logging
from datetime import datetime, timedelta, time as dtime

import numpy as np
import pandas as pd
import requests
import pytz

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("cryptobot")


def _env_float(name, default):
    raw = os.environ.get(name, "")
    try:
        return float(raw) if raw.strip() else default
    except ValueError:
        return default


# ---------------------------------------------------------------- AYARLAR
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
RUN_MODE = os.environ.get("RUN_MODE", "loop")
FORCE_RUN = os.environ.get("FORCE_RUN", "false").lower() == "true"
STATE_FILE = os.environ.get("CRYPTO_STATE_FILE", "crypto_state.json")
WATCHLIST_FILE = os.environ.get("CRYPTO_WATCHLIST_FILE", "crypto_watchlist.json")

BUDGET = _env_float("CRYPTO_BUDGET", 80.0)
FEE_PCT = _env_float("CRYPTO_FEE_PCT", 0.15)             # Midas Kripto: işlem başı %0,15
MIN_REWARD_PCT = _env_float("CRYPTO_MIN_REWARD", 5.0)
RISK_PER_TRADE_PCT = 2.0      # stopta en fazla bütçenin %2'si kaybedilsin (kripto oynak: hisseden geniş)

UTC = pytz.utc
LOCAL_TZ = pytz.timezone(os.environ.get("LOCAL_TZ") or "Europe/Podgorica")
CB_API = "https://api.exchange.coinbase.com"
CB_HEADERS = {"User-Agent": "midas-crypto-alert-bot/1.0", "Accept": "application/json"}

# Midas Kripto'daki coinler (midaskripto.com/midastaki-coinler, Eylül 2026).
# Stabil coinler ve altın tokenları çıkarıldı; Coinbase'de USD paritesi olmayanlar
# çalışma anında otomatik elenir.
MIDAS_COINS = set("""
WCT 0G 1INCH AEVO AAVE ACM ACX ACT AERO AI16Z AIXBT ACH ALGO ALLO AMP ANIME ANKR APE API3 APT ARB
ARIA ARK ARKM ARPA FET AR ASR ASTR ASTER ATM BEAT AUDIO AVAX AXL AXS BASED BEAM BEL BERA BNB BNX BIO
BTC BCH TAO BTT BLUR BONK BOME AUCTION BB ZKC BRETT BREV BMT GALA ADA CTSI CSPR MEW CATI TIA CFG CETUS
LINK COAI CHZ COMP CFX CORE ATOM COTI COW CRO CRV CYBER MANA HOME DEGEN DEXE DOGE DOGS WIF 2Z DYDX
DYM EIGEN ENJ ENSO EOS ENA ETHFI ETH ENS ROBO FF FARTCOIN BAR PORTO FIL FLR FLOKI FOGO GMT GNO GOAT
GPS GRASS G HMSTR HBAR HOT ZEN HUMA H WET HYPE IMX INIT INJ ICP IO IOTA JASMY JTO JUP JST JUV KAIA
KAITO KAS KAT KERNEL KITE KSM LAUNCHCOIN ZRO LDO LIT LINEA LTC LPT BARD LUMIA ME CITY MNT MANTRA
MANTA SYRUP MASK MEME MERL MET METIS MINA MOG MOODENG MORPHO MOVE EGLD SHELL NAORIS NEAR NEIRO NEO
CKB NEWT NEXO NOT NMR ROSE TRUMP OKB OMNI ONDO XCN EDU EDEN OPN OP ORCA ORDER ORDI OGN CAKE PRCL PSG
PNUT PENDLE PEPE PRL PHA PI PIXEL XPL PLUME DOT POL PENGU PUMP PYTH QANX QTUM QNT QUBIC RAD RAVE RAY
RED RENDER REZ XRP RONIN LAZIO SAGA SAHARA SAPIEN SCR SKR SEI SENT SHIB CAT SIREN SKL SKY SOL LAYER
SOLV SOMI S SXT SPELL SPX STX STRK XLM STEEM STORJ IP SUI SUN RARE SUPER SUSHI SNX TRB TNSR GRT SAND
THETA TON TOSHI MAGIC TREE TRX TWT TURBO UMA UNI CHIP USUAL VANA VANRY VET VVV VINE VIRTUAL VSN WAL
WLFI WLD W XAI XDC ZETA ZEUS ZK ZORA
""".split())
MARKET_COIN = "BTC"

# --- Coin seçimi (günlük mumlar) ---
WATCHLIST_SIZE = 30
WATCHLIST_REFRESH_MINUTES = 60
MIN_DAILY_ATR_PCT = 4.0          # günde ortalama %4'ten az oynayan coin %5 hedefe uygun değil
MIN_DOLLAR_VOLUME = 5_000_000    # Coinbase'de günlük ortalama işlem hacmi ($)

# --- Kırılım sinyali (15 dakikalık mumlar) ---
GRAN = 900                       # 15 dk (saniye)
BARS_PER_DAY = 96
BREAKOUT_LOOKBACK_BARS = 8       # ~2 saat
BASE_LOW_BARS = 4
ATR_PERIOD = 14
RSI_MIN, RSI_MAX = 55, 78
MIN_CHANGE_24H = 2.0             # son 24 saatte en az +%2 = hareketli coin
MIN_SCORE = 3
VOLUME_MULT = 1.5
MAX_CHASE_PCT = 1.0
STOP_ATR_MULT = 1.5
MIN_STOP_ATR = 1.0
MAX_RISK_PCT = 3.0
TARGET_RR = 2.0
MAX_TARGET_VS_DAILY_ATR = 1.3
MAX_HOLD_HOURS = 24              # hedef/stop gelmezse en geç 24 saatte çık
ALERT_COOLDOWN_MINUTES = 60
MAX_ALERTS_PER_RUN = 3
DATA_STALE_MINUTES = 45
SUMMARY_LOCAL_HOUR = 23
BACKTEST_DAYS = 60
LOOP_SECONDS = 60

BACKTEST_VARIANTS = [                       # (etiket, hedefin alt sınırı %)
    ("A · hedef en az %5", 5.0),
    ("B · hedef en az %3", 3.0),
    ("C · hedef riskin 2 katı (alt sınır yok)", 0.0),
]

REASON_LABELS = {
    "low_score": "sinyal skoru düşük", "no_breakout": "kırılım yok", "chased": "kırılım kaçmış",
    "risk_wide": "stop çok uzak", "target_unrealistic": "hedef gerçekçi değil", "no_data": "veri yok",
    "stale": "veri bayat", "cooldown": "yakın zamanda sinyal verildi", "open_signal": "zaten takipte",
}
RESULT_LABELS = {"target": "🎯", "stop": "🛑", "breakeven": "⚪", "timeout": "⏰"}


# ---------------------------------------------------------------- yardımcılar
def e(x):
    return html.escape(str(x), quote=False)


def price_fmt(x):
    if x >= 100:
        return f"${x:,.2f}"
    if x >= 1:
        return f"${x:,.3f}"
    return f"${x:.6g}"


def money(x):
    return f"${x:,.2f}"


def smoney(x):
    return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


def now_utc():
    return datetime.now(UTC)


def local_hm(dt):
    return dt.astimezone(LOCAL_TZ).strftime("%H:%M")


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


def _strip_tags(text):
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def send_telegram(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram ayarlı değil — mesaj loga yazılıyor:\n%s", _strip_tags(text))
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for chunk in [text[i:i + 3900] for i in range(0, len(text), 3900)] or [text]:
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": chunk, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
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


# ---------------------------------------------------------------- Coinbase verisi
def cb_get(path, params=None, retries=3):
    for attempt in range(retries):
        try:
            r = requests.get(CB_API + path, params=params, headers=CB_HEADERS, timeout=20)
            if r.status_code == 429:
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.ok:
                return r.json()
            log.warning("Coinbase %s -> %s %s", path, r.status_code, r.text[:120])
            return None
        except Exception as ex:
            log.warning("Coinbase isteği başarısız (%s): %s", path, ex)
            time.sleep(1.0 * (attempt + 1))
    return None


def cb_products():
    """Midas'taki coinlerden Coinbase'de aktif USD paritesi olanlar: {coin: 'BTC-USD'}."""
    data = cb_get("/products") or []
    out = {}
    for p in data if isinstance(data, list) else []:
        base = str(p.get("base_currency", "")).upper()
        if (p.get("quote_currency") == "USD" and p.get("status") == "online"
                and not p.get("trading_disabled") and base in MIDAS_COINS):
            out[base] = p.get("id") or f"{base}-USD"
    return out


def _candles_df(data):
    if not isinstance(data, list) or not data:
        return None
    df = pd.DataFrame(data, columns=["ts", "Low", "High", "Open", "Close", "Volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    df = df.set_index("ts").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df[["Open", "High", "Low", "Close", "Volume"]].astype(float)


def cb_candles(pid, granularity=GRAN, start=None, end=None):
    params = {"granularity": granularity}
    if start is not None:
        params["start"] = start.isoformat()
        params["end"] = end.isoformat()
    return _candles_df(cb_get(f"/products/{pid}/candles", params))


def cb_history(pid, days, granularity=GRAN):
    """Mumları sayfa sayfa geriye doğru çeker (istek başına en fazla 300 mum)."""
    end = now_utc().replace(second=0, microsecond=0)
    limit = end - timedelta(days=days)
    frames, cur = [], end
    while cur > limit:
        start = max(limit, cur - timedelta(seconds=granularity * 299))
        df = cb_candles(pid, granularity, start, cur)
        if df is not None:
            frames.append(df)
        cur = start
        time.sleep(0.12)
    if not frames:
        return None
    df = pd.concat(frames).sort_index()
    return df[~df.index.duplicated(keep="last")]


# ---------------------------------------------------------------- göstergeler
def compute_rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).where(loss != 0, 100.0)


def compute_atr(df, period=14):
    prev_close = df["Close"].shift(1)
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - prev_close).abs(),
                    (df["Low"] - prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def compute_intraday(df):
    """Son mum = anlık fiyat; direnç/stop tabanı/ATR/hacim kapanmış mumlardan."""
    if df is None or len(df) < BARS_PER_DAY + 25:
        return None
    closed = df.iloc[:-1]
    price = float(df["Close"].iloc[-1])
    win = df.iloc[-BARS_PER_DAY:]
    tp = (win["High"] + win["Low"] + win["Close"]) / 3
    vsum = float(win["Volume"].sum())
    vwap = float((tp * win["Volume"]).sum() / vsum) if vsum > 0 else float("nan")
    ref = float(df["Close"].iloc[-BARS_PER_DAY - 1])
    avg_vol = float(closed["Volume"].iloc[-21:-1].mean())
    return {
        "price": price, "last_ts": df.index[-1],
        "prior_high": float(closed["High"].iloc[-BREAKOUT_LOOKBACK_BARS:].max()),
        "base_low": float(closed["Low"].iloc[-BASE_LOW_BARS:].min()),
        "atr15": float(compute_atr(closed, ATR_PERIOD).iloc[-1]),
        "rsi": float(compute_rsi(df["Close"]).iloc[-1]),
        "ema_fast": float(df["Close"].ewm(span=9, adjust=False).mean().iloc[-1]),
        "ema_slow": float(df["Close"].ewm(span=21, adjust=False).mean().iloc[-1]),
        "vwap": vwap,
        "change24": (price / ref - 1) * 100 if ref > 0 else 0.0,
        "vol_ratio": float(closed["Volume"].iloc[-1]) / avg_vol if avg_vol > 0 else 0.0,
        "dollar_vol24": float((win["Close"] * win["Volume"]).sum()),
        "seen_high": float(df["High"].iloc[-1]), "seen_low": float(df["Low"].iloc[-1]),
    }


def trade_levels(entry, base_low, atr15, min_reward=None):
    """Giriş/stop/hedef + bütçeye göre tutar ve komisyon sonrası net (canlı ve test için ortak)."""
    min_reward = MIN_REWARD_PCT if min_reward is None else min_reward
    if not (np.isfinite(atr15) and atr15 > 0 and entry > 0):
        return None, "no_data"
    stop = max(entry - STOP_ATR_MULT * atr15, base_low * 0.999 if np.isfinite(base_low) else 0.0)
    if entry - stop < MIN_STOP_ATR * atr15:
        stop = entry - MIN_STOP_ATR * atr15
    risk_pct = (entry - stop) / entry * 100
    if risk_pct <= 0 or risk_pct > MAX_RISK_PCT:
        return None, "risk_wide"
    reward_pct = max(min_reward, TARGET_RR * risk_pct)
    value = min(BUDGET, BUDGET * RISK_PER_TRADE_PCT / 100 / (risk_pct / 100))
    fees = value * FEE_PCT / 100 * 2
    net_win = value * reward_pct / 100 - fees
    net_loss = value * risk_pct / 100 + fees
    return {"entry": entry, "stop": stop, "target": entry * (1 + reward_pct / 100), "risk_pct": risk_pct,
            "reward_pct": reward_pct, "value": value, "qty": value / entry, "fees": fees,
            "net_win": net_win, "net_loss": net_loss,
            "be_winrate": net_loss / (net_win + net_loss) * 100 if net_win > 0 else 100.0}, None


# ---------------------------------------------------------------- coin seçimi
def rank_coins(products):
    scored, meta = [], {}
    for coin, pid in products.items():
        d = cb_candles(pid, 86400)
        time.sleep(0.12)
        if d is None or len(d) < 30:
            continue
        hist = d.iloc[:-1]                          # son gün henüz bitmedi
        price = float(d["Close"].iloc[-1])
        if price <= 0:
            continue
        atr_pct = float(compute_atr(hist).iloc[-1]) / price * 100
        dollar_vol = float((hist["Close"] * hist["Volume"]).tail(7).mean())
        if atr_pct < MIN_DAILY_ATR_PCT or dollar_vol < MIN_DOLLAR_VOLUME:
            continue
        scored.append((coin, dollar_vol))
        meta[coin] = {"pid": pid, "atr_pct": round(atr_pct, 2), "dollar_vol": round(dollar_vol)}
    scored.sort(key=lambda x: x[1], reverse=True)
    top = [c for c, _ in scored[:WATCHLIST_SIZE]]
    log.info("Takip listesi (%d): %s", len(top), top)
    return top, {c: meta[c] for c in top}


def load_or_build_watchlist(now):
    data = read_json(WATCHLIST_FILE, {})
    if data.get("coins") and time.time() - float(data.get("built_at", 0)) < WATCHLIST_REFRESH_MINUTES * 60:
        return data["coins"], data.get("meta", {}), data.get("btc_pid", "BTC-USD"), False
    products = cb_products()
    if not products:
        log.error("Coinbase ürün listesi alınamadı")
        return data.get("coins", []), data.get("meta", {}), data.get("btc_pid", "BTC-USD"), False
    coins, meta = rank_coins(products)
    if not coins and data.get("coins"):
        return data["coins"], data.get("meta", {}), data.get("btc_pid", "BTC-USD"), False
    today = now.astimezone(LOCAL_TZ).strftime("%Y-%m-%d")
    announce = data.get("announced") != today
    write_json(WATCHLIST_FILE, {"built_at": time.time(), "coins": coins, "meta": meta,
                                "btc_pid": products.get(MARKET_COIN, "BTC-USD"),
                                "announced": today if announce else data.get("announced")})
    return coins, meta, products.get(MARKET_COIN, "BTC-USD"), announce


# ---------------------------------------------------------------- sinyal
def market_context(btc_ind):
    if not btc_ind:
        return {"weak": False, "strong": False, "text": "bilinmiyor"}
    above = btc_ind["vwap"] == btc_ind["vwap"] and btc_ind["price"] > btc_ind["vwap"]
    chg = btc_ind["change24"]
    weak = chg < -2.0 or (not above and chg < -0.5)
    strong = chg > 1.0 and above
    return {"weak": weak, "strong": strong,
            "text": f"BTC 24s %{chg:+.1f} · {'güçlü' if strong else 'zayıf' if weak else 'nötr'}"}


def evaluate(coin, ind, meta):
    price = ind["price"]
    signals = []
    if ind["vwap"] == ind["vwap"] and price > ind["vwap"]:
        signals.append("vwap")
    if ind["ema_fast"] > ind["ema_slow"]:
        signals.append("trend")
    if RSI_MIN <= ind["rsi"] <= RSI_MAX:
        signals.append("rsi")
    if ind["vol_ratio"] >= VOLUME_MULT:
        signals.append("volume")
    if ind["change24"] >= MIN_CHANGE_24H:
        signals.append("change")
    score = len(signals)
    if score < MIN_SCORE:
        return None, "low_score", f"{coin}: skor {score}/5"
    ext = (price - ind["prior_high"]) / ind["prior_high"] * 100
    if ext < 0:
        return None, "no_breakout", f"{coin}: dirence %{-ext:.1f} uzak"
    if ext > MAX_CHASE_PCT:
        return None, "chased", f"{coin}: kırılımın %{ext:.1f} üstünde"
    lv, err = trade_levels(price, ind["base_low"], ind["atr15"])
    if lv is None:
        return None, err, f"{coin}: {REASON_LABELS.get(err, err)}"
    daily_atr = meta.get("atr_pct")
    if daily_atr and lv["reward_pct"] > MAX_TARGET_VS_DAILY_ATR * daily_atr:
        return None, "target_unrealistic", f"{coin}: hedef %{lv['reward_pct']:.1f} > oynaklık %{daily_atr:.1f}"
    return {**lv, "coin": coin, "score": score, "signals": signals, "price": price, "ext": ext,
            "rsi": ind["rsi"], "change24": ind["change24"], "daily_atr": daily_atr,
            "dollar_vol24": ind["dollar_vol24"], "seen_high": ind["seen_high"], "seen_low": ind["seen_low"],
            "max_buy": ind["prior_high"] * (1 + MAX_CHASE_PCT / 100)}, "ok", f"{coin}: SİNYAL"


def assess_risk(c, market, bt):
    bad, good = [], []
    if c["rsi"] > 80:
        bad.append(("aşırı alım", 1.5))
    elif c["rsi"] > 72:
        bad.append(("aşırı alıma yakın", 1.0))
    if c["ext"] > 0.5:
        bad.append(("geç giriş", 1.0))
    if market.get("weak"):
        bad.append(("BTC zayıf", 1.5))
    elif market.get("strong"):
        good.append(("BTC güçlü", 0.5))
    if c.get("daily_atr") and c["reward_pct"] > c["daily_atr"]:
        bad.append(("hedef iddialı", 1.0))
    if c["dollar_vol24"] < 20_000_000:
        bad.append(("hacim düşük", 1.0))
    if bt and bt.get("n", 0) >= 5:
        if bt["net"] < 0:
            bad.append(("geçmişte zarar ettirmiş", 1.5))
        elif bt["target_rate"] >= c["be_winrate"]:
            good.append(("geçmişte iyi çalışmış", 0.5))
    if c["score"] >= 5:
        good.append(("5/5 sinyal", 0.5))
    points = sum(w for _, w in bad) - sum(w for _, w in good)
    label = "🟢 Düşük" if points <= 0.5 else "🟡 Orta" if points <= 2.0 else "🔴 Yüksek"
    return label, [x for x, _ in bad]


def signal_message(c, market, bt, risk):
    label, bad = risk
    lines = [f"🟢 <b>{e(c['coin'])} — ŞİMDİ AL</b> (kırılım oldu)",
             f"Fiyat: {price_fmt(c['price'])} (24 saat %{c['change24']:+.1f})", "",
             f"Al: şimdi, en fazla {price_fmt(c['max_buy'])} (piyasa emri)",
             f"Hedef: {price_fmt(c['target'])} (+%{c['reward_pct']:.1f}) → alır almaz limit satış emri koy",
             f"Stop: {price_fmt(c['stop'])} (-%{c['risk_pct']:.1f}) → Midas'ta stop emri yok, gelirse haber vereceğim",
             f"Tutar: {money(c['value'])} (≈ {c['qty']:.6g} {e(c['coin'])})",
             f"Net (komisyon dahil): hedefte {smoney(c['net_win'])}, stopta {smoney(-c['net_loss'])}",
             f"Kâra geçmek için bu tür işlemlerin %{c['be_winrate']:.0f}'i hedefe gitmeli", "",
             f"Risk: {label}" + (f" — {e(', '.join(bad))}" if bad else "")]
    if bt and bt.get("n", 0) >= 5:
        lines.append(f"Geçmiş (60 gün): benzer {bt['n']} işlem → {bt['target']} hedef, {bt['stop']} stop, "
                     f"net {smoney(bt['net'])}")
    elif bt and bt.get("n", 0) > 0:
        lines.append(f"Geçmiş: sadece {bt['n']} benzer işlem var, fikir vermez")
    lines.append(f"Hedef/stop gelmezse en geç {MAX_HOLD_HOURS} saat içinde sat")
    return "\n".join(lines)


# ---------------------------------------------------------------- geçmiş testi
def simulate_setups(df, daily_atr_pct, min_reward):
    """Canlı kuralların aynısı geçmiş mumlarda: giriş kırılım mumunun kapanışında,
    çıkış stop / hedef / başa-baş / süre dolumu. Aynı mumda stop ve hedef olmaz varsayılır."""
    if df is None or len(df) < BARS_PER_DAY * 3:
        return None
    idx = df.index
    c, h, l, v = (df[k].to_numpy(float) for k in ("Close", "High", "Low", "Volume"))
    ph = df["High"].shift(1).rolling(BREAKOUT_LOOKBACK_BARS).max().to_numpy()
    bl = df["Low"].shift(1).rolling(BASE_LOW_BARS).min().to_numpy()
    atr = compute_atr(df, ATR_PERIOD).shift(1).to_numpy()
    e9 = df["Close"].ewm(span=9, adjust=False).mean().to_numpy()
    e21 = df["Close"].ewm(span=21, adjust=False).mean().to_numpy()
    rsi = compute_rsi(df["Close"]).to_numpy()
    vavg = df["Volume"].shift(1).rolling(20).mean().to_numpy()
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    vwap = ((tp * df["Volume"]).rolling(BARS_PER_DAY).sum() / df["Volume"].rolling(BARS_PER_DAY).sum()).to_numpy()
    ref = df["Close"].shift(BARS_PER_DAY).to_numpy()
    max_hold = int(MAX_HOLD_HOURS * 4)

    trades, busy, n = [], -1, len(c)
    for i in range(BARS_PER_DAY, n):
        if i <= busy or not all(np.isfinite(x) for x in (ph[i], atr[i], vavg[i], vwap[i], ref[i])):
            continue
        if c[i] <= ph[i] or (c[i] - ph[i]) / ph[i] * 100 > MAX_CHASE_PCT:
            continue
        score = sum([c[i] > vwap[i], e9[i] > e21[i], RSI_MIN <= rsi[i] <= RSI_MAX,
                     vavg[i] > 0 and v[i] >= VOLUME_MULT * vavg[i], (c[i] / ref[i] - 1) * 100 >= MIN_CHANGE_24H])
        if score < MIN_SCORE:
            continue
        lv, _ = trade_levels(c[i], bl[i], atr[i], min_reward)
        if lv is None or (daily_atr_pct and lv["reward_pct"] > MAX_TARGET_VS_DAILY_ATR * daily_atr_pct):
            continue
        entry, target, one_r = lv["entry"], lv["target"], lv["entry"] - lv["stop"]
        cur_stop, be, result, j, last_j = lv["stop"], False, None, i + 1, i
        while j < n and j - i <= max_hold:
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
            if j >= n:
                break                                   # veri bitti, işlem sonuçlanmadı
            result = ("timeout", (c[last_j] - entry) / entry * 100)
        gross = lv["value"] * result[1] / 100
        trades.append({**lv, "result": result[0], "pnl": float(result[1]), "gross": gross,
                       "net": gross - lv["fees"], "entry_ts": idx[i], "exit_ts": idx[last_j]})
        busy = last_j
    return trades


def summarize(trades):
    if not trades:
        return {"n": 0, "net": 0.0}
    tg = sum(1 for t in trades if t["result"] == "target")
    return {"n": len(trades), "target": tg, "stop": sum(1 for t in trades if t["result"] == "stop"),
            "breakeven": sum(1 for t in trades if t["result"] == "breakeven"),
            "timeout": sum(1 for t in trades if t["result"] == "timeout"),
            "target_rate": tg / len(trades) * 100, "net": sum(t["net"] for t in trades),
            "gross": sum(t["gross"] for t in trades)}


def single_position(trades):
    """Bütçe tek pozisyona yetiyor: önceki işlem kapanmadan yenisine girilmez."""
    out, free_at = [], None
    for t in sorted(trades, key=lambda x: x["entry_ts"]):
        if free_at is None or t["entry_ts"] > free_at:
            out.append(t)
            free_at = t["exit_ts"]
    return out


def run_backtest():
    now = now_utc()
    products = cb_products()
    if not products:
        send_telegram("🧪 Kripto testi yapılamadı: Coinbase verisi alınamadı. Birazdan tekrar dene.")
        return
    coins, meta = rank_coins(products)
    history = {}
    for coin in coins:
        history[coin] = cb_history(meta[coin]["pid"], BACKTEST_DAYS)
        log.info("%s: %s mum", coin, 0 if history[coin] is None else len(history[coin]))
    start = now - timedelta(days=BACKTEST_DAYS)
    mid = start + (now - start) / 2

    lines = [f"🧪 <b>Kripto geriye dönük test</b> — son {BACKTEST_DAYS} gün, gerçek 15 dk verisi",
             f"{len(coins)} coin · bütçe {money(BUDGET)} · komisyon %{FEE_PCT:g} × 2 · tek seferde tek işlem",
             f"1. yarı: {start.astimezone(LOCAL_TZ):%d.%m} – {mid.astimezone(LOCAL_TZ):%d.%m} · "
             f"2. yarı: {mid.astimezone(LOCAL_TZ):%d.%m} – {now.astimezone(LOCAL_TZ):%d.%m}", ""]
    passing = []
    for label, min_reward in BACKTEST_VARIANTS:
        trades = []
        for coin in coins:
            for t in simulate_setups(history.get(coin), meta[coin]["atr_pct"], min_reward) or []:
                t["coin"] = coin
                trades.append(t)
        first = single_position([t for t in trades if t["entry_ts"] < mid])
        second = single_position([t for t in trades if t["entry_ts"] >= mid])
        s1, s2, sall = summarize(first), summarize(second), summarize(first + second)
        ok = s1["n"] >= 10 and s2["n"] >= 10 and s1["net"] > 0 and s2["net"] > 0
        if ok:
            passing.append(label)
        lines.append(f"<b>{e(label)}</b>")
        if sall["n"] == 0:
            lines.append("işlem yok")
        else:
            lines.append(f"1. yarı: {s1['n']} işlem, net {smoney(s1['net'])} · 2. yarı: {s2['n']} işlem, "
                         f"net {smoney(s2['net'])}")
            lines.append(f"Toplam: 🎯{sall['target']} 🛑{sall['stop']} ⚪{sall['breakeven']} ⏰{sall['timeout']} · "
                         f"hedef oranı %{sall['target_rate']:.0f} · komisyonsuz {smoney(sall['gross'])} → "
                         f"{'✅ iki yarıda da kâr' if ok else '❌ tutarlı kâr yok'}")
        lines.append("")
    if passing:
        lines.append("Sonuç: " + ", ".join(e(p) for p in passing) + " iki dönemde de kâr etti. Umut verici, ama "
                     "gerçek parayla başlamadan önce 2-3 hafta canlı sinyalleri izle.")
    else:
        lines.append("Sonuç: hiçbir ayar iki dönemde de kâr etmedi → bu kurallarla gerçek para koyma.")
    lines.append("<i>Yaklaşık simülasyon: kayma yok, stop anında uygulanmış varsayılır (Midas'ta stop'u elle "
                 "yapacağın için gerçek sonuç biraz daha kötü olur). Geçmiş sonuç geleceği garanti etmez.</i>")
    send_telegram("\n".join(lines))
    log.info("Kripto testi tamamlandı; geçen ayarlar: %s", passing)


def coin_backtest(pid, daily_atr):
    trades = simulate_setups(cb_history(pid, BACKTEST_DAYS), daily_atr, MIN_REWARD_PCT)
    return None if trades is None else summarize(trades)


# ---------------------------------------------------------------- swing testi (saatler-günler süren işlemler)
SWING_COINS = 10
SWING_DAYS = 365
SWING_VARIANTS = [   # (etiket, mum süresi sn, tür, giriş periyodu, çıkış periyodu)
    ("S1 · 1 saatlik kırılım (20 mumun tepesi al, 10 mumun dibi sat)", 3600, "donchian", 20, 10),
    ("S2 · 6 saatlik kırılım (20 mumun tepesi al, 10 mumun dibi sat)", 21600, "donchian", 20, 10),
    ("S3 · 1 saatlik trend (EMA20 EMA50'yi yukarı keserse al, fiyat EMA50 altına inerse sat)", 3600, "ema", 20, 50),
]


def swing_universe(products):
    """Midas'ta da olan, en az ~1 yıllık geçmişi bulunan en likit coinler (BTC/ETH dahil)."""
    ranked = []
    for coin, pid in products.items():
        d = cb_candles(pid, 86400)
        time.sleep(0.12)
        if d is None or len(d) < 250:
            continue
        ranked.append((coin, pid, float((d["Close"] * d["Volume"]).tail(30).mean())))
    ranked.sort(key=lambda x: x[2], reverse=True)
    return [(c, p) for c, p, _ in ranked[:SWING_COINS]]


def swing_signals(df, kind, p_in, p_out):
    """Her mum kapanışında giriş/çıkış sinyali (sadece o ana kadarki veriyle)."""
    c = df["Close"]
    if kind == "donchian":
        entry = c > df["High"].shift(1).rolling(p_in).max()
        exit_ = c < df["Low"].shift(1).rolling(p_out).min()
    else:
        fast, slow = c.ewm(span=p_in, adjust=False).mean(), c.ewm(span=p_out, adjust=False).mean()
        entry = (fast > slow) & (fast.shift(1) <= slow.shift(1)) & (c > fast)
        exit_ = c < slow
    return entry.fillna(False).to_numpy(), exit_.fillna(False).to_numpy()


def swing_simulate(frames, kind, p_in, p_out, start, end, capital):
    """Tek pozisyon, kâr yeniden yatırılır. Alım/satım sinyal mumunun kapanış fiyatından,
    her iki tarafta FEE_PCT komisyon. Dönem sonunda açık pozisyon kapatılır."""
    prepared = {}
    for coin, df in frames.items():
        sub = df[(df.index >= start) & (df.index < end)]
        if len(sub) < max(p_in, p_out) + 5:
            continue
        ent, ext = swing_signals(df, kind, p_in, p_out)
        mask = (df.index >= start) & (df.index < end)
        prepared[coin] = (sub["Close"].to_numpy(float), ent[mask], ext[mask], sub.index)
    if not prepared:
        return None
    timeline = sorted(set().union(*[set(p[3]) for p in prepared.values()]))
    pos = {c: {ts: i for i, ts in enumerate(p[3])} for c, p in prepared.items()}
    order = list(frames.keys())                        # likidite sırası: aynı anda birden çok sinyalde ilk coin
    equity, peak, max_dd = capital, capital, 0.0
    held, qty, entry_ts, trades, hold_hours = None, 0.0, None, [], []
    fee = FEE_PCT / 100
    for ts in timeline:
        if held is not None:
            i = pos[held].get(ts)
            if i is not None:
                closes, _, ext, _ = prepared[held]
                mark = qty * closes[i]
                if ext[i]:
                    equity = mark * (1 - fee)
                    trades.append(equity)
                    hold_hours.append((ts - entry_ts).total_seconds() / 3600)
                    held = None
                    peak = max(peak, equity)
                else:
                    peak = max(peak, mark)
                    max_dd = max(max_dd, (peak - mark) / peak * 100)
            continue
        for coin in order:
            if coin not in prepared:
                continue
            i = pos[coin].get(ts)
            if i is not None and prepared[coin][1][i]:
                price = prepared[coin][0][i]
                qty = equity * (1 - fee) / price
                held, entry_ts = coin, ts
                break
    if held is not None:                                # dönem sonu: açık pozisyonu kapat
        closes = prepared[held][0]
        equity = qty * closes[-1] * (1 - fee)
        trades.append(equity)
        hold_hours.append((prepared[held][3][-1] - entry_ts).total_seconds() / 3600)
    weeks = max((end - start).total_seconds() / (7 * 86400), 1e-9)
    return {"final": equity, "ret": (equity / capital - 1) * 100, "trades": len(trades),
            "per_week": len(trades) / weeks, "max_dd": max_dd,
            "avg_hold": sum(hold_hours) / len(hold_hours) if hold_hours else 0.0}


def buy_and_hold(frames, start, end):
    """Aynı coinlere eşit bölerek alıp dönem boyunca tutmanın getirisi (%)."""
    rets = []
    for df in frames.values():
        sub = df[(df.index >= start) & (df.index < end)]
        if len(sub) > 1:
            rets.append(float(sub["Close"].iloc[-1] / sub["Close"].iloc[0] - 1) * 100)
    return sum(rets) / len(rets) if rets else 0.0


def run_swing_test():
    now = now_utc()
    products = cb_products()
    if not products:
        send_telegram("📈 Swing testi yapılamadı: Coinbase verisi alınamadı. Birazdan tekrar dene.")
        return
    universe = swing_universe(products)
    start = now - timedelta(days=SWING_DAYS)
    mid = start + (now - start) / 2
    data = {}
    for gran in sorted({v[1] for v in SWING_VARIANTS}):
        data[gran] = {}
        for coin, pid in universe:
            df = cb_history(pid, SWING_DAYS + 10, gran)
            if df is not None and len(df) > 100:
                data[gran][coin] = df
    daily = {coin: d for coin, pid in universe if (d := cb_history(pid, SWING_DAYS + 10, 86400)) is not None}
    bh1, bh2 = buy_and_hold(daily, start, mid), buy_and_hold(daily, mid, now)
    btc = {k: v for k, v in daily.items() if k == MARKET_COIN}
    lines = [f"📈 <b>Kripto swing testi</b> — son {SWING_DAYS} gün, gerçek veri",
             f"Coinler: {e(', '.join(c for c, _ in universe))}",
             f"Bütçe {money(BUDGET)}, kâr yeniden yatırılıyor · komisyon %{FEE_PCT:g} × 2 · tek seferde tek işlem",
             f"1. yarı: {start.astimezone(LOCAL_TZ):%d.%m.%Y} – {mid.astimezone(LOCAL_TZ):%d.%m.%Y} · "
             f"2. yarı: {mid.astimezone(LOCAL_TZ):%d.%m.%Y} – {now.astimezone(LOCAL_TZ):%d.%m.%Y}",
             f"Kıyas — hiçbir şey yapmadan alıp tutmak: 1. yarı %{bh1:+.0f}, 2. yarı %{bh2:+.0f}"
             + (f" (sadece BTC: %{buy_and_hold(btc, start, mid):+.0f} / %{buy_and_hold(btc, mid, now):+.0f})"
                if btc else ""), ""]
    verdicts = []
    for label, gran, kind, p_in, p_out in SWING_VARIANTS:
        frames = {c: data[gran][c] for c, _ in universe if c in data[gran]}
        r1 = swing_simulate(frames, kind, p_in, p_out, start, mid, BUDGET)
        r2 = swing_simulate(frames, kind, p_in, p_out, mid, now, BUDGET)
        lines.append(f"<b>{e(label)}</b>")
        if not r1 or not r2:
            lines.append("yeterli veri yok")
            lines.append("")
            continue
        profit = r1["ret"] > 0 and r2["ret"] > 0
        beats = r1["ret"] > bh1 and r2["ret"] > bh2
        mark = "⭐ iki yarıda da kâr + al-tut'tan iyi" if profit and beats else \
            "✅ iki yarıda da kâr (al-tut'tan geride)" if profit else "❌ tutarlı kâr yok"
        if profit:
            verdicts.append((label, beats))
        per_week = (r1["trades"] + r2["trades"]) / (SWING_DAYS / 7)
        avg_hold = ((r1["avg_hold"] * r1["trades"] + r2["avg_hold"] * r2["trades"]) /
                    max(r1["trades"] + r2["trades"], 1))
        lines.append(f"Haftada ~{per_week:.1f} işlem · ortalama tutma {avg_hold:.0f} saat")
        lines.append(f"1. yarı: {money(BUDGET)} → {money(r1['final'])} (%{r1['ret']:+.0f}, en büyük düşüş "
                     f"%{r1['max_dd']:.0f}) · 2. yarı: {money(BUDGET)} → {money(r2['final'])} "
                     f"(%{r2['ret']:+.0f}, en büyük düşüş %{r2['max_dd']:.0f})")
        lines.append(mark)
        lines.append("")
    if any(b for _, b in verdicts):
        best = [e(l) for l, b in verdicts if b]
        lines.append("Sonuç: " + ", ".join(best) + " iki dönemde de kâr etti ve alıp tutmaktan iyiydi. Umut verici; "
                     "gerçek parayla başlamadan önce canlı sinyalleri 2-3 hafta izleyelim.")
    elif verdicts:
        lines.append("Sonuç: kâr eden ayar var ama alıp tutmanın gerisinde kalıyor. Bu durumda coini alıp "
                     "tutmak daha basit ve daha kârlı olurdu.")
    else:
        lines.append("Sonuç: hiçbir ayar iki dönemde de kâr etmedi → bu kurallarla gerçek para koyma.")
    lines.append("<i>Yaklaşık simülasyon: alım/satım mum kapanış fiyatından, kayma yok. Geçmiş sonuç geleceği "
                 "garanti etmez.</i>")
    send_telegram("\n".join(lines))
    log.info("Swing testi tamamlandı: %s", verdicts)


# ---------------------------------------------------------------- plan karşılaştırması (5 yıl, yıl yıl)
PLAN_YEARS = 5
PLAN_COINS = 10
PLAN_WARMUP_DAYS = 220


def daily_regime(btc_daily, n=200, shift=0):
    """BTC kapanışı n günlük ortalamanın üstünde mi? shift=1: gün içindeki kararlar için dünün
    kapanışı kullanılır (bugünün kapanışı henüz bilinmiyor)."""
    c = btc_daily["Close"]
    on = (c > c.rolling(n).mean()).astype(float).shift(shift)
    return on.fillna(0.0) > 0.5


def _aligned(frames, timeline, col="Close"):
    return pd.DataFrame({k: v[col] for k, v in frames.items()}).reindex(timeline)


def plan_breakout(frames6, order, regime6=None, p_in=20, p_out=10):
    """P1/P2: 6 saatlik mumlarda 20 mum tepesini kıran coini al, 10 mum dibini kırınca sat."""
    timeline = pd.DatetimeIndex(sorted(set().union(*[set(df.index) for df in frames6.values()])))
    ent, ext = {}, {}
    for coin, df in frames6.items():
        e_, x_ = swing_signals(df, "donchian", p_in, p_out)
        ent[coin] = pd.Series(e_, index=df.index).reindex(timeline, fill_value=False).to_numpy()
        ext[coin] = pd.Series(x_, index=df.index).reindex(timeline, fill_value=False).to_numpy()
    reg = None if regime6 is None else regime6.reindex(timeline, method="ffill").fillna(False).to_numpy()
    held, out = None, []
    for i in range(len(timeline)):
        if held is not None and ext[held][i]:
            held = None
        if held is None and (reg is None or reg[i]):
            for coin in order:
                if coin in ent and ent[coin][i]:
                    held = coin
                    break
        out.append(held)
    return pd.Series(out, index=timeline, dtype=object)


def plan_momentum(frames_d, order, regime_d, lookback=28, ma=50):
    """P3: her pazartesi son 4 haftanın en güçlü coinini al (kendi 50 günlük ortalamasının
    üstünde ve getirisi pozitifse); BTC 200 günlük ortalamasının altındaysa nakitte kal."""
    timeline = pd.DatetimeIndex(sorted(set().union(*[set(df.index) for df in frames_d.values()])))
    closes = _aligned(frames_d, timeline)
    ret = closes / closes.shift(lookback) - 1
    above = closes > closes.rolling(ma).mean()
    reg = regime_d.reindex(timeline).fillna(False).to_numpy()
    held, out = None, []
    for i, ts in enumerate(timeline):
        if ts.weekday() == 0:
            held = None
            if reg[i]:
                cands = [(ret.iat[i, j], c) for j, c in enumerate(closes.columns)
                         if above.iat[i, j] and np.isfinite(ret.iat[i, j]) and ret.iat[i, j] > 0]
                if cands:
                    held = max(cands)[1]
        out.append(held)
    return pd.Series(out, index=timeline, dtype=object)


def plan_btc_trend(btc_daily, ma=50):
    """P4: BTC kapanışı 50 günlük ortalamanın üstündeyse BTC'de, altındaysa nakitte."""
    c = btc_daily["Close"]
    on = c > c.rolling(ma).mean()
    return pd.Series(np.where(on, MARKET_COIN, None), index=btc_daily.index, dtype=object)


def plan_btc_dip(btc_daily, max_days=10):
    """P5: BTC 200 günlük ortalamanın üstündeyken 2 günlük RSI 10'un altına düşerse al;
    kapanış 5 günlük ortalamayı geçince ya da 10 gün dolunca sat."""
    c = btc_daily["Close"]
    rsi2, sma5, sma200 = compute_rsi(c, 2).to_numpy(), c.rolling(5).mean().to_numpy(), c.rolling(200).mean().to_numpy()
    cv = c.to_numpy()
    held, days, out = None, 0, []
    for i in range(len(cv)):
        if held is not None:
            days += 1
            if cv[i] > sma5[i] or days >= max_days:
                held = None
        elif np.isfinite(sma200[i]) and cv[i] > sma200[i] and rsi2[i] < 10:
            held, days = MARKET_COIN, 0
        out.append(held)
    return pd.Series(out, index=btc_daily.index, dtype=object)


def execute_plan(holdings, frames, capital, start, end):
    """Pozisyon planını uygular: her mumun kapanışında hedef coin değişirse eskisi satılır,
    yenisi alınır (iki tarafta FEE_PCT komisyon). Dönem başında nakit, sonunda satış."""
    h = holdings[(holdings.index >= start) & (holdings.index < end)]
    if h.empty:
        return None
    closes = _aligned(frames, holdings.index).ffill().loc[h.index]
    fee = FEE_PCT / 100
    cash, held, qty, peak, mdd, buys = capital, None, 0.0, capital, 0.0, 0
    for ts, target in h.items():
        if target != held:
            if held is not None:
                cash = qty * float(closes.at[ts, held]) * (1 - fee)
                held, qty = None, 0.0
            if target is not None and np.isfinite(closes.at[ts, target]) and closes.at[ts, target] > 0:
                qty = cash * (1 - fee) / float(closes.at[ts, target])
                held, buys = target, buys + 1
        value = qty * float(closes.at[ts, held]) if held is not None else cash
        peak = max(peak, value)
        mdd = max(mdd, (peak - value) / peak * 100)
    if held is not None:
        cash = qty * float(closes[held].iloc[-1]) * (1 - fee)
    return {"final": cash, "ret": (cash / capital - 1) * 100, "buys": buys, "max_dd": mdd}


def plan_universe(products, start):
    """Test başlangıcından önce de işlem gören, Midas'ta bulunan en likit coinler (BTC her zaman dahil)."""
    ranked = []
    for coin, pid in products.items():
        d = cb_candles(pid, 86400)
        time.sleep(0.12)
        if d is not None and len(d) >= 200:
            ranked.append((float((d["Close"] * d["Volume"]).tail(30).mean()), coin, pid))
    ranked.sort(reverse=True)
    chosen, daily = [], {}
    for _, coin, pid in ranked[:PLAN_COINS * 2]:
        d = cb_history(pid, PLAN_YEARS * 365 + PLAN_WARMUP_DAYS, 86400)
        if d is not None and d.index[0] <= start - timedelta(days=60):
            chosen.append((coin, pid))
            daily[coin] = d
        if len(chosen) >= PLAN_COINS and MARKET_COIN in daily:
            break
    return chosen, daily


PLAN_LABELS = [
    ("P1", "6 saatlik kırılım (S2)"),
    ("P2", "6 saatlik kırılım + BTC filtresi"),
    ("P3", "haftalık momentum (en güçlü coin)"),
    ("P4", "BTC trend (50 günlük ortalama)"),
    ("P5", "BTC düşüşte al, toparlanınca sat"),
]


def build_plans(daily, frames6, order):
    btc = daily[MARKET_COIN]
    return {
        "P1": (plan_breakout(frames6, order), frames6),
        "P2": (plan_breakout(frames6, order, regime6=daily_regime(btc, 200, shift=1)), frames6),
        "P3": (plan_momentum(daily, order, daily_regime(btc, 200, shift=0)), daily),
        "P4": (plan_btc_trend(btc), {MARKET_COIN: btc}),
        "P5": (plan_btc_dip(btc), {MARKET_COIN: btc}),
    }


def run_plan_comparison():
    now = now_utc()
    start = now - timedelta(days=PLAN_YEARS * 365)
    products = cb_products()
    if not products or MARKET_COIN not in products:
        send_telegram("🧭 Plan karşılaştırması yapılamadı: Coinbase verisi alınamadı. Birazdan tekrar dene.")
        return
    universe, daily = plan_universe(products, start)
    order = [c for c, _ in universe]
    frames6 = {}
    for coin, pid in universe:
        df = cb_history(pid, PLAN_YEARS * 365 + 10, 21600)
        if df is not None and len(df) > 100:
            frames6[coin] = df
    if MARKET_COIN not in daily or not frames6:
        send_telegram("🧭 Plan karşılaştırması yapılamadı: yeterli geçmiş veri yok.")
        return
    years = [(start + timedelta(days=365 * k), start + timedelta(days=365 * (k + 1))) for k in range(PLAN_YEARS)]
    years[-1] = (years[-1][0], now)

    def bh(frames, a, b):
        return buy_and_hold({k: v for k, v in frames.items()}, a, b)

    basket = [bh(daily, a, b) for a, b in years]
    btc_bh = [bh({MARKET_COIN: daily[MARKET_COIN]}, a, b) for a, b in years]
    fmt = lambda xs: " · ".join(f"%{x:+.0f}" for x in xs)
    lines = [f"🧭 <b>Kripto plan karşılaştırması</b> — son {PLAN_YEARS} yıl, yıl yıl, gerçek veri",
             f"Coinler: {e(', '.join(order))}",
             f"Her yıl {money(BUDGET)} ile başlanır · komisyon %{FEE_PCT:g} × 2 · tek seferde tek coin",
             "Yıllar: " + " · ".join(f"{a.astimezone(LOCAL_TZ):%m.%Y}–{b.astimezone(LOCAL_TZ):%m.%Y}" for a, b in years),
             f"Al-tut (sepet): {fmt(basket)}",
             f"Al-tut (sadece BTC): {fmt(btc_bh)}", ""]
    plans = build_plans(daily, frames6, order)
    stars, goods = [], []
    for key, label in PLAN_LABELS:
        holdings, frames = plans[key]
        yearly = [execute_plan(holdings, frames, BUDGET, a, b) for a, b in years]
        full = execute_plan(holdings, frames, BUDGET, years[0][0], now)
        lines.append(f"<b>{key} · {e(label)}</b>")
        if any(r is None for r in yearly) or full is None:
            lines += ["yeterli veri yok", ""]
            continue
        rets = [r["ret"] for r in yearly]
        wins = sum(1 for r in rets if r > 0)
        beats = sum(1 for r, b in zip(rets, basket) if r > b)
        per_year = sum(r["buys"] for r in yearly) / PLAN_YEARS
        if wins >= 4 and beats >= 3 and full["final"] > BUDGET:
            verdict, _ = "⭐ güçlü aday", stars.append(f"{key}")
        elif wins >= 3 and full["final"] > BUDGET:
            verdict, _ = "✅ ilginç ama güvenilir değil", goods.append(f"{key}")
        else:
            verdict = "❌ tutarlı kâr yok"
        lines.append(f"Yıllar: {fmt(rets)}")
        lines.append(f"{wins}/{PLAN_YEARS} yıl kâr · {beats}/{PLAN_YEARS} yıl al-tut'tan iyi · yılda ~{per_year:.0f} alım")
        lines.append(f"{PLAN_YEARS} yıl boyunca: {money(BUDGET)} → {money(full['final'])} · en büyük düşüş %{full['max_dd']:.0f}")
        lines += [verdict, ""]
    if stars:
        lines.append("Sonuç: " + ", ".join(stars) + " beş yılın çoğunda kâr etti ve alıp tutmaktan iyiydi. Gerçek parayla "
                     "başlamadan önce canlı sinyalleri birkaç hafta izleyelim.")
    elif goods:
        lines.append("Sonuç: " + ", ".join(goods) + " ilginç ama yeterince tutarlı değil. Gerçek para için erken.")
    else:
        lines.append("Sonuç: hiçbir plan beş yılda tutarlı kâr etmedi → gerçek para koyma.")
    lines.append("<i>Yaklaşık simülasyon: işlemler mum kapanış fiyatından, kayma yok. Coinler bugün hâlâ işlem gören "
                 "coinlerden seçildi (batan coinler yok), bu da sonuçları olduğundan biraz iyi gösterir. "
                 "Geçmiş sonuç geleceği garanti etmez.</i>")
    send_telegram("\n".join(lines))
    log.info("Plan karşılaştırması tamamlandı: yıldız=%s iyi=%s", stars, goods)


# ---------------------------------------------------------------- sinyal takibi
EVENT_TEXT = {
    "breakeven_move": "🔒 <b>{c}</b> yükseliyor — stop'u girişe çek: {entry} ({time})",
    "target": "🎯 <b>{c}</b> hedefe geldi {target} (+%{pnl:.1f}, net {net}) ({time})",
    "stop": "🛑 <b>{c}</b> STOP seviyesi geldi → SAT {stop} (%{pnl:.1f}, net {net}) ({time})",
    "breakeven": "⚪ <b>{c}</b> girişe geri döndü → SAT {entry} (net {net}) ({time})",
    "timeout": "⏰ <b>{c}</b> {hours} saat doldu → SAT {price} (%{pnl:+.1f}, net {net}) ({time})",
}


def simulate_signal(sig, df, now):
    created = datetime.fromtimestamp(sig["created"], UTC)
    created_bar = created.replace(minute=created.minute - created.minute % 15, second=0, microsecond=0)
    exit_at = created + timedelta(hours=MAX_HOLD_HOURS)
    entry, target, stop0 = sig["entry"], sig["target"], sig["stop0"]
    stop, be, events, last_close = stop0, False, [], None
    bars = df[(df.index >= created_bar) & (df.index < exit_at)] if df is not None else None
    for ts, bar in (bars.iterrows() if bars is not None else []):
        hi, lo = float(bar["High"]), float(bar["Low"])
        last_close = float(bar["Close"])
        if ts == created_bar:
            hi = hi if hi > sig["seen_high"] else float("-inf")
            lo = lo if lo < sig["seen_low"] else float("inf")
        if lo <= stop:
            kind = "breakeven" if be else "stop"
            events.append({"type": kind, "ts": ts, "price": stop, "pnl": (stop - entry) / entry * 100})
            return events, "closed"
        if hi >= target:
            events.append({"type": "target", "ts": ts, "price": target, "pnl": (target - entry) / entry * 100})
            return events, "closed"
        if not be and hi >= entry + (entry - stop0):
            events.append({"type": "breakeven_move", "ts": ts, "price": entry})
            stop, be = entry, True
    if now >= exit_at:
        px = last_close if last_close is not None else entry
        events.append({"type": "timeout", "ts": exit_at, "price": px, "pnl": (px - entry) / entry * 100})
        return events, "closed"
    return events, "active"


def update_open_signals(state, data, now):
    closed = []
    for sid, sig in state["signals"].items():
        df = data.get(sig["coin"])
        if df is None and now < datetime.fromtimestamp(sig["created"], UTC) + timedelta(hours=MAX_HOLD_HOURS):
            continue
        events, status = simulate_signal(sig, df, now)
        for ev in events[sig.get("sent", 0):]:
            ts = ev["ts"] if isinstance(ev["ts"], datetime) else pd.Timestamp(ev["ts"]).to_pydatetime()
            pnl = ev.get("pnl", 0.0)
            net = sig["value"] * pnl / 100 - sig["fees"]
            send_telegram(EVENT_TEXT[ev["type"]].format(
                c=e(sig["coin"]), time=local_hm(ts), entry=price_fmt(sig["entry"]), stop=price_fmt(sig["stop0"]),
                target=price_fmt(sig["target"]), price=price_fmt(ev["price"]), pnl=pnl, net=smoney(net),
                hours=MAX_HOLD_HOURS))
        sig["sent"] = max(sig.get("sent", 0), len(events))
        if status == "closed":
            final = events[-1]
            pnl = float(final.get("pnl", 0.0))
            state["history"].append({
                "date": datetime.fromtimestamp(sig["created"], UTC).astimezone(LOCAL_TZ).strftime("%Y-%m-%d"),
                "coin": sig["coin"], "result": final["type"], "pnl": round(pnl, 3),
                "net_usd": round(sig["value"] * pnl / 100 - sig["fees"], 2)})
            closed.append(sid)
    for sid in closed:
        state["signals"].pop(sid, None)
    state["history"] = state["history"][-500:]


def register_signal(state, c, now):
    state["signals"][f"{c['coin']}-{int(now.timestamp())}"] = {
        "coin": c["coin"], "created": now.timestamp(), "entry": float(c["entry"]), "stop0": float(c["stop"]),
        "target": float(c["target"]), "value": round(float(c["value"]), 2), "fees": round(float(c["fees"]), 4),
        "seen_high": float(c["seen_high"]), "seen_low": float(c["seen_low"]), "sent": 0}
    state["last_alerted"][c["coin"]] = now.timestamp()


# ---------------------------------------------------------------- raporlar ve durum
def perf_block(history, days=20):
    dates = sorted({h["date"] for h in history})[-days:]
    trades = [h for h in history if h["date"] in dates]
    if not trades:
        return f"Son {days} gün: henüz işlem yok"
    wins = sum(1 for h in trades if h["net_usd"] > 0)
    return (f"Son {len(dates)} gün: {len(trades)} işlem, %{wins / len(trades) * 100:.0f} kârla kapandı, "
            f"net {smoney(sum(h['net_usd'] for h in trades))}")


def maybe_daily_summary(state, now):
    local = now.astimezone(LOCAL_TZ)
    today = local.strftime("%Y-%m-%d")
    if local.hour < SUMMARY_LOCAL_HOUR or state["meta"].get("summary_date") == today:
        return
    todays = [h for h in state["history"] if h["date"] == today]
    lines = ["📊 <b>Kripto günlük rapor</b>"]
    if todays:
        lines.append("Bugün: " + ", ".join(f"{e(h['coin'])} {RESULT_LABELS.get(h['result'], h['result'])} "
                                           f"{smoney(h['net_usd'])}" for h in todays))
        lines.append(f"Bugünün toplamı: net {smoney(sum(h['net_usd'] for h in todays))}")
    else:
        lines.append("Bugün: kapanan işlem yok")
    if state["signals"]:
        lines.append(f"Takipte açık işlem: {len(state['signals'])}")
    lines.append(perf_block(state["history"]))
    if send_telegram("\n".join(lines)):
        state["meta"]["summary_date"] = today


def load_state():
    s = read_json(STATE_FILE, {})
    for key, default in (("last_alerted", {}), ("signals", {}), ("history", []), ("meta", {})):
        if not isinstance(s.get(key), type(default)):
            s[key] = default
    return s


def save_state(state):
    cutoff = time.time() - 3 * 86400
    state["last_alerted"] = {k: v for k, v in state["last_alerted"].items() if v > cutoff}
    write_json(STATE_FILE, state)


# ---------------------------------------------------------------- ana akış
def run_once(state, force=False):
    now = now_utc()
    coins, meta, btc_pid, announce = load_or_build_watchlist(now)
    if not coins:
        if force:
            send_telegram("⚠️ Kripto bot: Coinbase'den coin listesi alınamadı, sonraki turda tekrar denenecek.")
        return
    if announce:
        send_telegram(f"📋 <b>Bugün izlediğim coinler</b> ({local_hm(now)})\n{e(', '.join(coins))}")

    wanted = list(dict.fromkeys(coins + [s["coin"] for s in state["signals"].values()]))
    data = {}
    for coin in wanted:
        pid = meta.get(coin, {}).get("pid") or f"{coin}-USD"
        df = cb_candles(pid)
        if df is not None:
            data[coin] = df
        time.sleep(0.12)
    btc_df = data.get(MARKET_COIN)
    if btc_df is None:                                  # BTC listede değilse (ör. oynaklığı düşük) ayrıca çek
        btc_df = cb_candles(btc_pid)

    if not data:
        state["meta"]["fail_count"] = state["meta"].get("fail_count", 0) + 1
        if force or state["meta"]["fail_count"] == 3:
            send_telegram("⚠️ Kripto bot: fiyat verisi alınamıyor (Coinbase). Tekrar deneyeceğim.")
        return
    state["meta"]["fail_count"] = 0

    update_open_signals(state, data, now)
    market = market_context(compute_intraday(btc_df))
    open_coins = {s["coin"] for s in state["signals"].values()}
    reasons, cands = {}, []
    for coin in coins:
        df = data.get(coin)
        if df is None:
            reasons["no_data"] = reasons.get("no_data", 0) + 1
            continue
        if now - df.index[-1].to_pydatetime() > timedelta(minutes=DATA_STALE_MINUTES):
            reasons["stale"] = reasons.get("stale", 0) + 1
            continue
        if coin in open_coins:
            reasons["open_signal"] = reasons.get("open_signal", 0) + 1
            continue
        last = state["last_alerted"].get(coin)
        if last and now.timestamp() - last < ALERT_COOLDOWN_MINUTES * 60:
            reasons["cooldown"] = reasons.get("cooldown", 0) + 1
            continue
        ind = compute_intraday(df)
        if not ind:
            reasons["no_data"] = reasons.get("no_data", 0) + 1
            continue
        c, code, why = evaluate(coin, ind, meta.get(coin, {}))
        log.info(why)
        if c:
            cands.append(c)
        else:
            reasons[code] = reasons.get(code, 0) + 1

    cands.sort(key=lambda c: (c["score"], -c["be_winrate"], c["dollar_vol24"]), reverse=True)
    sent = 0
    for c in cands[:MAX_ALERTS_PER_RUN]:
        bt = coin_backtest(meta.get(c["coin"], {}).get("pid") or f"{c['coin']}-USD", c.get("daily_atr"))
        risk = assess_risk(c, market, bt)
        if send_telegram(signal_message(c, market, bt, risk)):
            register_signal(state, c, now)
            sent += 1
    maybe_daily_summary(state, now)

    if force:
        why = " · ".join(f"{REASON_LABELS.get(k, k)} {v}" for k, v in sorted(reasons.items(), key=lambda x: -x[1]))
        send_telegram("\n".join([
            f"✅ <b>Kripto bot çalışıyor</b> ({local_hm(now)})",
            f"📋 İzlenen ({len(coins)}): {e(', '.join(coins))}",
            f"🌎 Piyasa: {e(market['text'])}",
            f"💰 Bütçe {money(BUDGET)} · komisyon %{FEE_PCT:g} × 2 · hedef en az %{MIN_REWARD_PCT:g}",
            f"🔎 {len(coins)} coin tarandı → {len(cands)} uygun kurulum, {sent} sinyal gönderildi",
            f"Elenme nedenleri: {e(why) if why else '—'}",
            f"📂 Takipteki işlem: {len(state['signals'])}",
            perf_block(state["history"]),
        ]))


def main():
    if RUN_MODE == "backtest":
        run_backtest()
        return
    if RUN_MODE == "swing":
        run_swing_test()
        return
    if RUN_MODE == "plans":
        run_plan_comparison()
        return
    state = load_state()
    if RUN_MODE == "once":
        try:
            run_once(state, force=FORCE_RUN)
        finally:
            save_state(state)
        return
    send_telegram("🤖 Kripto bot başladı (döngü modu).")
    while True:
        try:
            run_once(state)
        except Exception as ex:
            log.exception("Tur hatası: %s", ex)
        save_state(state)
        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    main()
