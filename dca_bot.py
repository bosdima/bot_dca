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
# Bothost использует TELEGRAM_BOT_TOKEN или BOT_TOKEN
TELEGRAM_TOKEN = (os.getenv('TELEGRAM_BOT_TOKEN') or 
                  os.getenv('BOT_TOKEN') or 
                  os.getenv('API_TOKEN') or
                  os.getenv('TOKEN'))

BYBIT_API_KEY = os.getenv('BYBIT_API_KEY')
BYBIT_API_SECRET = os.getenv('BYBIT_API_SECRET')
ALLOWED_USER = 'bosdima'
DCA_FILE = 'dca_data.json'
SYMBOL = 'TONUSDT'

if not TELEGRAM_TOKEN:
    logger.error("Токен Telegram не найден!")
    logger.error(f"Доступные: {[k for k in os.environ.keys() if 'TOKEN' in k]}")
    exit(1)

logger.info(f"Токен найден: {TELEGRAM_TOKEN[:15]}...")

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

# --- ФУНКЦИИ РАБОТЫ С ДАННЫМИ ---
def load_dca_data():
    global dca_data
    try:
        if os.path.exists(DCA_FILE):
            with open(DCA_FILE, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                dca_data.update(loaded)
                logger.info(f"DCA данные загружены: {dca_data['total_quantity']:.4f} TON")
    except Exception as e:
        logger.error(f"Ошибка загрузки DCA: {e}")

def save_dca_data():
    try:
        with open(DCA_FILE, 'w', encoding='utf-8') as f:
            json.dump(dca_data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Ошибка сохранения DCA: {e}")

# --- ФУНКЦИИ BYBIT ---
def check_api_keys():
    try:
        response = session.get_wallet_balance(accountType="UNIFIED")
        if response['retCode'] == 0:
            return True, "✅ Bybit API работает"
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

def get_coin_avg_price(coin):
    try:
        symbol = f"{coin}USDT"
        response = session.get_executions(category="spot", symbol=symbol, limit=100)
        
        if response['retCode'] == 0 and response['result']['list']:
            buys = []
            for trade in response['result']['list']:
                if trade.get('side') == 'Buy':
                    try:
                        price = float(trade.get('execPrice', 0))
                        qty = float(trade.get('execQty', 0))
                        if price > 0 and qty > 0:
                            buys.append({'price': price, 'qty': qty})
                    except:
                        continue
            
            if buys:
                total_cost = sum(b['price'] * b['qty'] for b in buys)
                total_qty = sum(b['qty'] for b in buys)
                if total_qty > 0:
                    return total_cost / total_qty, total_qty, total_cost
        return None, 0, 0
    except Exception as e:
        logger.error(f"Ошибка avg price: {e}")
        return None, 0, 0

def get_detailed_portfolio():
    try:
        response = session.get_wallet_balance(accountType="UNIFIED")
        if response['retCode'] != 0:
            return [], 0, 0, 0, 0
        
        portfolio = []
        total_value = total_pnl = total_invested = 0
        all_prices = {}
        
        for coin_data in response['result']['list'][0].get('coin', []):
            coin = coin_data.get('coin', '')
            balance_str = coin_data.get('walletBalance', '0')
            
            try:
                balance = float(balance_str) if balance_str else 0.0
            except:
                balance = 0.0
            
            if balance <= 0:
                continue
            
            if coin == 'USDT':
                total_value += balance
                portfolio.append({
                    'coin': 'USDT',
                    'balance': balance,
                    'current_price': 1.0,
                    'avg_price': 1.0,
                    'current_value': balance,
                    'invested': balance,
                    'pnl': 0,
                    'pnl_percentage': 0
                })
                continue
            
            symbol = f"{coin}USDT"
            if symbol not in all_prices:
                all_prices[symbol] = get_current_price(symbol)
            current_price = all_prices[symbol]
            
            if not current_price:
                portfolio.append({
                    'coin': coin,
                    'balance': balance,
                    'current_price': 0,
                    'avg_price': None,
                    'current_value': 0,
                    'invested': 0,
                    'pnl': 0,
                    'pnl_percentage': 0,
                    'note': 'Цена недоступна'
                })
                continue
            
            current_value = balance * current_price
            total_value += current_value
            
            avg_price, total_qty, total_cost = get_coin_avg_price(coin)
            
            if avg_price and total_qty > 0:
                invested = total_cost
                pnl = current_value - invested
                pnl_percentage = (pnl / invested) * 100 if invested > 0 else 0
                total_pnl += pnl
                total_invested += invested
            else:
                avg_price = current_price
                invested = current_value
                pnl = 0
                pnl_percentage = 0
                total_invested += invested
            
            portfolio.append({
                'coin': coin,
                'balance': balance,
                'current_price': current_price,
                'avg_price': avg_price,
                'current_value': current_value,
                'invested': invested,
                'pnl': pnl,
                'pnl_percentage': pnl_percentage
            })
        
        portfolio.sort(key=lambda x: x['current_value'], reverse=True)
        
        total_pnl_percentage = (total_pnl / total_invested) * 100 if total_invested > 0 else 0
        
        return portfolio, total_value, total_pnl, total_pnl_percentage, total_invested
    except Exception as e:
        logger.error(f"Ошибка портфеля: {e}")
        return [], 0, 0, 0, 0

def buy_ton(usdt_amount):
    try:
        current_price = get_current_price()
        if not current_price or current_price <= 0:
            return None, "Не удалось получить цену"
        
        quantity = usdt_amount / current_price
        
        try:
            symbol_info = session.get_instruments_info(category="spot", symbol=SYMBOL)
            if symbol_info['retCode'] == 0 and symbol_info['result']['list']:
                lot_size = symbol_info['result']['list'][0].get('lotSizeFilter', {})
                qty_step = float(lot_size.get('qtyStep', '0.001'))
                quantity = round(quantity / qty_step) * qty_step
        except:
            quantity = round(quantity, 3)
        
        min_qty = 0.1
        if quantity < min_qty:
            return None, f"Минимум {min_qty} TON, вы пытаетесь купить {quantity:.4f}"
        
        portfolio, _, _, _, _ = get_detailed_portfolio()
        usdt_balance = next((p['balance'] for p in portfolio if p['coin'] == 'USDT'), 0)
        
        if usdt_balance < usdt_amount:
            return None, f"Недостаточно USDT. Баланс: {usdt_balance:.2f}, нужно: {usdt_amount:.2f}"
        
        logger.info(f"Ордер: {quantity} TON по рыночной цене")
        
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
        executed_qty = executed_cost = 0
        
        if executions['retCode'] == 0:
            for exec in executions['result']['list']:
                if exec.get('orderId') == order_id:
                    try:
                        price = float(exec.get('execPrice', 0))
                        qty = float(exec.get('execQty', 0))
                        if price > 0 and qty > 0:
                            executed_qty += qty
                            executed_cost += price * qty
                    except:
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
        return None, f"Ошибка покупки: {str(e)}"

def check_user(username: str) -> bool:
    return username == ALLOWED_USER

# --- ОБРАБОТЧИКИ TELEGRAM ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not check_user(user.username):
        await update.message.reply_text("❌ У вас нет доступа к этому боту.")
        return
    
    bybit_ok, bybit_msg = check_api_keys()
    
    keyboard = [["💰 Мой Портфель", "📊 Куплено по DCA"], ["🛒 Купить по DCA"]]
    markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    welcome = (
        f"👋 Добро пожаловать, {user.first_name}!\n\n"
        f"{bybit_msg}\n\n"
        "Выберите действие:"
    )
    
    await update.message.reply_text(welcome, reply_markup=markup)

async def show_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not check_user(user.username):
        return
    
    status_msg = await update.message.reply_text("🔄 Загружаю портфель...")
    
    with concurrent.futures.ThreadPoolExecutor() as executor:
        future = executor.submit(get_detailed_portfolio)
        portfolio, total_value, total_pnl, total_pnl_pct, total_invested = future.result()
    
    if not portfolio:
        await status_msg.edit_text("📭 Портфель пуст или ошибка загрузки")
        return
    
    response = "💰 **ДЕТАЛЬНЫЙ ПОРТФЕЛЬ**\n\n"
    response += f"📊 **Общая стоимость:** {total_value:.2f} USDT\n"
    response += f"💵 **Всего инвестировано:** {total_invested:.2f} USDT\n"
    
    if total_pnl != 0:
        emoji = "📈" if total_pnl > 0 else "📉"
        response += f"{emoji} **Общий PnL:** {total_pnl:+.2f} USDT ({total_pnl_pct:+.2f}%)\n"
    
    response += "\n" + "═" * 30 + "\n\n"
    
    for item in portfolio:
        coin = item['coin']
        balance = item['balance']
        
        if coin == 'USDT':
            response += f"💵 **USDT:** {balance:.2f}\n\n"
            continue
        
        response += f"💎 **{coin}**\n"
        response += f"   Количество: {balance:.4f}\n"
        
        if item.get('avg_price'):
            response += f"   Средняя цена: {item['avg_price']:.4f} USDT\n"
        
        if item['current_price'] > 0:
            response += f"   Текущая цена: {item['current_price']:.4f} USDT\n"
            response += f"   Стоимость: {item['current_value']:.2f} USDT\n"
            
            if item['pnl'] != 0:
                emoji = "✅" if item['pnl'] > 0 else "❌"
                response += f"   {emoji} PnL: {item['pnl']:+.2f} ({item['pnl_percentage']:+.2f}%)\n"
        
        if total_value > 0 and item['current_value'] > 0:
            pct = (item['current_value'] / total_value) * 100
            response += f"   Доля: {pct:.1f}%\n"
        
        response += "\n"
    
    if dca_data['total_quantity'] > 0:
        response += "═" * 30 + "\n"
        response += "📊 **DCA Статистика:**\n"
        response += f"   TON: {dca_data['total_quantity']:.4f}\n"
        response += f"   Средняя цена: {dca_data['avg_price']:.4f} USDT\n"
        
        ton = next((p for p in portfolio if p['coin'] == 'TON'), None)
        if ton and ton['current_price'] > 0:
            dca_value = dca_data['total_quantity'] * ton['current_price']
            dca_pnl = dca_value - dca_data['total_cost']
            dca_pnl_pct = (dca_pnl / dca_data['total_cost']) * 100 if dca_data['total_cost'] > 0 else 0
            emoji = "✅" if dca_pnl > 0 else "⏳"
            response += f"   {emoji} DCA PnL: {dca_pnl:+.2f} USDT ({dca_pnl_pct:+.2f}%)\n"
    
    await status_msg.edit_text(response, parse_mode='Markdown')

async def show_dca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not check_user(user.username):
        return
    
    if not dca_data['start_time'] or dca_data['total_quantity'] == 0:
        await update.message.reply_text("📊 DCA еще не начат. Нажмите '🛒 Купить по DCA'")
        return
    
    current_price = get_current_price()
    if not current_price:
        await update.message.reply_text("❌ Не удалось получить цену")
        return
    
    current_value = dca_data['total_quantity'] * current_price
    pnl = current_value - dca_data['total_cost']
    pnl_pct = (pnl / dca_data['total_cost']) * 100 if dca_data['total_cost'] > 0 else 0
    
    start_date = datetime.fromtimestamp(dca_data['start_time']).strftime('%Y-%m-%d %H:%M')
    emoji = "📈" if pnl > 0 else "📉" if pnl < 0 else "📊"
    
    response = (
        f"📊 **СТАТИСТИКА DCA**\n\n"
        f"📅 Начало: {start_date}\n"
        f"💰 Всего: {dca_data['total_quantity']:.4f} TON\n"
        f"💵 Средняя цена: {dca_data['avg_price']:.4f} USDT\n"
        f"📈 Текущая цена: {current_price:.4f} USDT\n"
        f"💵 Инвестировано: {dca_data['total_cost']:.2f} USDT\n"
        f"💰 Текущая стоимость: {current_value:.2f} USDT\n"
        f"{emoji} **PnL: {pnl:+.2f} USDT ({pnl_pct:+.2f}%)**\n\n"
        f"📊 Сделок: {len(dca_data['trades'])}"
    )
    
    if pnl > 0:
        response += f"\n\n💡 Рекомендация: прибыль {pnl_pct:+.2f}%. Рассмотрите продажу."
    
    await update.message.reply_text(response, parse_mode='Markdown')

async def buy_dca_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not check_user(user.username):
        return ConversationHandler.END
    
    current_price = get_current_price()
    if not current_price:
        await update.message.reply_text("❌ Не удалось получить цену")
        return ConversationHandler.END
    
    await update.message.reply_text(
        f"🛒 **Покупка по DCA**\n\n"
        f"Текущая цена TON: {current_price:.4f} USDT\n\n"
        f"Введите сумму в USDT (минимум 1 USDT):",
        parse_mode='Markdown'
    )
    return WAITING_FOR_AMOUNT

async def buy_dca_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip())
        
        if amount < 1:
            await update.message.reply_text("❌ Минимум 1 USDT")
            return WAITING_FOR_AMOUNT
        
        status_msg = await update.message.reply_text("🔄 Размещаю ордер...")
        
        with concurrent.futures.ThreadPoolExecutor() as executor:
            result, error = executor.submit(buy_ton, amount).result()
        
        if error:
            await status_msg.edit_text(f"❌ {error}")
            return ConversationHandler.END
        
        if not dca_data['start_time']:
            dca_data['start_time'] = time.time()
        
        dca_data['trades'].append({
            'timestamp': time.time(),
            'quantity': result['quantity'],
            'price': result['price'],
            'cost': result['cost'],
            'order_id': result.get('order_id', '')
        })
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
            f"📊 **DCA:**\n"
            f"Всего: {dca_data['total_quantity']:.4f} TON\n"
            f"Средняя: {dca_data['avg_price']:.4f} USDT\n"
            f"Инвестировано: {dca_data['total_cost']:.2f} USDT"
        )
        
        await status_msg.edit_text(response, parse_mode='Markdown')
        
        # Рекомендация по продаже
        if current_price and current_price > dca_data['avg_price']:
            profit_pct = ((current_price - dca_data['avg_price']) / dca_data['avg_price']) * 100
            
            if profit_pct > 0:
                portfolio, _, _, _, _ = get_detailed_portfolio()
                ton_balance = next((p['balance'] for p in portfolio if p['coin'] == 'TON'), 0)
                
                rec = (
                    f"💡 **РЕКОМЕНДАЦИЯ**\n\n"
                    f"Цена выше средней на {profit_pct:.2f}%\n"
                    f"Рекомендуется продать {dca_data['total_quantity']:.4f} TON"
                )
                if ton_balance > dca_data['total_quantity']:
                    rec += f"\nВсего TON: {ton_balance:.4f}"
                
                await update.message.reply_text(rec, parse_mode='Markdown')
        
        return ConversationHandler.END
        
    except ValueError:
        await update.message.reply_text("❌ Введите число")
        return WAITING_FOR_AMOUNT
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")
        return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Операция отменена")
    return ConversationHandler.END

async def echo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текстовых сообщений"""
    user = update.effective_user
    if not check_user(user.username):
        return
    
    text = update.message.text
    
    if text == "💰 Мой Портфель":
        await show_portfolio(update, context)
    elif text == "📊 Куплено по DCA":
        await show_dca(update, context)
    elif text == "🛒 Купить по DCA":
        return await buy_dca_start(update, context)
    else:
        await update.message.reply_text("Используйте кнопки меню")

# --- РЕГИСТРАЦИЯ ОБРАБОТЧИКОВ ---
application.add_handler(CommandHandler("start", start))

# Conversation для покупки
buy_conv = ConversationHandler(
    entry_points=[MessageHandler(filters.Regex("^🛒 Купить по DCA$"), buy_dca_start)],
    states={
        WAITING_FOR_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, buy_dca_process)]
    },
    fallbacks=[CommandHandler("cancel", cancel)]
)
application.add_handler(buy_conv)

# Кнопки меню
application.add_handler(MessageHandler(filters.Regex("^💰 Мой Портфель$"), show_portfolio))
application.add_handler(MessageHandler(filters.Regex("^📊 Куплено по DCA$"), show_dca))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo_handler))

# --- FLASK WEB SERVER ---
app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():
    """Обработка обновлений от Telegram"""
    try:
        data = request.get_json(force=True)
        logger.debug(f"Update: {data}")
        
        update = Update.de_json(data, application.bot)
        
        # Обработка в event loop приложения
        if application.running:
            application.create_task(application.process_update(update))
        else:
            # Инициализируем если нужно
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(application.initialize())
            loop.run_until_complete(application.process_update(update))
        
        return 'OK', 200
        
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return 'Error', 500

@app.route('/')
def index():
    return "✅ DCA Bot работает!", 200

@app.route('/health')
def health():
    return {
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "dca_quantity": dca_data['total_quantity']
    }, 200

# --- ЗАПУСК ---
if __name__ == '__main__':
    load_dca_data()
    
    # Логируем переменные окружения
    logger.info("=" * 50)
    logger.info("Переменные окружения:")
    for key in sorted(os.environ.keys()):
        if any(x in key.upper() for x in ['TOKEN', 'API', 'URL', 'PORT', 'KEY', 'SECRET']):
            val = os.environ[key]
            masked = val[:15] + "..." if len(val) > 20 else val
            logger.info(f"  {key}: {masked}")
    logger.info("=" * 50)
    
    # Проверка Bybit
    bybit_ok, bybit_msg = check_api_keys()
    logger.info(f"Bybit: {bybit_msg}")
    
    # Установка webhook
    webhook_url = os.getenv('WEBHOOK_URL')
    
    if not webhook_url:
        app_url = os.getenv('APP_URL')
        if app_url:
            webhook_url = f"{app_url.rstrip('/')}/webhook"
    
    if webhook_url:
        # Убеждаемся что URL правильный
        if not webhook_url.endswith('/webhook'):
            webhook_url = f"{webhook_url.rstrip('/')}/webhook"
        
        logger.info(f"Установка webhook: {webhook_url[:60]}...")
        
        async def setup():
            await application.initialize()
            await application.bot.set_webhook(
                url=webhook_url,
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True
            )
            logger.info("✅ Webhook установлен!")
        
        try:
            asyncio.run(setup())
        except Exception as e:
            logger.error(f"❌ Ошибка webhook: {e}")
    else:
        logger.warning("⚠️ WEBHOOK_URL не найден!")
    
    # Запуск Flask
    port = int(os.environ.get('PORT', 8443))
    logger.info(f"Старт Flask на порту {port}")
    logger.info("=" * 50)
    
    # threaded=True важно для обработки нескольких запросов
    app.run(host='0.0.0.0', port=port, threaded=True)