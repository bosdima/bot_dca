import os
import json
import time
import logging
import asyncio
from datetime import datetime
from flask import Flask, request
from telegram import Update, Bot
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
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
BYBIT_API_KEY = os.getenv('BYBIT_API_KEY')
BYBIT_API_SECRET = os.getenv('BYBIT_API_SECRET')
ALLOWED_USER = 'bosdima'  # БЕЗ @ в начале!
DCA_FILE = 'dca_data.json'
SYMBOL = 'TONUSDT'

# Проверка обязательных переменных
if not TELEGRAM_TOKEN:
    logger.error("TELEGRAM_TOKEN не найден!")
    exit(1)

# --- ИНИЦИАЛИЗАЦИЯ КЛИЕНТОВ ---
# Bybit клиент (синхронный, используем в отдельных потоках или асинхронно)
session = HTTP(
    testnet=False,
    api_key=BYBIT_API_KEY,
    api_secret=BYBIT_API_SECRET,
)

# Telegram Application
application = Application.builder().token(TELEGRAM_TOKEN).build()

# --- ГЛОБАЛЬНЫЕ ДАННЫЕ DCA ---
dca_data = {
    'start_time': None,
    'total_quantity': 0.0,
    'total_cost': 0.0,
    'avg_price': 0.0,
    'trades': []
}

# Состояния для ConversationHandler
WAITING_FOR_AMOUNT = 1

# --- ФУНКЦИИ РАБОТЫ С ДАННЫМИ ---
def load_dca_data():
    global dca_data
    try:
        if os.path.exists(DCA_FILE):
            with open(DCA_FILE, 'r', encoding='utf-8') as f:
                loaded_data = json.load(f)
                dca_data.update(loaded_data)
                logger.info("DCA данные загружены")
    except Exception as e:
        logger.error(f"Ошибка загрузки DCA данных: {e}")

def save_dca_data():
    try:
        with open(DCA_FILE, 'w', encoding='utf-8') as f:
            json.dump(dca_data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Ошибка сохранения DCA данных: {e}")

# --- ФУНКЦИИ API BYBIT ---
def check_api_keys():
    try:
        response = session.get_wallet_balance(accountType="UNIFIED")
        if response['retCode'] == 0:
            return True, "✅ API ключи Bybit работают"
        else:
            return False, f"❌ Ошибка API: {response['retMsg']}"
    except Exception as e:
        return False, f"❌ Ошибка подключения: {str(e)}"

def get_current_price(symbol=SYMBOL):
    try:
        response = session.get_tickers(category="spot", symbol=symbol)
        if response['retCode'] == 0 and response['result']['list']:
            price_str = response['result']['list'][0].get('lastPrice', '0')
            return float(price_str) if price_str else None
        return None
    except Exception as e:
        logger.error(f"Ошибка получения цены: {e}")
        return None

def get_coin_avg_price(coin):
    try:
        symbol = f"{coin}USDT"
        response = session.get_executions(category="spot", symbol=symbol, limit=100)
        
        if response['retCode'] == 0 and response['result']['list']:
            buys = []
            for trade in response['result']['list']:
                if trade.get('side') == 'Buy':
                    try:
                        price = float(trade.get('execPrice', '0'))
                        qty = float(trade.get('execQty', '0'))
                        if price > 0 and qty > 0:
                            buys.append({'price': price, 'qty': qty})
                    except ValueError:
                        continue
            
            if buys:
                total_cost = sum(b['price'] * b['qty'] for b in buys)
                total_qty = sum(b['qty'] for b in buys)
                if total_qty > 0:
                    return total_cost / total_qty, total_qty, total_cost
        return None, 0, 0
    except Exception as e:
        logger.error(f"Ошибка получения средней цены для {coin}: {e}")
        return None, 0, 0

def get_detailed_portfolio():
    try:
        response = session.get_wallet_balance(accountType="UNIFIED")
        
        if response['retCode'] == 0:
            portfolio = []
            total_usdt_value = 0.0
            total_pnl = 0.0
            total_pnl_percentage = 0.0
            total_invested = 0.0
            
            if not response['result']['list']:
                return [], 0.0, 0.0, 0.0, 0.0
            
            all_prices = {}
            
            for coin_data in response['result']['list'][0].get('coin', []):
                coin = coin_data.get('coin', '')
                wallet_balance_str = coin_data.get('walletBalance', '0')
                
                try:
                    wallet_balance = float(wallet_balance_str) if wallet_balance_str else 0.0
                except ValueError:
                    wallet_balance = 0.0
                
                if wallet_balance > 0 and coin != 'USDT':
                    symbol = f"{coin}USDT"
                    if symbol not in all_prices:
                        current_price = get_current_price(symbol)
                        all_prices[symbol] = current_price
                    else:
                        current_price = all_prices[symbol]
                    
                    if current_price and current_price > 0:
                        avg_price, total_qty, total_cost = get_coin_avg_price(coin)
                        current_value = wallet_balance * current_price
                        total_usdt_value += current_value
                        
                        if avg_price and total_qty > 0:
                            invested = total_cost
                            pnl = current_value - invested
                            pnl_percentage = (pnl / invested) * 100 if invested > 0 else 0
                            total_pnl += pnl
                            total_invested += invested
                        else:
                            avg_price = current_price
                            pnl = 0
                            pnl_percentage = 0
                            invested = current_value
                            total_invested += invested
                        
                        portfolio.append({
                            'coin': coin,
                            'balance': wallet_balance,
                            'current_price': current_price,
                            'avg_price': avg_price,
                            'current_value': current_value,
                            'invested': invested,
                            'pnl': pnl,
                            'pnl_percentage': pnl_percentage
                        })
                    else:
                        portfolio.append({
                            'coin': coin,
                            'balance': wallet_balance,
                            'current_price': 0,
                            'avg_price': None,
                            'current_value': 0,
                            'invested': 0,
                            'pnl': 0,
                            'pnl_percentage': 0,
                            'note': 'Цена не доступна'
                        })
                elif coin == 'USDT' and wallet_balance > 0:
                    total_usdt_value += wallet_balance
                    portfolio.append({
                        'coin': 'USDT',
                        'balance': wallet_balance,
                        'current_price': 1.0,
                        'avg_price': 1.0,
                        'current_value': wallet_balance,
                        'invested': wallet_balance,
                        'pnl': 0,
                        'pnl_percentage': 0
                    })
            
            portfolio.sort(key=lambda x: x['current_value'], reverse=True)
            
            if total_invested > 0:
                total_pnl_percentage = (total_pnl / total_invested) * 100
            
            return portfolio, total_usdt_value, total_pnl, total_pnl_percentage, total_invested
        return [], 0.0, 0.0, 0.0, 0.0
    except Exception as e:
        logger.error(f"Ошибка получения портфеля: {e}")
        return [], 0.0, 0.0, 0.0, 0.0

def buy_ton(usdt_amount):
    try:
        current_price = get_current_price()
        if not current_price or current_price <= 0:
            return None, "Не удалось получить текущую цену"
        
        quantity = usdt_amount / current_price
        
        try:
            symbol_info = session.get_instruments_info(category="spot", symbol=SYMBOL)
            if symbol_info['retCode'] == 0 and symbol_info['result']['list']:
                lot_size_filter = symbol_info['result']['list'][0].get('lotSizeFilter', {})
                qty_step = float(lot_size_filter.get('qtyStep', '0.001'))
                quantity = round(quantity / qty_step) * qty_step
        except:
            quantity = round(quantity, 3)
        
        min_qty = 0.1
        if quantity < min_qty:
            return None, f"Минимальное количество: {min_qty} TON. Вы пытаетесь купить {quantity:.4f} TON"
        
        portfolio, total_usdt, _, _, _ = get_detailed_portfolio()
        usdt_balance = 0
        for item in portfolio:
            if item['coin'] == 'USDT':
                usdt_balance = item['balance']
                break
        
        if usdt_balance < usdt_amount:
            return None, f"Недостаточно USDT. Баланс: {usdt_balance:.2f}, нужно: {usdt_amount:.2f}"
        
        logger.info(f"Размещаем ордер на покупку {quantity} TON")
        
        order = session.place_order(
            category="spot",
            symbol=SYMBOL,
            side="Buy",
            orderType="Market",
            qty=str(quantity),
            timeInForce="GTC"
        )
        
        if order['retCode'] != 0:
            return None, f"Ошибка биржи: {order['retMsg']}"
        
        order_id = order['result']['orderId']
        time.sleep(2)
        
        executions = session.get_executions(category="spot", symbol=SYMBOL, limit=10)
        
        executed_qty = 0
        executed_cost = 0
        
        if executions['retCode'] == 0:
            for exec in executions['result']['list']:
                if exec.get('orderId') == order_id:
                    try:
                        exec_price = float(exec.get('execPrice', '0'))
                        exec_qty = float(exec.get('execQty', '0'))
                        if exec_price > 0 and exec_qty > 0:
                            executed_qty += exec_qty
                            executed_cost += exec_price * exec_qty
                    except ValueError:
                        continue
        
        if executed_qty > 0:
            executed_price = executed_cost / executed_qty
            return {
                'quantity': executed_qty,
                'price': executed_price,
                'cost': executed_cost,
                'order_id': order_id
            }, None
        else:
            return {
                'quantity': quantity,
                'price': current_price,
                'cost': usdt_amount,
                'order_id': order_id
            }, None
            
    except Exception as e:
        return None, f"Ошибка при покупке: {str(e)}"

# --- ПРОВЕРКА ДОСТУПА ---
def check_user(username: str) -> bool:
    """Проверяет, имеет ли пользователь доступ к боту"""
    return username == ALLOWED_USER

# --- ОБРАБОТЧИКИ TELEGRAM ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user = update.effective_user
    if not check_user(user.username):
        await update.message.reply_text("❌ У вас нет доступа к этому боту.")
        return
    
    bybit_status, bybit_msg = check_api_keys()
    
    keyboard = [
        ["💰 Мой Портфель", "📊 Куплено по DCA"],
        ["🛒 Купить по DCA"]
    ]
    
    from telegram import ReplyKeyboardMarkup
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    welcome_text = (
        f"👋 Добро пожаловать, {user.first_name}!\n\n"
        f"{bybit_msg}\n\n"
        "Выберите действие:"
    )
    
    await update.message.reply_text(welcome_text, reply_markup=reply_markup)

async def show_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать детальный портфель"""
    user = update.effective_user
    if not check_user(user.username):
        return
    
    status_msg = await update.message.reply_text("🔄 Загружаю данные портфеля...")
    
    # Запускаем синхронную функцию в отдельном потоке
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor() as executor:
        future = executor.submit(get_detailed_portfolio)
        portfolio, total_value, total_pnl, total_pnl_percentage, total_invested = future.result()
    
    if not portfolio:
        await status_msg.edit_text("📭 Портфель пуст или не удалось загрузить данные")
        return
    
    response = "💰 **ДЕТАЛЬНЫЙ ПОРТФЕЛЬ**\n\n"
    response += f"📊 **Общая стоимость:** {total_value:.2f} USDT\n"
    response += f"💵 **Всего инвестировано:** {total_invested:.2f} USDT\n"
    
    if total_pnl != 0:
        pnl_emoji = "📈" if total_pnl > 0 else "📉"
        response += f"{pnl_emoji} **Общий PnL:** {total_pnl:+.2f} USDT ({total_pnl_percentage:+.2f}%)\n"
    
    response += "\n" + "═" * 30 + "\n\n"
    
    for item in portfolio:
        coin = item['coin']
        balance = item['balance']
        current_price = item['current_price']
        avg_price = item['avg_price']
        current_value = item['current_value']
        pnl = item['pnl']
        pnl_percentage = item['pnl_percentage']
        
        if coin == 'USDT':
            response += f"💵 **USDT**\n"
            response += f"   Баланс: {balance:.2f}\n\n"
        else:
            coin_emoji = "💎"
            response += f"{coin_emoji} **{coin}**\n"
            response += f"   Количество: {balance:.4f}\n"
            
            if avg_price and avg_price > 0:
                response += f"   Средняя цена: {avg_price:.4f} USDT\n"
            
            if current_price > 0:
                response += f"   Текущая цена: {current_price:.4f} USDT\n"
                response += f"   Стоимость: {current_value:.2f} USDT\n"
                
                if pnl != 0:
                    pnl_emoji = "✅" if pnl > 0 else "❌"
                    response += f"   {pnl_emoji} PnL: {pnl:+.2f} USDT ({pnl_percentage:+.2f}%)\n"
            
            if total_value > 0:
                percentage = (current_value / total_value) * 100
                response += f"   Доля: {percentage:.1f}%\n"
            
            response += "\n"
    
    # DCA статистика
    if dca_data['total_quantity'] > 0:
        response += "═" * 30 + "\n"
        response += "📊 **DCA Статистика:**\n"
        response += f"   TON в DCA: {dca_data['total_quantity']:.4f}\n"
        response += f"   Средняя цена DCA: {dca_data['avg_price']:.4f} USDT\n"
        
        ton_item = next((item for item in portfolio if item['coin'] == 'TON'), None)
        if ton_item and ton_item['current_price'] > 0:
            dca_value = dca_data['total_quantity'] * ton_item['current_price']
            dca_pnl = dca_value - dca_data['total_cost']
            dca_pnl_percentage = (dca_pnl / dca_data['total_cost']) * 100 if dca_data['total_cost'] > 0 else 0
            dca_emoji = "✅" if dca_pnl > 0 else "⏳"
            response += f"   {dca_emoji} DCA PnL: {dca_pnl:+.2f} USDT ({dca_pnl_percentage:+.2f}%)\n"
    
    await status_msg.edit_text(response, parse_mode='Markdown')

async def show_dca_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать статистику DCA"""
    user = update.effective_user
    if not check_user(user.username):
        return
    
    if not dca_data['start_time'] or dca_data['total_quantity'] == 0:
        await update.message.reply_text("📊 DCA еще не начат. Нажмите '🛒 Купить по DCA'")
        return
    
    current_price = get_current_price()
    
    if current_price is None:
        await update.message.reply_text("❌ Не удалось получить текущую цену")
        return
    
    current_value = dca_data['total_quantity'] * current_price
    pnl = current_value - dca_data['total_cost']
    pnl_percentage = (pnl / dca_data['total_cost']) * 100 if dca_data['total_cost'] > 0 else 0
    
    start_date = datetime.fromtimestamp(dca_data['start_time']).strftime('%Y-%m-%d %H:%M')
    pnl_emoji = "📈" if pnl > 0 else "📉" if pnl < 0 else "📊"
    
    response = (
        f"📊 **СТАТИСТИКА DCA**\n\n"
        f"📅 Начало: {start_date}\n"
        f"💰 Всего: {dca_data['total_quantity']:.4f} TON\n"
        f"💵 Средняя цена: {dca_data['avg_price']:.4f} USDT\n"
        f"📈 Текущая цена: {current_price:.4f} USDT\n"
        f"💵 Инвестировано: {dca_data['total_cost']:.2f} USDT\n"
        f"💰 Текущая стоимость: {current_value:.2f} USDT\n"
        f"{pnl_emoji} **PnL: {pnl:+.2f} USDT ({pnl_percentage:+.2f}%)**\n\n"
        f"📊 Сделок: {len(dca_data['trades'])}"
    )
    
    if pnl > 0:
        response += f"\n\n💡 Рекомендация: Прибыль {pnl_percentage:+.2f}%. Рассмотрите продажу."
    
    await update.message.reply_text(response, parse_mode='Markdown')

async def buy_dca_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало покупки по DCA"""
    user = update.effective_user
    if not check_user(user.username):
        return
    
    current_price = get_current_price()
    
    if current_price is None:
        await update.message.reply_text("❌ Не удалось получить текущую цену")
        return ConversationHandler.END
    
    await update.message.reply_text(
        f"🛒 **Покупка по DCA**\n\n"
        f"Текущая цена TON: {current_price:.4f} USDT\n\n"
        f"Введите сумму в USDT (минимум 1 USDT):",
        parse_mode='Markdown'
    )
    
    return WAITING_FOR_AMOUNT

async def process_dca_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка суммы покупки"""
    try:
        amount = float(update.message.text.strip())
        
        if amount < 1:
            await update.message.reply_text("❌ Минимальная сумма: 1 USDT")
            return ConversationHandler.END
        
        status_msg = await update.message.reply_text("🔄 Размещаю ордер...")
        
        # Выполняем покупку в отдельном потоке
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(buy_ton, amount)
            result, error = future.result()
        
        if error:
            await status_msg.edit_text(f"❌ {error}")
            return ConversationHandler.END
        
        # Обновляем DCA данные
        if not dca_data['start_time']:
            dca_data['start_time'] = time.time()
        
        trade = {
            'timestamp': time.time(),
            'quantity': result['quantity'],
            'price': result['price'],
            'cost': result['cost'],
            'order_id': result.get('order_id', '')
        }
        
        dca_data['trades'].append(trade)
        dca_data['total_quantity'] += result['quantity']
        dca_data['total_cost'] += result['cost']
        dca_data['avg_price'] = dca_data['total_cost'] / dca_data['total_quantity']
        
        save_dca_data()
        
        current_price = get_current_price()
        
        response = (
            f"✅ **Покупка выполнена!**\n\n"
            f"Куплено: {result['quantity']:.4f} TON\n"
            f"Цена: {result['price']:.4f} USDT\n"
            f"Потрачено: {result['cost']:.2f} USDT\n"
            f"ID: `{result.get('order_id', 'N/A')}`\n\n"
            f"📊 **DCA Статистика:**\n"
            f"Всего: {dca_data['total_quantity']:.4f} TON\n"
            f"Средняя цена: {dca_data['avg_price']:.4f} USDT\n"
            f"Инвестировано: {dca_data['total_cost']:.2f} USDT"
        )
        
        await status_msg.edit_text(response, parse_mode='Markdown')
        
        # Проверяем рекомендацию по продаже
        if current_price and current_price > dca_data['avg_price']:
            profit_percentage = ((current_price - dca_data['avg_price']) / dca_data['avg_price']) * 100
            
            if profit_percentage > 0:
                portfolio, _, _, _, _ = get_detailed_portfolio()
                ton_balance = next((item['balance'] for item in portfolio if item['coin'] == 'TON'), 0)
                
                sell_message = (
                    f"💡 **РЕКОМЕНДАЦИЯ**\n\n"
                    f"Цена ({current_price:.4f}) > средней ({dca_data['avg_price']:.4f})\n"
                    f"Прибыль: {profit_percentage:.2f}%\n\n"
                    f"Рекомендуется продать {dca_data['total_quantity']:.4f} TON"
                )
                
                if ton_balance > dca_data['total_quantity']:
                    sell_message += f"\nВсего TON: {ton_balance:.4f}"
                
                await update.message.reply_text(sell_message, parse_mode='Markdown')
        
        return ConversationHandler.END
        
    except ValueError:
        await update.message.reply_text("❌ Введите число")
        return WAITING_FOR_AMOUNT
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")
        return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена операции"""
    await update.message.reply_text("❌ Операция отменена")
    return ConversationHandler.END

async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка неизвестных команд"""
    user = update.effective_user
    if not check_user(user.username):
        return
    
    text = update.message.text
    
    if text == "💰 Мой Портфель":
        await show_portfolio(update, context)
    elif text == "📊 Куплено по DCA":
        await show_dca_stats(update, context)
    elif text == "🛒 Купить по DCA":
        return await buy_dca_start(update, context)
    else:
        await update.message.reply_text("Используйте кнопки меню или /start")

# --- РЕГИСТРАЦИЯ ОБРАБОТЧИКОВ ---
application.add_handler(CommandHandler("start", start))

# ConversationHandler для покупки
conv_handler = ConversationHandler(
    entry_points=[MessageHandler(filters.Regex("^🛒 Купить по DCA$"), buy_dca_start)],
    states={
        WAITING_FOR_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_dca_purchase)]
    },
    fallbacks=[CommandHandler("cancel", cancel)]
)
application.add_handler(conv_handler)

# Обработчики кнопок
application.add_handler(MessageHandler(filters.Regex("^💰 Мой Портфель$"), show_portfolio))
application.add_handler(MessageHandler(filters.Regex("^📊 Куплено по DCA$"), show_dca_stats))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_command))

# --- WEB-СЕРВЕР FLASK ---
app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():
    """Принимаем обновления от Telegram"""
    update = Update.de_json(request.get_json(force=True), application.bot)
    
    # Используем asyncio для обработки
    async def process():
        await application.process_update(update)
    
    # Запускаем в существующем event loop или создаем новый
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
    return "✅ DCA Bot работает!", 200

@app.route('/health')
def health():
    """Health check для мониторинга"""
    return {"status": "ok", "timestamp": datetime.now().isoformat()}, 200

# --- ЗАПУСК ---
if __name__ == '__main__':
    load_dca_data()
    
    # Проверка API при старте
    bybit_ok, bybit_msg = check_api_keys()
    logger.info(bybit_msg)
    
    # Установка webhook
    webhook_url = os.getenv("WEBHOOK_URL")
    if not webhook_url:
        # Пробуем составить из APP_URL или используем дефолт
        app_url = os.getenv("APP_URL", "https://your-bot.bothost.ru")
        webhook_url = f"{app_url}/webhook"
    
    # Инициализация и установка webhook
    async def init_webhook():
        await application.initialize()
        await application.bot.set_webhook(url=webhook_url)
        logger.info(f"Webhook установлен: {webhook_url}")
    
    asyncio.run(init_webhook())
    
    # Запуск Flask
    port = int(os.environ.get('PORT', 8443))
    logger.info(f"Запуск сервера на порту {port}")
    app.run(host='0.0.0.0', port=port)