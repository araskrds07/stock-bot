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
RUN_MODE             ("once" = GitHub Actions, "loop" = kendi bilgisayarın, "backtest")
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


def cb_history(pid, days):
    """15 dk mumları sayfa sayfa geriye doğru çeker (istek başına en fazla 300 mum)."""
    end = now_utc().replace(second=0, microsecond=0)
    limit = end - timedelta(days=days)
    frames, cur = [], end
    while cur > limit:
        start = max(limit, cur - timedelta(seconds=GRAN * 299))
        df = cb_candles(pid, GRAN, start, cur)
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
