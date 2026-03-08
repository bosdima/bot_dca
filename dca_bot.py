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

# Пробуем загрузить dotenv, если установлен
try:
    from dotenv import load_dotenv
    load_dotenv()
    print("✅ .env файл загружен")
except ImportError:
    print("⚠️ python-dotenv не установлен, используем системные переменные")

# --- НАСТРОЙКА ЛОГИРОВАНИЯ ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- КОНФИГУРАЦИЯ ---
# Telegram токен (Bothost предоставляет разные имена)
TELEGRAM_TOKEN = (os.getenv('TELEGRAM_BOT_TOKEN') or 
                  os.getenv('BOT_TOKEN') or 
                  os.getenv('API_TOKEN') or
                  os.getenv('TOKEN'))

# Bybit ключи (из .env файла или переменных окружения)
BYBIT_API_KEY = os.getenv('BYBIT_API_KEY')
BYBIT_API_SECRET = os.getenv('BYBIT_API_SECRET')

ALLOWED_USER = 'bosdima'
DCA_FILE = 'dca_data.json'
SYMBOL = 'TONUSDT'

# --- ПРОВЕРКА КОНФИГУРАЦИИ ---
logger.info("=" * 60)
logger.info("ПРОВЕРКА КОНФИГУРАЦИИ:")

if TELEGRAM_TOKEN:
    logger.info(f"✅ Telegram токен: {TELEGRAM_TOKEN[:20]}...")
else:
    logger.error("❌ Telegram токен не найден!")
    logger.error("Проверьте переменные: TELEGRAM_BOT_TOKEN, BOT_TOKEN, API_TOKEN, TOKEN")
    exit(1)

if BYBIT_API_KEY:
    logger.info(f"✅ Bybit API Key: {BYBIT_API_KEY[:15]}...")
else:
    logger.warning("❌ BYBIT_API_KEY не найден!")

if BYBIT_API_SECRET:
    logger.info(f"✅ Bybit API Secret: {BYBIT_API_SECRET[:15]}...")
else:
    logger.warning("❌ BYBIT_API_SECRET не найден!")

if not BYBIT_API_KEY or not BYBIT_API_SECRET:
    logger.warning("⚠️ Bybit ключи отсутствуют - торговые функции не будут работать")
    logger.warning("Убедитесь, что файл .env находится в корне проекта и содержит:")
    logger.warning("  BYBIT_API_KEY=...")
    logger.warning("  BYBIT_API_SECRET=...")

logger.info("=" * 60)

# --- ИНИЦИАЛИЗАЦИЯ BYBIT ---
session = None
if BYBIT_API_KEY and BYBIT_API_SECRET:
    try:
        session = HTTP(
            testnet=False,
            api_key=BYBIT_API_KEY,
            api_secret=BYBIT_API_SECRET,
        )
        logger.info("✅ Bybit клиент инициализирован")
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации Bybit: {e}")
else:
    logger.warning("⚠️ Bybit клиент не создан (нет ключей)")

# --- TELEGRAM APPLICATION ---
try:
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    logger.info("✅ Telegram Application создан")
except Exception as e:
    logger.error(f"❌ Ошибка создания Telegram Application: {e}")
    exit(1)

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
                loaded = json.load(f)
                dca_data.update(loaded)
                logger.info(f"✅ DCA данные загружены: {dca_data['total_quantity']:.4f} TON")
        else:
            logger.info("ℹ️ Файл DCA не найден, будет создан при первой покупке")
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки DCA: {e}")

def save_dca_data():
    try:
        with open(DCA_FILE, 'w', encoding='utf-8') as f:
            json.dump(dca_data, f, indent=2, ensure_ascii=False)
        logger.info("✅ DCA данные сохранены")
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения DCA: {e}")

def check_api_keys():
    if not session:
        return False, "❌ Bybit не инициализирован. Проверьте BYBIT_API_KEY и BYBIT_API_SECRET в .env файле."
    try:
        response = session.get_wallet_balance(accountType="UNIFIED")
        if response['retCode'] == 0:
            return True, "✅ Bybit API работает корректно"
        return False, f"❌ Bybit ошибка: {response['retMsg']}"
    except Exception as e:
        return False, f"❌ Bybit: {str(e)}"

def get_current_price(symbol=SYMBOL):
    if not session:
        return None
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
    if not session:
        return None, 0, 0
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
        logger.error(f"Ошибка avg price для {coin}: {e}")
        return None, 0, 0

def get_detailed_portfolio():
    if not session:
        return [], 0, 0, 0, 0
    
    try:
        response = session.get_wallet_balance(accountType="UNIFIED")
        if response['retCode'] != 0:
            logger.warning(f"Bybit вернул ошибку: {response}")
            return [], 0, 0, 0, 0
        
        portfolio = []
        total_usdt_value = 0.0
        total_pnl = 0.0
        total_invested = 0.0
        all_prices = {}
        
        coins = response['result']['list'][0].get('coin', []) if response['result']['list'] else []
        
        for coin_data in coins:
            coin = coin_data.get('coin', '')
            wallet_balance_str = coin_data.get('walletBalance', '0')
            
            try:
                wallet_balance = float(wallet_balance_str) if wallet_balance_str else 0.0
            except ValueError:
                wallet_balance = 0.0
            
            if wallet_balance <= 0:
                continue
            
            if coin == 'USDT':
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
                continue
            
            symbol = f"{coin}USDT"
            if symbol not in all_prices:
                all_prices[symbol] = get_current_price(symbol)
            
            current_price = all_prices[symbol]
            
            if current_price and current_price > 0:
                current_value = wallet_balance * current_price
                total_usdt_value += current_value
                
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
        
        portfolio.sort(key=lambda x: x['current_value'], reverse=True)
        
        total_pnl_percentage = (total_pnl / total_invested) * 100 if total_invested > 0 else 0
        
        return portfolio, total_usdt_value, total_pnl, total_pnl_percentage, total_invested
    except Exception as e:
        logger.error(f"Ошибка получения портфеля: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return [], 0.0, 0.0, 0.0, 0.0

def buy_ton(usdt_amount):
    if not session:
        return None, "Bybit API не настроен. Проверьте BYBIT_API_KEY и BYBIT_API_SECRET в .env файле."
    
    try:
        current_price = get_current_price()
        if not current_price or current_price <= 0:
            return None, "Не удалось получить текущую цену TON"
        
        quantity = usdt_amount / current_price
        
        # Получаем точность
        try:
            symbol_info = session.get_instruments_info(category="spot", symbol=SYMBOL)
            if symbol_info['retCode'] == 0 and symbol_info['result']['list']:
                lot_size_filter = symbol_info['result']['list'][0].get('lotSizeFilter', {})
                qty_step = float(lot_size_filter.get('qtyStep', '0.001'))
                quantity = round(quantity / qty_step) * qty_step
        except Exception as e:
            logger.warning(f"Не удалось получить lot size: {e}")
            quantity = round(quantity, 3)
        
        min_qty = 0.1
        if quantity < min_qty:
            return None, f"Минимальное количество: {min_qty} TON. Вы пытаетесь купить {quantity:.4f} TON"
        
        # Проверка баланса
        portfolio, _, _, _, _ = get_detailed_portfolio()
        usdt_balance = 0
        for item in portfolio:
            if item['coin'] == 'USDT':
                usdt_balance = item['balance']
                break
        
        if usdt_balance < usdt_amount:
            return None, f"Недостаточно USDT. Баланс: {usdt_balance:.2f} USDT, необходимо: {usdt_amount:.2f} USDT"
        
        logger.info(f"Размещение ордера: {quantity} TON на {usdt_amount} USDT")
        
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
        logger.info(f"Ордер размещен: {order_id}")
        
        # Ждем исполнения
        time.sleep(2)
        
        # Получаем детали исполнения
        executions = session.get_executions(category="spot", symbol=SYMBOL, limit=10)
        executed_qty = 0.0
        executed_cost = 0.0
        
        if executions['retCode'] == 0:
            for exec in executions['result']['list']:
                if exec.get('orderId') == order_id:
                    try:
                        exec_price = float(exec.get('execPrice', 0))
                        exec_qty = float(exec.get('execQty', 0))
                        if exec_price > 0 and exec_qty > 0:
                            executed_qty += exec_qty
                            executed_cost += exec_price * exec_qty
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
            # Если не нашли исполнение, используем расчетные данные
            return {
                'quantity': quantity,
                'price': current_price,
                'cost': usdt_amount,
                'order_id': order_id
            }, None
            
    except Exception as e:
        logger.error(f"Ошибка покупки: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None, f"Ошибка при покупке: {str(e)}"

def check_user(username: str) -> bool:
    """Проверка доступа пользователя"""
    return username == ALLOWED_USER

# --- ОБРАБОТЧИКИ TELEGRAM ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user = update.effective_user
    logger.info(f"Пользователь: @{user.username} (ID: {user.id})")
    
    if not check_user(user.username):
        await update.message.reply_text(f"❌ У вас нет доступа. Ваш username: @{user.username}")
        return
    
    bybit_ok, bybit_msg = check_api_keys()
    
    keyboard = [["💰 Мой Портфель", "📊 Куплено по DCA"], ["🛒 Купить по DCA"]]
    markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, row_width=2)
    
    welcome_text = (
        f"👋 Добро пожаловать, {user.first_name}!\n\n"
        f"{bybit_msg}\n\n"
        f"📁 DCA: {dca_data['total_quantity']:.4f} TON\n"
        f"Выберите действие:"
    )
    
    await update.message.reply_text(welcome_text, reply_markup=markup)

async def show_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать детальный портфель"""
    user = update.effective_user
    if not check_user(user.username):
        return
    
    if not session:
        await update.message.reply_text("❌ Bybit API не настроен. Добавьте BYBIT_API_KEY и BYBIT_API_SECRET в файл .env")
        return
    
    status_msg = await update.message.reply_text("🔄 Загружаю данные портфеля...")
    
    try:
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(get_detailed_portfolio)
            portfolio, total_value, total_pnl, total_pnl_pct, total_invested = future.result()
    except Exception as e:
        logger.error(f"Ошибка в потоке: {e}")
        await status_msg.edit_text(f"❌ Ошибка загрузки: {str(e)}")
        return
    
    if not portfolio:
        await status_msg.edit_text("📭 Портфель пуст или не удалось загрузить данные")
        return
    
    # Формируем ответ
    response = "💰 **ДЕТАЛЬНЫЙ ПОРТФЕЛЬ**\n\n"
    response += f"📊 **Общая стоимость:** {total_value:.2f} USDT\n"
    response += f"💵 **Всего инвестировано:** {total_invested:.2f} USDT\n"
    
    if total_pnl != 0:
        pnl_emoji = "📈" if total_pnl > 0 else "📉"
        response += f"{pnl_emoji} **Общий PnL:** {total_pnl:+.2f} USDT ({total_pnl_pct:+.2f}%)\n"
    
    response += "\n" + "═" * 30 + "\n\n"
    
    for item in portfolio:
        coin = item['coin']
        balance = item['balance']
        
        if coin == 'USDT':
            response += f"💵 **USDT**\n"
            response += f"   Баланс: {balance:.2f}\n\n"
        else:
            coin_emoji = "💎"
            response += f"{coin_emoji} **{coin}**\n"
            response += f"   Количество: {balance:.4f}\n"
            
            if item.get('avg_price'):
                response += f"   Средняя цена: {item['avg_price']:.4f} USDT\n"
            
            if item['current_price'] > 0:
                response += f"   Текущая цена: {item['current_price']:.4f} USDT\n"
                response += f"   Стоимость: {item['current_value']:.2f} USDT\n"
                
                if item['pnl'] != 0:
                    pnl_emoji = "✅" if item['pnl'] > 0 else "❌"
                    response += f"   {pnl_emoji} PnL: {item['pnl']:+.2f} USDT ({item['pnl_percentage']:+.2f}%)\n"
            
            if total_value > 0 and item['current_value'] > 0:
                percentage = (item['current_value'] / total_value) * 100
                response += f"   Доля: {percentage:.1f}%\n"
            
            response += "\n"
    
    # DCA статистика
    if dca_data['total_quantity'] > 0:
        response += "═" * 30 + "\n"
        response += "📊 **DCA Статистика:**\n"
        response += f"   TON в DCA: {dca_data['total_quantity']:.4f}\n"
        response += f"   Средняя цена DCA: {dca_data['avg_price']:.4f} USDT\n"
        
        ton_item = next((p for p in portfolio if p['coin'] == 'TON'), None)
        if ton_item and ton_item['current_price'] > 0:
            dca_value = dca_data['total_quantity'] * ton_item['current_price']
            dca_pnl = dca_value - dca_data['total_cost']
            dca_pnl_pct = (dca_pnl / dca_data['total_cost']) * 100 if dca_data['total_cost'] > 0 else 0
            dca_emoji = "✅" if dca_pnl > 0 else "⏳"
            response += f"   {dca_emoji} DCA PnL: {dca_pnl:+.2f} USDT ({dca_pnl_pct:+.2f}%)\n"
    
    try:
        await status_msg.edit_text(response, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения: {e}")
        # Пробуем без markdown
        await status_msg.edit_text(response.replace('**', '').replace('═', '='))

async def show_dca_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать статистику DCA"""
    user = update.effective_user
    if not check_user(user.username):
        return
    
    if not dca_data['start_time'] or dca_data['total_quantity'] == 0:
        await update.message.reply_text("📊 Стратегия DCA еще не начата. Нажмите '🛒 Купить по DCA' для первой покупки.")
        return
    
    if not session:
        await update.message.reply_text("❌ Bybit API не настроен")
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
        response += f"\n\n💡 Рекомендация: прибыль {pnl_percentage:+.2f}%. Рассмотрите продажу."
    
    await update.message.reply_text(response, parse_mode='Markdown')

async def buy_dca_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало покупки по DCA"""
    user = update.effective_user
    if not check_user(user.username):
        return ConversationHandler.END
    
    if not session:
        await update.message.reply_text("❌ Bybit API не настроен. Добавьте ключи в .env файл:\n\nBYBIT_API_KEY=...\nBYBIT_API_SECRET=...")
        return ConversationHandler.END
    
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
    """Обработка покупки"""
    try:
        amount = float(update.message.text.strip())
        
        if amount < 1:
            await update.message.reply_text("❌ Минимальная сумма: 1 USDT")
            return WAITING_FOR_AMOUNT
        
        status_msg = await update.message.reply_text("🔄 Размещаю ордер на покупку...")
        
        try:
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(buy_ton, amount)
                result, error = future.result()
        except Exception as e:
            logger.error(f"Ошибка в потоке покупки: {e}")
            await status_msg.edit_text(f"❌ Ошибка: {str(e)}")
            return ConversationHandler.END
        
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
        await update.message.reply_text("❌ Пожалуйста, введите число")
        return WAITING_FOR_AMOUNT
    except Exception as e:
        logger.error(f"Ошибка обработки покупки: {e}")
        import traceback
        logger.error(traceback.format_exc())
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")
        return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена операции"""
    await update.message.reply_text("❌ Операция отменена")
    return ConversationHandler.END

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка кнопок меню"""
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
        await update.message.reply_text("Используйте кнопки меню")

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
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))

# --- FLASK WEB SERVER ---
app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():
    """Обработка обновлений от Telegram"""
    try:
        data = request.get_json(force=True)
        logger.debug(f"Получено обновление: {data}")
        
        update = Update.de_json(data, application.bot)
        
        # Обработка в event loop
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(application.process_update(update))
            else:
                loop.run_until_complete(application.process_update(update))
        except RuntimeError:
            # Новый loop
            asyncio.run(application.process_update(update))
        
        return 'OK', 200
        
    except Exception as e:
        logger.error(f"Ошибка webhook: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return 'Error', 500

@app.route('/')
def index():
    """Главная страница"""
    return "✅ DCA Bot работает!<br>Bybit: " + ("✅" if session else "❌"), 200

@app.route('/health')
def health():
    """Health check"""
    return {
        "status": "ok",
        "bybit": "connected" if session else "disconnected",
        "dca_quantity": dca_data['total_quantity'],
        "timestamp": datetime.now().isoformat()
    }, 200

# --- ТОЧКА ВХОДА ---
if __name__ == '__main__':
    load_dca_data()
    
    logger.info("=" * 60)
    logger.info("ЗАПУСК БОТА")
    logger.info("=" * 60)
    
    # Проверяем все переменные окружения для отладки
    logger.info("Все переменные окружения:")
    for key in sorted(os.environ.keys()):
        if any(x in key.upper() for x in ['TOKEN', 'API', 'KEY', 'SECRET', 'URL', 'PORT']):
            val = os.environ[key]
            # Маскируем секреты
            if 'SECRET' in key or 'TOKEN' in key or 'KEY' in key:
                display = val[:10] + "..." if len(val) > 15 else val
            else:
                display = val
            logger.info(f"  {key}: {display}")
    
    logger.info("=" * 60)
    
    # Установка webhook
    webhook_url = os.getenv('WEBHOOK_URL', '').strip()
    
    # Если WEBHOOK_URL пустой, пробуем другие варианты
    if not webhook_url:
        # Bothost может дать домен через другие переменные
        app_domain = os.getenv('APP_DOMAIN') or os.getenv('DOMAIN') or os.getenv('HOST')
        if app_domain:
            webhook_url = f"https://{app_domain}/webhook"
            logger.info(f"Собран URL из домена: {app_domain}")
    
    if not webhook_url:
        # Пробуем получить из запроса (не работает на старте, но оставляем для совместимости)
        logger.warning("WEBHOOK_URL не найден, пробуем использовать APP_URL")
        app_url = os.getenv('APP_URL', '')
        if app_url:
            webhook_url = f"{app_url.rstrip('/')}/webhook"
    
    if webhook_url:
        # Убеждаемся что URL правильный
        if not webhook_url.endswith('/webhook'):
            webhook_url = f"{webhook_url.rstrip('/')}/webhook"
        
        logger.info(f"Установка webhook: {webhook_url[:60]}...")
        
        async def setup_webhook():
            try:
                await application.initialize()
                await application.bot.set_webhook(
                    url=webhook_url,
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=True
                )
                logger.info("✅ Webhook установлен успешно!")
            except Exception as e:
                logger.error(f"❌ Ошибка установки webhook: {e}")
                import traceback
                logger.error(traceback.format_exc())
        
        try:
            asyncio.run(setup_webhook())
        except Exception as e:
            logger.error(f"Критическая ошибка при setup: {e}")
    else:
        logger.error("❌ Не удалось определить WEBHOOK_URL!")
        logger.error("Бот будет работать, но webhook не установлен.")
        logger.error("Установите переменную WEBHOOK_URL вручную или проверьте настройки Bothost.")
    
    # Запуск Flask
    port = int(os.environ.get('PORT', 8443))
    logger.info(f"Запуск Flask на порту {port}")
    logger.info("=" * 60)
    
    # threaded=True важно!
    app.run(host='0.0.0.0', port=port, threaded=True)