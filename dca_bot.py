import os
import json
import time
import logging
import asyncio
from datetime import datetime
from flask import Flask, request
from telegram import Update, Bot, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, ConversationHandler
from pybit.unified_trading import HTTP
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()

# --- НАСТРОЙКА ЛОГИРОВАНИЯ ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- КОНФИГУРАЦИЯ ---
# Bothost использует TELEGRAM_BOT_TOKEN, проверяем оба варианта
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN') or os.getenv('TELEGRAM_TOKEN')
BYBIT_API_KEY = os.getenv('BYBIT_API_KEY')
BYBIT_API_SECRET = os.getenv('BYBIT_API_SECRET')
ALLOWED_USER = 'bosdima'
DCA_FILE = 'dca_data.json'
SYMBOL = 'TONUSDT'

# Проверка токена
if not TELEGRAM_TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN не найден в переменных окружения!")
    logger.error(f"Доступные переменные: {[k for k in os.environ.keys() if 'TOKEN' in k or 'TELEGRAM' in k]}")
    exit(1)

logger.info(f"Токен найден: {TELEGRAM_TOKEN[:10]}...")

# --- ИНИЦИАЛИЗАЦИЯ ---
session = HTTP(
    testnet=False,
    api_key=BYBIT_API_KEY,
    api_secret=BYBIT_API_SECRET,
)

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
    except Exception as e:
        logger.error(f"Ошибка загрузки DCA: {e}")

def save_dca_data():
    try:
        with open(DCA_FILE, 'w', encoding='utf-8') as f:
            json.dump(dca_data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Ошибка сохранения DCA: {e}")

def check_api_keys():
    try:
        response = session.get_wallet_balance(accountType="UNIFIED")
        if response['retCode'] == 0:
            return True, "✅ Bybit API OK"
        return False, f"❌ Bybit ошибка: {response['retMsg']}"
    except Exception as e:
        return False, f"❌ Bybit: {str(e)}"

def get_current_price(symbol=SYMBOL):
    try:
        response = session.get_tickers(category="spot", symbol=symbol)
        if response['retCode'] == 0 and response['result']['list']:
            return float(response['result']['list'][0].get('lastPrice', 0))
        return None
    except Exception as e:
        logger.error(f"Ошибка цены: {e}")
        return None

def get_detailed_portfolio():
    try:
        response = session.get_wallet_balance(accountType="UNIFIED")
        if response['retCode'] != 0:
            return [], 0, 0, 0, 0
        
        portfolio = []
        total_value = total_pnl = total_invested = 0
        
        for coin_data in response['result']['list'][0].get('coin', []):
            coin = coin_data.get('coin', '')
            balance = float(coin_data.get('walletBalance', 0) or 0)
            
            if balance <= 0:
                continue
                
            if coin == 'USDT':
                total_value += balance
                portfolio.append({
                    'coin': 'USDT', 'balance': balance, 'current_price': 1,
                    'current_value': balance, 'pnl': 0
                })
                continue
            
            price = get_current_price(f"{coin}USDT")
            if not price:
                continue
                
            value = balance * price
            total_value += value
            
            portfolio.append({
                'coin': coin, 'balance': balance, 'current_price': price,
                'current_value': value, 'pnl': 0
            })
        
        portfolio.sort(key=lambda x: x['current_value'], reverse=True)
        return portfolio, total_value, total_pnl, 0, total_invested
    except Exception as e:
        logger.error(f"Ошибка портфеля: {e}")
        return [], 0, 0, 0, 0

def buy_ton(usdt_amount):
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
            return None, f"Ошибка: {order['retMsg']}"
        
        return {
            'quantity': qty, 'price': current_price,
            'cost': usdt_amount, 'order_id': order['result']['orderId']
        }, None
        
    except Exception as e:
        return None, str(e)

def check_user(username: str) -> bool:
    return username == ALLOWED_USER

# --- ОБРАБОТЧИКИ ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not check_user(user.username):
        await update.message.reply_text("❌ Нет доступа")
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
    
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor() as executor:
        future = executor.submit(get_detailed_portfolio)
        portfolio, total, _, _, _ = future.result()
    
    if not portfolio:
        await msg.edit_text("📭 Портфель пуст")
        return
    
    text = f"💰 **Портфель: {total:.2f} USDT**\n\n"
    for p in portfolio:
        if p['coin'] == 'USDT':
            text += f"💵 USDT: {p['balance']:.2f}\n"
        else:
            text += f"💎 {p['coin']}: {p['balance']:.4f} × {p['current_price']:.4f} = {p['current_value']:.2f} USDT\n"
    
    if dca_data['total_quantity'] > 0:
        text += f"\n📊 DCA: {dca_data['total_quantity']:.4f} TON по {dca_data['avg_price']:.4f}"
    
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
        f"📊 **DCA Статистика**\n\n"
        f"Количество: {dca_data['total_quantity']:.4f} TON\n"
        f"Средняя цена: {dca_data['avg_price']:.4f} USDT\n"
        f"Вложено: {dca_data['total_cost']:.2f} USDT\n"
        f"Текущая цена: {price:.4f if price else 'N/A'} USDT\n"
        f"Стоимость: {value:.2f} USDT\n"
        f"PnL: {pnl:+.2f} USDT",
        parse_mode='Markdown'
    )

async def buy_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_user(update.effective_user.username):
        return ConversationHandler.END
    
    price = get_current_price()
    await update.message.reply_text(
        f"🛒 **Покупка TON**\n"
        f"Текущая цена: {price:.4f if price else 'N/A'} USDT\n\n"
        f"Введите сумму в USDT:"
    )
    return WAITING_FOR_AMOUNT

async def buy_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text)
        if amount < 1:
            await update.message.reply_text("❌ Минимум 1 USDT")
            return WAITING_FOR_AMOUNT
        
        status = await update.message.reply_text("🔄 Покупка...")
        
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as executor:
            result, error = executor.submit(buy_ton, amount).result()
        
        if error:
            await status.edit_text(f"❌ {error}")
            return ConversationHandler.END
        
        # Обновляем DCA
        if not dca_data['start_time']:
            dca_data['start_time'] = time.time()
        
        dca_data['trades'].append({
            'timestamp': time.time(), 'quantity': result['quantity'],
            'price': result['price'], 'cost': result['cost']
        })
        dca_data['total_quantity'] += result['quantity']
        dca_data['total_cost'] += result['cost']
        dca_data['avg_price'] = dca_data['total_cost'] / dca_data['total_quantity']
        save_dca_data()
        
        await status.edit_text(
            f"✅ **Куплено {result['quantity']:.4f} TON**\n"
            f"Цена: {result['price']:.4f} USDT\n"
            f"Потрачено: {result['cost']:.2f} USDT\n\n"
            f"📊 Всего DCA: {dca_data['total_quantity']:.4f} TON\n"
            f"Средняя: {dca_data['avg_price']:.4f} USDT",
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
application.add_handler(MessageHandler(filters.Regex("^💰 Портфель$"), show_portfolio))
application.add_handler(MessageHandler(filters.Regex("^📊 DCA$"), show_dca))

conv = ConversationHandler(
    entry_points=[MessageHandler(filters.Regex("^🛒 Купить$"), buy_start)],
    states={WAITING_FOR_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, buy_process)]},
    fallbacks=[CommandHandler("cancel", cancel)]
)
application.add_handler(conv)

# --- FLASK ---
app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    
    async def process():
        await application.process_update(update)
    
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(process())
        else:
            loop.run_until_complete(process())
    except RuntimeError:
        asyncio.run(process())
    
    return 'OK', 200

@app.route('/')
def index():
    return "✅ Бот работает!"

# --- ЗАПУСК ---
if __name__ == '__main__':
    load_dca_data()
    
    # Показываем все переменные окружения (для отладки)
    logger.info("Переменные окружения:")
    for key in os.environ:
        if any(x in key for x in ['TOKEN', 'API', 'URL', 'PORT']):
            val = os.environ[key]
            logger.info(f"  {key}: {val[:20] if len(val) > 20 else val}...")
    
    # Установка webhook
    webhook_url = os.getenv('WEBHOOK_URL') or f"{os.getenv('APP_URL', '')}/webhook"
    if webhook_url and webhook_url != '/webhook':
        asyncio.run(application.bot.set_webhook(url=webhook_url))
        logger.info(f"Webhook: {webhook_url}")
    
    port = int(os.environ.get('PORT', 8443))
    logger.info(f"Старт на порту {port}")
    app.run(host='0.0.0.0', port=port)