import ccxt.async_support as ccxt_async
import pandas as pd
import numpy as np
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import asyncio
import platform
import random
import sqlite3
from datetime import datetime, timedelta
import aiohttp
import os
import traceback

# Windows için olay döngüsü politikasını ayarla
if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Binance ve Telegram ayarları
BINANCE = ccxt_async.binance({
    'enableRateLimit': True,  # Rate limit yönetimini etkinleştir
    'rateLimit': 1000,  # Milisaniye cinsinden istekler arası bekleme süresi
    # Proxy kullanımı gerekiyorsa şu satırları aktif edin:
    # 'proxies': {
    #     'http': os.getenv('PROXY_HTTP', 'http://your_proxy:port'),
    #     'https': os.getenv('PROXY_HTTPS', 'https://your_proxy:port'),
    # }
})
TELEGRAM_TOKEN = "7818791938:AAH0yjQc1dfNavW5QZ_vo_aZ3dimytZFHQE"
CMC_API_KEY = os.getenv("CMC_API_KEY", "")  # CoinMarketCap API anahtarı (opsiyonel)

# Veritabanı ayarları
DB_FILE = "users.db"

# Önbellek için global değişkenler
MARKETS_CACHE = None
MARKETS_CACHE_TIME = None
DATA_CACHE = {}  # {symbol_timeframe: (df, timestamp)}
CACHE_DURATION = 3600  # 1 saat

# Geçerli zaman dilimleri
VALID_TIMEFRAMES = ["1h", "2h", "4h", "6h"]

# Binance API bağlantısını test et
async def test_binance_connection():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.binance.com/api/v3/ping") as response:
                if response.status == 200:
                    print("Binance API bağlantısı başarılı!")
                    return True
                else:
                    print(f"Binance API bağlantısı başarısız: Status {response.status}")
                    return False
    except Exception as e:
        print(f"Bağlantı hatası: {e}")
        return False

# Veritabanını başlat
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id TEXT PRIMARY KEY, chat_id INTEGER, created_at TEXT, favorites TEXT)''')
    predefined_users = [
        ("yetkiliadmin", None),
        ("prokullanici", None),
        ("vipkullanici", None)
    ]
    for user_id, chat_id in predefined_users:
        c.execute("INSERT OR IGNORE INTO users (user_id, chat_id, created_at, favorites) VALUES (?, ?, ?, ?)",
                  (user_id, chat_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ""))
    conn.commit()
    conn.close()

# Kullanıcıyı kontrol et
def check_user(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT chat_id FROM users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result

# Kullanıcıyı kaydet
def save_user(user_id, chat_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE users SET chat_id = ?, created_at = ? WHERE user_id = ?",
              (chat_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id))
    conn.commit()
    conn.close()

# Chat ID'yi kontrol et
def check_chat_id(chat_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE chat_id = ?", (chat_id,))
    result = c.fetchone()
    conn.close()
    return result

# Kullanıcıyı çıkış yaptır
def exit_user(chat_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE users SET chat_id = NULL WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()

# Tüm USDT çiftlerini çek (önbellekleme ile)
async def get_usdt_pairs():
    global MARKETS_CACHE, MARKETS_CACHE_TIME
    max_retries = 3
    for attempt in range(max_retries):
        try:
            if MARKETS_CACHE is None or MARKETS_CACHE_TIME is None or \
                    (datetime.now() - MARKETS_CACHE_TIME).total_seconds() > CACHE_DURATION:
                await asyncio.sleep(0.5 * (attempt + 1))
                markets = await BINANCE.load_markets()
                MARKETS_CACHE = markets
                MARKETS_CACHE_TIME = datetime.now()
            usdt_pairs = [market for market in MARKETS_CACHE if
                          market.endswith('/USDT') and MARKETS_CACHE[market]['active']]
            return usdt_pairs
        except ccxt_async.RateLimitExceeded:
            if attempt < max_retries - 1:
                print(f"Rate limit hatası, {10 * (attempt + 1)} saniye bekleniyor...")
                await asyncio.sleep(10 * (attempt + 1))
                continue
            print("USDT çiftleri alınamadı: Rate limit aşıldı")
            return []
        except Exception as e:
            print(f"USDT çiftleri alınamadı: {e}")
            traceback.print_exc()
            return []

# Fiyat verisi çekme (önbellekleme ile)
async def get_price_data(symbol, timeframe="1h", limit=100):
    cache_key = f"{symbol}_{timeframe}"
    try:
        if cache_key in DATA_CACHE:
            cached_df, cached_time = DATA_CACHE[cache_key]
            if (datetime.now() - cached_time).total_seconds() < CACHE_DURATION:
                return cached_df

        await asyncio.sleep(0.5)
        ohlcv = await BINANCE.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        if len(df) < limit:
            print(f"Uyarı: {symbol} için yeterli veri yok ({len(df)} mum)")

        DATA_CACHE[cache_key] = (df, datetime.now())
        return df
    except Exception as e:
        print(f"Veri alınamadı ({symbol}, {timeframe}): {e}")
        traceback.print_exc()
        return None

# CoinMarketCap Top 30
async def get_top_30_coins():
    url = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest"
    headers = {"X-CMC_PRO_API_KEY": CMC_API_KEY} if CMC_API_KEY else {}
    params = {"start": "1", "limit": "30", "convert": "USD"}

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers, params=params) as response:
                if response.status != 200:
                    return "❌ CoinMarketCap API hatası. Lütfen daha sonra tekrar deneyin."
                data = await response.json()
                if "data" not in data:
                    return "❌ CoinMarketCap verisi alınamadı."

                coins = data["data"]
                message = "📊 CoinMarketCap En İyi 30 Coin:\n\n"
                for coin in coins:
                    rank = coin["cmc_rank"]
                    name = coin["name"]
                    symbol = coin["symbol"]
                    price = coin["quote"]["USD"]["price"]
                    percent_change = coin["quote"]["USD"]["percent_change_24h"]
                    message += (
                        f"#{rank} {name} ({symbol})\n"
                        f"💵 Fiyat: ${price:,.2f}\n"
                        f"📈 24s Değişim: {percent_change:.2f}%\n\n"
                    )
                return message
        except Exception as e:
            print(f"CoinMarketCap hatası: {e}")
            traceback.print_exc()
            return f"❌ Hata: CoinMarketCap verisi alınamadı. {str(e)}"

# İndikatör hesaplamaları
def calculate_rsi(data, periods=14):
    delta = data.diff()
    gain = delta.where(delta > 0, 0).rolling(window=periods).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=periods).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calculate_macd(data, fast=12, slow=26, signal=9):
    exp1 = data.ewm(span=fast, adjust=False).mean()
    exp2 = data.ewm(span=slow, adjust=False).mean()
    macd = exp1 - exp2
    signal_line = macd.ewm(span=signal, adjust=False).mean()
    return macd, signal_line

def calculate_bollinger_bands(data, periods=20, std_dev=2):
    sma = data.rolling(window=periods).mean()
    std = data.rolling(window=periods).std()
    upper_band = sma + (std * std_dev)
    lower_band = sma - (std * std_dev)
    return sma, upper_band, lower_band

def calculate_sma(data, periods=20):
    return data.rolling(window=periods).mean()

def calculate_ema(data, periods=20):
    return data.ewm(span=periods, adjust=False).mean()

def calculate_stochastic(data_high, data_low, data_close, periods=14):
    lowest_low = data_low.rolling(window=periods).min()
    highest_high = data_high.rolling(window=periods).max()
    k = 100 * (data_close - lowest_low) / (highest_high - lowest_low)
    d = k.rolling(window=3).mean()
    return k, d

def calculate_adx(data_high, data_low, data_close, periods=14):
    tr = np.maximum(data_high - data_low,
                    np.maximum(abs(data_high - data_close.shift()), abs(data_low - data_close.shift())))
    tr = tr.rolling(window=periods).mean()
    dm_plus = (data_high - data_high.shift()).where((data_high - data_high.shift()) > (data_low.shift() - data_low), 0)
    dm_minus = (data_low.shift() - data_low).where((data_low.shift() - data_low) > (data_high - data_high.shift()), 0)
    dm_plus = dm_plus.rolling(window=periods).mean()
    dm_minus = dm_minus.rolling(window=periods).mean()
    di_plus = 100 * dm_plus / tr
    di_minus = 100 * dm_minus / tr
    dx = 100 * abs(di_plus - di_minus) / (di_plus + di_minus)
    adx = dx.rolling(window=periods).mean()
    return adx, di_plus, di_minus

def calculate_cci(data_high, data_low, data_close, periods=20):
    typical_price = (data_high + data_low + data_close) / 3
    sma = typical_price.rolling(window=periods).mean()
    mad = typical_price.rolling(window=periods).apply(lambda x: np.mean(np.abs(x - np.mean(x))), raw=True)
    cci = (typical_price - sma) / (0.015 * mad)
    return cci

def calculate_williams_r(data_high, data_low, data_close, periods=14):
    highest_high = data_high.rolling(window=periods).max()
    lowest_low = data_low.rolling(window=periods).min()
    williams_r = -100 * (highest_high - data_close) / (highest_high - lowest_low)
    return williams_r

def calculate_mfi(data_high, data_low, data_close, volume, periods=14):
    typical_price = (data_high + data_low + data_close) / 3
    raw_money_flow = typical_price * volume
    positive_flow = raw_money_flow.where(typical_price > typical_price.shift(), 0)
    negative_flow = raw_money_flow.where(typical_price < typical_price.shift(), 0)
    positive_flow = positive_flow.rolling(window=periods).sum()
    negative_flow = negative_flow.rolling(window=periods).sum()
    mfi = 100 - (100 / (1 + positive_flow / negative_flow))
    return mfi

def calculate_atr(df, periods=14):
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    tr = np.maximum(high_low, np.maximum(high_close, low_close))
    atr = tr.rolling(window=periods).mean()
    return atr

# Sinyal üretme
def generate_signal(df, symbol, timeframe):
    if df is None or len(df) < 50 or not all(col in df.columns for col in ['open', 'high', 'low', 'close', 'volume']):
        return f"❌ Hata: {symbol} için yeterli veya geçerli veri yok."

    df["rsi"] = calculate_rsi(df["close"])
    df["macd"], df["macd_signal"] = calculate_macd(df["close"])
    df["sma"], df["bb_upper"], df["bb_lower"] = calculate_bollinger_bands(df["close"])
    df["ema"] = calculate_ema(df["close"])
    df["stoch_k"], df["stoch_d"] = calculate_stochastic(df["high"], df["low"], df["close"])
    df["adx"], df["di_plus"], df["di_minus"] = calculate_adx(df["high"], df["low"], df["close"])
    df["cci"] = calculate_cci(df["high"], df["low"], df["close"])
    df["williams_r"] = calculate_williams_r(df["high"], df["low"], df["close"])
    df["mfi"] = calculate_mfi(df["high"], df["low"], df["close"], df["volume"])
    df["atr"] = calculate_atr(df)

    if df[["rsi", "macd", "bb_upper", "bb_lower", "atr"]].iloc[-1].isna().any():
        return f"❌ Hata: {symbol} için indikatör hesaplaması başarısız."

    latest_rsi = df["rsi"].iloc[-1]
    latest_macd = df["macd"].iloc[-1]
    latest_macd_signal = df["macd_signal"].iloc[-1]
    latest_price = df["close"].iloc[-1]
    latest_bb_upper = df["bb_upper"].iloc[-1]
    latest_bb_lower = df["bb_lower"].iloc[-1]
    latest_sma = df["sma"].iloc[-1]
    latest_ema = df["ema"].iloc[-1]
    latest_stoch_k = df["stoch_k"].iloc[-1]
    latest_stoch_d = df["stoch_d"].iloc[-1]
    latest_adx = df["adx"].iloc[-1]
    latest_di_plus = df["di_plus"].iloc[-1]
    latest_di_minus = df["di_minus"].iloc[-1]
    latest_cci = df["cci"].iloc[-1]
    latest_williams_r = df["williams_r"].iloc[-1]
    latest_mfi = df["mfi"].iloc[-1]
    latest_atr = df["atr"].iloc[-1]

    buy_signals = []
    sell_signals = []

    if latest_rsi < 30:
        buy_signals.append("RSI")
    elif latest_rsi > 70:
        sell_signals.append("RSI")

    if latest_macd > latest_macd_signal and df["macd"].iloc[-2] <= df["macd_signal"].iloc[-2]:
        buy_signals.append("MACD")
    elif latest_macd < latest_macd_signal and df["macd"].iloc[-2] >= df["macd_signal"].iloc[-2]:
        sell_signals.append("MACD")

    if latest_price < latest_bb_lower:
        buy_signals.append("Bollinger")
    elif latest_price > latest_bb_upper:
        sell_signals.append("Bollinger")

    if latest_price > latest_sma and df["close"].iloc[-2] <= df["sma"].iloc[-2]:
        buy_signals.append("SMA")
    elif latest_price < latest_sma and df["close"].iloc[-2] >= df["sma"].iloc[-2]:
        sell_signals.append("SMA")

    if latest_price > latest_ema and df["close"].iloc[-2] <= df["ema"].iloc[-2]:
        buy_signals.append("EMA")
    elif latest_price < latest_ema and df["close"].iloc[-2] >= df["ema"].iloc[-2]:
        sell_signals.append("EMA")

    if latest_stoch_k < 20 and latest_stoch_k > latest_stoch_d:
        buy_signals.append("Stochastic")
    elif latest_stoch_k > 80 and latest_stoch_k < latest_stoch_d:
        sell_signals.append("Stochastic")

    if latest_adx > 25 and latest_di_plus > latest_di_minus:
        buy_signals.append("ADX")
    elif latest_adx > 25 and latest_di_minus > latest_di_plus:
        sell_signals.append("ADX")

    if latest_cci < -100:
        buy_signals.append("CCI")
    elif latest_cci > 100:
        sell_signals.append("CCI")

    if latest_williams_r < -80:
        buy_signals.append("Williams %R")
    elif latest_williams_r > -20:
        sell_signals.append("Williams %R")

    if latest_mfi < 20:
        buy_signals.append("MFI")
    elif latest_mfi > 80:
        sell_signals.append("MFI")

    buy_count = len(buy_signals)
    sell_count = len(sell_signals)
    if buy_count > sell_count and buy_count > 0:
        signal_type = "Uzun"
        signal_emoji = "🟢"
        stop_loss = latest_price - (2 * latest_atr)
        take_profit = latest_price + (4 * latest_atr)
        rr = (take_profit - latest_price) / (latest_price - stop_loss) if latest_price != stop_loss else 0
    elif sell_count > buy_count and sell_count > 0:
        signal_type = "Kısa"
        signal_emoji = "🔴"
        stop_loss = latest_price + (2 * latest_atr)
        take_profit = latest_price - (4 * latest_atr)
        rr = (latest_price - take_profit) / (stop_loss - latest_price) if stop_loss != latest_price else 0
    else:
        signal_type = random.choice(["Uzun", "Kısa"])
        signal_emoji = "🟢" if signal_type == "Uzun" else "🔴"
        if signal_type == "Uzun":
            stop_loss = latest_price - (2 * latest_atr)
            take_profit = latest_price + (4 * latest_atr)
            rr = (take_profit - latest_price) / (latest_price - stop_loss) if latest_price != stop_loss else 0
        else:
            stop_loss = latest_price + (2 * latest_atr)
            take_profit = latest_price - (4 * latest_atr)
            rr = (latest_price - take_profit) / (stop_loss - latest_price) if stop_loss != latest_price else 0
        return (
            f"ℹ️ {symbol} için sinyal bulunamadı. Yeni sinyal için lütfen biraz bekleyin.\n"
            f"{signal_emoji} {signal_type} Sinyal | #{symbol.replace('/', '')}\n"
            f"🕒 Zaman Dilimi: {timeframe}\n"
            f"💵 Fiyat: {latest_price:.5f} USDT\n"
            f"🎯 Kâr Al: {take_profit:.5f} USDT\n"
            f"🛡️ Zarar Durdur: {stop_loss:.5f} USDT\n"
            f"📊 Risk-Ödül Oranı: {rr:.2f}"
        )

    message = (
        f"{signal_emoji} {signal_type} Sinyal | #{symbol.replace('/', '')}\n"
        f"🕒 Zaman Dilimi: {timeframe}\n"
        f"💵 Fiyat: {latest_price:.5f} USDT\n"
        f"🎯 Kâr Al: {take_profit:.5f} USDT\n"
        f"🛡️ Zarar Durdur: {stop_loss:.5f} USDT\n"
        f"📊 Risk-Ödül Oranı: {rr:.2f}"
    )
    return message

# Telegram komutları
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    args = context.args

    if not args:
        await update.message.reply_text("🚫 Yetkisiz erişim! /start id ile yetki al.")
        return

    user_id = args[0]
    result = check_user(user_id)

    if result:
        saved_chat_id = result[0]
        if saved_chat_id is None:
            save_user(user_id, chat_id)
            await update.message.reply_text(
                f"🚀 Yetki alındı! Kullanıcı ID: {user_id}\n📡 Sinyal almak için /sinyal yaz.")
        elif saved_chat_id == chat_id:
            await update.message.reply_text(
                f"🚀 Zaten yetkiniz var! Kullanıcı ID: {user_id}\n📡 Sinyal almak için /sinyal yaz.")
        else:
            await update.message.reply_text(
                f"🚫 Bu kullanıcı ID ({user_id}) başka bir hesaba kayıtlı!")
    else:
        await update.message.reply_text("🚫 Geçersiz ID! Lütfen doğru ID ile tekrar deneyin.")

async def sinyal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if not check_chat_id(chat_id):
        await update.message.reply_text("🚫 Yetkisiz erişim! /start id ile yetki al.")
        return

    args = context.args
    coin = None
    timeframe = random.choice(VALID_TIMEFRAMES)  # Varsayılan: rastgele zaman dilimi

    if args:
        if len(args) == 1:
            if args[0] in ["1", "2", "4", "6"]:
                timeframe = f"{args[0]}h"
            else:
                coin = args[0].upper()
        elif len(args) == 2:
            coin = args[0].upper()
            if args[1] in ["1", "2", "4", "6"]:
                timeframe = f"{args[1]}h"
            else:
                await update.message.reply_text("❌ Geçersiz zaman dilimi! (1, 2, 4, 6 kullanın)")
                return

    usdt_pairs = await get_usdt_pairs()
    if not usdt_pairs:
        await update.message.reply_text("❌ USDT çiftleri alınamadı!")
        return

    if coin:
        symbol = f"{coin}/USDT"
        if symbol not in usdt_pairs:
            await update.message.reply_text(f"❌ {coin}/USDT Binance’ta bulunamadı!")
            return
    else:
        symbol = random.choice(usdt_pairs)

    max_retries = 3
    for attempt in range(max_retries):
        try:
            df = await get_price_data(symbol, timeframe)
            message = generate_signal(df, symbol, timeframe)
            await update.message.reply_text(message)
            return
        except ccxt_async.RateLimitExceeded:
            if attempt < max_retries - 1:
                print(f"Rate limit hatası, {10 * (attempt + 1)} saniye bekleniyor...")
                await asyncio.sleep(10 * (attempt + 1))
                continue
            await update.message.reply_text("❌ Binance API limitine ulaşıldı. Lütfen daha sonra tekrar deneyin.")
            return
        except ccxt_async.NetworkError:
            await update.message.reply_text("❌ Ağ hatası. İnternet bağlantınızı kontrol edin.")
            return
        except Exception as e:
            print(f"Sinyal hatası: {e}")
            traceback.print_exc()
            await update.message.reply_text(f"❌ Hata: {symbol} için sinyal üretilemedi. {str(e)}")
            return

# Favori coin işlemleri için veritabanı fonksiyonları
def get_favorites(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT favorites FROM users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    if result and result[0]:
        return result[0].split(",")
    return []

def add_favorite(user_id, coin):
    favorites = get_favorites(user_id)
    if coin not in favorites:
        favorites.append(coin)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE users SET favorites = ? WHERE user_id = ?", (",".join(favorites), user_id))
    conn.commit()
    conn.close()

def remove_favorite(user_id, coin):
    favorites = get_favorites(user_id)
    if coin in favorites:
        favorites.remove(coin)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE users SET favorites = ? WHERE user_id = ?", (",".join(favorites), user_id))
    conn.commit()
    conn.close()

# Favori coin komutları
async def favori(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    user = check_chat_id(chat_id)
    if not user:
        await update.message.reply_text("🚫 Yetkisiz erişim! /start id ile yetki al.")
        return

    user_id = user[0]
    args = context.args
    favorites = get_favorites(user_id)

    if args:
        action = args[0].lower()
        if action == "ekle" and len(args) > 1:
            coin = args[1].upper()
            add_favorite(user_id, coin)
            await update.message.reply_text(f"✅ {coin} favorilerinize eklendi.")
            return
        elif action == "sil" and len(args) > 1:
            coin = args[1].upper()
            remove_favorite(user_id, coin)
            await update.message.reply_text(f"❌ {coin} favorilerinizden silindi.")
            return
        else:
            await update.message.reply_text("Kullanım: /favori ekle BTC veya /favori sil ETH")
            return

    if not favorites:
        await update.message.reply_text("⭐ Henüz favori coin eklemediniz. /favori ekle BTC gibi ekleyebilirsiniz.")
        return

    await update.message.reply_text("⭐ Favori coinleriniz: " + ", ".join(favorites))

async def favorilerim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    user = check_chat_id(chat_id)
    if not user:
        await update.message.reply_text("🚫 Yetkisiz erişim! /start id ile yetki al.")
        return

    user_id = user[0]
    favs = get_favorites(user_id)
    if favs:
        await update.message.reply_text("⭐ Favori coinleriniz: " + ", ".join(favs))
    else:
        await update.message.reply_text("⭐ Henüz favori coin eklemediniz.")

# Bilgi ve yardım komutları
async def bilgi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bilgi_text = (
        "ℹ️ *SmartKoinBot Hakkında Detaylı Bilgi*\n\n"
        "SmartKoinBot, Binance spot piyasasındaki USDT pariteleri için teknik analiz tabanlı otomatik sinyal üreten bir Telegram botudur.\n\n"
        "*Özellikler:*\n"
        "- Gerçek zamanlı teknik analiz (RSI, MACD, Bollinger, EMA, vb.)\n"
        "- Favori coin ekleme/çıkarma ve sadece favorilerden sinyal alma\n"
        "- CoinMarketCap Top 30 listesi\n"
        "- Kullanıcıya özel yetkilendirme ve güvenlik\n"
        "- Gelişmiş /help ve /bilgi menüleri\n"
        "- (Yakında) Sinyal geçmişi, premium sistem, alarm, admin paneli ve daha fazlası!\n\n"
        "*Kullanım için örnekler:*\n"
        "- /start yetkiliadmin\n"
        "- /sinyal BTC 1\n"
        "- /favori ekle BTC\n"
        "- /top30\n"
        "Her türlü soru ve destek için: @senin_telegram_kullanıcı_adın\n"
        "Botun sahibi: [Senin Adın veya Markan]\n"
        "Gizlilik: Kullanıcı verileriniz üçüncü kişilerle paylaşılmaz.\n"
    )
    await update.message.reply_text(bilgi_text, parse_mode="HTML")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🤖 *Komutlar ve Açıklamaları:*\n\n"
        "/start id - Yetki alır ve giriş yaparsın. (Örnek: /start kullanıcı adınız)\n"
        "/sinyal [COIN] [1|2|4|6] - Sinyal alırsın. (Örnek: /sinyal BTC 1)\n"
        "/top30 - CoinMarketCap'ten en iyi 30 coini listeler.\n"
        "/exit - Çıkış yapar ve hesabını devre dışı bırakır.\n"
        "/help - Bu yardım menüsünü gösterir.\n"
        "/bilgi - Botun detaylı açıklaması ve kullanım rehberi.\n"
        "/favori ekle COIN - Favori coin ekler. (Örnek: /favori ekle BTC)\n"
        "/favori sil COIN - Favori coin siler. (Örnek: /favori sil ETH)\n"
        "/favorilerim - Tüm favori coinlerini listeler.\n"
    )
    await update.message.reply_text(help_text, parse_mode="HTML")

async def exit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    result = check_chat_id(chat_id)
    if not result:
        await update.message.reply_text("🚫 Yetkisiz erişim! Önce /start id ile yetki almalısınız.")
        return
    exit_user(chat_id)
    await update.message.reply_text("✅ Başarıyla çıkış yaptınız. Tekrar giriş için /start id kullanabilirsiniz.")

async def top30(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if not check_chat_id(chat_id):
        await update.message.reply_text("🚫 Yetkisiz erişim! /start id ile yetki al.")
        return
    message = await get_top_30_coins()
    await update.message.reply_text(message)

# Botu başlat
async def shutdown(app, binance):
    if app:
        await app.stop()
        await app.shutdown()
    await binance.close()

def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = None
    try:
        init_db()
        # Binance bağlantısını test et
        loop.run_until_complete(test_binance_connection())
        app = Application.builder().token(TELEGRAM_TOKEN).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("sinyal", sinyal))
        app.add_handler(CommandHandler("exit", exit))
        app.add_handler(CommandHandler("Top30", top30))
        app.add_handler(CommandHandler("favori", favori))
        app.add_handler(CommandHandler("favorilerim", favorilerim))
        app.add_handler(CommandHandler("bilgi", bilgi))
        app.add_handler(CommandHandler("help", help_command))
        print("Bot başlatılıyor...")
        loop.run_until_complete(app.run_polling(allowed_updates=Update.ALL_TYPES))
    except KeyboardInterrupt:
        print("Bot durduruluyor...")
    except Exception as e:
        print(f"Hata: {e}")
        traceback.print_exc()
    finally:
        if not loop.is_closed():
            try:
                loop.run_until_complete(shutdown(app, BINANCE))
            except Exception as e:
                print(f"Kapatma sırasında hata: {e}")
            finally:
                loop.close()

if __name__ == "__main__":
    main()