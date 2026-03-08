import os
import json
import time
import logging
import asyncio
import concurrent.futures
from datetime import datetime
from flask import Flask, request
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, ConversationHandler
from pybit.unified_trading import HTTP

# Пробуем загрузить dotenv
try:
    from dotenv import load_dotenv
    load_dotenv()
    print("✅ .env файл загружен")
except ImportError:
    print("⚠️ python-dotenv не установлен")

# --- НАСТРОЙКА ЛОГИРОВАНИЯ ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- КОНФИГУРАЦИЯ ---
TELEGRAM_TOKEN = (os.getenv('TELEGRAM_BOT_TOKEN') or 
                  os.getenv('BOT_TOKEN') or 
                  os.getenv('API_TOKEN') or
                  os.getenv('TOKEN'))

BYBIT_API_KEY = os.getenv('BYBIT_API_KEY')
BYBIT_API_SECRET = os.getenv('BYBIT_API_SECRET')
ALLOWED_USER = 'bosdima'
DCA_FILE = 'dca_data.json'
SYMBOL = 'TONUSDT'

# Проверка
if not TELEGRAM_TOKEN:
    logger.error("Telegram токен не найден!")
    exit(1)

logger.info(f"Telegram токен: {TELEGRAM_TOKEN[:20]}...")

# --- ИНИЦИАЛИЗАЦИЯ ---
session = None
if BYBIT_API_KEY and BYBIT_API_SECRET:
    try:
        session = HTTP(
            testnet=False,
            api_key=BYBIT_API_KEY,
            api_secret=BYBIT_API_SECRET,
        )
        logger.info(f"Bybit инициализирован, ключ: {BYBIT_API_KEY[:15]}...")
    except Exception as e:
        logger.error(f"Ошибка Bybit: {e}")
else:
    logger.error("Bybit ключи не найдены!")

application = Application.builder().token(TELEGRAM_TOKEN).build()

# --- DCA ДАННЫЕ ---
dca_data = {
    'start_time': None,
    'total_quantity': 0.0,
    'total_cost': 0.0,
    'avg_price': 0.0,
    'trades': []
}

WAITING_FOR_AMOUNT = 1

# --- ФУНКЦИИ ---
def load_dca_data():
    global dca_data
    try:
        if os.path.exists(DCA_FILE):
            with open(DCA_FILE, 'r', encoding='utf-8') as f:
                dca_data.update(json.load(f))
                logger.info(f"DCA загружено: {dca_data['total_quantity']:.4f} TON")
    except Exception as e:
        logger.error(f"Ошибка загрузки DCA: {e}")

def save_dca_data():
    try:
        with open(DCA_FILE, 'w', encoding='utf-8') as f:
            json.dump(dca_data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Ошибка сохранения DCA: {e}")

def check_api_keys():
    if not session:
        return False, "❌ Bybit не инициализирован"
    try:
        response = session.get_wallet_balance(accountType="UNIFIED")
        if response['retCode'] == 0:
            return True, "✅ Bybit API работает"
        return False, f"❌ Bybit: {response['retMsg']}"
    except Exception as e:
        return False, f"❌ Bybit: {str(e)}"

def get_current_price(symbol=SYMBOL):
    if not session:
        return None
    try:
        response = session.get_tickers(category="spot", symbol=symbol)
        if response['retCode'] == 0 and response['result']['list']:
            return float(response['result']['list'][0].get('lastPrice', 0))
        return None
    except Exception as e:
        logger.error(f"Ошибка цены: {e}")
        return None

def get_detailed_portfolio():
    if not session:
        return [], 0, 0, 0, 0
    try:
        response = session.get_wallet_balance(accountType="UNIFIED")
        if response['retCode'] != 0:
            return [], 0, 0, 0, 0
        
        portfolio = []
        total_value = 0
        
        for coin_data in response['result']['list'][0].get('coin', []):
            coin = coin_data.get('coin', '')
            try:
                balance = float(coin_data.get('walletBalance', 0) or 0)
            except:
                balance = 0
            
            if balance <= 0:
                continue
            
            if coin == 'USDT':
                total_value += balance
                portfolio.append({
                    'coin': 'USDT', 'balance': balance,
                    'current_price': 1, 'current_value': balance, 'pnl': 0
                })
                continue
            
            price = get_current_price(f"{coin}USDT")
            if price:
                value = balance * price
                total_value += value
                portfolio.append({
                    'coin': coin, 'balance': balance,
                    'current_price': price, 'current_value': value, 'pnl': 0
                })
        
        portfolio.sort(key=lambda x: x['current_value'], reverse=True)
        return portfolio, total_value, 0, 0, 0
    except Exception as e:
        logger.error(f"Ошибка портфеля: {e}")
        return [], 0, 0, 0, 0

def buy_ton(usdt_amount):
    if not session:
        return None, "Bybit не инициализирован"
    try:
        current_price = get_current_price()
        if not current_price:
            return None, "Нет цены"
        
        qty = round(usdt_amount / current_price, 3)
        if qty < 0.1:
            return None, f"Минимум 0.1 TON, нужно {qty}"
        
        # Проверка баланса
        portfolio, _, _, _, _ = get_detailed_portfolio()
        usdt_balance = next((p['balance'] for p in portfolio if p['coin'] == 'USDT'), 0)
        if usdt_balance < usdt_amount:
            return None, f"Баланс: {usdt_balance:.2f} USDT, нужно: {usdt_amount}"
        
        order = session.place_order(
            category="spot", symbol=SYMBOL, side="Buy",
            orderType="Market", qty=str(qty), timeInForce="GTC"
        )
        
        if order['retCode'] != 0:
            return None, f"Ошибка биржи: {order['retMsg']}"
        
        return {
            'quantity': qty, 'price': current_price,
            'cost': usdt_amount, 'order_id': order['result']['orderId']
        }, None
    except Exception as e:
        return None, f"Ошибка: {str(e)}"

def check_user(username: str) -> bool:
    return username == ALLOWED_USER

# --- ОБРАБОТЧИКИ ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"User: @{user.username}")
    
    if not check_user(user.username):
        await update.message.reply_text(f"❌ Нет доступа. Ваш username: @{user.username}")
        return
    
    bybit_ok, bybit_msg = check_api_keys()
    
    keyboard = [["💰 Портфель", "📊 DCA"], ["🛒 Купить"]]
    markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        f"👋 Привет, {user.first_name}!\n{bybit_msg}\n\nВыберите действие:",
        reply_markup=markup
    )

async def show_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_user(update.effective_user.username):
        return
    
    msg = await update.message.reply_text("🔄 Загрузка...")
    
    with concurrent.futures.ThreadPoolExecutor() as executor:
        portfolio, total, _, _, _ = executor.submit(get_detailed_portfolio).result()
    
    if not portfolio:
        await msg.edit_text("📭 Портфель пуст")
        return
    
    text = f"💰 **Портфель: {total:.2f} USDT**\n\n"
    for p in portfolio:
        if p['coin'] == 'USDT':
            text += f"💵 USDT: {p['balance']:.2f}\n"
        else:
            text += f"💎 {p['coin']}: {p['balance']:.4f} = {p['current_value']:.2f} USDT\n"
    
    if dca_data['total_quantity'] > 0:
        text += f"\n📊 DCA: {dca_data['total_quantity']:.4f} TON @ {dca_data['avg_price']:.4f}"
    
    await msg.edit_text(text, parse_mode='Markdown')

async def show_dca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_user(update.effective_user.username):
        return
    
    if not dca_data['total_quantity']:
        await update.message.reply_text("📊 DCA не начат")
        return
    
    price = get_current_price()
    value = dca_data['total_quantity'] * (price or 0)
    pnl = value - dca_data['total_cost']
    
    await update.message.reply_text(
        f"📊 **DCA**\n\n"
        f"TON: {dca_data['total_quantity']:.4f}\n"
        f"Средняя: {dca_data['avg_price']:.4f} USDT\n"
        f"Вложено: {dca_data['total_cost']:.2f}\n"
        f"Тек.цена: {price:.4f if price else 'N/A'}\n"
        f"Стоимость: {value:.2f} USDT\n"
        f"PnL: {pnl:+.2f} USDT",
        parse_mode='Markdown'
    )

async def buy_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_user(update.effective_user.username):
        return ConversationHandler.END
    
    price = get_current_price()
    await update.message.reply_text(
        f"🛒 **Покупка TON**\nЦена: {price:.4f if price else 'N/A'} USDT\n\nВведите сумму USDT:"
    )
    return WAITING_FOR_AMOUNT

async def buy_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text)
        if amount < 1:
            await update.message.reply_text("❌ Минимум 1 USDT")
            return WAITING_FOR_AMOUNT
        
        status = await update.message.reply_text("🔄 Покупка...")
        
        with concurrent.futures.ThreadPoolExecutor() as executor:
            result, error = executor.submit(buy_ton, amount).result()
        
        if error:
            await status.edit_text(f"❌ {error}")
            return ConversationHandler.END
        
        if not dca_data['start_time']:
            dca_data['start_time'] = time.time()
        
        dca_data['trades'].append({
            'timestamp': time.time(),
            'quantity': result['quantity'],
            'price': result['price'],
            'cost': result['cost']
        })
        dca_data['total_quantity'] += result['quantity']
        dca_data['total_cost'] += result['cost']
        dca_data['avg_price'] = dca_data['total_cost'] / dca_data['total_quantity']
        save_dca_data()
        
        await status.edit_text(
            f"✅ Куплено {result['quantity']:.4f} TON\n"
            f"Цена: {result['price']:.4f} USDT\n"
            f"Всего DCA: {dca_data['total_quantity']:.4f} TON",
            parse_mode='Markdown'
        )
        return ConversationHandler.END
        
    except ValueError:
        await update.message.reply_text("❌ Введите число")
        return WAITING_FOR_AMOUNT

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Отменено")
    return ConversationHandler.END

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "💰 Портфель":
        await show_portfolio(update, context)
    elif text == "📊 DCA":
        await show_dca(update, context)
    elif text == "🛒 Купить":
        return await buy_start(update, context)

# --- РЕГИСТРАЦИЯ ---
application.add_handler(CommandHandler("start", start))

conv = ConversationHandler(
    entry_points=[MessageHandler(filters.Regex("^🛒 Купить$"), buy_start)],
    states={WAITING_FOR_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, buy_process)]},
    fallbacks=[CommandHandler("cancel", cancel)]
)
application.add_handler(conv)

application.add_handler(MessageHandler(filters.Regex("^💰 Портфель$"), show_portfolio))
application.add_handler(MessageHandler(filters.Regex("^📊 DCA$"), show_dca))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))

# --- FLASK ---
app = Flask(__name__)

# Глобальная переменная для webhook URL (устанавливается при первом запросе)
webhook_url_set = False

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json(force=True)
        update = Update.de_json(data, application.bot)
        
        # Устанавливаем webhook при первом запросе от Telegram если еще не установлен
        global webhook_url_set
        if not webhook_url_set:
            try:
                host = request.headers.get('Host', '')
                if host and not host.startswith('127.0.0.1') and not host.startswith('localhost'):
                    # Пробуем определить URL из заголовков
                    proto = request.headers.get('X-Forwarded-Proto', 'https')
                    webhook_url = f"{proto}://{host}/webhook"
                    logger.info(f"Авто-определение webhook: {webhook_url}")
                    
                    asyncio.run(application.bot.set_webhook(url=webhook_url))
                    webhook_url_set = True
                    logger.info("✅ Webhook установлен автоматически!")
            except Exception as e:
                logger.warning(f"Не удалось авто-установить webhook: {e}")
        
        # Обработка обновления
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(application.process_update(update))
            else:
                loop.run_until_complete(application.process_update(update))
        except RuntimeError:
            asyncio.run(application.process_update(update))
        
        return 'OK', 200
        
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return 'Error', 500

@app.route('/')
def index():
    return "✅ DCA Bot работает!", 200

@app.route('/set_webhook', methods=['GET', 'POST'])
def set_webhook_manual():
    """Ручная установка webhook"""
    try:
        # Пробуем получить URL из запроса или параметров
        url = request.args.get('url') or request.form.get('url')
        
        if not url:
            # Пробуем определить автоматически
            host = request.headers.get('Host', '')
            if host:
                proto = request.headers.get('X-Forwarded-Proto', 'https')
                url = f"{proto}://{host}/webhook"
            else:
                return "❌ Не удалось определить URL. Передайте ?url=https://your-domain.com/webhook", 400
        
        # Устанавливаем webhook
        async def do_set():
            await application.bot.set_webhook(url=url)
        
        asyncio.run(do_set())
        global webhook_url_set
        webhook_url_set = True
        
        logger.info(f"✅ Webhook установлен вручную: {url}")
        return f"✅ Webhook установлен: {url}", 200
        
    except Exception as e:
        logger.error(f"Ошибка установки webhook: {e}")
        return f"❌ Ошибка: {str(e)}", 500

@app.route('/get_info')
def get_info():
    """Информация о сервере для отладки"""
    return {
        "headers": dict(request.headers),
        "host": request.host,
        "url": request.url,
        "remote_addr": request.remote_addr
    }, 200

# --- ЗАПУСК ---
if __name__ == '__main__':
    load_dca_data()
    
    logger.info("=" * 60)
    logger.info("ЗАПУСК БОТА")
    logger.info("=" * 60)
    
    # Показываем переменные
    for key in sorted(os.environ.keys()):
        if any(x in key.upper() for x in ['TOKEN', 'API', 'KEY', 'SECRET', 'URL', 'PORT']):
            val = os.environ[key]
            masked = val[:15] + "..." if len(val) > 20 and ('SECRET' in key or 'TOKEN' in key or 'KEY' in key) else val
            logger.info(f"  {key}: {masked}")
    
    # Проверяем Bybit
    if session:
        bybit_ok, bybit_msg = check_api_keys()
        logger.info(f"Bybit: {bybit_msg}")
    
    logger.info("=" * 60)
    logger.info("⚠️  Webhook будет установлен автоматически при первом запросе от Telegram")
    logger.info("   Или вручную через GET /set_webhook?url=https://your-domain.com/webhook")
    logger.info("=" * 60)
    
    port = int(os.environ.get('PORT', 8443))
    logger.info(f"Старт на порту {port}")
    app.run(host='0.0.0.0', port=port, threaded=True)