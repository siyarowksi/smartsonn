import os
import asyncio
import sqlite3
import numpy as np
import pandas as pd
import ccxt.async_support as ccxt_async
from datetime import datetime, timedelta
import traceback
import random
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import platform
import aiohttp
import aiohttp.resolver
from dotenv import load_dotenv
import logging
from logging.handlers import TimedRotatingFileHandler

# Random seed başlat
random.seed()

# .env dosyasını yükle
load_dotenv()

# Windows için SelectorEventLoop
if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# aiohttp için varsayılan resolver'ı ThreadedResolver olarak ayarla
aiohttp.resolver.DefaultResolver = aiohttp.resolver.ThreadedResolver

# Loglama ayarları
# Genel loglar için (bot.log)
file_handler = logging.FileHandler("bot.log")
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

# Sinyal mesajları için aylık rotasyonlu log (signals.log)
signal_handler = TimedRotatingFileHandler(
    "signals.log",
    when="midnight",
    interval=30,  # Aylık rotasyon
    backupCount=12,  # 12 aylık log sakla
    encoding='utf-8'
)
signal_handler.setLevel(logging.INFO)
signal_handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
signal_handler.suffix = "%Y-%m.log"  # Log dosyalarına yıl-ay eki (ör: signals-2025-07.log)

# Konsola hiçbir logging çıktısı göndermemek için sadece dosya işleyicilerini ekle
logging.basicConfig(
    level=logging.INFO,
    handlers=[file_handler, signal_handler]
)

# Sinyal mesajları için özel logger
signal_logger = logging.getLogger('signal_logger')
signal_logger.setLevel(logging.INFO)
signal_logger.addHandler(signal_handler)
signal_logger.propagate = False  # Genel logger'a yayılmasın

# Ortam değişkenleri
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', '7818791938:AAHt8jbloyU-TdOyKBd_T172Eqyd1jKIUJo')
CMC_API_KEY = os.getenv('CMC_API_KEY', '')
DB_FILE = 'users.db'

# Binance ayarları
BINANCE = ccxt_async.binance({
    'apiKey': os.getenv('BINANCE_API_KEY', 'ba2AjFONSLAVd2c95WCgZZL23xOs6MYiWHW8r4E0d2AcLynQUDeBWVkULxDSkB3X'),
    'secret': os.getenv('BINANCE_API_SECRET', 'i8FYOJQ1fMoHP7Sbsf3VbuyjPRwrKoHaprXxl7n53ZalvvsV0M8C9Mp8bfTgiTov'),
    'enableRateLimit': True,
    'rateLimit': 1000,
    'options': {
        'defaultType': 'spot',
    }
})

# Önbellek ayarları
MARKETS_CACHE = None
MARKETS_CACHE_TIME = None
DATA_CACHE = {}
CACHE_DURATION = 3600
VALID_TIMEFRAMES = ['1h', '2h', '4h', '6h']
TICKERS_CACHE = None
TICKERS_CACHE_TIME = None
TICKERS_CACHE_DURATION = 120


def init_db():
    """Veritabanını başlatır ve gerekli tabloları oluşturur/günceller."""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute('''CREATE TABLE IF NOT EXISTS users
                        (user_id TEXT PRIMARY KEY, chat_id INTEGER, created_at TEXT)''')

            # Signals tablosunu oluştur veya güncelle
            c.execute('''CREATE TABLE IF NOT EXISTS signals
                        (id INTEGER PRIMARY KEY AUTOINCREMENT,
                         symbol TEXT,
                         timeframe TEXT,
                         signal_type TEXT,
                         price REAL,
                         stop_loss REAL,
                         take_profit REAL,
                         atr REAL,
                         created_at TEXT,
                         status TEXT DEFAULT 'aktif',
                         outcome_timestamp TEXT)''')

            # Geriye dönük uyumluluk için sütunları kontrol et ve ekle
            c.execute("PRAGMA table_info(signals)")
            columns = [col[1] for col in c.fetchall()]
            if 'status' not in columns:
                c.execute("ALTER TABLE signals ADD COLUMN status TEXT DEFAULT 'aktif'")
                logging.info("signals tablosuna 'status' sütunu eklendi.")
            if 'outcome_timestamp' not in columns:
                c.execute("ALTER TABLE signals ADD COLUMN outcome_timestamp TEXT")
                logging.info("signals tablosuna 'outcome_timestamp' sütunu eklendi.")

            predefined_users = [
                ('yetkiliadmin', None),
                ('prokullanici', None),
                ('vipkullanici', None)
            ]
            for user_id, chat_id in predefined_users:
                c.execute('INSERT OR IGNORE INTO users (user_id, chat_id, created_at) VALUES (?, ?, ?)',
                          (user_id, chat_id, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
            conn.commit()
            logging.info("Veritabanı başlatma ve güncelleme tamamlandı.")
    except Exception as e:
        logging.critical(f"Veritabanı başlatılırken kritik hata: {e}", exc_info=True)


async def log_to_excel(signal_data):
    """Sinyal verilerini ve sonuçlarını aylık Excel dosyalarına kaydeder."""
    try:
        created_at_dt = datetime.strptime(signal_data['created_at'], '%Y-%m-%d %H:%M:%S')
        filename = f"signals_{created_at_dt.strftime('%Y-%m')}.xlsx"

        columns = [
            'ID', 'Tarih', 'Sembol', 'Zaman Dilimi', 'Sinyal Tipi', 'Giriş Fiyatı',
            'Zarar Durdur (SL)', 'Kâr Al (TP)', 'Durum', 'Sonuç Tarihi'
        ]

        if os.path.exists(filename):
            df = pd.read_excel(filename)
        else:
            df = pd.DataFrame(columns=columns)

        if signal_data['id'] in df['ID'].values:
            index = df[df['ID'] == signal_data['id']].index[0]
            df.loc[index, 'Durum'] = signal_data['status']
            df.loc[index, 'Sonuç Tarihi'] = signal_data['outcome_timestamp']
        else:
            new_row_data = {
                'ID': signal_data['id'], 'Tarih': signal_data['created_at'], 'Sembol': signal_data['symbol'],
                'Zaman Dilimi': signal_data['timeframe'], 'Sinyal Tipi': signal_data['signal_type'],
                'Giriş Fiyatı': signal_data['price'], 'Zarar Durdur (SL)': signal_data['stop_loss'],
                'Kâr Al (TP)': signal_data['take_profit'], 'Durum': signal_data['status'],
                'Sonuç Tarihi': signal_data.get('outcome_timestamp', '')
            }
            new_row = pd.DataFrame([new_row_data])
            df = pd.concat([df, new_row], ignore_index=True)

        df.to_excel(filename, index=False)
        logging.info(f"Sinyal {signal_data['id']} Excel'e kaydedildi/güncellendi: {filename}")

    except Exception as e:
        logging.error(f"Excel'e yazma hatası: {e}", exc_info=True)


def save_signal(symbol, timeframe, signal_type, price, stop_loss, take_profit, atr):
    """Üretilen sinyali veritabanına kaydeder ve sinyal ID'sini ve oluşturma zamanını döndürür."""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            created_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            c.execute('''INSERT INTO signals (symbol, timeframe, signal_type, price, stop_loss, take_profit, atr, created_at, status) 
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                      (symbol, timeframe, signal_type, price, stop_loss, take_profit, atr, created_at, 'aktif'))
            signal_id = c.lastrowid
            conn.commit()
            logging.info(f"Sinyal veritabanına kaydedildi: {symbol} - {signal_type} (ID: {signal_id})")
            return signal_id, created_at
    except Exception as e:
        logging.error(f"Sinyal kaydetme hatası ({symbol}, {signal_type}): {e}", exc_info=True)
        return None, None


# ... (check_user, save_user, check_chat_id, exit_user, get_authorized_users fonksiyonları HİÇBİR DEĞİŞİKLİK OLMADAN aynı kalacak) ...
def check_user(user_id):
    """Belirtilen user_id'ye sahip kullanıcının varlığını ve chat_id'sini kontrol eder."""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute('SELECT chat_id FROM users WHERE user_id = ?', (user_id,))
            result = c.fetchone()
            return result
    except Exception as e:
        logging.error(f"Kullanıcı kontrol edilirken hata ({user_id}): {e}", exc_info=True)
        return None


def save_user(user_id, chat_id):
    """Kullanıcının chat_id'sini günceller veya yeni kullanıcıyı kaydeder."""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute('UPDATE users SET chat_id = ?, created_at = ? WHERE user_id = ?',
                      (chat_id, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), user_id))
            conn.commit()
            logging.info(f"Kullanıcı {user_id} ({chat_id}) kaydedildi/güncellendi.")
    except Exception as e:
        logging.error(f"Kullanıcı kaydedilirken hata ({user_id}, {chat_id}): {e}", exc_info=True)


def check_chat_id(chat_id):
    """Belirtilen chat_id'ye sahip kullanıcının user_id'sini kontrol eder."""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute('SELECT user_id FROM users WHERE chat_id = ?', (chat_id,))
            result = c.fetchone()
            return result
    except Exception as e:
        logging.error(f"Chat ID kontrol edilirken hata ({chat_id}): {e}", exc_info=True)
        return None


def exit_user(chat_id):
    """Belirtilen chat_id'ye sahip kullanıcının yetkisini kaldırır (chat_id'yi NULL yapar)."""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute('UPDATE users SET chat_id = NULL WHERE chat_id = ?', (chat_id,))
            conn.commit()
            logging.info(f"Kullanıcı {chat_id} çıkış yaptı.")
    except Exception as e:
        logging.error(f"Kullanıcı çıkış yaparken hata ({chat_id}): {e}", exc_info=True)


def get_authorized_users():
    """Aktif (chat_id'si olan) yetkili kullanıcıları döndürür."""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute('SELECT user_id, chat_id FROM users WHERE chat_id IS NOT NULL')
            results = c.fetchall()
            return results
    except Exception as e:
        logging.error(f"Yetkili kullanıcılar alınırken hata: {e}", exc_info=True)
        return []


# ... (get_top_20_binance_pairs, get_usdt_pairs, get_price_data, get_top_30_coins fonksiyonları HİÇBİR DEĞİŞİKLİK OLMADAN aynı kalacak) ...
async def get_top_20_binance_pairs():
    """Binance'tan en yüksek hacimli 20 USDT çiftini çeker."""
    global TICKERS_CACHE, TICKERS_CACHE_TIME
    now = datetime.now()
    if TICKERS_CACHE is None or TICKERS_CACHE_TIME is None or \
            (now - TICKERS_CACHE_TIME).total_seconds() > TICKERS_CACHE_DURATION:
        max_retries = 3
        for attempt in range(max_retries):
            try:
                logging.info(f"Binance Top 20 coin verisi çekiliyor... (Deneme {attempt + 1}/{max_retries})")
                await asyncio.sleep(0.5 * (attempt + 1))
                tickers = await BINANCE.fetch_tickers()
                TICKERS_CACHE = tickers
                TICKERS_CACHE_TIME = now
                logging.info("Binance ticker verileri başarıyla çekildi ve önbelleklendi.")
                break
            except ccxt_async.RateLimitExceeded:
                logging.warning(f'Binance rate limit aşıldı. {10 * (attempt + 1)} saniye bekleniyor...')
                await asyncio.sleep(10 * (attempt + 1))
            except (ccxt_async.ExchangeError, aiohttp.ClientError) as e:
                logging.error(f'Binance Top 20 çekilirken borsa veya ağ hatası: {e}')
                if attempt < max_retries - 1:
                    await asyncio.sleep(5 * (attempt + 1))
                else:
                    logging.error("Maksimum deneme sayısına ulaşıldı, Top 20 coin çekilemedi.")
                    return []
            except Exception as e:
                logging.critical(f'Beklenmeyen hata Binance Top 20 çekilirken: {e}', exc_info=True)
                return []
    if TICKERS_CACHE is None:
        logging.warning("TICKERS_CACHE boş, Top 20 coin alınamıyor.")
        return []
    usdt_tickers = {symbol: data for symbol, data in TICKERS_CACHE.items() if symbol.endswith('/USDT')}
    stable_coins = ['USDC', 'TUSD', 'BUSD', 'DAI', 'FDUSD', 'TRX']
    usdt_tickers = {symbol: data for symbol, data in usdt_tickers.items()
                    if not any(symbol.startswith(stable + '/') for stable in stable_coins)}
    all_volumes = [float(data.get('quoteVolume', 0) or 0) for data in usdt_tickers.values() if
                   data.get('quoteVolume') is not None]
    volume_threshold = 50000000
    if all_volumes:
        calculated_threshold = np.percentile(all_volumes, 50) * 0.1
        volume_threshold = max(calculated_threshold, 10000000)
        logging.info(f"Dinamik Hacim Eşiği: {volume_threshold:,.2f} USDT")
    else:
        logging.warning("Hacim verisi bulunamadı, varsayılan hacim eşiği kullanılıyor.")
    usdt_tickers = {symbol: data for symbol, data in usdt_tickers.items()
                    if float(data.get('quoteVolume', 0) or 0) > volume_threshold}
    sorted_pairs = sorted(
        usdt_tickers.items(),
        key=lambda x: float(x[1].get('quoteVolume', 0) or 0),
        reverse=True
    )
    top20_pairs = [symbol for symbol, _ in sorted_pairs[:20]]
    logging.info(f"Seçilen Top 20 Binance Çifti Sayısı: {len(top20_pairs)}")
    return top20_pairs


async def get_usdt_pairs():
    """Binance'tan aktif USDT çiftlerini çeker."""
    global MARKETS_CACHE, MARKETS_CACHE_TIME
    max_retries = 3
    for attempt in range(max_retries):
        try:
            if MARKETS_CACHE is None or MARKETS_CACHE_TIME is None or \
                    (datetime.now() - MARKETS_CACHE_TIME).total_seconds() > CACHE_DURATION:
                logging.info(f"Binance piyasa verileri çekiliyor... (Deneme {attempt + 1}/{max_retries})")
                await asyncio.sleep(0.5 * (attempt + 1))
                markets = await BINANCE.load_markets()
                MARKETS_CACHE = markets
                MARKETS_CACHE_TIME = datetime.now()
                logging.info("Binance piyasa verileri başarıyla çekildi ve önbelleklendi.")
            usdt_pairs = [market for market in MARKETS_CACHE if
                          market.endswith('/USDT') and MARKETS_CACHE[market]['active']]
            stable_coins = ['USDC', 'TUSD', 'BUSD', 'DAI', 'FDUSD']
            usdt_pairs = [pair for pair in usdt_pairs
                          if not any(pair.startswith(stable + '/') for stable in stable_coins)]
            return usdt_pairs
        except ccxt_async.RateLimitExceeded:
            logging.warning(f'USDT çiftleri alınırken rate limit hatası, {10 * (attempt + 1)} saniye bekleniyor...')
            await asyncio.sleep(10 * (attempt + 1))
        except (ccxt_async.ExchangeError, aiohttp.ClientError) as e:
            logging.error(f'USDT çiftleri alınırken borsa veya ağ hatası: {e}')
            if attempt < max_retries - 1:
                await asyncio.sleep(5 * (attempt + 1))
            else:
                logging.error("Maksimum deneme sayısına ulaşıldı, USDT çiftleri alınamadı.")
                return []
        except Exception as e:
            logging.critical(f'Beklenmeyen hata USDT çiftleri alınırken: {e}', exc_info=True)
            return []
    return []


async def get_price_data(symbol, timeframe='1h', limit=100):
    """Belirtilen sembol ve zaman dilimi için fiyat verilerini çeker."""
    cache_key = f'{symbol}_{timeframe}'
    try:
        if cache_key in DATA_CACHE:
            cached_df, cached_time = DATA_CACHE[cache_key]
            if (datetime.now() - cached_time).total_seconds() < CACHE_DURATION:
                return cached_df
        logging.info(f"Fiyat verisi çekiliyor: {symbol} {timeframe} (limit: {limit})")
        await asyncio.sleep(0.5)
        ohlcv = await BINANCE.fetch_ohlcv(symbol, timeframe, limit=limit)
        if not ohlcv:
            logging.warning(f"OHLCV verisi boş geldiği için {symbol} {timeframe} için veri çekilemedi.")
            return None
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        if len(df) < 50:
            logging.warning(f"Yetersiz veri ({len(df)} çubuk) için {symbol} {timeframe}, sinyal üretilemez.")
            return None
        DATA_CACHE[cache_key] = (df, datetime.now())
        logging.info(f"Veri çekme ve önbelleğe alma başarılı: {symbol}_{timeframe}")
        return df
    except ccxt_async.RateLimitExceeded:
        logging.warning(f'Veri çekilirken rate limit hatası ({symbol}): Yeniden deneme bekleniyor.')
        return None
    except Exception as e:
        logging.error(f'Veri çekme hatası ({symbol}, {timeframe}): {e}', exc_info=True)
        return None


async def get_top_30_coins():
    """CoinMarketCap'ten en iyi 30 coini çeker ve formatlı mesaj döndürür."""
    url = 'https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest'
    headers = {'X-CMC_PRO_API_KEY': CMC_API_KEY} if CMC_API_KEY else {}
    params = {'start': '1', 'limit': '30', 'convert': 'USD'}
    if not CMC_API_KEY:
        logging.warning("CMC_API_KEY ayarlanmamış, CoinMarketCap verisi çekilemiyor.")
        return '❌ CoinMarketCap API anahtarı ayarlanmamış. Lütfen yöneticinizle iletişime geçin.'
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers, params=params) as response:
                if response.status != 200:
                    logging.error(
                        f"CoinMarketCap API hatası: Status {response.status}, Response: {await response.text()}")
                    return '❌ CoinMarketCap API hatası. Lütfen daha sonra tekrar deneyin.'
                data = await response.json()
                if 'data' not in data:
                    logging.error("CoinMarketCap verisi 'data' anahtarı içermiyor.")
                    return '❌ CoinMarketCap verisi alınamadı.'
                coins = data['data']
                message = '📊 CoinMarketCap En İyi 30 Coin:\n\n'
                for coin in coins:
                    rank = coin['cmc_rank']
                    name = coin['name']
                    symbol = coin['symbol']
                    price = coin['quote']['USD']['price']
                    percent_change = coin['quote']['USD']['percent_change_24h']
                    message += (
                        f'#{rank} {name} ({symbol})\n'
                        f'💵 Fiyat: ${price:,.2f}\n'
                        f'📈 24s Değişim: {percent_change:.2f}%\n\n'
                    )
                logging.info("CoinMarketCap Top 30 verisi başarıyla çekildi.")
                return message
        except aiohttp.ClientError as e:
            logging.error(f'CoinMarketCap ağ hatası: {e}', exc_info=True)
            return f'❌ Hata: CoinMarketCap verisi alınamadı. Ağ bağlantı sorunu.'
        except Exception as e:
            logging.critical(f'Beklenmeyen hata CoinMarketCap verisi çekilirken: {e}', exc_info=True)
            return f'❌ Hata: CoinMarketCap verisi alınamadı. {str(e)}'


# ... (Tüm calculate_* fonksiyonları HİÇBİR DEĞİŞİKLİK OLMADAN aynı kalacak) ...
def calculate_rsi(data, periods=14):
    """RSI (Relative Strength Index) hesaplar."""
    delta = data.diff()
    gain = delta.where(delta > 0, 0).rolling(window=periods).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=periods).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def calculate_macd(data, fast=12, slow=26, signal=9):
    """MACD (Moving Average Convergence Divergence) hesaplar."""
    exp1 = data.ewm(span=fast, adjust=False).mean()
    exp2 = data.ewm(span=slow, adjust=False).mean()
    macd = exp1 - exp2
    signal_line = macd.ewm(span=signal, adjust=False).mean()
    return macd, signal_line


def calculate_bollinger_bands(data, periods=20, std_dev=2):
    """Bollinger Bantlarını hesaplar."""
    sma = data.rolling(window=periods).mean()
    std = data.rolling(window=periods).std()
    upper_band = sma + (std * std_dev)
    lower_band = sma - (std * std_dev)
    return sma, upper_band, lower_band


def calculate_sma(data, periods=20):
    """Basit Hareketli Ortalama (SMA) hesaplar."""
    return data.rolling(window=periods).mean()


def calculate_ema(data, periods=20):
    """Üstel Hareketli Ortalama (EMA) hesaplar."""
    return data.ewm(span=periods, adjust=False).mean()


def calculate_stochastic(data_high, data_low, data_close, periods=14):
    """Stokastik Osilatör hesaplar."""
    lowest_low = data_low.rolling(window=periods).min()
    highest_high = data_high.rolling(window=periods).max()
    k = 100 * (data_close - lowest_low) / (highest_high - lowest_low)
    d = k.rolling(window=3).mean()
    return k, d


def calculate_adx(data_high, data_low, data_close, periods=14):
    """ADX (Average Directional Index) hesaplar."""
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


def calculate_williams_r(data_high, data_low, data_close, periods=14):
    """Williams %R hesaplar."""
    highest_high = data_high.rolling(window=periods).max()
    lowest_low = data_low.rolling(window=periods).min()
    williams_r = -100 * (highest_high - data_close) / (highest_high - lowest_low)
    return williams_r


def calculate_mfi(data_high, data_low, data_close, volume, periods=14):
    """MFI (Money Flow Index) hesaplar."""
    typical_price = (data_high + data_low + data_close) / 3
    raw_money_flow = typical_price * volume
    positive_flow = raw_money_flow.where(typical_price > typical_price.shift(), 0)
    negative_flow = raw_money_flow.where(typical_price < typical_price.shift(), 0)
    positive_flow = positive_flow.rolling(window=periods).sum()
    negative_flow = negative_flow.rolling(window=periods).sum()
    mfi = 100 - (100 / (1 + positive_flow / negative_flow)) if negative_flow.iloc[-1] != 0 else np.nan
    return mfi


def calculate_atr(df, periods=14):
    """ATR (Average True Range) hesaplar."""
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    tr = np.maximum(high_low, np.maximum(high_close, low_close))
    atr = tr.rolling(window=periods).mean()
    return atr


def calculate_ema_cross(data, fast_period=9, slow_period=21):
    """EMA Kesişimi için hızlı ve yavaş EMA'ları hesaplar."""
    fast_ema = data.ewm(span=fast_period, adjust=False).mean()
    slow_ema = data.ewm(span=slow_period, adjust=False).mean()
    return fast_ema, slow_ema


def calculate_price_action(df):
    """Basit Boğa/Ayı Yutan formasyonunu tespit eder."""
    if len(df) < 3:
        return None
    latest = df.iloc[-1]
    prev = df.iloc[-2]
    bullish_engulfing = (prev['close'] < prev['open'] and
                         latest['close'] > latest['open'] and
                         latest['close'] > prev['open'] and
                         latest['open'] < prev['close'])
    bearish_engulfing = (prev['close'] > prev['open'] and
                         latest['close'] < latest['open'] and
                         latest['close'] < prev['open'] and
                         latest['open'] > prev['close'])
    return 'bullish' if bullish_engulfing else 'bearish' if bearish_engulfing else None


async def generate_signal(df, symbol, timeframe):
    """Veri çerçevesi ve göstergeleri kullanarak alım/satım sinyali üretir."""
    try:
        if df is None or len(df) < 50 or not all(
                col in df.columns for col in ['open', 'high', 'low', 'close', 'volume']):
            logging.warning(f"Sinyal üretilemedi: Yetersiz veya geçersiz veri ({symbol}, {timeframe}).")
            return None
        # İndikatör hesaplamaları (değişiklik yok)
        df['rsi'] = calculate_rsi(df['close'])
        df['macd'], df['macd_signal'] = calculate_macd(df['close'])
        df['sma'], df['bb_upper'], df['bb_lower'] = calculate_bollinger_bands(df['close'])
        df['ema'] = calculate_ema(df['close'])
        df['stoch_k'], df['stoch_d'] = calculate_stochastic(df['high'], df['low'], df['close'])
        df['adx'], df['di_plus'], df['di_minus'] = calculate_adx(df['high'], df['low'], df['close'])
        df['williams_r'] = calculate_williams_r(df['high'], df['low'], df['close'])
        df['mfi'] = calculate_mfi(df['high'], df['low'], df['close'], df['volume'])
        df['atr'] = calculate_atr(df)
        df['fast_ema'], df['slow_ema'] = calculate_ema_cross(df['close'])
        pa_signal = calculate_price_action(df)
        required_indicators = ['rsi', 'macd', 'bb_upper', 'bb_lower', 'atr', 'fast_ema', 'slow_ema',
                               'stoch_k', 'stoch_d', 'adx', 'di_plus', 'di_minus', 'williams_r', 'mfi']
        if df[required_indicators].iloc[-1].isna().any():
            logging.warning(f"Sinyal üretilemedi: Son indikatör değerlerinde NaN var ({symbol}, {timeframe}).")
            return None

        # Son değerleri alma (değişiklik yok)
        latest_rsi = df['rsi'].iloc[-1]
        latest_macd = df['macd'].iloc[-1]
        # ... diğer tüm latest değişkenleri ...
        latest_price = df['close'].iloc[-1]
        latest_atr = df['atr'].iloc[-1]
        latest_macd_signal = df['macd_signal'].iloc[-1]
        latest_bb_upper = df['bb_upper'].iloc[-1]
        latest_bb_lower = df['bb_lower'].iloc[-1]
        latest_sma = df['sma'].iloc[-1]
        latest_ema = df['ema'].iloc[-1]
        latest_stoch_k = df['stoch_k'].iloc[-1]
        latest_stoch_d = df['stoch_d'].iloc[-1]
        latest_adx = df['adx'].iloc[-1]
        latest_di_plus = df['di_plus'].iloc[-1]
        latest_di_minus = df['di_minus'].iloc[-1]
        latest_williams_r = df['williams_r'].iloc[-1]
        latest_mfi = df['mfi'].iloc[-1]
        latest_fast_ema = df['fast_ema'].iloc[-1]
        latest_slow_ema = df['slow_ema'].iloc[-1]

        # Sinyal koşulları (değişiklik yok)
        buy_signals = []
        sell_signals = []
        if latest_rsi < 30:
            buy_signals.append('RSI')
        elif latest_rsi > 70:
            sell_signals.append('RSI')
        if latest_macd > latest_macd_signal and df['macd'].iloc[-2] <= df['macd_signal'].iloc[-2]:
            buy_signals.append('MACD')
        elif latest_macd < latest_macd_signal and df['macd'].iloc[-2] >= df['macd_signal'].iloc[-2]:
            sell_signals.append('MACD')
        # ... diğer tüm sinyal koşulları ...
        if latest_price < latest_bb_lower:
            buy_signals.append('Bollinger')
        elif latest_price > latest_bb_upper:
            sell_signals.append('Bollinger')
        if latest_price > latest_sma and df['close'].iloc[-2] <= df['sma'].iloc[-2]:
            buy_signals.append('SMA')
        elif latest_price < latest_sma and df['close'].iloc[-2] >= df['sma'].iloc[-2]:
            sell_signals.append('SMA')
        if latest_price > latest_ema and df['close'].iloc[-2] <= df['ema'].iloc[-2]:
            buy_signals.append('EMA')
        elif latest_price < latest_ema and df['close'].iloc[-2] >= df['ema'].iloc[-2]:
            sell_signals.append('EMA')
        if latest_stoch_k < 20 and latest_stoch_k > latest_stoch_d:
            buy_signals.append('Stochastic')
        elif latest_stoch_k > 80 and latest_stoch_k < latest_stoch_d:
            sell_signals.append('Stochastic')
        if latest_adx > 25 and latest_di_plus > latest_di_minus:
            buy_signals.append('ADX')
        elif latest_adx > 25 and latest_di_minus > latest_di_plus:
            sell_signals.append('ADX')
        if latest_williams_r < -80:
            buy_signals.append('Williams %R')
        elif latest_williams_r > -20:
            sell_signals.append('Williams %R')
        if latest_mfi < 20:
            buy_signals.append('MFI')
        elif latest_mfi > 80:
            sell_signals.append('MFI')
        if latest_fast_ema > latest_slow_ema and df['fast_ema'].iloc[-2] <= df['slow_ema'].iloc[-2]:
            buy_signals.append('EMA Cross')
        elif latest_fast_ema < latest_slow_ema and df['fast_ema'].iloc[-2] >= df['slow_ema'].iloc[-2]:
            sell_signals.append('EMA Cross')
        if pa_signal == 'bullish':
            buy_signals.append('Price Action')
        elif pa_signal == 'bearish':
            sell_signals.append('Price Action')

        buy_count = len(buy_signals)
        sell_count = len(sell_signals)
        signal_type, signal_emoji = None, ''
        atr_stop_loss, atr_take_profit, rr = 0.0, 0.0, 0.0

        # YENİ: ATR Çarpanları Değiştirildi
        sl_multiplier = 1.5
        tp_multiplier = 3.0

        if buy_count > sell_count and buy_count > 0:
            signal_type, signal_emoji = 'Uzun', '🟢'
            atr_stop_loss = latest_price - (sl_multiplier * latest_atr)
            atr_take_profit = latest_price + (tp_multiplier * latest_atr)
            rr = (atr_take_profit - latest_price) / (latest_price - atr_stop_loss) if (
                                                                                                  latest_price - atr_stop_loss) != 0 else 0
        elif sell_count > buy_count and sell_count > 0:
            signal_type, signal_emoji = 'Kısa', '🔴'
            atr_stop_loss = latest_price + (sl_multiplier * latest_atr)
            atr_take_profit = latest_price - (tp_multiplier * latest_atr)
            rr = (latest_price - atr_take_profit) / (atr_stop_loss - latest_price) if (
                                                                                                  atr_stop_loss - latest_price) != 0 else 0
        else:
            return None  # Yeterli sinyal yoksa çık

        # Yorum oluşturma (değişiklik yok)
        comments = []
        if signal_type == 'Uzun':
            if 'RSI' in buy_signals and latest_rsi < 30: comments.append(
                "RSI aşırı satım bölgesinde, toparlanma bekleniyor.")
            if 'MACD' in buy_signals: comments.append("MACD yukarı kesişme yaptı, yükseliş momentumu güçlü.")
            # ... diğer tüm yorumlar ...
        elif signal_type == 'Kısa':
            if 'RSI' in sell_signals and latest_rsi > 70: comments.append(
                "RSI aşırı alım bölgesinde, düzeltme bekleniyor.")
            if 'MACD' in sell_signals: comments.append("MACD aşağı kesişme yaptı, düşüş momentumu güçlü.")
            # ... diğer tüm yorumlar ...
        comment = random.choice(comments) if comments else "Teknik göstergeler sinyali destekliyor."

        message = (
            f'{signal_emoji} {signal_type} Sinyal | #{symbol.replace("/", "")}\n'
            f'🕒 Zaman Dilimi: {timeframe}\n'
            f'💵 Fiyat: {latest_price:.8f} USDT\n'
            f'🎯 Kâr Al: {atr_take_profit:.8f} USDT\n'
            f'🛡️ Zarar Durdur: {atr_stop_loss:.8f} USDT\n'
            f'🧠 Yorum: {comment}\n'
            f'📊 Risk-Ödül Oranı: {rr:.2f}'
        )

        signal_id, created_at = save_signal(symbol, timeframe, signal_type, latest_price, atr_stop_loss,
                                            atr_take_profit, latest_atr)

        if signal_id:
            signal_data_for_excel = {
                'id': signal_id, 'created_at': created_at, 'symbol': symbol, 'timeframe': timeframe,
                'signal_type': signal_type, 'price': latest_price, 'stop_loss': atr_stop_loss,
                'take_profit': atr_take_profit, 'status': 'aktif'
            }
            await log_to_excel(signal_data_for_excel)
            logging.info(f"Sinyal başarıyla üretildi ve loglandı: {symbol} - {signal_type}")
            return message
        else:
            logging.error(f"Sinyal veritabanına kaydedilemedi.")
            return None

    except Exception as e:
        logging.critical(f"Sinyal oluşturma sırasında kritik hata ({symbol}, {timeframe}): {e}", exc_info=True)
        return None


async def test_binance_connection():
    # ... (Bu fonksiyon HİÇBİR DEĞİŞİKLİK OLMADAN aynı kalacak) ...
    """Binance API bağlantısını test eder."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get('https://api.binance.com/api/v3/ping') as response:
                if response.status == 200:
                    print('Binance API bağlantısı başarılı!')
                    return True
                else:
                    logging.error(f'Binance API bağlantısı başarısız: Status {response.status}')
                    return False
    except aiohttp.ClientError as e:
        logging.error(f'Binance bağlantı hatası (Ağ): {e}', exc_info=True)
        return False
    except Exception as e:
        logging.critical(f'Binance bağlantı testinde beklenmeyen hata: {e}', exc_info=True)
        return False


async def send_periodic_signals(app: Application):
    # ... (Bu fonksiyon HİÇBİR DEĞİŞİKLİK OLMADAN aynı kalacak) ...
    """Belirtilen saatlerde (09:30, 14:45, 19:15, 23:15) yetkili kullanıcılara sinyal gönderir."""
    signal_times = [
        "09:30:00",
        "14:45:00",
        "19:15:00",
        "23:15:00"
    ]
    while True:
        try:
            now = datetime.now()
            current_time = now.strftime("%H:%M:%S")
            next_signal_time = None
            current_date = now.date()
            # Bir sonraki sinyal zamanını bul
            for time_str in signal_times:
                signal_datetime = datetime.strptime(f"{current_date} {time_str}", "%Y-%m-%d %H:%M:%S")
                if now <= signal_datetime:
                    next_signal_time = signal_datetime
                    break
            if not next_signal_time:
                # Gün içinde başka sinyal yoksa, ertesi günün ilk sinyaline geç
                next_signal_time = datetime.strptime(
                    f"{(current_date + timedelta(days=1))} {signal_times[0]}", "%Y-%m-%d %H:%M:%S"
                )
            seconds_to_wait = (next_signal_time - now).total_seconds()
            if seconds_to_wait > 0:
                print(f"Bir sonraki sinyal: {next_signal_time.strftime('%H:%M')} ({next_signal_time})")
                await asyncio.sleep(seconds_to_wait)
            authorized_users = get_authorized_users()
            if not authorized_users:
                logging.info("Hiçbir yetkili kullanıcı bulunamadı, sinyal gönderilmiyor.")
                continue
            top20_pairs = await get_top_20_binance_pairs()
            if not top20_pairs:
                logging.warning("Top 20 coin alınamadı, bir sonraki zaman dilimine geçiliyor.")
                continue
            timeframe = random.choice(VALID_TIMEFRAMES)
            tried_coins = set()
            signal_sent_this_cycle = False
            while len(tried_coins) < len(top20_pairs):
                available_coins = [coin for coin in top20_pairs if coin not in tried_coins]
                if not available_coins:
                    logging.info("Tüm Top 20 coinler denendi, bir sonraki zaman dilimine geçiliyor.")
                    break
                symbol = random.choice(available_coins)
                tried_coins.add(symbol)
                logging.info(f"Sinyal üretimi deneniyor: {symbol} ({timeframe})")
                df = await get_price_data(symbol, timeframe)
                message = await generate_signal(df, symbol, timeframe)
                if message:
                    for user_id, chat_id in authorized_users:
                        try:
                            print(
                                f"Sinyal gönderildi: {datetime.now().strftime('%H:%M')} - {symbol.replace('/USDT', '')}/USDT")
                            await app.bot.send_message(chat_id=chat_id, text=message)
                            signal_logger.info(message)  # Sinyal mesajını signals.log'a kaydet
                            logging.info(f"Sinyal gönderildi: Kullanıcı {user_id} ({chat_id}), Sembol: {symbol}")
                        except Exception as e:
                            logging.error(f"Sinyal gönderilemedi (Kullanıcı {user_id}, Chat ID: {chat_id}): {e}",
                                          exc_info=True)
                    signal_sent_this_cycle = True
                    break
                else:
                    logging.info(f"Sinyal üretilemedi: {symbol} ({timeframe}). Başka bir coin deneniyor.")
            if not signal_sent_this_cycle:
                logging.info("Bu periyodik döngüde hiçbir sinyal gönderilemedi.")
        except Exception as e:
            logging.critical(f"Periyodik sinyal gönderme döngüsünde kritik hata: {e}", exc_info=True)
            await asyncio.sleep(60)  # Hata durumunda 1 dakika bekle ve tekrar dene


# YENİ FONKSİYON: TP/SL Takibi
async def monitor_active_signals(app: Application):
    """Aktif sinyalleri izler ve TP/SL durumunda bildirim gönderir."""
    await asyncio.sleep(30)
    logging.info("Sinyal takip mekanizması başlatıldı.")

    while True:
        try:
            with sqlite3.connect(DB_FILE) as conn:
                conn.row_factory = sqlite3.Row
                c = conn.cursor()
                c.execute("SELECT * FROM signals WHERE status = 'aktif'")
                active_signals = c.fetchall()

            if not active_signals:
                await asyncio.sleep(300)  # Aktif sinyal yoksa 5 dakika bekle
                continue

            symbols_to_check = list(set([s['symbol'] for s in active_signals]))
            tickers = await BINANCE.fetch_tickers(symbols=symbols_to_check)

            if not tickers:
                logging.warning("Takip edilecek sinyaller için fiyat verisi alınamadı.")
                await asyncio.sleep(60)
                continue

            for signal in active_signals:
                symbol = signal['symbol']
                current_price = tickers.get(symbol, {}).get('last')
                if not current_price:
                    continue

                outcome, outcome_status, message = None, '', ''

                is_long = signal['signal_type'] == 'Uzun'
                if is_long and current_price >= signal['take_profit']:
                    outcome, outcome_status = 'TP', 'TP Oldu'
                    message = f"✅ KÂR ALINDI (TP) | #{symbol.replace('/', '')}\n\nOrijinal sinyal kâr hedefine ulaştı!"
                elif is_long and current_price <= signal['stop_loss']:
                    outcome, outcome_status = 'SL', 'SL Oldu'
                    message = f"❌ ZARAR DURDUR (SL) | #{symbol.replace('/', '')}\n\nOrijinal sinyal zarar durdur seviyesine geriledi."
                elif not is_long and current_price <= signal['take_profit']:
                    outcome, outcome_status = 'TP', 'TP Oldu'
                    message = f"✅ KÂR ALINDI (TP) | #{symbol.replace('/', '')}\n\nOrijinal sinyal kâr hedefine ulaştı!"
                elif not is_long and current_price >= signal['stop_loss']:
                    outcome, outcome_status = 'SL', 'SL Oldu'
                    message = f"❌ ZARAR DURDUR (SL) | #{symbol.replace('/', '')}\n\nOrijinal sinyal zarar durdur seviyesine yükseldi."

                if outcome:
                    outcome_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

                    with sqlite3.connect(DB_FILE) as conn_update:
                        c_update = conn_update.cursor()
                        c_update.execute("UPDATE signals SET status = ?, outcome_timestamp = ? WHERE id = ?",
                                         (outcome_status, outcome_time, signal['id']))
                        conn_update.commit()
                    logging.info(f"Sinyal durumu güncellendi: ID {signal['id']} -> {outcome_status}")

                    signal_data = dict(signal)
                    signal_data.update({'status': outcome_status, 'outcome_timestamp': outcome_time})
                    await log_to_excel(signal_data)

                    authorized_users = get_authorized_users()
                    for user_id, chat_id in authorized_users:
                        if chat_id:
                            try:
                                await app.bot.send_message(chat_id=chat_id, text=message)
                            except Exception as e:
                                logging.error(f"TP/SL bildirimi gönderilemedi (Kullanıcı {user_id}): {e}")

            await asyncio.sleep(300)  # Kontroller arası 5 dakika bekle

        except Exception as e:
            logging.critical(f"Sinyal takip döngüsünde kritik hata: {e}", exc_info=True)
            await asyncio.sleep(60)


# ... (start, sinyal, active_users, bilgi, help_command, exit, top30, tum_cikis, kullanici_cikis, shutdown fonksiyonları HİÇBİR DEĞİŞİKLİK OLMADAN aynı kalacak) ...
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Botu başlatma ve kullanıcı yetkilendirme komutu."""
    chat_id = update.message.chat_id
    args = context.args
    if not args:
        await update.message.reply_text(
            '🚫 Yetkisiz erişim! Lütfen /start <ID> şeklinde bir ID ile yetki alın.')
        logging.warning(f"Geçersiz /start komutu: ID eksik. Chat ID: {chat_id}")
        return
    user_id = args[0]
    result = check_user(user_id)
    if result:
        saved_chat_id = result[0]
        if saved_chat_id is None:
            save_user(user_id, chat_id)
            await update.message.reply_text(
                f'🚀 Yetki alındı! Kullanıcı ID: {user_id}\n📡 Sinyal almak için /sinyal yaz.'
            )
            logging.info(f"Yeni kullanıcı yetkilendirildi: {user_id} ({chat_id})")
        elif saved_chat_id == chat_id:
            await update.message.reply_text(
                f'🚀 Zaten yetkiniz var! Kullanıcı ID: {user_id}\n📡 Sinyal almak için /sinyal yaz.'
            )
            logging.info(f"Mevcut kullanıcı tekrar bağlandı: {user_id} ({chat_id})")
        else:
            await update.message.reply_text(
                f'🚫 Bu kullanıcı ID ({user_id}) başka bir hesaba kayıtlı!'
            )
            logging.warning(
                f"ID ({user_id}) başka bir chat ID'ye ({saved_chat_id}) kayıtlı. Mevcut istek ({chat_id}) reddedildi.")
    else:
        await update.message.reply_text('🚫 Geçersiz ID! Lütfen doğru ID ile tekrar deneyin.')
        logging.warning(f"Geçersiz ID ile /start denemesi: {user_id}. Chat ID: {chat_id}")


async def sinyal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kullanıcının isteği üzerine tek seferlik sinyal üretir."""
    chat_id = update.message.chat_id
    if not check_chat_id(chat_id):
        await update.message.reply_text('🚫 Yetkisiz erişim! Lütfen /start <ID> ile yetki alın.')
        logging.warning(f"Yetkisiz sinyal isteği. Chat ID: {chat_id}")
        return
    try:
        args = context.args
        coin = None
        timeframe = random.choice(VALID_TIMEFRAMES)
        if args:
            if len(args) == 1:
                if args[0] in ['1', '2', '4', '6']:
                    timeframe = f'{args[0]}h'
                else:
                    coin = args[0].upper()
            elif len(args) == 2:
                coin = args[0].upper()
                if args[1] in ['1', '2', '4', '6']:
                    timeframe = f'{args[1]}h'
                else:
                    await update.message.reply_text('❌ Geçersiz zaman dilimi! (1, 2, 4, 6 kullanın)')
                    logging.warning(f"Geçersiz zaman dilimi argümanı: {args[1]}. Chat ID: {chat_id}")
                    return
        usdt_pairs = await get_usdt_pairs()
        if not usdt_pairs:
            await update.message.reply_text('❌ USDT çiftleri alınamadı! Lütfen daha sonra tekrar deneyin.')
            logging.error(f"USDT çiftleri alınamadı. Sinyal isteği başarısız. Chat ID: {chat_id}")
            return
        if coin:
            symbol = f'{coin}/USDT'
            if symbol not in usdt_pairs:
                await update.message.reply_text(
                    f'❌ {coin} USDT çifti bulunamadı veya aktif değil!')
                logging.warning(f"İstenen coin ({coin}) bulunamadı. Chat ID: {chat_id}")
                return
            logging.info(f"Belirli coin için sinyal denemesi: {symbol} ({timeframe}). Chat ID: {chat_id}")
            df = await get_price_data(symbol, timeframe)
            message = await generate_signal(df, symbol, timeframe)
            if message:
                await update.message.reply_text(message)
                signal_logger.info(message)  # Sinyal mesajını signals.log'a kaydet
                logging.info(f"Sinyal gönderildi: {symbol} ({timeframe}). Chat ID: {chat_id}")
            else:
                await update.message.reply_text(
                    f'❌ {coin} için bu zaman diliminde ({timeframe}) herhangi bir sinyal üretilemedi.')
                logging.info(f"Sinyal üretilemedi: {symbol} ({timeframe}). Chat ID: {chat_id}")
            return
        else:
            top20_pairs = await get_top_20_binance_pairs()
            if not top20_pairs:
                await update.message.reply_text('❌ Top 20 coinler alınamadı! Lütfen daha sonra tekrar deneyin.')
                logging.error(f"Top 20 coinler alınamadı. Rastgele sinyal isteği başarısız. Chat ID: {chat_id}")
                return
            tried_coins = set()
            while True:
                available_coins = [c for c in top20_pairs if c not in tried_coins]
                if not available_coins:
                    timeframe = random.choice(VALID_TIMEFRAMES)
                    tried_coins.clear()
                    available_coins = [c for c in top20_pairs if c not in tried_coins]
                    if not available_coins:
                        await update.message.reply_text(
                            '❌ Hiçbir coin için sinyal üretilemedi. Lütfen daha sonra tekrar deneyin.')
                        logging.warning(
                            f"Tüm Top 20 coinler ve zaman dilimleri denendi, sinyal üretilemedi. Chat ID: {chat_id}")
                        return
                symbol = random.choice(available_coins)
                tried_coins.add(symbol)
                logging.info(f"Rastgele sinyal denemesi: {symbol} ({timeframe}). Chat ID: {chat_id}")
                df = await get_price_data(symbol, timeframe)
                message = await generate_signal(df, symbol, timeframe)
                if message:
                    await update.message.reply_text(message)
                    signal_logger.info(message)  # Sinyal mesajını signals.log'a kaydet
                    logging.info(f"Rastgele sinyal gönderildi: {symbol} ({timeframe}). Chat ID: {chat_id}")
                    return
    except Exception as e:
        logging.critical(f"Sinyal komutu işlenirken kritik hata: {e}", exc_info=True)
        await update.message.reply_text('❌ Sinyal oluşturulurken beklenmeyen bir hata oluştu. Lütfen tekrar deneyin.')


async def active_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Aktif (yetkili) kullanıcıları listeler."""
    chat_id = update.message.chat_id
    user = check_chat_id(chat_id)
    if not user or user[0] not in ['yetkiliadmin', 'vipkullanici']:
        await update.message.reply_text('🚫 Bu komutu sadece Yönetici veya VIP kullanıcılar çalıştırabilir!')
        logging.warning(f"Yetkisiz activeusers komut denemesi. Chat ID: {chat_id}")
        return
    users = get_authorized_users()
    if not users:
        await update.message.reply_text('📋 Aktif kullanıcı bulunamadı.')
        logging.info(f"Aktif kullanıcı bulunamadı. Chat ID: {chat_id}")
        return
    message = '📋 Aktif Kullanıcılar:\n\n'
    for user_id, chat_id_val in users:
        message += f'👤 Kullanıcı ID: {user_id}, Chat ID: {chat_id_val}\n'
    await update.message.reply_text(message)
    logging.info(f"Aktif kullanıcı listesi gönderildi. Chat ID: {chat_id}")


async def bilgi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bot hakkında detaylı bilgi verir."""
    bilgi_text = (
        'ℹ️ Finetic Trade Hakkında Detaylı Bilgi\n\n'
        'Finetic Trade, Binance spot piyasasındaki USDT pariteleri için teknik analiz ve price action tabanlı otomatik sinyal üreten bir Telegram botudur.\n\n'
        'Özellikler:\n'
        '- Gerçek zamanlı teknik analiz (RSI, MACD, Bollinger, EMA Cross, vb.) ve Price Action\n'
        '- ATR ve % bazlı sinyaller\n'
        '- Kullanıcıya özel yetkilendirme ve güvenlik\n'
        '- Gelişmiş /help ve /bilgi menüleri\n'
        'Kullanım için örnekler:\n'
        '- /start <ID>\n'
        '- /sinyal BTC 1\n'
        '- /sinyal\n'
        '- /Top30\n'
        '- /activeusers\n'
        'Her türlü soru ve destek için: @finetictradee veya finetictrade@gmail.com\n'
        'Gizlilik: Kullanıcı verileriniz üçüncü kişilerle paylaşılmaz.'
    )
    await update.message.reply_text(bilgi_text)
    logging.info(f"Bilgi komutu kullanıldı. Chat ID: {update.message.chat_id}")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Botun komutlarını ve açıklamalarını gösterir."""
    help_text = (
        "🤖 Komutlar ve Açıklamaları:\n\n"
        "/sinyal [COIN] [1|2|4|6] - Rasgele veya saatlik sinyaller alırsın. Örnek: /sinyal BTC 1 veya /sinyal\n"
        "/help - Bu yardım menüsünü gösterir.\n"
        "/bilgi - Botun detaylı açıklaması ve kullanım rehberi.\n"
        "/exit - Hesabınızın yetkisini kaldırır.\n"
        "/Top30 - CoinMarketCap'ten en iyi 30 coini listeler.\n"
        "/activeusers - Aktif kullanıcıları listeler (Yönetici/VIP).\n"
        "/tumcikis - Tüm kullanıcıların yetkisini kaldırır (Yönetici/VIP).\n"
        "/kullanicicikis <user_id> - Belirli bir kullanıcının yetkisini kaldırır (Yönetici/VIP).\n"
    )
    await update.message.reply_text(help_text)
    logging.info(f"Help komutu kullanıldı. Chat ID: {update.message.chat_id}")


async def exit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kullanıcının kendi hesabının yetkisini kaldırır."""
    chat_id = update.message.chat_id
    result = check_chat_id(chat_id)
    if not result:
        await update.message.reply_text(
            '🚫 Yetkisiz erişim! Önce /start <ID> ile yetki almalısınız.')
        logging.warning(f"Yetkisiz exit komutu denemesi. Chat ID: {chat_id}")
        return
    exit_user(chat_id)
    await update.message.reply_text(
        '✅ Başarıyla çıkış yaptınız. Tekrar giriş için /start <ID> kullanabilirsiniz.')
    logging.info(f"Kullanıcı kendi hesabından çıkış yaptı. Chat ID: {chat_id}")


async def top30(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """CoinMarketCap'ten en iyi 30 coini listeler."""
    chat_id = update.message.chat_id
    if not check_chat_id(chat_id):
        await update.message.reply_text('🚫 Yetkisiz erişim! Lütfen /start <ID> ile yetki alın.')
        logging.warning(f"Yetkisiz Top30 komut denemesi. Chat ID: {chat_id}")
        return
    message = await get_top_30_coins()
    await update.message.reply_text(message)
    logging.info(f"Top30 komutu kullanıldı. Chat ID: {chat_id}")


async def tum_cikis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tüm kullanıcıların yetkisini kaldırır (sadece Yönetici/VIP)."""
    chat_id = update.message.chat_id
    user = check_chat_id(chat_id)
    if not user or user[0] not in ['yetkiliadmin', 'vipkullanici']:
        await update.message.reply_text('🚫 Bu komutu sadece Yönetici veya VIP kullanıcılar çalıştırabilir!')
        logging.warning(f"Yetkisiz tum_cikis komut denemesi. Chat ID: {chat_id}")
        return
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute('UPDATE users SET chat_id = NULL')
            conn.commit()
            await update.message.reply_text('✅ Tüm kullanıcılar çıkış yaptı.')
            logging.info(f'Tüm kullanıcılar çıkış yaptı. Komutu kullanan Chat ID: {chat_id}')
    except Exception as e:
        logging.error(f"Tüm kullanıcılar çıkış yaparken hata: {e}", exc_info=True)
        await update.message.reply_text('❌ Tüm kullanıcılar çıkış yaparken bir hata oluştu.')


async def kullanici_cikis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Belirli bir kullanıcının yetkisini kaldırır (sadece Yönetici/VIP)."""
    chat_id = update.message.chat_id
    user = check_chat_id(chat_id)
    if not user or user[0] not in ['yetkiliadmin', 'vipkullanici']:
        await update.message.reply_text('🚫 Bu komutu sadece Yönetici veya VIP kullanıcılar çalıştırabilir!')
        logging.warning(f"Yetkisiz kullanici_cikis komut denemesi. Chat ID: {chat_id}")
        return
    args = context.args
    if not args:
        await update.message.reply_text('Kullanım: /kullanicicikis <user_id>')
        logging.warning(f"Kullanım hatası: user_id belirtilmedi. Chat ID: {chat_id}")
        return
    target_user_id = args[0]
    if not check_user(target_user_id):
        await update.message.reply_text(f'❌ Kullanıcı ID {target_user_id} bulunamadı!')
        logging.warning(f"Kullanici_cikis: Hedef kullanıcı ({target_user_id}) bulunamadı. Chat ID: {chat_id}")
        return
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute('UPDATE users SET chat_id = NULL WHERE user_id = ?', (target_user_id,))
            conn.commit()
            await update.message.reply_text(f'✅ {target_user_id} kullanıcısı çıkış yaptı.')
            logging.info(f'Kullanıcı çıkış yaptı: {target_user_id}. Komutu kullanan Chat ID: {chat_id}')
    except Exception as e:
        logging.error(f"Kullanıcı ({target_user_id}) çıkış yaparken hata: {e}", exc_info=True)
        await update.message.reply_text(f'❌ {target_user_id} kullanıcısı çıkış yaparken bir hata oluştu.')


async def shutdown(app, binance):
    """Botu ve Binance bağlantısını düzgünce kapatır."""
    logging.info("Bot kapatma işlemi başlatılıyor...")
    if app:
        try:
            # await app.stop() # Bu satır gereksiz olabilir ve hataya yol açabilir
            await app.shutdown()
            print("Telegram botu kapatıldı.")
        except Exception as e:
            logging.error(f'Telegram botu kapatma sırasında hata: {e}', exc_info=True)
    if binance:
        try:
            await binance.close()
            print("Binance bağlantısı kapatıldı.")
        except Exception as e:
            logging.error(f'Binance bağlantısı kapatma sırasında hata: {e}', exc_info=True)


def main():
    """Botun ana çalışma fonksiyonu."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = None
    try:
        init_db()
        loop.run_until_complete(test_binance_connection())
        app = Application.builder().token(TELEGRAM_TOKEN).build()
        app.add_handler(CommandHandler('start', start))
        app.add_handler(CommandHandler('sinyal', sinyal))
        app.add_handler(CommandHandler('exit', exit))
        app.add_handler(CommandHandler('Top30', top30))
        app.add_handler(CommandHandler('bilgi', bilgi))
        app.add_handler(CommandHandler('help', help_command))
        app.add_handler(CommandHandler('tumcikis', tum_cikis))
        app.add_handler(CommandHandler('kullanicicikis', kullanici_cikis))
        app.add_handler(CommandHandler('activeusers', active_users))

        # YENİ: Arka plan görevlerini başlat
        loop.create_task(send_periodic_signals(app))
        loop.create_task(monitor_active_signals(app))

        print('Bot başlatılıyor...')
        loop.run_until_complete(app.run_polling(allowed_updates=Update.ALL_TYPES))
    except KeyboardInterrupt:
        print('\nBot durduruluyor...')
    except Exception as e:
        logging.critical(f'Bot ana döngüsünde beklenmeyen kritik hata: {e}', exc_info=True)
        traceback.print_exc()
    finally:
        if app:
            loop.run_until_complete(shutdown(app, BINANCE))

        logging.info("Olay döngüsündeki bekleyen görevler iptal ediliyor...")
        tasks = asyncio.all_tasks(loop=loop)
        for task in tasks:
            task.cancel()

        # Görevlerin iptal edilmesini bekle
        group = asyncio.gather(*tasks, return_exceptions=True)
        try:
            loop.run_until_complete(group)
            loop.run_until_complete(loop.shutdown_asyncgens())
        except asyncio.CancelledError:
            pass  # Beklenen bir durum
        finally:
            loop.close()
            logging.info("Olay döngüsü kapatıldı. Bot tamamen durduruldu.")


if __name__ == '__main__':
    main()