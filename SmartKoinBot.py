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
from dotenv import load_dotenv

# .env dosyasını yükle
load_dotenv()

# Windows için SelectorEventLoop
if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Ortam değişkenleri
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', '7818791938:AAEzKfKf83Lp5fdae2_PTkAw9Qo3_0bNRfw')
CMC_API_KEY = os.getenv('CMC_API_KEY', '')
DB_FILE = 'users.db'
SIGNAL_LOG_DB = 'signal_log.db'

# Binance ayarları
BINANCE = ccxt_async.binance({
    'apiKey': os.getenv('BINANCE_API_KEY', 'ba2AjFONSLAVd2c95WCgZZL23xOs6MYiWHW8r4E0d2AcLynQUDeBWVkULxDSkB3X'),
    'secret': os.getenv('BINANCE_API_SECRET', 'i8FYOJQ1fMoHP7Sbsf3VbuyjPRwrKoHaprXxl7n53ZalvvsV0M8C9Mp8bfTgiTov'),
    'enableRateLimit': True,
    'rateLimit': 1000,
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

# Global değişken: Sinyalin gönderildiği kullanıcıları takip etmek için
signal_chat_ids = {}  # signal_id -> [chat_ids]


def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id TEXT PRIMARY KEY, chat_id INTEGER, created_at TEXT, favorites TEXT)''')
    predefined_users = [
        ('yetkiliadmin', None),
        ('prokullanici', None),
        ('vipkullanici', None)
    ]
    for user_id, chat_id in predefined_users:
        c.execute('INSERT OR IGNORE INTO users (user_id, chat_id, created_at, favorites) VALUES (?, ?, ?, ?)',
                  (user_id, chat_id, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), ''))
    conn.commit()
    conn.close()

    # Sinyal log veritabanı
    conn = sqlite3.connect(SIGNAL_LOG_DB)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS signal_log
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, timeframe TEXT, signal_type TEXT,
                  price REAL, take_profit REAL, stop_loss REAL, timestamp TEXT, result TEXT)''')
    conn.commit()
    conn.close()


def check_user(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT chat_id FROM users WHERE user_id = ?', (user_id,))
    result = c.fetchone()
    conn.close()
    return result


def save_user(user_id, chat_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('UPDATE users SET chat_id = ?, created_at = ? WHERE user_id = ?',
              (chat_id, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), user_id))
    conn.commit()
    conn.close()


def check_chat_id(chat_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT user_id FROM users WHERE chat_id = ?', (chat_id,))
    result = c.fetchone()
    conn.close()
    return result


def exit_user(chat_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('UPDATE users SET chat_id = NULL WHERE chat_id = ?', (chat_id,))
    conn.commit()
    conn.close()


def get_favorites(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT favorites FROM users WHERE user_id = ?', (user_id,))
    result = c.fetchone()
    conn.close()
    return result[0].split(',') if result and result[0] else []


def add_favorite(user_id, coin):
    favorites = get_favorites(user_id)
    if coin not in favorites:
        favorites.append(coin)
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('UPDATE users SET favorites = ? WHERE user_id = ?', (','.join(favorites), user_id))
        conn.commit()
        conn.close()


def remove_favorite(user_id, coin):
    favorites = get_favorites(user_id)
    if coin in favorites:
        favorites.remove(coin)
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('UPDATE users SET favorites = ? WHERE user_id = ?', (','.join(favorites), user_id))
        conn.commit()
        conn.close()


def log_signal(symbol, timeframe, signal_type, price, take_profit, stop_loss, result=None):
    conn = sqlite3.connect(SIGNAL_LOG_DB)
    c = conn.cursor()
    c.execute(
        'INSERT INTO signal_log (symbol, timeframe, signal_type, price, take_profit, stop_loss, timestamp, result) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (symbol, timeframe, signal_type, price, take_profit, stop_loss, datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
         result))
    signal_id = c.lastrowid
    conn.commit()
    conn.close()
    return signal_id


def update_signal_result(signal_id, result):
    conn = sqlite3.connect(SIGNAL_LOG_DB)
    c = conn.cursor()
    c.execute('UPDATE signal_log SET result = ? WHERE id = ?', (result, signal_id))
    conn.commit()
    conn.close()


def get_signal_details(signal_id):
    conn = sqlite3.connect(SIGNAL_LOG_DB)
    c = conn.cursor()
    c.execute('SELECT symbol, signal_type, price, take_profit, stop_loss FROM signal_log WHERE id = ?', (signal_id,))
    result = c.fetchone()
    conn.close()
    return result


def get_pending_signals():
    conn = sqlite3.connect(SIGNAL_LOG_DB)
    c = conn.cursor()
    c.execute('SELECT id, symbol, signal_type, price, take_profit, stop_loss FROM signal_log WHERE result IS NULL')
    results = c.fetchall()
    conn.close()
    return results


def get_signal_performance():
    conn = sqlite3.connect(SIGNAL_LOG_DB)
    c = conn.cursor()
    two_days_ago = (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S')
    c.execute('SELECT result FROM signal_log WHERE timestamp >= ?', (two_days_ago,))
    results = c.fetchall()
    conn.close()

    total_trades = len(results)
    successful_trades = len([r for r in results if r[0] == 'successful'])
    failed_trades = len([r for r in results if r[0] == 'failed'])

    return total_trades, successful_trades, failed_trades


async def get_top_50_binance_pairs():
    global TICKERS_CACHE, TICKERS_CACHE_TIME
    now = datetime.now()
    if TICKERS_CACHE is None or TICKERS_CACHE_TIME is None or (
            now - TICKERS_CACHE_TIME).total_seconds() > TICKERS_CACHE_DURATION:
        try:
            TICKERS_CACHE = await BINANCE.fetch_tickers()
            TICKERS_CACHE_TIME = now
        except Exception as e:
            print(f'Binance top 50 çekilemedi: {e}')
            return []
    usdt_tickers = {symbol: data for symbol, data in TICKERS_CACHE.items() if symbol.endswith('/USDT')}
    stable_coins = ['USDC', 'TUSD', 'BUSD', 'DAI', 'FDUSD']
    usdt_tickers = {symbol: data for symbol, data in usdt_tickers.items()
                    if not any(symbol.startswith(stable + '/') for stable in stable_coins)}
    sorted_pairs = sorted(
        usdt_tickers.items(),
        key=lambda x: x[1].get('quoteVolume', 0),
        reverse=True
    )
    top50_pairs = [symbol for symbol, _ in sorted_pairs[:50]]
    return top50_pairs


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
            stable_coins = ['USDC', 'TUSD', 'BUSD', 'DAI', 'FDUSD']
            usdt_pairs = [pair for pair in usdt_pairs
                          if not any(pair.startswith(stable + '/') for stable in stable_coins)]
            return usdt_pairs
        except ccxt_async.RateLimitExceeded:
            if attempt < max_retries - 1:
                print(f'Rate limit hatası, {10 * (attempt + 1)} saniye bekleniyor...')
                await asyncio.sleep(10 * (attempt + 1))
                continue
            print('USDT çiftleri alınamadı: Rate limit aşıldı')
            return []
        except Exception as e:
            print(f'USDT çiftleri alınamadı: {e}')
            return []


async def get_price_data(symbol, timeframe='1h', limit=100):
    cache_key = f'{symbol}_{timeframe}'
    try:
        if cache_key in DATA_CACHE:
            cached_df, cached_time = DATA_CACHE[cache_key]
            if (datetime.now() - cached_time).total_seconds() < CACHE_DURATION:
                return cached_df

        await asyncio.sleep(0.5)
        ohlcv = await BINANCE.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        if len(df) < 50:
            return None
        DATA_CACHE[cache_key] = (df, datetime.now())
        return df
    except Exception as e:
        print(f'Veri alınamadı ({symbol}, {timeframe}): {e}')
        return None


async def get_current_price(symbol):
    try:
        ticker = await BINANCE.fetch_ticker(symbol)
        return ticker['last']
    except Exception as e:
        print(f'Fiyat alınamadı ({symbol}): {e}')
        return None


async def get_top_30_coins():
    url = 'https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest'
    headers = {'X-CMC_PRO_API_KEY': CMC_API_KEY} if CMC_API_KEY else {}
    params = {'start': '1', 'limit': '30', 'convert': 'USD'}

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers, params=params) as response:
                if response.status != 200:
                    return '❌ CoinMarketCap API hatası. Lütfen daha sonra tekrar deneyin.'
                data = await response.json()
                if 'data' not in data:
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
                return message
        except Exception as e:
            return f'❌ Hata: CoinMarketCap verisi alınamadı. {str(e)}'


# Teknik indikatörler
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


def detect_price_action_patterns(df):
    price_action_signals = {'buy': [], 'sell': []}

    for i in range(2, len(df)):
        body = abs(df['close'].iloc[i] - df['open'].iloc[i])
        upper_wick = df['high'].iloc[i] - max(df['open'].iloc[i], df['close'].iloc[i])
        lower_wick = min(df['open'].iloc[i], df['close'].iloc[i]) - df['low'].iloc[i]
        total_range = df['high'].iloc[i] - df['low'].iloc[i]

        if total_range > 0:
            if upper_wick > 2 * body and lower_wick < 0.3 * upper_wick:
                price_action_signals['sell'].append('Pin Bar')
            elif lower_wick > 2 * body and upper_wick < 0.3 * lower_wick:
                price_action_signals['buy'].append('Pin Bar')

    for i in range(1, len(df)):
        if (df['high'].iloc[i] < df['high'].iloc[i - 1] and
                df['low'].iloc[i] > df['low'].iloc[i - 1]):
            if df['close'].iloc[i] > df['open'].iloc[i]:
                price_action_signals['buy'].append('Inside Bar')
            else:
                price_action_signals['sell'].append('Inside Bar')

    for i in range(1, len(df)):
        if df['close'].iloc[i - 1] > df['open'].iloc[i - 1]:  # Önceki mum yükseliş
            if (df['close'].iloc[i] < df['open'].iloc[i] and
                    df['open'].iloc[i] > df['close'].iloc[i - 1] and
                    df['close'].iloc[i] < df['open'].iloc[i - 1]):
                price_action_signals['sell'].append('Bearish Engulfing')
        elif df['close'].iloc[i - 1] < df['open'].iloc[i - 1]:  # Önceki mum düşüş
            if (df['close'].iloc[i] > df['open'].iloc[i] and
                    df['open'].iloc[i] < df['close'].iloc[i - 1] and
                    df['close'].iloc[i] > df['open'].iloc[i - 1]):
                price_action_signals['buy'].append('Bullish Engulfing')

    return price_action_signals


def generate_signal(df, symbol, timeframe):
    if df is None or len(df) < 50 or not all(col in df.columns for col in ['open', 'high', 'low', 'close', 'volume']):
        return None

    df['rsi'] = calculate_rsi(df['close'])
    df['macd'], df['macd_signal'] = calculate_macd(df['close'])
    df['sma'], df['bb_upper'], df['bb_lower'] = calculate_bollinger_bands(df['close'])
    df['ema'] = calculate_ema(df['close'])
    df['stoch_k'], df['stoch_d'] = calculate_stochastic(df['high'], df['low'], df['close'])
    df['adx'], df['di_plus'], df['di_minus'] = calculate_adx(df['high'], df['low'], df['close'])
    df['cci'] = calculate_cci(df['high'], df['low'], df['close'])
    df['williams_r'] = calculate_williams_r(df['high'], df['low'], df['close'])
    df['mfi'] = calculate_mfi(df['high'], df['low'], df['close'], df['volume'])
    df['atr'] = calculate_atr(df)

    price_action_signals = detect_price_action_patterns(df)

    if df[['rsi', 'macd', 'bb_upper', 'bb_lower', 'atr']].iloc[-1].isna().any():
        return None

    latest_rsi = df['rsi'].iloc[-1]
    latest_macd = df['macd'].iloc[-1]
    latest_macd_signal = df['macd_signal'].iloc[-1]
    latest_price = df['close'].iloc[-1]
    latest_bb_upper = df['bb_upper'].iloc[-1]
    latest_bb_lower = df['bb_lower'].iloc[-1]
    latest_sma = df['sma'].iloc[-1]
    latest_ema = df['ema'].iloc[-1]
    latest_stoch_k = df['stoch_k'].iloc[-1]
    latest_stoch_d = df['stoch_d'].iloc[-1]
    latest_adx = df['adx'].iloc[-1]
    latest_di_plus = df['di_plus'].iloc[-1]
    latest_di_minus = df['di_minus'].iloc[-1]
    latest_cci = df['cci'].iloc[-1]
    latest_williams_r = df['williams_r'].iloc[-1]
    latest_mfi = df['mfi'].iloc[-1]
    latest_atr = df['atr'].iloc[-1]

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

    if latest_cci < -100:
        buy_signals.append('CCI')
    elif latest_cci > 100:
        sell_signals.append('CCI')

    if latest_williams_r < -80:
        buy_signals.append('Williams %R')
    elif latest_williams_r > -20:
        sell_signals.append('Williams %R')

    if latest_mfi < 20:
        buy_signals.append('MFI')
    elif latest_mfi > 80:
        sell_signals.append('MFI')

    buy_signals.extend(price_action_signals['buy'])
    sell_signals.extend(price_action_signals['sell'])

    buy_count = len(buy_signals)
    sell_count = len(sell_signals)

    if buy_count > sell_count and buy_count > 0:
        signal_type = 'Uzun'
        signal_emoji = '🟢'
        stop_loss = latest_price - (2 * latest_atr)
        take_profit = latest_price + (4 * latest_atr)
        rr = (take_profit - latest_price) / (latest_price - stop_loss) if latest_price != stop_loss else 0
    elif sell_count > buy_count and sell_count > 0:
        signal_type = 'Kısa'
        signal_emoji = '🔴'
        stop_loss = latest_price + (2 * latest_atr)
        take_profit = latest_price - (4 * latest_atr)
        rr = (latest_price - take_profit) / (stop_loss - latest_price) if stop_loss != latest_price else 0
    else:
        return None

    message = (
        f'{signal_emoji} {signal_type} Sinyal | #{symbol.replace("/", "")}\n'
        f'🕒 Zaman Dilimi: {timeframe}\n'
        f'💵 Fiyat: {latest_price:.7f} USDT\n'
        f'🎯 Kâr Al: {take_profit:.7f} USDT\n'
        f'🛡️ Zarar Durdur: {stop_loss:.7f} USDT\n'
        f'📊 Risk-Ödül Oranı: {rr:.2f}'
    )

    signal_id = log_signal(symbol, timeframe, signal_type, latest_price, take_profit, stop_loss)
    return message, signal_id


async def notify_signal_result(app, signal_id, result, current_price):
    if signal_id not in signal_chat_ids:
        return
    chat_ids = signal_chat_ids[signal_id]
    signal_details = get_signal_details(signal_id)
    if not signal_details:
        return
    symbol, signal_type, entry_price, take_profit, stop_loss = signal_details
    result_emoji = '✅' if result == 'successful' else '❌'
    target_reached = 'Kâr Al' if result == 'successful' else 'Zarar Durdur'
    message = (
        f'{result_emoji} Sinyal Sonucu | #{symbol.replace("/", "")}\n'
        f'📈 Tür: {signal_type}\n'
        f'💵 Giriş Fiyatı: {entry_price:.7f} USDT\n'
        f'🎯 {target_reached}: {current_price:.7f} USDT\n'
        f'🕒 Zaman: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'
    )
    for chat_id in chat_ids:
        try:
            await app.bot.send_message(chat_id=chat_id, text=message)
            print(f"Sonuç bildirimi gönderildi: {chat_id}, {symbol}, Sonuç: {result}")
        except Exception as e:
            print(f"Sonuç bildirimi gönderilemedi ({chat_id}): {e}")


async def monitor_signal(app, signal_id, symbol, signal_type, price, take_profit, stop_loss):
    while True:
        current_price = await get_current_price(symbol)
        if current_price is None:
            await asyncio.sleep(60)
            continue

        result = None
        if signal_type == 'Uzun':
            if current_price >= take_profit:
                result = 'successful'
            elif current_price <= stop_loss:
                result = 'failed'
        elif signal_type == 'Kısa':
            if current_price <= take_profit:
                result = 'successful'
            elif current_price >= stop_loss:
                result = 'failed'

        if result:
            update_signal_result(signal_id, result)
            print(f"Sinyal sonucu güncellendi: {signal_id}, {symbol}, Sonuç: {result}")
            await notify_signal_result(app, signal_id, result, current_price)
            # Bildirimden sonra chat_ids listesinden kaldır
            if signal_id in signal_chat_ids:
                del signal_chat_ids[signal_id]
            break

        await asyncio.sleep(60)  # Her 60 saniyede bir kontrol et


async def monitor_signals(app):
    while True:
        pending_signals = get_pending_signals()
        for signal_id, symbol, signal_type, price, take_profit, stop_loss in pending_signals:
            asyncio.create_task(monitor_signal(app, signal_id, symbol, signal_type, price, take_profit, stop_loss))
        await asyncio.sleep(300)  # Her 5 dakikada bir yeni sinyalleri kontrol et


async def test_binance_connection():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get('https://api.binance.com/api/v3/ping') as response:
                if response.status == 200:
                    print('Binance API bağlantısı başarılı!')
                    return True
                else:
                    print(f'Binance API bağlantısı başarısız: Status {response.status}')
                    return False
    except Exception as e:
        print(f'Bağlantı hatası: {e}')
        return False


async def send_periodic_signals(app: Application):
    print("Otomatik sinyal döngüsü başlatıldı.")
    while True:
        try:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Sinyal üretimi başlıyor...")
            top50_pairs = await get_top_50_binance_pairs()
            if not top50_pairs:
                print("Top 50 coin alınamadı.")
                await asyncio.sleep(7200)
                continue

            timeframe = random.choice(VALID_TIMEFRAMES)
            tried_coins = set()
            while True:
                available_coins = [coin for coin in top50_pairs if coin not in tried_coins]
                if not available_coins:
                    print("Tüm coin’ler denendi, 2 saat bekleniyor.")
                    break
                symbol = random.choice(available_coins)
                tried_coins.add(symbol)
                df = await get_price_data(symbol, timeframe)
                signal_result = generate_signal(df, symbol, timeframe)
                if signal_result is not None:
                    message, signal_id = signal_result
                    # Botun çalıştığı tüm chat’lere sinyal gönder
                    async for dialog in app.bot.get_updates(timeout=10):
                        chat_id = dialog.message.chat_id
                        signal_chat_ids[signal_id] = [chat_id]  # Her chat’e ayrı ayrı gönder
                        try:
                            await app.bot.send_message(chat_id=chat_id, text=message)
                            print(
                                f"Otomatik sinyal gönderildi: {chat_id}, {symbol}, Zaman: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                        except Exception as e:
                            print(f"Otomatik sinyal gönderilemedi ({chat_id}): {e}")
                    break
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 2 saat bekleniyor...")
            await asyncio.sleep(7200)  # 2 saat bekle
        except Exception as e:
            print(f"Otomatik sinyal hatası: {e}")
            traceback.print_exc()
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Hata sonrası 2 saat bekleniyor...")
            await asyncio.sleep(7200)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    print(f"Bot {chat_id} chat’inde başlatıldı.")
    await update.message.reply_text('🚀 Bot aktif! Otomatik sinyaller bu chat’e gönderilecek.')


async def sinyal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    print(f"Sinyal komutu {chat_id} chat’inde çalıştırıldı.")

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
                return

    usdt_pairs = await get_usdt_pairs()
    if not usdt_pairs:
        await update.message.reply_text('❌ USDT çiftleri alınamadı!')
        return

    top50_pairs = await get_top_50_binance_pairs()
    if not top50_pairs:
        await update.message.reply_text('❌ Binance top 50 coinler alınamadı!')
        return

    tried_coins = set()
    if coin:
        symbol = f'{coin}/USDT'
        if symbol not in usdt_pairs:
            symbol = None
        else:
            df = await get_price_data(symbol, timeframe)
            signal_result = generate_signal(df, symbol, timeframe)
            if signal_result is not None:
                message, signal_id = signal_result
                signal_chat_ids[signal_id] = [chat_id]
                await update.message.reply_text(message)
                return
            tried_coins.add(symbol)
    while True:
        available_coins = [coin for coin in top50_pairs if coin not in tried_coins]
        if not available_coins:
            await update.message.reply_text('❌ Tüm coin’ler denendi, sinyal üretilemedi!')
            return
        symbol = random.choice(available_coins)
        tried_coins.add(symbol)
        df = await get_price_data(symbol, timeframe)
        signal_result = generate_signal(df, symbol, timeframe)
        if signal_result is not None:
            message, signal_id = signal_result
            signal_chat_ids[signal_id] = [chat_id]
            await update.message.reply_text(message)
            return


async def favori(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    print(f"Favori komutu {chat_id} chat’inde çalıştırıldı.")

    args = context.args
    favorites = get_favorites('default')  # Basit bir favori listesi, kullanıcıya bağlı değil
    if args:
        action = args[0].lower()
        if action == 'ekle' and len(args) > 1:
            coin = args[1].upper()
            add_favorite('default', coin)
            await update.message.reply_text(f'✅ {coin} favorilere eklendi.')
            return
        elif action == 'sil' and len(args) > 1:
            coin = args[1].upper()
            remove_favorite('default', coin)
            await update.message.reply_text(f'❌ {coin} favorilerden silindi.')
            return
        else:
            await update.message.reply_text('Kullanım: /favori ekle BTC veya /favori sil ETH')
            return
    if not favorites:
        await update.message.reply_text('⭐ Henüz favori coin eklemediniz. /favori ekle BTC gibi ekleyebilirsiniz.')
        return
    await update.message.reply_text('⭐ Favori coinler: ' + ', '.join(favorites))


async def favorilerim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    print(f"Favorilerim komutu {chat_id} chat’inde çalıştırıldı.")

    favs = get_favorites('default')  # Basit bir favori listesi
    if favs:
        await update.message.reply_text('⭐ Favori coinler: ' + ', '.join(favs))
    else:
        await update.message.reply_text('⭐ Henüz favori coin eklemediniz.')


async def tum_cikis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    print(f"Tüm çıkış komutu {chat_id} chat’inde çalıştırıldı.")

    if str(chat_id) != '-1002664325731':  # Sadece yetkiliadmin grubundan çalışsın
        await update.message.reply_text('🚫 Bu komutu sadece yetkiliadmin çalıştırabilir!')
        return
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('UPDATE users SET chat_id = NULL')
    conn.commit()
    conn.close()
    await update.message.reply_text('✅ Tüm kullanıcılar çıkış yaptı.')
    print(f'Tüm kullanıcılar çıkış yaptı: {chat_id}')


async def kullanici_cikis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    print(f"Kullanıcı çıkış komutu {chat_id} chat’inde çalıştırıldı.")

    if str(chat_id) != '-1002664325731':  # Sadece yetkiliadmin grubundan çalışsın
        await update.message.reply_text('🚫 Bu komutu sadece yetkiliadmin çalıştırabilir!')
        return
    args = context.args
    if not args:
        await update.message.reply_text('Kullanım: /kullanicicikis <user_id>')
        return
    target_user_id = args[0]
    if not check_user(target_user_id):
        await update.message.reply_text(f'❌ Kullanıcı ID bulunamadı!')
        return
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('UPDATE users SET chat_id = NULL WHERE user_id = ?', (target_user_id,))
    conn.commit()
    conn.close()
    await update.message.reply_text(f'✅ {target_user_id} kullanıcısı çıkış yaptı.')
    print(f'Kullanıcı çıkış yaptı: {target_user_id}, {chat_id}')


async def bilgi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    print(f"Bilgi komutu {chat_id} chat’inde çalıştırıldı.")

    bilgi_text = (
        'ℹ️ *SmartKoinBot Hakkında Detaylı Bilgi*\n\n'
        'SmartKoinBot, Binance spot piyasasındaki USDT pariteleri için teknik analiz tabanlı otomatik sinyal üreten bir Telegram botudur.\n\n'
        '*Özellikler:*\n'
        '- Gerçek zamanlı teknik analiz (RSI, MACD, Bollinger, vb.)\n'
        '- Otomatik sinyal gönderimi (aktif chat’e)\n'
        '- Gelişmiş /help ve /bilgi menüleri\n'
        '- (Yakında) Sinyal geçmişi, premium sistem, alarm ve daha fazlası!\n\n'
        '*Kullanım için örnekler:*\n'
        '- /sinyal BTC 1\n'
        '- /sinyal\n'
        'Her türlü soru ve destek için: @finetictradee veya finetictrade@gmail.com\n'
        'Gizlilik: Kullanıcı verileriniz üçüncü kişilerle paylaşılmaz.'
    )
    await update.message.reply_text(bilgi_text, parse_mode='Markdown')


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    print(f"Help komutu {chat_id} chat’inde çalıştırıldı.")

    help_text = (
        '🤖 *Komutlar ve Açıklamaları:*\n\n'
        '/sinyal [COIN] [1|2|4|6] - Rasgele veya Saatlik sinyaller alırsın. (Örnek: /sinyal BTC 1 veya /sinyal)\n'
        '/help - Bu yardım menüsünü gösterir.\n'
        '/bilgi - Botun detaylı açıklaması ve kullanım rehberi.\n'
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')


async def exit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    print(f"Exit komutu {chat_id} chat’inde çalıştırıldı.")

    await update.message.reply_text('✅ Çıkış yapıldı. Bot bu chat’ten sinyal göndermeyi durduracak.')


async def top30(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    print(f"Top30 komutu {chat_id} chat’inde çalıştırıldı.")

    message = await get_top_30_coins()
    await update.message.reply_text(message)


async def shutdown(app, binance):
    if app:
        try:
            await app.stop()
            await app.shutdown()
            print("Telegram botu kapatıldı.")
        except Exception as e:
            print(f'Kapatma sırasında hata: {e}')
    try:
        await binance.close()
        print("Binance bağlantısı kapatıldı.")
    except Exception as e:
        print(f'Binance kapatma hatası: {e}')


def main():
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
        app.add_handler(CommandHandler('favori', favori))
        app.add_handler(CommandHandler('favorilerim', favorilerim))
        app.add_handler(CommandHandler('bilgi', bilgi))
        app.add_handler(CommandHandler('help', help_command))
        app.add_handler(CommandHandler('tumcikis', tum_cikis))
        app.add_handler(CommandHandler('kullanicicikis', kullanici_cikis))
        loop.create_task(send_periodic_signals(app))
        loop.create_task(monitor_signals(app))
        print('Bot başlatılıyor...')
        loop.run_until_complete(app.run_polling(allowed_updates=Update.ALL_TYPES))
    except KeyboardInterrupt:
        print('Bot manuel olarak durduruldu...')
        if app:
            loop.run_until_complete(shutdown(app, BINANCE))
    except Exception as e:
        print(f'Hata: {e}')
        traceback.print_exc()
    finally:
        if app:
            loop.run_until_complete(shutdown(app, BINANCE))
        if not loop.is_closed():
            pending = asyncio.all_tasks(loop)
            for task in pending:
                if not task.done():
                    task.cancel()
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()
        print("Olay döngüsü kapatıldı.")


if __name__ == '__main__':
    main()