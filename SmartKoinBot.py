# main.py - Fibonacci Analizi Tamamen Kaldırılmış Versiyon

import asyncio
import logging
import platform
import random
import sys
import os
import sqlite3
from datetime import datetime, timedelta

import aiohttp
import numpy as np
import pandas as pd
import ccxt.async_support as ccxt_async
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import BadRequest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz

# ==============================================================================
# 1. BAŞLANGIÇ AYARLARI VE KONFİGÜRASYON
# ==============================================================================

load_dotenv()

if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log", encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
BINANCE_API_KEY = os.getenv('BINANCE_API_KEY')
BINANCE_API_SECRET = os.getenv('BINANCE_API_SECRET')
CMC_API_KEY = os.getenv('CMC_API_KEY')
DB_FILE = 'users.db'
CACHE_DURATION = 3600
VALID_TIMEFRAMES = ['1h', '2h', '4h', '6h']
TICKERS_CACHE_DURATION = 120


# ==============================================================================
# 2. VERİTABANI FONKSİYONLARI
# ==============================================================================

def init_db():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute('CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY, chat_id INTEGER, created_at TEXT)')
            c.execute('''CREATE TABLE IF NOT EXISTS signals
                         (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, timeframe TEXT, signal_type TEXT, price REAL,
                          stop_loss REAL, take_profit REAL, atr REAL, percent_sl REAL, percent_tp REAL, created_at TEXT,
                          status TEXT DEFAULT 'active')''')
            predefined_users = [('yetkiliadmin', None), ('prokullanici', None), ('vipkullanici', None)]
            c.executemany('INSERT OR IGNORE INTO users (user_id, chat_id, created_at) VALUES (?, ?, ?)',
                          [(user, chat_id, datetime.now().strftime('%Y-%m-%d %H:%M:%S')) for user, chat_id in
                           predefined_users])
            conn.commit()
            logging.info("Veritabanı başlatma tamamlandı.")
    except Exception as e:
        logging.critical(f"Veritabanı başlatılırken kritik hata: {e}", exc_info=True)


def check_user(user_id):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            return conn.cursor().execute('SELECT chat_id FROM users WHERE user_id = ?', (user_id,)).fetchone()
    except Exception as e:
        logging.error(f"Kullanıcı kontrol hatası ({user_id}): {e}", exc_info=True)
        return None


def save_user(user_id, chat_id):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.cursor().execute('UPDATE users SET chat_id = ?, created_at = ? WHERE user_id = ?',
                                  (chat_id, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), user_id))
            conn.commit()
            logging.info(f"Kullanıcı {user_id} ({chat_id}) kaydedildi/güncellendi.")
    except Exception as e:
        logging.error(f"Kullanıcı kaydetme hatası ({user_id}): {e}", exc_info=True)


def check_chat_id(chat_id):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            return conn.cursor().execute('SELECT user_id FROM users WHERE chat_id = ?', (chat_id,)).fetchone()
    except Exception as e:
        logging.error(f"Chat ID kontrol hatası ({chat_id}): {e}", exc_info=True)
        return None


def exit_user(chat_id):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.cursor().execute('UPDATE users SET chat_id = NULL WHERE chat_id = ?', (chat_id,))
            conn.commit()
            logging.info(f"Kullanıcı {chat_id} çıkış yaptı.")
    except Exception as e:
        logging.error(f"Kullanıcı çıkış hatası ({chat_id}): {e}", exc_info=True)


def get_authorized_users():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            return conn.cursor().execute('SELECT user_id, chat_id FROM users WHERE chat_id IS NOT NULL').fetchall()
    except Exception as e:
        logging.error(f"Yetkili kullanıcıları alma hatası: {e}", exc_info=True)
        return []


def save_signal(symbol, timeframe, signal_type, price, stop_loss, take_profit, atr, percent_sl, percent_tp,
                status='active'):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.cursor().execute('''INSERT INTO signals (symbol, timeframe, signal_type, price, stop_loss, take_profit, atr, 
                                     percent_sl, percent_tp, created_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                                  (symbol, timeframe, signal_type, price, stop_loss, take_profit, atr,
                                   percent_sl, percent_tp, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), status))
            conn.commit()
            logging.info(f"Sinyal veritabanına kaydedildi: {symbol} - {signal_type}")
    except Exception as e:
        logging.error(f"Sinyal kaydetme hatası ({symbol}): {e}", exc_info=True)


def get_signal_stats():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            forty_eight_hours_ago = (datetime.now() - timedelta(hours=48)).strftime('%Y-%m-%d %H:%M:%S')
            c.execute("SELECT COUNT(*) FROM signals WHERE created_at >= ?", (forty_eight_hours_ago,))
            total = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM signals WHERE created_at >= ? AND status = 'TP_hit'",
                      (forty_eight_hours_ago,))
            tp_hits = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM signals WHERE created_at >= ? AND status = 'SL_hit'",
                      (forty_eight_hours_ago,))
            sl_hits = c.fetchone()[0]
            return total, tp_hits, sl_hits
    except Exception as e:
        logging.error(f"Sinyal istatistikleri alma hatası: {e}", exc_info=True)
        return 0, 0, 0


# ==============================================================================
# 3. YARDIMCI & API FONKSİYONLARI
# ==============================================================================

def setup_signal_logger():
    log_filename = f"sinyaller_{datetime.now().strftime('%Y_%m')}.log"
    signal_logger = logging.getLogger('SignalLogger')
    signal_logger.setLevel(logging.INFO)
    if signal_logger.hasHandlers():
        signal_logger.handlers.clear()
    handler = logging.FileHandler(log_filename, encoding='utf-8')
    formatter = logging.Formatter('%(asctime)s - %(message)s')
    handler.setFormatter(formatter)
    signal_logger.addHandler(handler)
    return signal_logger


def log_signal(message):
    logger = logging.getLogger('SignalLogger')
    clean_message = message.replace('*', '').replace('_', '').replace('`', '').replace('\\', '')
    logger.info("\n" + "-" * 50 + "\n" + clean_message + "\n" + "-" * 50)


BINANCE = ccxt_async.binance({
    'apiKey': BINANCE_API_KEY,
    'secret': BINANCE_API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'spot'},
})

MARKETS_CACHE, MARKETS_CACHE_TIME = None, None
TICKERS_CACHE, TICKERS_CACHE_TIME = None, None
DATA_CACHE = {}


async def get_top_20_binance_pairs():
    global TICKERS_CACHE, TICKERS_CACHE_TIME
    now = datetime.now()
    if TICKERS_CACHE is None or TICKERS_CACHE_TIME is None or (
            now - TICKERS_CACHE_TIME).total_seconds() > TICKERS_CACHE_DURATION:
        try:
            logging.info("Binance Top 20 coin verisi çekiliyor...")
            tickers = await BINANCE.fetch_tickers()
            TICKERS_CACHE, TICKERS_CACHE_TIME = tickers, now
        except Exception as e:
            logging.error(f'Binance Top 20 çekilirken hata: {e}')
            return []
    usdt_tickers = {s: d for s, d in TICKERS_CACHE.items() if s.endswith('/USDT')}
    stable_coins = ['USDC', 'TUSD', 'BUSD', 'DAI', 'FDUSD', 'TRX']
    usdt_tickers = {s: d for s, d in usdt_tickers.items() if not any(s.startswith(sc + '/') for sc in stable_coins)}
    volumes = [float(d.get('quoteVolume', 0) or 0) for d in usdt_tickers.values()]
    if not volumes: return []
    volume_threshold = max(np.percentile(volumes, 50) * 0.1, 10_000_000)
    high_volume_pairs = {s: d for s, d in usdt_tickers.items() if
                         float(d.get('quoteVolume', 0) or 0) > volume_threshold}
    sorted_pairs = sorted(high_volume_pairs.items(), key=lambda x: float(x[1].get('quoteVolume', 0) or 0), reverse=True)
    return [s for s, _ in sorted_pairs[:20]]


async def get_price_data(symbol, timeframe='1h', limit=200):
    cache_key = f'{symbol}_{timeframe}'
    if cache_key in DATA_CACHE and (datetime.now() - DATA_CACHE[cache_key][1]).total_seconds() < CACHE_DURATION:
        return DATA_CACHE[cache_key][0]
    try:
        logging.info(f"Fiyat verisi çekiliyor: {symbol} {timeframe}")
        await asyncio.sleep(0.5)
        ohlcv = await BINANCE.fetch_ohlcv(symbol, timeframe, limit=limit)
        if not ohlcv or len(ohlcv) < 100:
            logging.warning(f"Yetersiz OHLCV verisi: {symbol} {timeframe}")
            return None
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        DATA_CACHE[cache_key] = (df, datetime.now())
        return df
    except Exception as e:
        logging.error(f'Veri çekme hatası ({symbol}, {timeframe}): {e}')
        return None


async def close_binance_session():
    await BINANCE.close()


async def get_top_30_coins():
    if not CMC_API_KEY:
        return '❌ CoinMarketCap API anahtarı ayarlanmamış\\.'
    url = 'https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest'
    headers = {'X-CMC_PRO_API_KEY': CMC_API_KEY}
    params = {'start': '1', 'limit': '30', 'convert': 'USD'}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params) as response:
                if response.status != 200:
                    return '❌ CoinMarketCap API hatası\\.'
                data = await response.json()
                coins = data['data']
                message = '📊 *CoinMarketCap En İyi 30 Coin:*\n\n'
                for coin in coins:
                    price = coin['quote']['USD']['price']
                    change = coin['quote']['USD']['percent_change_24h']
                    message += (f"*{coin['cmc_rank']}\\. {coin['name']} ({coin['symbol']})*\n"
                                f"› Fiyat: `${price:,.4f}`\n"
                                f"› 24s Değişim: `{change:.2f}%`\n\n")
                return message
    except Exception as e:
        logging.error(f'CMC verisi alınırken hata: {e}')
        return '❌ CoinMarketCap verisi alınamadı\\.'


# ==============================================================================
# 4. TEKNİK ANALİZ VE SİNYAL ÜRETME
# ==============================================================================

def calculate_rsi(data, periods=14):
    delta = data.diff();
    gain = delta.where(delta > 0, 0).rolling(window=periods).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=periods).mean();
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def calculate_macd(data, fast=12, slow=26, signal=9):
    exp1 = data.ewm(span=fast, adjust=False).mean();
    exp2 = data.ewm(span=slow, adjust=False).mean()
    macd = exp1 - exp2;
    signal_line = macd.ewm(span=signal, adjust=False).mean()
    return macd, signal_line


def calculate_bollinger_bands(data, periods=20, std_dev=2):
    sma = data.rolling(window=periods).mean();
    std = data.rolling(window=periods).std()
    return sma, sma + (std * std_dev), sma - (std * std_dev)


def calculate_atr(df, periods=14):
    tr = np.maximum(df['high'] - df['low'],
                    np.maximum(abs(df['high'] - df['close'].shift()), abs(df['low'] - df['close'].shift())))
    return tr.rolling(window=periods).mean()


def calculate_price_action(df):
    if len(df) < 2: return None
    latest, prev = df.iloc[-1], df.iloc[-2]
    bullish_engulfing = (
                prev['close'] < prev['open'] and latest['close'] > latest['open'] and latest['close'] > prev['open'] and
                latest['open'] < prev['close'])
    bearish_engulfing = (
                prev['close'] > prev['open'] and latest['close'] < latest['open'] and latest['close'] < prev['open'] and
                latest['open'] > prev['close'])
    if bullish_engulfing: return 'bullish_engulfing'
    if bearish_engulfing: return 'bearish_engulfing'
    return None


# --- Fibonacci Fonskiyonu KALDIRILDI ---

async def generate_signal(df, symbol, timeframe):
    if df is None or len(df) < 50: return None
    try:
        df['rsi'] = calculate_rsi(df['close']);
        df['macd'], df['macd_signal'] = calculate_macd(df['close'])
        df['sma'], df['bb_upper'], df['bb_lower'] = calculate_bollinger_bands(df['close']);
        df['atr'] = calculate_atr(df)
        pa_signal = calculate_price_action(df);
        latest = df.iloc[-1]
        if df[['rsi', 'macd', 'bb_upper', 'atr']].iloc[-1].isna().any(): return None
        buy_score, sell_score = 0, 0
        if latest['rsi'] < 35: buy_score += 1
        if latest['rsi'] > 65: sell_score += 1
        if latest['macd'] > latest['macd_signal'] and df['macd'].iloc[-2] <= df['macd_signal'].iloc[-2]: buy_score += 1
        if latest['macd'] < latest['macd_signal'] and df['macd'].iloc[-2] >= df['macd_signal'].iloc[-2]: sell_score += 1
        if latest['close'] < latest['bb_lower']: buy_score += 1
        if latest['close'] > latest['bb_upper']: sell_score += 1
        if pa_signal == 'bullish_engulfing': buy_score += 2
        if pa_signal == 'bearish_engulfing': sell_score += 2
        signal_type = 'Uzun' if buy_score > sell_score and buy_score >= 2 else 'Kısa' if sell_score > buy_score and sell_score >= 2 else None
        if not signal_type: return None
        latest_price, atr_val = latest['close'], latest['atr']
        if signal_type == 'Uzun':
            stop_loss, take_profit, emoji = latest_price - (2 * atr_val), latest_price + (4 * atr_val), '🟢'
        else:
            stop_loss, take_profit, emoji = latest_price + (2 * atr_val), latest_price - (4 * atr_val), '🔴'
        rr = abs(take_profit - latest_price) / abs(latest_price - stop_loss) if abs(latest_price - stop_loss) > 0 else 0

        # --- Fibonacci ile ilgili mesaj oluşturma kısmı KALDIRILDI ---
        message = (
            f'{emoji} *{signal_type} Sinyal \\| #{symbol.replace("/", "")}*\n\n'
            f'🕒 *Zaman Dilimi:* `{timeframe}`\n'
            f'💵 *Giriş Fiyatı:* `{latest_price:.4f}`\n'
            f'🎯 *Kâr Al:* `{take_profit:.4f}`\n'
            f'🛡️ *Zarar Durdur:* `{stop_loss:.4f}`\n'
            f'📊 *Risk/Ödül Oranı:* `{rr:.2f}`'  # Mesajın sonundan Fibonacci bölümü çıkarıldı
        )
        save_signal(symbol, timeframe, signal_type, latest_price, stop_loss, take_profit, atr_val, 0, 0)
        log_signal(message)
        return message
    except Exception as e:
        logging.critical(f"Sinyal üretme hatası ({symbol}): {e}", exc_info=True)
        return None


# ==============================================================================
# 5. ZAMANLANMIŞ GÖREV VE TELEGRAM KOMUTLARI
# ==============================================================================

async def send_scheduled_signal(app: Application):
    """Zamanlayıcı tarafından tetiklendiğinde tek bir sinyal bulup gönderir."""
    logging.info("Zamanlanmış sinyal görevi tetiklendi.")
    try:
        authorized_users = get_authorized_users()
        if not authorized_users:
            logging.info("Yetkili kullanıcı yok, sinyal gönderilmiyor.")
            return

        top20_pairs = await get_top_20_binance_pairs()
        if not top20_pairs:
            logging.warning("Top 20 coin alınamadı, sinyal gönderilemiyor.")
            return

        timeframe = random.choice(VALID_TIMEFRAMES)
        random.shuffle(top20_pairs)

        for symbol in top20_pairs:
            df = await get_price_data(symbol, timeframe)
            if df is None: continue
            message = await generate_signal(df, symbol, timeframe)
            if message:
                logging.info(f"Zamanlanmış sinyal bulundu: {symbol}. Gönderiliyor...")
                for user_id, chat_id in authorized_users:
                    try:
                        await app.bot.send_message(chat_id=chat_id, text=message, parse_mode='MarkdownV2')
                    except BadRequest as e:
                        if "Chat not found" in str(e):
                            logging.warning(f"Kullanıcı botu engellemiş: {user_id}. Yetkisi kaldırılıyor.")
                            exit_user(chat_id)
                        else:
                            logging.error(f"Sinyal gönderim hatası (Kullanıcı: {user_id}): {e}")
                return

        logging.info("Bu zaman diliminde uygun sinyal bulunamadı.")

    except Exception as e:
        logging.critical(f"Zamanlanmış sinyal görevinde kritik hata: {e}", exc_info=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text('🚫 Yetkisiz erişim\\! `/start <ID>` şeklinde yetki alın\\.',
                                        parse_mode='MarkdownV2')
        return
    user_id = context.args[0]
    result = check_user(user_id)
    if result:
        save_user(user_id, update.message.chat_id)
        await update.message.reply_text(f'🚀 Yetki alındı\\! Kullanıcı ID: `{user_id}`', parse_mode='MarkdownV2')
    else:
        await update.message.reply_text('🚫 Geçersiz ID\\!', parse_mode='MarkdownV2')


async def sinyal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_chat_id(update.message.chat_id):
        await update.message.reply_text('🚫 Yetkiniz yok\\. `/start <ID>`', parse_mode='MarkdownV2')
        return
    await update.message.reply_text('🔎 Sinyal aranıyor, lütfen bekleyin\\.\\.\\.', parse_mode='MarkdownV2')
    coin = context.args[0].upper() if context.args else None
    timeframe = f'{context.args[1]}h' if len(context.args) > 1 and context.args[1] in ['1', '2', '4',
                                                                                       '6'] else random.choice(
        VALID_TIMEFRAMES)
    pairs_to_check = [f'{coin}/USDT'] if coin else await get_top_20_binance_pairs()
    if not pairs_to_check:
        await update.message.reply_text('❌ Coin listesi alınamadı\\.', parse_mode='MarkdownV2')
        return
    for symbol in pairs_to_check:
        df = await get_price_data(symbol, timeframe)
        if df is None: continue
        message = await generate_signal(df, symbol, timeframe)
        if message:
            await update.message.reply_text(message, parse_mode='MarkdownV2')
            return
    await update.message.reply_text(
        f'❌ `{coin or "Rastgele"}` için `{timeframe}` zaman diliminde uygun bir sinyal bulunamadı\\.',
        parse_mode='MarkdownV2')


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total, tp, sl = get_signal_stats()
    await update.message.reply_text(
        f'📊 *Son 48 Saat İstatistikleri:*\n\\- Toplam Sinyal: {total}\n\\- Başarılı \\(TP\\): {tp}\n\\- Başarısız \\(SL\\): {sl}',
        parse_mode='MarkdownV2')


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = ("🤖 *Komut Listesi:*\n\n"
                 "`/sinyal [COIN] [SAAT]` \\- Sinyal üretir\\. Ör: `/sinyal BTC 4` veya `/sinyal`\n"
                 "`/stats` \\- Son 48 saatlik sinyal istatistiklerini gösterir\\.\n"
                 "`/top30` \\- CoinMarketCap'ten en iyi 30 coini listeler\\.\n"
                 "`/exit` \\- Hesabınızın sinyal yetkisini kaldırır\\.\n")
    await update.message.reply_text(help_text, parse_mode='MarkdownV2')


async def top30_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = await get_top_30_coins()
    await update.message.reply_text(message, parse_mode='MarkdownV2')


async def exit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    exit_user(update.message.chat_id)
    await update.message.reply_text('✅ Başarıyla çıkış yaptınız\\.', parse_mode='MarkdownV2')


# ==============================================================================
# 6. ANA ÇALIŞTIRMA BLOGU
# ==============================================================================

async def main_async():
    if not TELEGRAM_TOKEN:
        logging.critical("TELEGRAM_TOKEN bulunamadı! Lütfen .env dosyasını kontrol edin.")
        return

    init_db()
    setup_signal_logger()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    turkey_tz = pytz.timezone('Europe/Istanbul')
    scheduler = AsyncIOScheduler(timezone=turkey_tz)
    scheduler.add_job(send_scheduled_signal, 'cron', hour=9, minute=30, args=[app])
    scheduler.add_job(send_scheduled_signal, 'cron', hour=14, minute=45, args=[app])
    scheduler.add_job(send_scheduled_signal, 'cron', hour=19, minute=15, args=[app])
    scheduler.add_job(send_scheduled_signal, 'cron', hour=23, minute=15, args=[app])
    scheduler.start()
    logging.info(f"Sinyal zamanlayıcı kuruldu. Sinyal saatleri: 09:30, 14:45, 19:15, 23:15 (Türkiye Saati)")

    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler(['sinyal', 'signal'], sinyal_command))
    app.add_handler(CommandHandler(['stats', 'istatistik'], stats_command))
    app.add_handler(CommandHandler(['help', 'yardim'], help_command))
    app.add_handler(CommandHandler('top30', top30_command))
    app.add_handler(CommandHandler('exit', exit_command))

    try:
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        logging.info("Bot başlatıldı ve çalışıyor. Durdurmak için Ctrl+C'ye basın.")
        print("Bot başlatıldı ve çalışıyor. Durdurmak için Ctrl+C'ye basın.")
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        logging.info("Durdurma sinyali (Ctrl+C) algılandı.")
    finally:
        logging.info("Bot ve bağlantılar kapatılıyor...")
        scheduler.shutdown()
        if app.updater and app.updater.is_running:
            await app.updater.stop()
        if app.is_running:
            await app.stop()
        await app.shutdown()
        await close_binance_session()
        logging.info("Tüm işlemler düzgünce kapatıldı.")
        print("Bot kapatıldı.")


if __name__ == '__main__':
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logging.info("Program kullanıcı tarafından sonlandırıldı.")