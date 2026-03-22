#!/usr/bin/env python3
"""
DCA Bybit Trading Bot - МАРТИНГЕЙЛ ЛЕСЕНКОЙ
С линейным ростом коэффициента от 0 до 3

ver 26.3.23
"""

import os
import sys
import asyncio
import logging
import json
import sqlite3
import re
import time
import aiohttp
import socket
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from typing import Dict, List, Optional, Tuple
from colorama import init, Fore, Style

from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)
from telegram.request import HTTPXRequest
from pybit.unified_trading import HTTP

# Для Windows - устанавливаем политику событий
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Инициализация colorama для цветного вывода
init(autoreset=True)

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot_errors.log", encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Константы
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
AUTHORIZED_USER = os.getenv('AUTHORIZED_USER', '@bosdima')
BYBIT_API_KEY = os.getenv('BYBIT_API_KEY')
BYBIT_API_SECRET = os.getenv('BYBIT_API_SECRET')
BYBIT_TESTNET = os.getenv('BYBIT_TESTNET', 'false').lower() == 'true'

# Состояния для ConversationHandler
(
    SELECTING_ACTION,
    SET_SYMBOL,
    SET_SYMBOL_MANUAL,
    SET_AMOUNT,
    SET_PROFIT_PERCENT,
    SET_MAX_DROP,
    SET_SCHEDULE_TIME,
    SET_FREQUENCY_HOURS,
    MANAGE_ORDERS,
    EDIT_ORDER_PRICE,
    MANUAL_BUY_PRICE,
    MANUAL_BUY_AMOUNT,
    MANUAL_ADD_PRICE,
    MANUAL_ADD_AMOUNT,
    EDIT_PURCHASE_SELECT,
    EDIT_PRICE,
    EDIT_AMOUNT,
    EDIT_DATE,
    DELETE_CONFIRM,
    SETTINGS_MENU,
    NOTIFICATION_SETTINGS_MENU,
    WAITING_ALERT_PERCENT,
    WAITING_ALERT_INTERVAL,
    WAITING_IMPORT_FILE,
    SELECTING_SYMBOL,
    LADDER_MENU,
    SET_LADDER_START_PRICE,
    SET_LADDER_STEP_PERCENT,
    SET_LADDER_DEPTH,
    SET_LADDER_BASE_AMOUNT,
) = range(30)

# Файл для экспорта/импорта
DB_EXPORT_FILE = 'dca_data_export.json'

# Список популярных токенов
POPULAR_SYMBOLS = ["TONUSDT", "BTCUSDT", "ETHUSDT"]

# Максимальная глубина просадки (%)
MAX_DROP_DEPTH = 80
# Шаг падения (%)
STEP_PERCENT = 3


def format_price(price: float, decimals: int = 4) -> str:
    """Форматирование цены с указанным количеством знаков после запятой"""
    if price is None:
        return "N/A"
    return f"{price:.{decimals}f}"


def format_quantity(qty: float, decimals: int = 6) -> str:
    """Форматирование количества с указанным количеством знаков после запятой"""
    if qty is None:
        return "N/A"
    return f"{qty:.{decimals}f}"


def calculate_ladder_amounts(base_amount: float, max_amount: float, steps_count: int) -> List[float]:
    """
    Рассчитать суммы для каждого шага лестницы с линейным ростом коэффициента от 0 до 3
    """
    amounts = []
    if steps_count <= 0:
        return amounts
    
    # Первый шаг - базовая сумма (коэффициент 1)
    # Но по стратегии на шаге 0 коэффициент 0, сумма 1.10 (базовая)
    # На последнем шаге коэффициент 3, сумма = базовая * 3
    
    for i in range(steps_count):
        # Линейный рост коэффициента от 0 до 3
        if steps_count == 1:
            ratio = 1
        else:
            ratio = i / (steps_count - 1) * 3  # от 0 до 3
        # Если ratio < 0.1, то базовая сумма
        if ratio < 0.1:
            amount = base_amount
        else:
            amount = base_amount * ratio
        amounts.append(amount)
    
    # Ограничиваем максимальной суммой
    amounts = [min(a, max_amount) for a in amounts]
    return amounts


def get_ladder_levels(drop_percent: float) -> Tuple[int, float]:
    """
    Определить уровень лестницы по проценту падения
    Возвращает (уровень, коэффициент)
    """
    if drop_percent <= 0:
        return 0, 0
    # Уровень = округление вниз (падение / шаг)
    level = int(drop_percent / STEP_PERCENT)
    # Ограничиваем максимальным уровнем (до 80%)
    max_level = int(MAX_DROP_DEPTH / STEP_PERCENT)
    if level > max_level:
        level = max_level
    
    # Линейный коэффициент от 0 до 3
    if max_level == 0:
        ratio = 0
    else:
        ratio = (level / max_level) * 3
    ratio = min(ratio, 3.0)
    
    return level, ratio


# ==================== DATABASE ====================

class Database:
    def __init__(self, db_file: str = "dca_bot.db"):
        self.db_file = db_file
        self.settings_cache = {}
        self.cache_ttl = 60
        self.cache_timestamp = 0
        self.init_db()
    
    def init_db(self):
        """Инициализация БД"""
        try:
            conn = sqlite3.connect(self.db_file, timeout=10)
            cursor = conn.cursor()
            
            # Таблица настроек
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Таблица DCA покупок
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS dca_purchases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    amount_usdt REAL NOT NULL,
                    price REAL NOT NULL,
                    quantity REAL NOT NULL,
                    multiplier REAL DEFAULT 1.0,
                    drop_percent REAL DEFAULT 0,
                    step_level INTEGER DEFAULT 0,
                    date TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Таблица активных ордеров
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sell_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    order_id TEXT NOT NULL UNIQUE,
                    quantity REAL NOT NULL,
                    target_price REAL NOT NULL,
                    profit_percent REAL NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status TEXT DEFAULT 'active'
                )
            ''')
            
            # Таблица истории
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    symbol TEXT,
                    details TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Таблица старта DCA
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS dca_start (
                    id INTEGER PRIMARY KEY,
                    start_date TIMESTAMP,
                    symbol TEXT,
                    initial_price REAL
                )
            ''')
            
            # Таблица уведомлений
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    enabled BOOLEAN DEFAULT 1,
                    alert_percent REAL DEFAULT 10.0,
                    alert_interval_minutes INTEGER DEFAULT 30,
                    last_check TIMESTAMP
                )
            ''')
            
            # Таблица настроек лестницы
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS ladder_settings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    start_price REAL NOT NULL,
                    step_percent REAL NOT NULL,
                    max_depth REAL NOT NULL,
                    base_amount REAL NOT NULL,
                    max_amount REAL NOT NULL,
                    current_drop_percent REAL DEFAULT 0,
                    last_buy_price REAL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Дефолтные настройки
            defaults = [
                ('symbol', 'TONUSDT'),
                ('invest_amount', '1.1'),
                ('profit_percent', '5'),
                ('max_drop_percent', '60'),
                ('max_multiplier', '3'),
                ('schedule_time', '09:00'),
                ('frequency_hours', '24'),
                ('price_alert_enabled', 'false'),
                ('dca_active', 'false'),
                ('last_purchase_price', '0'),
                ('initial_reference_price', '0'),
                ('last_purchase_time', '0'),
                ('ladder_base_amount', '1.1'),
                ('ladder_step_percent', '3'),
                ('ladder_max_depth', '80'),
                ('ladder_max_amount', '3.3'),
                ('ladder_start_price', '0'),
            ]
            
            for key, value in defaults:
                cursor.execute('''
                    INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)
                ''', (key, value))
            
            cursor.execute('''
                INSERT OR IGNORE INTO notifications (id, enabled, alert_percent, alert_interval_minutes, last_check)
                VALUES (1, 1, 10.0, 30, CURRENT_TIMESTAMP)
            ''')
            
            conn.commit()
            conn.close()
            logger.info(f"Database initialized successfully at {self.db_file}")
        except Exception as e:
            logger.error(f"DB init error: {e}")
    
    def get_setting(self, key: str, default: str = '') -> str:
        """Получить настройку"""
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('SELECT value FROM settings WHERE key = ?', (key,))
            result = cursor.fetchone()
            conn.close()
            return result[0] if result else default
        except Exception:
            return default
    
    def set_setting(self, key: str, value: str):
        """Установить настройку"""
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO settings (key, value, updated_at) 
                VALUES (?, ?, CURRENT_TIMESTAMP)
            ''', (key, value))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error setting {key}: {e}")
    
    def get_notification_settings(self) -> Dict:
        """Получить настройки уведомлений"""
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('SELECT enabled, alert_percent, alert_interval_minutes FROM notifications WHERE id = 1')
            row = cursor.fetchone()
            conn.close()
            if row:
                return {'enabled': bool(row[0]), 'alert_percent': row[1], 'alert_interval_minutes': row[2]}
            return {'enabled': True, 'alert_percent': 10.0, 'alert_interval_minutes': 30}
        except Exception as e:
            logger.error(f"Error getting notification settings: {e}")
            return {'enabled': True, 'alert_percent': 10.0, 'alert_interval_minutes': 30}
    
    def update_notification_settings(self, **kwargs):
        """Обновить настройки уведомлений"""
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            updates = []
            values = []
            if 'enabled' in kwargs:
                updates.append("enabled = ?")
                values.append(1 if kwargs['enabled'] else 0)
            if 'alert_percent' in kwargs:
                updates.append("alert_percent = ?")
                values.append(kwargs['alert_percent'])
            if 'alert_interval_minutes' in kwargs:
                updates.append("alert_interval_minutes = ?")
                values.append(kwargs['alert_interval_minutes'])
            if updates:
                values.append(1)
                cursor.execute(f"UPDATE notifications SET {', '.join(updates)}, last_check = CURRENT_TIMESTAMP WHERE id = ?", values)
                conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error updating notification settings: {e}")
    
    def add_purchase(self, symbol: str, amount_usdt: float, price: float, 
                     quantity: float, multiplier: float = 1.0, drop_percent: float = 0,
                     step_level: int = 0, date: str = None):
        """Добавить покупку"""
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO dca_purchases 
                (symbol, amount_usdt, price, quantity, multiplier, drop_percent, step_level, date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (symbol, amount_usdt, price, quantity, multiplier, drop_percent, step_level, date))
            purchase_id = cursor.lastrowid
            conn.commit()
            conn.close()
            return purchase_id
        except Exception as e:
            logger.error(f"Error adding purchase: {e}")
            return None
    
    def get_purchases(self, symbol: str = None) -> List[Dict]:
        """Получить все покупки"""
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if symbol:
                cursor.execute('SELECT * FROM dca_purchases WHERE symbol = ? ORDER BY date ASC', (symbol,))
            else:
                cursor.execute('SELECT * FROM dca_purchases ORDER BY date ASC')
            rows = cursor.fetchall()
            conn.close()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Error getting purchases: {e}")
            return []
    
    def get_purchase_by_id(self, purchase_id: int) -> Optional[Dict]:
        """Получить покупку по ID"""
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM dca_purchases WHERE id = ?', (purchase_id,))
            row = cursor.fetchone()
            conn.close()
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"Error getting purchase {purchase_id}: {e}")
            return None
    
    def update_purchase(self, purchase_id: int, **kwargs) -> bool:
        """Обновить поля покупки"""
        allowed_fields = ['symbol', 'amount_usdt', 'price', 'quantity', 'multiplier', 'drop_percent', 'step_level', 'date']
        updates = []
        values = []
        for key, value in kwargs.items():
            if key in allowed_fields:
                updates.append(f"{key} = ?")
                values.append(value)
        if not updates:
            return False
        values.append(purchase_id)
        query = f"UPDATE dca_purchases SET {', '.join(updates)} WHERE id = ?"
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute(query, values)
            success = cursor.rowcount > 0
            conn.commit()
            conn.close()
            return success
        except Exception as e:
            logger.error(f"Error updating purchase {purchase_id}: {e}")
            return False
    
    def delete_purchase(self, purchase_id: int) -> bool:
        """Удалить покупку"""
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM dca_purchases WHERE id = ?', (purchase_id,))
            success = cursor.rowcount > 0
            conn.commit()
            conn.close()
            return success
        except Exception as e:
            logger.error(f"Error deleting purchase {purchase_id}: {e}")
            return False
    
    def get_dca_stats(self, symbol: str) -> Dict:
        """Получить статистику DCA"""
        purchases = self.get_purchases(symbol)
        if not purchases:
            return None
        total_usdt = sum(p['amount_usdt'] for p in purchases)
        total_qty = sum(p['quantity'] for p in purchases)
        avg_price = total_usdt / total_qty if total_qty > 0 else 0
        return {
            'total_purchases': len(purchases),
            'total_usdt': total_usdt,
            'total_quantity': total_qty,
            'avg_price': avg_price,
        }
    
    def add_sell_order(self, symbol: str, order_id: str, quantity: float, 
                       target_price: float, profit_percent: float):
        """Добавить ордер на продажу"""
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            try:
                cursor.execute('''
                    INSERT INTO sell_orders (symbol, order_id, quantity, target_price, profit_percent)
                    VALUES (?, ?, ?, ?, ?)
                ''', (symbol, order_id, quantity, target_price, profit_percent))
                conn.commit()
            except sqlite3.IntegrityError:
                cursor.execute('''
                    UPDATE sell_orders SET target_price = ?, profit_percent = ?, status = 'active'
                    WHERE order_id = ?
                ''', (target_price, profit_percent, order_id))
                conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error adding sell order: {e}")
    
    def get_active_sell_orders(self, symbol: str = None) -> List[Dict]:
        """Получить активные ордера на продажу"""
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if symbol:
                cursor.execute('SELECT * FROM sell_orders WHERE symbol = ? AND status = "active" ORDER BY created_at DESC', (symbol,))
            else:
                cursor.execute('SELECT * FROM sell_orders WHERE status = "active" ORDER BY created_at DESC')
            rows = cursor.fetchall()
            conn.close()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Error getting active sell orders: {e}")
            return []
    
    def update_sell_order_status(self, order_id: str, status: str):
        """Обновить статус ордера"""
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('UPDATE sell_orders SET status = ? WHERE order_id = ?', (status, order_id))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error updating order status: {e}")
    
    def update_order_price(self, order_id: str, new_price: float, new_profit_percent: float):
        """Обновить цену ордера"""
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('UPDATE sell_orders SET target_price = ?, profit_percent = ? WHERE order_id = ?', 
                          (new_price, new_profit_percent, order_id))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error updating order price: {e}")
    
    def log_action(self, action: str, symbol: str = None, details: str = None):
        """Логировать действие"""
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('INSERT INTO history (action, symbol, details) VALUES (?, ?, ?)', (action, symbol, details))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error logging action: {e}")
    
    def set_dca_start(self, symbol: str, initial_price: float):
        """Установить дату старта DCA"""
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM dca_start')
            cursor.execute('INSERT INTO dca_start (id, start_date, symbol, initial_price) VALUES (1, CURRENT_TIMESTAMP, ?, ?)', 
                          (symbol, initial_price))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error setting dca start: {e}")
    
    def get_dca_start(self) -> Optional[Dict]:
        """Получить информацию о старте DCA"""
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM dca_start WHERE id = 1')
            row = cursor.fetchone()
            conn.close()
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"Error getting dca start: {e}")
            return None
    
    # ============= МЕТОДЫ ДЛЯ ЛЕСТНИЦЫ =============
    
    def get_ladder_settings(self, symbol: str = None) -> Dict:
        """Получить настройки лестницы"""
        if symbol is None:
            symbol = self.get_setting('symbol', 'TONUSDT')
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM ladder_settings WHERE symbol = ? ORDER BY created_at DESC LIMIT 1', (symbol,))
            row = cursor.fetchone()
            conn.close()
            if row:
                return dict(row)
            else:
                return {
                    'symbol': symbol,
                    'start_price': float(self.get_setting('ladder_start_price', '0')),
                    'step_percent': float(self.get_setting('ladder_step_percent', '3')),
                    'max_depth': float(self.get_setting('ladder_max_depth', '80')),
                    'base_amount': float(self.get_setting('ladder_base_amount', '1.1')),
                    'max_amount': float(self.get_setting('ladder_max_amount', '3.3')),
                    'current_drop_percent': 0,
                    'last_buy_price': float(self.get_setting('ladder_start_price', '0')) if float(self.get_setting('ladder_start_price', '0')) > 0 else None
                }
        except Exception as e:
            logger.error(f"Error getting ladder settings: {e}")
            return {
                'symbol': symbol,
                'start_price': 0,
                'step_percent': 3,
                'max_depth': 80,
                'base_amount': 1.1,
                'max_amount': 3.3,
                'current_drop_percent': 0,
                'last_buy_price': None
            }
    
    def save_ladder_settings(self, settings: Dict):
        """Сохранить настройки лестницы"""
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM ladder_settings WHERE symbol = ?', (settings['symbol'],))
            cursor.execute('''
                INSERT INTO ladder_settings 
                (symbol, start_price, step_percent, max_depth, base_amount, max_amount, current_drop_percent, last_buy_price)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                settings['symbol'],
                settings['start_price'],
                settings['step_percent'],
                settings['max_depth'],
                settings['base_amount'],
                settings['max_amount'],
                settings.get('current_drop_percent', 0),
                settings.get('last_buy_price', None)
            ))
            conn.commit()
            conn.close()
            
            self.set_setting('ladder_start_price', str(settings['start_price']))
            self.set_setting('ladder_step_percent', str(settings['step_percent']))
            self.set_setting('ladder_max_depth', str(settings['max_depth']))
            self.set_setting('ladder_base_amount', str(settings['base_amount']))
            self.set_setting('ladder_max_amount', str(settings['max_amount']))
        except Exception as e:
            logger.error(f"Error saving ladder settings: {e}")
    
    def calculate_ladder_purchase(self, current_price: float, symbol: str = None) -> Dict:
        """Рассчитать параметры покупки по лестнице"""
        if symbol is None:
            symbol = self.get_setting('symbol', 'TONUSDT')
        
        settings = self.get_ladder_settings(symbol)
        
        if settings['start_price'] <= 0:
            return {
                'should_buy': False,
                'step_level': 0,
                'amount_usdt': 0,
                'target_price': 0,
                'reason': 'Не задана начальная цена лестницы'
            }
        
        # Получаем последнюю покупку
        purchases = self.get_purchases(symbol)
        last_purchases = sorted([p for p in purchases if p.get('step_level', 0) > 0], key=lambda x: x['date'], reverse=True)
        
        # Определяем текущий максимальный уровень падения
        max_drop = 0
        last_buy_price = settings['start_price']
        
        for p in last_purchases:
            if p.get('drop_percent', 0) > max_drop:
                max_drop = p.get('drop_percent', 0)
                last_buy_price = p['price']
        
        # Если нет покупок, считаем от стартовой цены
        if max_drop == 0:
            last_buy_price = settings['start_price']
        
        # Вычисляем текущий процент падения от стартовой цены
        current_drop = ((settings['start_price'] - current_price) / settings['start_price']) * 100
        
        # Определяем следующий уровень для покупки
        # Покупаем при каждом достижении нового уровня падения (кратного шагу)
        step_percent = settings['step_percent']
        next_level = int((max_drop + step_percent) / step_percent) * step_percent
        
        if next_level <= 0:
            next_level = step_percent
        
        # Проверяем, достигли ли мы следующего уровня
        if current_drop >= next_level and next_level > max_drop:
            # Определяем коэффициент для суммы
            level, ratio = get_ladder_levels(next_level)
            
            # Вычисляем сумму покупки
            base_amount = settings['base_amount']
            max_amount = settings['max_amount']
            
            # Линейный рост суммы от base_amount до max_amount
            if ratio >= 3:
                amount_usdt = max_amount
            elif ratio <= 0:
                amount_usdt = base_amount
            else:
                amount_usdt = base_amount + (max_amount - base_amount) * (ratio / 3)
            
            # Ограничиваем максимальной суммой
            amount_usdt = min(amount_usdt, max_amount)
            
            # Проверяем, не превышен ли лимит глубины
            if next_level > settings['max_depth']:
                return {
                    'should_buy': False,
                    'step_level': level,
                    'amount_usdt': amount_usdt,
                    'target_price': current_price,
                    'reason': f'Достигнута максимальная глубина ({settings["max_depth"]}%)'
                }
            
            return {
                'should_buy': True,
                'step_level': level,
                'amount_usdt': amount_usdt,
                'target_price': current_price,
                'drop_percent': next_level,
                'reason': f'Падение {next_level:.1f}%, коэффициент {ratio:.2f}'
            }
        
        # Следующая цена для покупки
        next_price = settings['start_price'] * (1 - next_level / 100)
        
        return {
            'should_buy': False,
            'step_level': 0,
            'amount_usdt': 0,
            'target_price': next_price,
            'current_drop': current_drop,
            'next_drop': next_level,
            'reason': f'Ждем падения до {next_level:.1f}% ({format_price(next_price)})'
        }
    
    def get_ladder_summary(self, symbol: str = None) -> Dict:
        """Получить сводку по лестнице"""
        if symbol is None:
            symbol = self.get_setting('symbol', 'TONUSDT')
        
        settings = self.get_ladder_settings(symbol)
        purchases = self.get_purchases(symbol)
        
        # Группируем покупки по уровням падения
        levels = {}
        for p in purchases:
            drop = p.get('drop_percent', 0)
            if drop > 0:
                level = int(drop / settings['step_percent'])
                if level not in levels:
                    levels[level] = []
                levels[level].append(p)
        
        # Создаем список всех шагов
        max_level = int(settings['max_depth'] / settings['step_percent'])
        steps = []
        
        for i in range(max_level + 1):
            drop_percent = i * settings['step_percent']
            level, ratio = get_ladder_levels(drop_percent)
            
            # Вычисляем сумму для этого шага
            base_amount = settings['base_amount']
            max_amount = settings['max_amount']
            if ratio >= 3:
                amount = max_amount
            elif ratio <= 0:
                amount = base_amount
            else:
                amount = base_amount + (max_amount - base_amount) * (ratio / 3)
            
            # Проверяем, была ли покупка на этом уровне
            if i in levels:
                step_purchases = levels[i]
                total_amount = sum(p['amount_usdt'] for p in step_purchases)
                total_qty = sum(p['quantity'] for p in step_purchases)
                avg_price = total_amount / total_qty if total_qty > 0 else 0
                steps.append({
                    'step': i + 1,
                    'drop_percent': drop_percent,
                    'ratio': ratio,
                    'price': avg_price,
                    'amount': amount,
                    'quantity': total_qty,
                    'status': 'completed'
                })
            else:
                target_price = settings['start_price'] * (1 - drop_percent / 100)
                steps.append({
                    'step': i + 1,
                    'drop_percent': drop_percent,
                    'ratio': ratio,
                    'price': target_price,
                    'amount': amount,
                    'quantity': 0,
                    'status': 'pending'
                })
        
        # Находим текущий максимальный уровень
        current_drop = max([p.get('drop_percent', 0) for p in purchases], default=0)
        current_step = int(current_drop / settings['step_percent']) if current_drop > 0 else 0
        
        return {
            'symbol': symbol,
            'start_price': settings['start_price'],
            'step_percent': settings['step_percent'],
            'max_depth': settings['max_depth'],
            'base_amount': settings['base_amount'],
            'max_amount': settings['max_amount'],
            'current_step': current_step,
            'current_drop': current_drop,
            'steps': steps
        }
    
    def reset_ladder(self, symbol: str = None):
        """Сбросить лестницу"""
        if symbol is None:
            symbol = self.get_setting('symbol', 'TONUSDT')
        settings = self.get_ladder_settings(symbol)
        settings['current_drop_percent'] = 0
        settings['last_buy_price'] = settings['start_price']
        self.save_ladder_settings(settings)
    
    def export_database(self) -> Tuple[bool, int, str]:
        """Экспорт базы данных"""
        try:
            purchases = self.get_purchases()
            sell_orders = self.get_active_sell_orders()
            settings = {}
            
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('SELECT key, value FROM settings')
            for key, value in cursor.fetchall():
                settings[key] = value
            
            cursor.execute('SELECT enabled, alert_percent, alert_interval_minutes FROM notifications WHERE id = 1')
            notification_row = cursor.fetchone()
            notifications = {
                'enabled': bool(notification_row[0]) if notification_row else True,
                'alert_percent': notification_row[1] if notification_row else 10.0,
                'alert_interval_minutes': notification_row[2] if notification_row else 30
            }
            
            cursor.execute('SELECT start_date, symbol, initial_price FROM dca_start WHERE id = 1')
            dca_start_row = cursor.fetchone()
            dca_start = {
                'start_date': dca_start_row[0] if dca_start_row else None,
                'symbol': dca_start_row[1] if dca_start_row else None,
                'initial_price': dca_start_row[2] if dca_start_row else None
            } if dca_start_row else None
            
            cursor.execute('SELECT * FROM ladder_settings')
            ladder_settings = [dict(row) for row in cursor.fetchall()] if cursor.description else []
            
            conn.close()
            
            export_data = {
                'export_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'version': '1.2',
                'purchases': purchases,
                'sell_orders': sell_orders,
                'settings': settings,
                'notifications': notifications,
                'dca_start': dca_start,
                'ladder_settings': ladder_settings
            }
            
            with open(DB_EXPORT_FILE, 'w', encoding='utf-8') as f:
                json.dump(export_data, f, indent=2, ensure_ascii=False, default=str)
            
            return True, len(purchases), DB_EXPORT_FILE
        except Exception as e:
            logger.error(f"Error exporting database: {e}")
            return False, 0, str(e)
    
    def import_database(self, file_path: str) -> Tuple[bool, str]:
        """Импорт базы данных"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            
            cursor.execute("DELETE FROM dca_purchases")
            cursor.execute("DELETE FROM sell_orders")
            cursor.execute("DELETE FROM settings")
            cursor.execute("DELETE FROM dca_start")
            cursor.execute("DELETE FROM ladder_settings")
            
            purchases_imported = 0
            for purchase in data.get('purchases', []):
                cursor.execute('''
                    INSERT INTO dca_purchases 
                    (id, symbol, amount_usdt, price, quantity, multiplier, drop_percent, step_level, date, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    purchase.get('id'),
                    purchase.get('symbol', 'TONUSDT'),
                    purchase.get('amount_usdt', 0),
                    purchase.get('price', 0),
                    purchase.get('quantity', 0),
                    purchase.get('multiplier', 1.0),
                    purchase.get('drop_percent', 0),
                    purchase.get('step_level', 0),
                    purchase.get('date', datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                    purchase.get('created_at', datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                ))
                purchases_imported += 1
            
            for order in data.get('sell_orders', []):
                try:
                    cursor.execute('''
                        INSERT OR IGNORE INTO sell_orders 
                        (id, symbol, order_id, quantity, target_price, profit_percent, created_at, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        order.get('id'),
                        order.get('symbol', 'TONUSDT'),
                        order.get('order_id', f"imported_{order.get('id', 0)}"),
                        order.get('quantity', 0),
                        order.get('target_price', 0),
                        order.get('profit_percent', 5),
                        order.get('created_at', datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                        order.get('status', 'active')
                    ))
                except:
                    pass
            
            for key, value in data.get('settings', {}).items():
                cursor.execute('INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)', (key, value))
            
            dca_start = data.get('dca_start')
            if dca_start and dca_start.get('start_date'):
                cursor.execute('INSERT OR REPLACE INTO dca_start (id, start_date, symbol, initial_price) VALUES (1, ?, ?, ?)',
                              (dca_start['start_date'], dca_start['symbol'], dca_start['initial_price']))
            
            notifications = data.get('notifications', {})
            if notifications:
                cursor.execute('''
                    UPDATE notifications SET enabled = ?, alert_percent = ?, alert_interval_minutes = ?, last_check = CURRENT_TIMESTAMP WHERE id = 1
                ''', (1 if notifications.get('enabled', True) else 0, notifications.get('alert_percent', 10.0), notifications.get('alert_interval_minutes', 30)))
            
            for ladder in data.get('ladder_settings', []):
                cursor.execute('''
                    INSERT OR REPLACE INTO ladder_settings 
                    (id, symbol, start_price, step_percent, max_depth, base_amount, max_amount, current_drop_percent, last_buy_price, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    ladder.get('id'),
                    ladder.get('symbol', 'TONUSDT'),
                    ladder.get('start_price', 0),
                    ladder.get('step_percent', 3),
                    ladder.get('max_depth', 80),
                    ladder.get('base_amount', 1.1),
                    ladder.get('max_amount', 3.3),
                    ladder.get('current_drop_percent', 0),
                    ladder.get('last_buy_price'),
                    ladder.get('created_at', datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                ))
            
            conn.commit()
            conn.close()
            return True, f"Импортировано: {purchases_imported} покупок"
        except Exception as e:
            logger.error(f"Error importing database: {e}")
            return False, str(e)


# ==================== BYBIT CLIENT ====================

class BybitClient:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = False):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.session = None
        self._price_cache = {}
        self._cache_time = {}
        self._cache_ttl = 5
        self._init_session()
    
    def _init_session(self):
        try:
            self.session = HTTP(testnet=self.testnet, api_key=self.api_key, api_secret=self.api_secret, recv_window=5000)
            logger.info("Bybit session initialized")
        except Exception as e:
            logger.error(f"Session init error: {e}")
    
    async def test_connection(self) -> Tuple[bool, str]:
        try:
            if not self.session:
                self._init_session()
            response = self.session.get_account_info()
            if response['retCode'] == 0:
                return True, "✅ Bybit API подключен"
            return False, f"❌ Bybit API ошибка: {response['retMsg']}"
        except Exception as e:
            return False, f"❌ Bybit API ошибка: {str(e)}"
    
    async def get_symbol_price(self, symbol: str) -> Optional[float]:
        now = time.time()
        if symbol in self._cache_time and now - self._cache_time.get(symbol, 0) < self._cache_ttl:
            return self._price_cache.get(symbol)
        try:
            if not self.session:
                self._init_session()
            response = self.session.get_tickers(category="spot", symbol=symbol)
            if response['retCode'] == 0 and response['result']['list']:
                price = float(response['result']['list'][0]['lastPrice'])
                self._price_cache[symbol] = price
                self._cache_time[symbol] = now
                return price
            return None
        except Exception as e:
            logger.error(f"Error getting price for {symbol}: {e}")
            return None
    
    async def get_balance(self, coin: str = None) -> Dict:
        try:
            if not self.session:
                self._init_session()
            response = self.session.get_wallet_balance(accountType="UNIFIED")
            if response['retCode'] == 0:
                result_list = response['result']['list']
                if result_list:
                    account_data = result_list[0]
                    coins = account_data.get('coin', [])
                    if coin:
                        for c in coins:
                            if c.get('coin') == coin:
                                wallet_balance = float(c.get('walletBalance', 0) or 0)
                                equity = float(c.get('equity', 0) or 0) or wallet_balance
                                locked = float(c.get('locked', 0) or 0)
                                available = wallet_balance - locked
                                usd_value = float(c.get('usdValue', 0) or 0)
                                return {'coin': coin, 'equity': equity, 'available': available, 'usdValue': usd_value}
                        return {'coin': coin, 'equity': 0, 'available': 0, 'usdValue': 0}
                    else:
                        return {'total_equity': float(account_data.get('totalEquity', 0) or 0), 'coins': coins}
            return {'error': 'Не удалось получить баланс'}
        except Exception as e:
            logger.error(f"Error in get_balance: {e}")
            return {'error': str(e)}
    
    async def get_open_orders(self, symbol: str = None) -> List[Dict]:
        """Получить открытые ордера"""
        try:
            if not self.session:
                self._init_session()
            params = {"category": "spot", "openOnly": 0}
            if symbol:
                params['symbol'] = symbol
            response = self.session.get_open_orders(**params)
            if response['retCode'] == 0:
                return response['result']['list']
            return []
        except Exception as e:
            logger.error(f"Error getting open orders: {e}")
            return []
    
    async def get_open_orders_by_side(self, symbol: str = None) -> Dict[str, List[Dict]]:
        """Получить открытые ордера с разделением по сторонам"""
        orders = await self.get_open_orders(symbol)
        buy_orders = [o for o in orders if o.get('side') == 'Buy']
        sell_orders = [o for o in orders if o.get('side') == 'Sell']
        return {'buy': buy_orders, 'sell': sell_orders}
    
    async def cancel_order(self, symbol: str, order_id: str) -> Dict:
        try:
            if not self.session:
                self._init_session()
            response = self.session.cancel_order(category="spot", symbol=symbol, orderId=order_id)
            if response['retCode'] == 0:
                return {'success': True}
            return {'success': False, 'error': response['retMsg']}
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    async def amend_order_price(self, symbol: str, order_id: str, new_price: float) -> Dict:
        try:
            if not self.session:
                self._init_session()
            response = self.session.amend_order(category="spot", symbol=symbol, orderId=order_id, price=str(new_price))
            if response['retCode'] == 0:
                return {'success': True}
            return {'success': False, 'error': response['retMsg']}
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    async def place_limit_sell(self, symbol: str, quantity: float, price: float) -> Dict:
        try:
            if not self.session:
                self._init_session()
            response = self.session.place_order(category="spot", symbol=symbol, side="Sell", orderType="Limit", qty=str(quantity), price=str(price), timeInForce="GTC")
            if response['retCode'] == 0:
                return {'success': True, 'order_id': response['result']['orderId'], 'quantity': quantity, 'price': price}
            return {'success': False, 'error': response['retMsg']}
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    async def place_market_buy(self, symbol: str, amount_usdt: float) -> Dict:
        try:
            if not self.session:
                self._init_session()
            instrument = self.session.get_instruments_info(category="spot", symbol=symbol)
            if instrument['retCode'] != 0:
                return {'success': False, 'error': instrument['retMsg']}
            lot_size = float(instrument['result']['list'][0]['lotSizeFilter']['basePrecision'])
            min_qty = float(instrument['result']['list'][0]['lotSizeFilter']['minOrderQty'])
            min_order_amt = float(instrument['result']['list'][0]['lotSizeFilter'].get('minOrderAmt', 1))
            if amount_usdt < min_order_amt:
                return {'success': False, 'error': f'Минимальная сумма: {min_order_amt} USDT'}
            price = await self.get_symbol_price(symbol)
            if not price:
                return {'success': False, 'error': 'Не удалось получить цену'}
            quantity = amount_usdt / price
            quantity = Decimal(str(quantity)).quantize(Decimal(str(lot_size)), rounding=ROUND_DOWN)
            if float(quantity) < min_qty:
                return {'success': False, 'error': f'Минимальное количество: {min_qty}'}
            response = self.session.place_order(category="spot", symbol=symbol, side="Buy", orderType="Market", qty=str(quantity))
            if response['retCode'] == 0:
                order_id = response['result']['orderId']
                await asyncio.sleep(1)
                order_details = self.session.get_order_history(category="spot", orderId=order_id)
                avg_price = price
                if order_details['retCode'] == 0 and order_details['result']['list']:
                    avg_price = float(order_details['result']['list'][0].get('avgPrice', price))
                return {'success': True, 'order_id': order_id, 'quantity': float(quantity), 'price': avg_price, 'total_usdt': amount_usdt}
            return {'success': False, 'error': response['retMsg']}
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    async def place_limit_buy(self, symbol: str, price: float, amount_usdt: float) -> Dict:
        try:
            if not self.session:
                self._init_session()
            instrument = self.session.get_instruments_info(category="spot", symbol=symbol)
            if instrument['retCode'] != 0:
                return {'success': False, 'error': instrument['retMsg']}
            lot_size = float(instrument['result']['list'][0]['lotSizeFilter']['basePrecision'])
            min_qty = float(instrument['result']['list'][0]['lotSizeFilter']['minOrderQty'])
            min_order_amt = float(instrument['result']['list'][0]['lotSizeFilter'].get('minOrderAmt', 1))
            if amount_usdt < min_order_amt:
                return {'success': False, 'error': f'Минимальная сумма: {min_order_amt} USDT'}
            quantity = amount_usdt / price
            quantity = Decimal(str(quantity)).quantize(Decimal(str(lot_size)), rounding=ROUND_DOWN)
            if float(quantity) < min_qty:
                return {'success': False, 'error': f'Минимальное количество: {min_qty}'}
            response = self.session.place_order(category="spot", symbol=symbol, side="Buy", orderType="Limit", qty=str(quantity), price=str(price), timeInForce="GTC")
            if response['retCode'] == 0:
                return {'success': True, 'order_id': response['result']['orderId'], 'quantity': float(quantity), 'price': price, 'total_usdt': amount_usdt}
            return {'success': False, 'error': response['retMsg']}
        except Exception as e:
            return {'success': False, 'error': str(e)}


# ==================== DCA STRATEGY ====================

class DCAStrategy:
    def __init__(self, db: Database, bybit: BybitClient):
        self.db = db
        self.bybit = bybit
    
    async def execute_ladder_purchase(self, symbol: str, profit_percent: float) -> Dict:
        """Выполнить покупку по лестнице"""
        current_price = await self.bybit.get_symbol_price(symbol)
        if not current_price:
            return {'success': False, 'error': 'Не удалось получить цену'}
        
        ladder_info = self.db.calculate_ladder_purchase(current_price, symbol)
        if not ladder_info['should_buy']:
            return {'success': False, 'error': ladder_info['reason']}
        
        amount_usdt = ladder_info['amount_usdt']
        drop_percent = ladder_info.get('drop_percent', 0)
        step_level = ladder_info['step_level']
        
        usdt_balance = await self.bybit.get_balance('USDT')
        available_usdt = usdt_balance.get('available', 0) if usdt_balance else 0
        
        if available_usdt < amount_usdt:
            return {'success': False, 'error': f'Недостаточно средств. Нужно {amount_usdt:.2f} USDT'}
        
        result = await self.bybit.place_market_buy(symbol, amount_usdt)
        
        if result['success']:
            current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.db.add_purchase(symbol=symbol, amount_usdt=result['total_usdt'], price=result['price'],
                                quantity=result['quantity'], multiplier=1.0, drop_percent=drop_percent,
                                step_level=step_level, date=current_date)
            self.db.set_setting('last_purchase_price', str(result['price']))
            self.db.set_setting('last_purchase_time', str(datetime.now().timestamp()))
            
            target_price_sell = result['price'] * (1 + profit_percent / 100)
            sell_result = await self.bybit.place_limit_sell(symbol, result['quantity'], target_price_sell)
            
            if sell_result['success']:
                self.db.add_sell_order(symbol=symbol, order_id=sell_result['order_id'],
                                      quantity=result['quantity'], target_price=target_price_sell,
                                      profit_percent=profit_percent)
                result['sell_order_id'] = sell_result['order_id']
                result['target_price'] = target_price_sell
            
            result['step_level'] = step_level
            result['amount_usdt'] = amount_usdt
            result['drop_percent'] = drop_percent
            
            self.db.log_action('LADDER_PURCHASE', symbol, f"Уровень {drop_percent:.1f}%: {result['total_usdt']:.2f} USDT")
        
        return result
    
    async def check_and_update_sell_orders(self, symbol: str):
        """Проверить статус ордеров"""
        active_orders = self.db.get_active_sell_orders(symbol)
        open_orders = await self.bybit.get_open_orders(symbol)
        open_order_ids = {o['orderId'] for o in open_orders}
        
        for order in active_orders:
            if order['order_id'] not in open_order_ids:
                self.db.update_sell_order_status(order['order_id'], 'completed')
                self.db.log_action('SELL_COMPLETED', symbol, f"Продано по {format_price(order['target_price'])}")
    
    async def get_recommended_purchase(self, symbol: str) -> Dict:
        """Получить рекомендацию для ручной покупки"""
        current_price = await self.bybit.get_symbol_price(symbol)
        if not current_price:
            return {'success': False, 'error': 'Не удалось получить цену'}
        
        ladder_info = self.db.calculate_ladder_purchase(current_price, symbol)
        
        if ladder_info['should_buy']:
            return {'success': True, 'should_buy': True, 'amount_usdt': ladder_info['amount_usdt'],
                   'step_level': ladder_info['step_level'], 'target_price': ladder_info['target_price'],
                   'drop_percent': ladder_info.get('drop_percent', 0), 'reason': ladder_info['reason'], 
                   'current_price': current_price}
        else:
            return {'success': True, 'should_buy': False, 'reason': ladder_info['reason'],
                   'current_price': current_price, 'next_buy_price': ladder_info['target_price']}
    
    def calculate_target_info(self, stats: Dict, profit_percent: float) -> Dict:
        """Рассчитать информацию о целевой продаже"""
        if not stats or stats['total_quantity'] <= 0:
            return None
        
        total_qty = stats['total_quantity']
        avg_price = stats['avg_price']
        target_price = avg_price * (1 + profit_percent / 100)
        target_value = total_qty * target_price
        total_cost = stats['total_usdt']
        target_profit = target_value - total_cost
        
        return {
            'target_price': target_price,
            'target_value': target_value,
            'target_profit': target_profit,
            'total_qty': total_qty,
            'avg_price': avg_price,
            'profit_percent': profit_percent
        }


# ==================== FAST DCA BOT ====================

class FastDCABot:
    def __init__(self):
        self.db = Database()
        self.bybit = None
        self.strategy = None
        self.bybit_initialized = False
        
        request_kwargs = {'connect_timeout': 60.0, 'read_timeout': 60.0, 'write_timeout': 60.0, 'pool_timeout': 60.0}
        request = HTTPXRequest(**request_kwargs)
        builder = Application.builder().token(TELEGRAM_TOKEN).request(request)
        self.application = builder.build()
        
        self.scheduler_running = False
        self.authorized_user_id = None
        
        self.setup_handlers()
    
    def _init_bybit(self):
        if not self.bybit_initialized and BYBIT_API_KEY and BYBIT_API_SECRET:
            try:
                self.bybit = BybitClient(BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_TESTNET)
                self.strategy = DCAStrategy(self.db, self.bybit)
                self.bybit_initialized = True
                logger.info("Bybit client initialized")
            except Exception as e:
                logger.error(f"Bybit init error: {e}")
    
    def get_main_keyboard(self):
        is_active = self.db.get_setting('dca_active', 'false') == 'true'
        dca_button = "⏹ Остановить Авто DCA" if is_active else "🚀 Запустить Авто DCA"
        keyboard = [
            [KeyboardButton("📊 Мой Портфель"), KeyboardButton(dca_button)],
            [KeyboardButton("💰 Ручная покупка (лимит)"), KeyboardButton("📈 Статистика DCA")],
            [KeyboardButton("➕ Добавить покупку вручную"), KeyboardButton("✏️ Редактировать покупки")],
            [KeyboardButton("⚙️ Настройки"), KeyboardButton("📋 Статус бота")],
            [KeyboardButton("📉 Текущая цена"), KeyboardButton("📝 Управление ордерами")],
            [KeyboardButton("🪜 Настройка лестницы"), KeyboardButton("🔔 Уведомления")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_settings_keyboard(self):
        keyboard = [
            [KeyboardButton("🪙 Выбор токена"), KeyboardButton("💵 Сумма покупки")],
            [KeyboardButton("📊 Процент прибыли"), KeyboardButton("📉 Настройки падения")],
            [KeyboardButton("⏰ Время покупки"), KeyboardButton("🔄 Частота покупки")],
            [KeyboardButton("🪜 Настройка лестницы"), KeyboardButton("🔔 Настройки уведомлений")],
            [KeyboardButton("📤 Экспорт базы"), KeyboardButton("📥 Импорт базы")],
            [KeyboardButton("🔙 Назад в меню")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_ladder_settings_keyboard(self):
        keyboard = [
            [KeyboardButton("💰 Цена старта"), KeyboardButton("📊 Шаг падения (%)")],
            [KeyboardButton("📉 Глубина просадки (%)"), KeyboardButton("💵 Базовая сумма")],
            [KeyboardButton("💰 Максимальная сумма"), KeyboardButton("📋 Текущие настройки")],
            [KeyboardButton("🔄 Сбросить лестницу"), KeyboardButton("🔙 Назад в меню")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_symbol_selection_keyboard(self):
        keyboard = []
        for symbol in POPULAR_SYMBOLS:
            keyboard.append([KeyboardButton(symbol)])
        keyboard.append([KeyboardButton("✏️ Ввести свой токен")])
        keyboard.append([KeyboardButton("❌ Отмена")])
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_notification_settings_keyboard(self):
        keyboard = [
            [KeyboardButton("📊 Процент для уведомления"), KeyboardButton("⏱ Частота проверки")],
            [KeyboardButton("🔔 Вкл/Выкл уведомления"), KeyboardButton("📋 Текущие настройки")],
            [KeyboardButton("🔙 Назад в меню")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_orders_management_keyboard(self):
        keyboard = [
            [KeyboardButton("📋 Список ордеров"), KeyboardButton("❌ Удалить ордер")],
            [KeyboardButton("✏️ Изменить цену"), KeyboardButton("🔙 Назад в меню")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_edit_purchases_keyboard(self):
        keyboard = [
            [KeyboardButton("💰 Изменить цену"), KeyboardButton("📊 Изменить количество")],
            [KeyboardButton("📅 Изменить дату"), KeyboardButton("❌ Удалить покупку")],
            [KeyboardButton("🔙 Назад к списку"), KeyboardButton("🏠 Главное меню")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_cancel_keyboard(self):
        return ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
    
    def get_confirm_keyboard(self):
        return ReplyKeyboardMarkup([[KeyboardButton("✅ Да, удалить"), KeyboardButton("❌ Нет, отмена")]], resize_keyboard=True)
    
    def get_purchases_list_keyboard(self, purchases):
        keyboard = []
        for p in purchases:
            try:
                date_display = datetime.strptime(p['date'], "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y")
            except:
                date_display = p['date'][:10] if p['date'] else "N/A"
            drop_text = f" [{p.get('drop_percent', 0):.0f}%]" if p.get('drop_percent', 0) > 0 else ""
            btn_text = f"ID{p['id']}: {date_display} - {format_quantity(p['quantity'], 4)} по {format_price(p['price'], 4)}{drop_text}"
            keyboard.append([KeyboardButton(btn_text)])
        keyboard.append([KeyboardButton("🏠 Главное меню")])
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_manual_buy_keyboard(self):
        return ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
    
    async def _check_user_fast(self, update: Update) -> bool:
        user = update.effective_user
        username = f"@{user.username}" if user.username else f"ID:{user.id}"
        if self.authorized_user_id is None:
            if username == AUTHORIZED_USER:
                self.authorized_user_id = user.id
                return True
        elif user.id == self.authorized_user_id:
            return True
        await update.message.reply_text("⛔ Доступ запрещен")
        return False
    
    async def cmd_start_fast(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return
        await update.message.reply_text(
            f"👋 Привет, {update.effective_user.first_name}!\n\n🤖 DCA Bybit Bot (Мартингейл лесенкой)\nГлавное меню:",
            reply_markup=self.get_main_keyboard()
        )
    
    async def show_portfolio(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.")
            return
        try:
            symbol = self.db.get_setting('symbol', 'TONUSDT')
            coin = symbol.replace('USDT', '')
            coin_balance = await self.bybit.get_balance(coin)
            usdt_balance = await self.bybit.get_balance('USDT')
            current_price = await self.bybit.get_symbol_price(symbol)
            
            message = f"📊 *Мой Портфель*\n\n"
            
            if usdt_balance and 'equity' in usdt_balance:
                available_usdt = usdt_balance.get('available', usdt_balance.get('equity', 0))
                message += f"💵 USDT доступно: `{available_usdt:.2f}`\n\n"
            
            if coin_balance and 'equity' in coin_balance:
                equity = coin_balance['equity']
                available = coin_balance.get('available', 0)
                usd_value = coin_balance.get('usdValue', 0)
                
                if usd_value == 0 and current_price and equity > 0:
                    usd_value = equity * current_price
                
                dca_stats = self.db.get_dca_stats(symbol)
                avg_price = dca_stats['avg_price'] if dca_stats else 0
                
                if avg_price > 0 and current_price and equity > 0:
                    pnl_percent = ((current_price - avg_price) / avg_price * 100)
                    pnl_usd = (current_price - avg_price) * equity
                else:
                    pnl_percent = 0
                    pnl_usd = 0
                
                emoji = "🟢" if pnl_percent >= 0 else "🔴"
                
                message += f"🪙 *{coin}*\n"
                message += f"Количество: `{format_quantity(equity, 6)}`\n"
                message += f"Доступно: `{format_quantity(available, 6)}`\n"
                message += f"Стоимость: `{usd_value:.2f}` USDT\n"
                if avg_price > 0:
                    message += f"Средняя цена входа: `{format_price(avg_price, 4)}` USDT\n"
                if current_price:
                    message += f"Текущая цена: `{format_price(current_price, 4)}` USDT\n"
                    message += f"{emoji} PnL: `{pnl_percent:+.2f}%` ({pnl_usd:+.2f} USDT)\n\n"
            
            # Получаем ордера с разделением
            orders_by_side = await self.bybit.get_open_orders_by_side(symbol)
            
            # Ордера на продажу
            sell_orders = orders_by_side.get('sell', [])
            if sell_orders:
                message += f"🔴 *ОРДЕРА НА ПРОДАЖУ ({len(sell_orders)})*\n"
                for order in sell_orders[:10]:
                    order_id = order.get('orderId', 'N/A')
                    price = float(order.get('price', 0))
                    qty = float(order.get('qty', 0))
                    message += f"`{order_id}` - `{format_quantity(qty, 6)}` {coin} @ `{format_price(price, 4)}` USDT\n"
                if len(sell_orders) > 10:
                    message += f"_...и еще {len(sell_orders) - 10}_\n"
                message += f"\n"
            else:
                message += f"🔴 *Нет ордеров на продажу*\n\n"
            
            # Ордера на покупку
            buy_orders = orders_by_side.get('buy', [])
            if buy_orders:
                message += f"🟢 *ОРДЕРА НА ПОКУПКУ ({len(buy_orders)})*\n"
                for order in buy_orders[:10]:
                    order_id = order.get('orderId', 'N/A')
                    price = float(order.get('price', 0))
                    qty = float(order.get('qty', 0))
                    message += f"`{order_id}` - `{format_quantity(qty, 6)}` {coin} @ `{format_price(price, 4)}` USDT\n"
                if len(buy_orders) > 10:
                    message += f"_...и еще {len(buy_orders) - 10}_\n"
            else:
                message += f"🟢 *Нет ордеров на покупку*"
            
            await update.message.reply_text(message, parse_mode='Markdown')
            
        except Exception as e:
            logger.error(f"Error in show_portfolio: {e}")
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")
    
    async def show_dca_stats_detailed(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.")
            return
        try:
            symbol = self.db.get_setting('symbol', 'TONUSDT')
            coin = symbol.replace('USDT', '')
            stats = self.db.get_dca_stats(symbol)
            current_price = await self.bybit.get_symbol_price(symbol)
            profit_percent = float(self.db.get_setting('profit_percent', '5'))
            
            if not stats:
                await update.message.reply_text("📈 *Статистика DCA*\n\nПокупок пока нет.", parse_mode='Markdown')
                return
            
            total_amount = stats['total_quantity']
            total_cost = stats['total_usdt']
            avg_price = stats['avg_price']
            current_value = total_amount * current_price if current_price else 0
            pnl = current_value - total_cost
            pnl_percent = (pnl / total_cost * 100) if total_cost > 0 else 0
            
            # Информация о целевой продаже
            target_info = self.strategy.calculate_target_info(stats, profit_percent)
            
            text = f"📊 *ДЕТАЛЬНАЯ СТАТИСТИКА DCA*\n\n"
            text += f"🪙 Токен: `{symbol}`\n"
            text += f"💰 Куплено: `{format_quantity(total_amount, 6)}` {coin}\n"
            text += f"💵 Инвестировано: `{total_cost:.2f}` USDT\n"
            text += f"📈 Средняя цена входа: `{format_price(avg_price, 4)}` USDT\n"
            
            if current_price:
                text += f"\n📊 *ТЕКУЩАЯ СИТУАЦИЯ*\n"
                text += f"📉 Текущая цена: `{format_price(current_price, 4)}` USDT\n"
                text += f"💰 Текущая стоимость: `{current_value:.2f}` USDT\n"
                emoji = "📈" if pnl >= 0 else "📉"
                text += f"{emoji} Текущий PnL: `{pnl:.2f}` USDT ({pnl_percent:+.2f}%)\n"
            
            if target_info:
                text += f"\n🎯 *ЦЕЛЕВАЯ ПРИБЫЛЬ {profit_percent}%:*\n"
                text += f"Нужно продать: `{format_quantity(target_info['total_qty'], 6)}` {coin}\n"
                text += f"Цена продажи: `{format_price(target_info['target_price'], 4)}` USDT\n"
                text += f"Получите: `{target_info['target_value']:.2f}` USDT\n"
                text += f"Прибыль: `{target_info['target_profit']:.2f}` USDT\n"
                
                if current_price:
                    increase_needed = ((target_info['target_price'] - current_price) / current_price * 100)
                    text += f"Нужен рост: `{increase_needed:+.2f}%` от текущей цены\n"
            
            # Информация о лестнице
            ladder_summary = self.db.get_ladder_summary(symbol)
            if ladder_summary and ladder_summary['start_price'] > 0:
                text += f"\n🪜 *НАСТРОЙКИ ЛЕСТНИЦЫ*\n"
                text += f"Стартовая цена: `{format_price(ladder_summary['start_price'], 4)}` USDT\n"
                text += f"Шаг падения: `{ladder_summary['step_percent']}%`\n"
                text += f"Глубина просадки: `{ladder_summary['max_depth']}%`\n"
                text += f"Базовая сумма: `{ladder_summary['base_amount']}` USDT\n"
                text += f"Максимальная сумма: `{ladder_summary['max_amount']}` USDT\n"
                text += f"Текущее падение: `{ladder_summary['current_drop']:.1f}%`\n"
                text += f"Текущий шаг: `{ladder_summary['current_step']}`"
            
            await update.message.reply_text(text, parse_mode='Markdown')
            
        except Exception as e:
            logger.error(f"Error in show_dca_stats_detailed: {e}")
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")
    
    async def show_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return
        symbol = self.db.get_setting('symbol', 'TONUSDT')
        is_active = self.db.get_setting('dca_active', 'false') == 'true'
        invest_amount = float(self.db.get_setting('invest_amount', '1.1'))
        ladder_settings = self.db.get_ladder_settings(symbol)
        
        message = f"📋 *Статус бота*\n\n"
        message += f"🤖 Статус: {'✅ Активен' if is_active else '⏹ Остановлен'}\n"
        message += f"🪙 Токен: `{symbol}`\n"
        message += f"💵 Сумма покупки: `{invest_amount}` USDT\n"
        message += f"📈 Цель: `{self.db.get_setting('profit_percent', '5')}%`\n"
        message += f"\n🪜 *Лестница:*\n"
        message += f"Стартовая цена: `{format_price(ladder_settings['start_price'], 4)}` USDT\n"
        message += f"Базовая сумма: `{ladder_settings['base_amount']}` USDT\n"
        message += f"Макс. сумма: `{ladder_settings['max_amount']}` USDT\n"
        
        stats = self.db.get_dca_stats(symbol)
        if stats:
            message += f"\n📊 Всего покупок: `{stats['total_purchases']}`\n💰 Вложено: `{stats['total_usdt']:.2f}` USDT"
        
        await update.message.reply_text(message, parse_mode='Markdown')
    
    async def show_price(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.")
            return
        symbol = self.db.get_setting('symbol', 'TONUSDT')
        price = await self.bybit.get_symbol_price(symbol)
        if price:
            await update.message.reply_text(f"💹 *{symbol}*: `{format_price(price, 4)}` USDT", parse_mode='Markdown')
        else:
            await update.message.reply_text("❌ Не удалось получить цену.")
    
    async def toggle_dca(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.")
            return
        is_active = self.db.get_setting('dca_active', 'false') == 'true'
        if is_active:
            self.db.set_setting('dca_active', 'false')
            await update.message.reply_text("⏹ DCA остановлен", reply_markup=self.get_main_keyboard())
        else:
            symbol = self.db.get_setting('symbol', 'TONUSDT')
            current_price = await self.bybit.get_symbol_price(symbol)
            if not current_price:
                await update.message.reply_text("❌ Не удалось получить цену")
                return
            
            ladder_settings = self.db.get_ladder_settings(symbol)
            if ladder_settings['start_price'] <= 0:
                ladder_settings['start_price'] = current_price
                ladder_settings['last_buy_price'] = current_price
                self.db.save_ladder_settings(ladder_settings)
                await update.message.reply_text(f"🪜 Установлена стартовая цена: {format_price(current_price, 4)} USDT")
            
            self.db.set_setting('dca_active', 'true')
            self.db.set_setting('initial_reference_price', str(current_price))
            self.db.set_dca_start(symbol, current_price)
            
            invest_amount = float(self.db.get_setting('invest_amount', '1.1'))
            await update.message.reply_text(
                f"✅ DCA запущен!\n\n"
                f"🪙 {symbol}\n"
                f"💰 Стартовая цена: {format_price(current_price, 4)} USDT\n"
                f"💵 Базовая сумма: {invest_amount} USDT\n"
                f"📊 Шаг падения: {STEP_PERCENT}%\n"
                f"📉 Макс. просадка: {MAX_DROP_DEPTH}%",
                reply_markup=self.get_main_keyboard()
            )
    
    # ============= НАСТРОЙКИ ЛЕСТНИЦЫ =============
    
    async def ladder_settings_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        
        await update.message.reply_text(
            "🪜 *Настройка лестницы (Мартингейл)*\n\n"
            "Стратегия: при каждом падении цены на 3% от стартовой цены\n"
            "происходит докупка с линейным ростом суммы от базовой до максимальной.\n\n"
            "Параметры:\n"
            "• Шаг падения: 3% (фиксированный)\n"
            "• Глубина просадки: до 80%\n"
            "• Рост суммы: от базовой до максимальной",
            reply_markup=self.get_ladder_settings_keyboard(),
            parse_mode='Markdown'
        )
        return LADDER_MENU
    
    async def show_ladder_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return LADDER_MENU
        
        symbol = self.db.get_setting('symbol', 'TONUSDT')
        ladder = self.db.get_ladder_settings(symbol)
        summary = self.db.get_ladder_summary(symbol)
        
        text = f"🪜 *ТЕКУЩИЕ НАСТРОЙКИ ЛЕСТНИЦЫ*\n\n"
        text += f"🪙 Токен: `{symbol}`\n"
        text += f"💰 Стартовая цена: `{format_price(ladder['start_price'], 4)}` USDT\n"
        text += f"📊 Шаг падения: `{ladder['step_percent']}%`\n"
        text += f"📉 Глубина просадки: `{ladder['max_depth']}%`\n"
        text += f"💵 Базовая сумма: `{ladder['base_amount']}` USDT\n"
        text += f"💰 Максимальная сумма: `{ladder['max_amount']}` USDT\n"
        text += f"📊 Текущее падение: `{summary['current_drop']:.1f}%`\n"
        text += f"📊 Текущий шаг: `{summary['current_step']}`\n\n"
        
        text += "*План покупок:*\n"
        for step in summary['steps'][:15]:
            status_emoji = "✅" if step['status'] == 'completed' else "⏳"
            text += f"{status_emoji} {step['drop_percent']:.0f}%: {step['amount']:.2f} USDT (коэф. {step['ratio']:.2f})\n"
        
        if len(summary['steps']) > 15:
            text += f"_...и еще {len(summary['steps']) - 15} уровней_"
        
        await update.message.reply_text(text, parse_mode='Markdown', reply_markup=self.get_ladder_settings_keyboard())
        return LADDER_MENU
    
    async def set_ladder_start_price_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return LADDER_MENU
        await update.message.reply_text("💰 Введите новую стартовую цену (USDT):", reply_markup=self.get_cancel_keyboard(), parse_mode='Markdown')
        return SET_LADDER_START_PRICE
    
    async def set_ladder_start_price_save(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_ladder_settings_keyboard())
            return LADDER_MENU
        try:
            start_price = float(text.replace(',', '.'))
            if start_price <= 0:
                raise ValueError
            symbol = self.db.get_setting('symbol', 'TONUSDT')
            ladder = self.db.get_ladder_settings(symbol)
            ladder['start_price'] = start_price
            ladder['last_buy_price'] = start_price
            self.db.save_ladder_settings(ladder)
            await update.message.reply_text(f"✅ Стартовая цена установлена: {format_price(start_price, 4)} USDT", reply_markup=self.get_ladder_settings_keyboard())
            return LADDER_MENU
        except ValueError:
            await update.message.reply_text("❌ Некорректная цена.", reply_markup=self.get_cancel_keyboard())
            return SET_LADDER_START_PRICE
    
    async def set_ladder_step_percent_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return LADDER_MENU
        await update.message.reply_text("📊 Введите шаг падения в процентах (1-5%):\n*Рекомендуется 3%*", reply_markup=self.get_cancel_keyboard(), parse_mode='Markdown')
        return SET_LADDER_STEP_PERCENT
    
    async def set_ladder_step_percent_save(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_ladder_settings_keyboard())
            return LADDER_MENU
        try:
            step_percent = float(text.replace(',', '.'))
            if step_percent <= 0 or step_percent > 10:
                raise ValueError
            symbol = self.db.get_setting('symbol', 'TONUSDT')
            ladder = self.db.get_ladder_settings(symbol)
            ladder['step_percent'] = step_percent
            self.db.save_ladder_settings(ladder)
            await update.message.reply_text(f"✅ Шаг падения установлен: {step_percent}%", reply_markup=self.get_ladder_settings_keyboard())
            return LADDER_MENU
        except ValueError:
            await update.message.reply_text("❌ Некорректное значение (1-10).", reply_markup=self.get_cancel_keyboard())
            return SET_LADDER_STEP_PERCENT
    
    async def set_ladder_max_depth_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return LADDER_MENU
        await update.message.reply_text("📉 Введите глубину просадки в процентах (50-90%):\n*Рекомендуется 80%*", reply_markup=self.get_cancel_keyboard(), parse_mode='Markdown')
        return SET_LADDER_DEPTH
    
    async def set_ladder_max_depth_save(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_ladder_settings_keyboard())
            return LADDER_MENU
        try:
            max_depth = float(text.replace(',', '.'))
            if max_depth < 30 or max_depth > 95:
                raise ValueError
            symbol = self.db.get_setting('symbol', 'TONUSDT')
            ladder = self.db.get_ladder_settings(symbol)
            ladder['max_depth'] = max_depth
            self.db.save_ladder_settings(ladder)
            await update.message.reply_text(f"✅ Глубина просадки установлена: {max_depth}%", reply_markup=self.get_ladder_settings_keyboard())
            return LADDER_MENU
        except ValueError:
            await update.message.reply_text("❌ Некорректное значение (30-95).", reply_markup=self.get_cancel_keyboard())
            return SET_LADDER_DEPTH
    
    async def set_ladder_base_amount_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return LADDER_MENU
        await update.message.reply_text("💵 Введите базовую сумму (мин 1 USDT):\n*Сумма первого ордера*", reply_markup=self.get_cancel_keyboard(), parse_mode='Markdown')
        return SET_LADDER_BASE_AMOUNT
    
    async def set_ladder_base_amount_save(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_ladder_settings_keyboard())
            return LADDER_MENU
        try:
            base_amount = float(text.replace(',', '.'))
            if base_amount < 1:
                raise ValueError
            symbol = self.db.get_setting('symbol', 'TONUSDT')
            ladder = self.db.get_ladder_settings(symbol)
            ladder['base_amount'] = base_amount
            # Обновляем максимальную сумму (базовая * 3)
            ladder['max_amount'] = base_amount * 3
            self.db.save_ladder_settings(ladder)
            await update.message.reply_text(f"✅ Базовая сумма: {base_amount} USDT\n💰 Максимальная сумма: {base_amount * 3} USDT", reply_markup=self.get_ladder_settings_keyboard())
            return LADDER_MENU
        except ValueError:
            await update.message.reply_text("❌ Некорректная сумма (мин 1).", reply_markup=self.get_cancel_keyboard())
            return SET_LADDER_BASE_AMOUNT
    
    async def set_ladder_max_amount_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return LADDER_MENU
        await update.message.reply_text("💰 Введите максимальную сумму (USDT):\n*Сумма последнего ордера*", reply_markup=self.get_cancel_keyboard(), parse_mode='Markdown')
        return SET_LADDER_BASE_AMOUNT  # Используем то же состояние, так как это последний шаг
    
    async def set_ladder_max_amount_save(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_ladder_settings_keyboard())
            return LADDER_MENU
        try:
            max_amount = float(text.replace(',', '.'))
            if max_amount < 1:
                raise ValueError
            symbol = self.db.get_setting('symbol', 'TONUSDT')
            ladder = self.db.get_ladder_settings(symbol)
            ladder['max_amount'] = max_amount
            self.db.save_ladder_settings(ladder)
            await update.message.reply_text(f"✅ Максимальная сумма: {max_amount} USDT", reply_markup=self.get_ladder_settings_keyboard())
            return LADDER_MENU
        except ValueError:
            await update.message.reply_text("❌ Некорректная сумма (мин 1).", reply_markup=self.get_cancel_keyboard())
            return SET_LADDER_BASE_AMOUNT
    
    async def reset_ladder(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return LADDER_MENU
        symbol = self.db.get_setting('symbol', 'TONUSDT')
        self.db.reset_ladder(symbol)
        await update.message.reply_text("🔄 Лестница сброшена!", reply_markup=self.get_ladder_settings_keyboard())
        return LADDER_MENU
    
    # ============= НАСТРОЙКИ УВЕДОМЛЕНИЙ =============
    
    async def notification_settings_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return SELECTING_ACTION
        await update.message.reply_text("🔔 *Настройки уведомлений*", reply_markup=self.get_notification_settings_keyboard(), parse_mode='Markdown')
        return SELECTING_ACTION
    
    async def show_current_notification_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return SELECTING_ACTION
        settings = self.db.get_notification_settings()
        status = "✅ Включены" if settings['enabled'] else "❌ Выключены"
        text = f"📋 *НАСТРОЙКИ УВЕДОМЛЕНИЙ*\n\n📊 Процент: `{settings['alert_percent']}%`\n⏱ Частота: `{settings['alert_interval_minutes']}` мин\n🔔 Уведомления: {status}"
        await update.message.reply_text(text, parse_mode='Markdown', reply_markup=self.get_notification_settings_keyboard())
        return SELECTING_ACTION
    
    async def toggle_notifications(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return SELECTING_ACTION
        settings = self.db.get_notification_settings()
        new_status = not settings['enabled']
        self.db.update_notification_settings(enabled=new_status)
        status = "включены" if new_status else "выключены"
        await update.message.reply_text(f"🔔 Уведомления {status}!", reply_markup=self.get_notification_settings_keyboard())
        return SELECTING_ACTION
    
    async def set_alert_percent_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return SELECTING_ACTION
        await update.message.reply_text("📊 Введите процент для уведомления:", reply_markup=self.get_cancel_keyboard())
        return WAITING_ALERT_PERCENT
    
    async def set_alert_percent_save(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_settings_keyboard())
            return SELECTING_ACTION
        try:
            percent = float(text.replace(',', '.'))
            if percent <= 0:
                raise ValueError
            self.db.update_notification_settings(alert_percent=percent)
            await update.message.reply_text(f"✅ Процент изменен на {percent}%", reply_markup=self.get_settings_keyboard())
            return SELECTING_ACTION
        except ValueError:
            await update.message.reply_text("❌ Ошибка! Введите число.", reply_markup=self.get_cancel_keyboard())
            return WAITING_ALERT_PERCENT
    
    async def set_alert_interval_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return SELECTING_ACTION
        await update.message.reply_text("⏱ Введите частоту проверки (минуты):", reply_markup=self.get_cancel_keyboard())
        return WAITING_ALERT_INTERVAL
    
    async def set_alert_interval_save(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_settings_keyboard())
            return SELECTING_ACTION
        try:
            interval = int(float(text))
            if interval < 1:
                raise ValueError
            self.db.update_notification_settings(alert_interval_minutes=interval)
            await update.message.reply_text(f"✅ Частота изменена на {interval} минут", reply_markup=self.get_settings_keyboard())
            return SELECTING_ACTION
        except ValueError:
            await update.message.reply_text("❌ Ошибка! Введите целое число.", reply_markup=self.get_cancel_keyboard())
            return WAITING_ALERT_INTERVAL
    
    # ============= УПРАВЛЕНИЕ ОРДЕРАМИ =============
    
    async def orders_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.")
            return ConversationHandler.END
        symbol = self.db.get_setting('symbol', 'TONUSDT')
        orders_by_side = await self.bybit.get_open_orders_by_side(symbol)
        sell_count = len(orders_by_side.get('sell', []))
        buy_count = len(orders_by_side.get('buy', []))
        await update.message.reply_text(f"📝 *Управление ордерами*\n\nТокен: `{symbol}`\n📊 Ордера на продажу: `{sell_count}`\n📊 Ордера на покупку: `{buy_count}`", reply_markup=self.get_orders_management_keyboard(), parse_mode='Markdown')
        return MANAGE_ORDERS
    
    async def list_orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.")
            return MANAGE_ORDERS
        symbol = self.db.get_setting('symbol', 'TONUSDT')
        coin = symbol.replace('USDT', '')
        orders_by_side = await self.bybit.get_open_orders_by_side(symbol)
        
        message = f"📋 *СПИСОК ОРДЕРОВ*\n\n"
        
        # Ордера на продажу
        sell_orders = orders_by_side.get('sell', [])
        if sell_orders:
            message += f"🔴 *ОРДЕРА НА ПРОДАЖУ ({len(sell_orders)})*\n"
            for i, order in enumerate(sell_orders[:20], 1):
                order_id = order.get('orderId', 'N/A')
                price = float(order.get('price', 0))
                qty = float(order.get('qty', 0))
                message += f"{i}. `{order_id}` - `{format_quantity(qty, 6)}` {coin} @ `{format_price(price, 4)}` USDT\n"
            if len(sell_orders) > 20:
                message += f"_...и еще {len(sell_orders) - 20}_\n"
            message += f"\n"
        else:
            message += f"🔴 *Нет ордеров на продажу*\n\n"
        
        # Ордера на покупку
        buy_orders = orders_by_side.get('buy', [])
        if buy_orders:
            message += f"🟢 *ОРДЕРА НА ПОКУПКУ ({len(buy_orders)})*\n"
            for i, order in enumerate(buy_orders[:20], 1):
                order_id = order.get('orderId', 'N/A')
                price = float(order.get('price', 0))
                qty = float(order.get('qty', 0))
                message += f"{i}. `{order_id}` - `{format_quantity(qty, 6)}` {coin} @ `{format_price(price, 4)}` USDT\n"
            if len(buy_orders) > 20:
                message += f"_...и еще {len(buy_orders) - 20}_\n"
        else:
            message += f"🟢 *Нет ордеров на покупку*"
        
        await update.message.reply_text(message, parse_mode='Markdown')
        return MANAGE_ORDERS
    
    async def delete_order_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.")
            return MANAGE_ORDERS
        symbol = self.db.get_setting('symbol', 'TONUSDT')
        coin = symbol.replace('USDT', '')
        open_orders = await self.bybit.get_open_orders(symbol)
        if not open_orders:
            await update.message.reply_text("❌ Нет открытых ордеров", reply_markup=self.get_orders_management_keyboard())
            return MANAGE_ORDERS
        keyboard = []
        for i, order in enumerate(open_orders[:20], 1):
            order_id = order.get('orderId', 'N/A')
            price = float(order.get('price', 0))
            qty = float(order.get('qty', 0))
            side = order.get('side', 'Unknown')
            side_emoji = "🔴" if side == "Sell" else "🟢"
            side_text = "Продажа" if side == "Sell" else "Покупка"
            keyboard.append([InlineKeyboardButton(f"{side_emoji} Удалить #{i} - {side_text} {format_quantity(qty, 6)} {coin} @ {format_price(price, 4)}", callback_data=f"order_delete_{order_id}")])
        keyboard.append([InlineKeyboardButton("🔙 Отмена", callback_data="order_cancel")])
        await update.message.reply_text("❌ *Выберите ордер для удаления:*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        return MANAGE_ORDERS
    
    async def edit_order_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.")
            return MANAGE_ORDERS
        symbol = self.db.get_setting('symbol', 'TONUSDT')
        coin = symbol.replace('USDT', '')
        open_orders = await self.bybit.get_open_orders(symbol)
        if not open_orders:
            await update.message.reply_text("❌ Нет открытых ордеров", reply_markup=self.get_orders_management_keyboard())
            return MANAGE_ORDERS
        keyboard = []
        for i, order in enumerate(open_orders[:20], 1):
            order_id = order.get('orderId', 'N/A')
            price = float(order.get('price', 0))
            qty = float(order.get('qty', 0))
            side = order.get('side', 'Unknown')
            side_emoji = "🔴" if side == "Sell" else "🟢"
            side_text = "Продажа" if side == "Sell" else "Покупка"
            keyboard.append([InlineKeyboardButton(f"{side_emoji} Изменить #{i} - {side_text} {format_quantity(qty, 6)} {coin} @ {format_price(price, 4)}", callback_data=f"order_edit_{order_id}_{price}_{qty}")])
        keyboard.append([InlineKeyboardButton("🔙 Отмена", callback_data="order_cancel")])
        await update.message.reply_text("✏️ *Выберите ордер для изменения цены:*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        return MANAGE_ORDERS
    
    async def handle_order_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data
        if data == "order_cancel":
            await query.edit_message_text("✅ Отменено")
            return
        if data.startswith("order_delete_"):
            order_id = data.replace("order_delete_", "")
            await self.process_order_delete(update, context, order_id)
        elif data.startswith("order_edit_"):
            parts = data.split("_")
            if len(parts) >= 4:
                order_id = parts[2]
                current_price = float(parts[3])
                qty = float(parts[4]) if len(parts) > 4 else 0
                await self.process_order_edit_start(update, context, order_id, current_price, qty)
    
    async def process_order_delete(self, update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: str):
        self._init_bybit()
        if not self.bybit_initialized:
            await update.callback_query.edit_message_text("❌ Bybit API не инициализирован.")
            return
        symbol = self.db.get_setting('symbol', 'TONUSDT')
        result = await self.bybit.cancel_order(symbol, order_id)
        if result['success']:
            self.db.update_sell_order_status(order_id, 'cancelled')
            await update.callback_query.edit_message_text("✅ Ордер удален!")
        else:
            await update.callback_query.edit_message_text(f"❌ Ошибка: {result.get('error', 'Unknown')}")
    
    async def process_order_edit_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: str, current_price: float, qty: float):
        context.user_data['editing_order_id'] = order_id
        context.user_data['editing_order_qty'] = qty
        context.user_data['editing_order_current_price'] = current_price
        await update.callback_query.edit_message_text(f"✏️ Введите новую цену (текущая: {format_price(current_price, 4)}):")
        context.user_data['current_state'] = EDIT_ORDER_PRICE
    
    async def edit_order_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        self._init_bybit()
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_orders_management_keyboard())
            return MANAGE_ORDERS
        try:
            new_price = float(text)
            order_id = context.user_data.get('editing_order_id')
            old_price = context.user_data.get('editing_order_current_price', 0)
            symbol = self.db.get_setting('symbol', 'TONUSDT')
            if not order_id:
                await update.message.reply_text("❌ Ошибка", reply_markup=self.get_orders_management_keyboard())
                return MANAGE_ORDERS
            result = await self.bybit.amend_order_price(symbol, order_id, new_price)
            if result['success']:
                self.db.update_order_price(order_id, new_price, 0)
                await update.message.reply_text(f"✅ Цена изменена: {format_price(old_price, 4)} -> {format_price(new_price, 4)}", reply_markup=self.get_orders_management_keyboard())
            else:
                await update.message.reply_text(f"❌ Ошибка: {result.get('error', 'Unknown')}", reply_markup=self.get_orders_management_keyboard())
        except ValueError:
            await update.message.reply_text("❌ Некорректная цена", reply_markup=self.get_orders_management_keyboard())
            return EDIT_ORDER_PRICE
        context.user_data.pop('editing_order_id', None)
        return MANAGE_ORDERS
    
    # ============= РУЧНАЯ ПОКУПКА =============
    
    async def manual_buy_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.")
            return ConversationHandler.END
        symbol = self.db.get_setting('symbol', 'TONUSDT')
        current_price = await self.bybit.get_symbol_price(symbol)
        if not current_price:
            await update.message.reply_text("❌ Не удалось получить цену", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        recommendation = await self.strategy.get_recommended_purchase(symbol)
        context.user_data['manual_buy_current_price'] = current_price
        context.user_data['manual_buy_symbol'] = symbol
        context.user_data['manual_buy_recommendation'] = recommendation
        msg = f"💰 Текущая цена {symbol}: `{format_price(current_price, 4)}` USDT\n\n"
        if recommendation['success'] and recommendation['should_buy']:
            msg += f"🟢 *РЕКОМЕНДАЦИЯ:* Сейчас хороший момент!\n📉 Падение {recommendation.get('drop_percent', 0):.1f}%\n📊 Сумма: `{recommendation['amount_usdt']:.2f}` USDT\n\n"
        elif recommendation['success']:
            msg += f"⏳ *РЕКОМЕНДАЦИЯ:* Ждем снижения\n📉 Следующая цена: `{format_price(recommendation['next_buy_price'], 4)}` USDT\n\n"
        msg += f"Введите цену лимитного ордера (или нажмите Отмена):"
        await update.message.reply_text(msg, reply_markup=self.get_manual_buy_keyboard(), parse_mode='Markdown')
        return MANUAL_BUY_PRICE
    
    async def manual_buy_price_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        try:
            price = float(text.replace(',', '.'))
            if price <= 0:
                raise ValueError
            context.user_data['manual_buy_price'] = price
            recommendation = context.user_data.get('manual_buy_recommendation', {})
            suggested_amount = recommendation.get('amount_usdt', 1.1) if recommendation.get('should_buy') else 1.1
            await update.message.reply_text(f"💰 Введите сумму покупки в USDT\n*Рекомендуемая сумма:* {suggested_amount:.2f} USDT\nМинимум: 1.1 USDT:", reply_markup=self.get_manual_buy_keyboard(), parse_mode='Markdown')
            return MANUAL_BUY_AMOUNT
        except ValueError:
            await update.message.reply_text("❌ Некорректная цена.", reply_markup=self.get_manual_buy_keyboard())
            return MANUAL_BUY_PRICE
    
    async def manual_buy_amount_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        self._init_bybit()
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        try:
            amount = float(text.replace(',', '.'))
            if amount < 1.1:
                raise ValueError("Минимальная сумма 1.1 USDT")
            price = context.user_data.get('manual_buy_price')
            symbol = context.user_data.get('manual_buy_symbol', 'TONUSDT')
            recommendation = context.user_data.get('manual_buy_recommendation', {})
            if not price:
                await update.message.reply_text("❌ Ошибка", reply_markup=self.get_main_keyboard())
                return ConversationHandler.END
            await update.message.reply_text("⏳ Создаю ордер...")
            result = await self.bybit.place_limit_buy(symbol, price, amount)
            if result['success']:
                profit_percent = float(self.db.get_setting('profit_percent', '5'))
                target_price = price * (1 + profit_percent / 100)
                current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                drop_percent = recommendation.get('drop_percent', 0) if recommendation.get('should_buy') else 0
                step_level = recommendation.get('step_level', 0) if recommendation.get('should_buy') else 0
                self.db.add_purchase(symbol=symbol, amount_usdt=amount, price=price, quantity=result['quantity'], multiplier=1.0, drop_percent=drop_percent, step_level=step_level, date=current_date)
                sell_result = await self.bybit.place_limit_sell(symbol, result['quantity'], target_price)
                if sell_result['success']:
                    self.db.add_sell_order(symbol=symbol, order_id=sell_result['order_id'], quantity=result['quantity'], target_price=target_price, profit_percent=profit_percent)
                msg = f"✅ *Лимитный ордер создан!*\n\nЦена: `{format_price(price, 4)}` USDT\nСумма: `{amount:.2f}` USDT\nКоличество: `{format_quantity(result['quantity'], 6)}`\n"
                if drop_percent > 0:
                    msg += f"📉 Падение: `{drop_percent:.1f}%`\n"
                msg += f"Цель продажи: `{format_price(target_price, 4)}` USDT ({profit_percent}%)"
                await update.message.reply_text(msg, reply_markup=self.get_main_keyboard(), parse_mode='Markdown')
            else:
                await update.message.reply_text(f"❌ Ошибка: {result.get('error', 'Unknown')}", reply_markup=self.get_main_keyboard())
        except ValueError as e:
            await update.message.reply_text(f"❌ {str(e)}", reply_markup=self.get_manual_buy_keyboard())
            return MANUAL_BUY_AMOUNT
        return ConversationHandler.END
    
    # ============= РУЧНОЕ ДОБАВЛЕНИЕ ПОКУПКИ =============
    
    async def manual_add_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        await update.message.reply_text("➕ *Добавление покупки*\n\nВведите цену покупки (USDT):", reply_markup=self.get_cancel_keyboard(), parse_mode='Markdown')
        return MANUAL_ADD_PRICE
    
    async def manual_add_price(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        try:
            price = float(text.replace(',', '.'))
            if price <= 0:
                raise ValueError
            context.user_data['manual_price'] = price
            await update.message.reply_text(f"✅ Цена {format_price(price, 4)} USDT\n\nВведите количество монет:", reply_markup=self.get_cancel_keyboard())
            return MANUAL_ADD_AMOUNT
        except ValueError:
            await update.message.reply_text("❌ Ошибка! Введите число.", reply_markup=self.get_cancel_keyboard())
            return MANUAL_ADD_PRICE
    
    async def manual_add_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        try:
            quantity = float(text.replace(',', '.'))
            if quantity <= 0:
                raise ValueError
            price = context.user_data.get('manual_price')
            if not price:
                await update.message.reply_text("❌ Ошибка", reply_markup=self.get_main_keyboard())
                return ConversationHandler.END
            symbol = self.db.get_setting('symbol', 'TONUSDT')
            amount_usdt = price * quantity
            purchase_id = self.db.add_purchase(symbol=symbol, amount_usdt=amount_usdt, price=price, quantity=quantity, multiplier=1.0, drop_percent=0, step_level=0, date=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            if purchase_id:
                await update.message.reply_text(f"✅ *Покупка добавлена!*\n\nID: `{purchase_id}`\nЦена: `{format_price(price, 4)}` USDT\nКоличество: `{format_quantity(quantity, 6)}`\nСумма: `{amount_usdt:.2f}` USDT", reply_markup=self.get_main_keyboard(), parse_mode='Markdown')
            else:
                await update.message.reply_text("❌ Ошибка сохранения", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        except ValueError:
            await update.message.reply_text("❌ Ошибка! Введите число.", reply_markup=self.get_cancel_keyboard())
            return MANUAL_ADD_AMOUNT
    
    # ============= РЕДАКТИРОВАНИЕ ПОКУПОК =============
    
    async def edit_purchases_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        symbol = self.db.get_setting('symbol', 'TONUSDT')
        purchases = self.db.get_purchases(symbol)
        if not purchases:
            await update.message.reply_text("Нет покупок", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        context.user_data.pop('editing_purchase_id', None)
        await update.message.reply_text("✏️ Выберите покупку:", reply_markup=self.get_purchases_list_keyboard(purchases))
        return EDIT_PURCHASE_SELECT
    
    async def edit_purchase_selected(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        if text == "🏠 Главное меню":
            await self.back_to_main(update, context)
            return ConversationHandler.END
        if text in ["💰 Изменить цену", "📊 Изменить количество", "📅 Изменить дату", "❌ Удалить покупку", "🔙 Назад к списку"]:
            return EDIT_PURCHASE_SELECT
        try:
            import re
            match = re.search(r'ID(\d+)', text)
            if not match:
                await update.message.reply_text("❌ Неверный формат.", reply_markup=self.get_purchases_list_keyboard(self.db.get_purchases(self.db.get_setting('symbol', 'TONUSDT'))))
                return EDIT_PURCHASE_SELECT
            purchase_id = int(match.group(1))
            purchase = self.db.get_purchase_by_id(purchase_id)
            if not purchase:
                await update.message.reply_text("❌ Покупка не найдена", reply_markup=self.get_main_keyboard())
                return ConversationHandler.END
            context.user_data['editing_purchase_id'] = purchase_id
            try:
                date_display = datetime.strptime(purchase['date'], "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y %H:%M")
            except:
                date_display = purchase['date'][:10] if purchase['date'] else "N/A"
            drop_text = f" (падение {purchase.get('drop_percent', 0):.0f}%)" if purchase.get('drop_percent', 0) > 0 else ""
            await update.message.reply_text(f"✏️ *РЕДАКТИРОВАНИЕ ID: {purchase_id}*\n\n📅 Дата: `{date_display}`\n💰 Цена: `{format_price(purchase['price'], 4)}` USDT\n📊 Количество: `{format_quantity(purchase['quantity'], 6)}`{drop_text}", reply_markup=self.get_edit_purchases_keyboard(), parse_mode='Markdown')
            return EDIT_PURCHASE_SELECT
        except Exception as e:
            await update.message.reply_text("❌ Ошибка выбора", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
    
    async def edit_price_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("💰 Введите новую цену:", reply_markup=self.get_cancel_keyboard())
        return EDIT_PRICE
    
    async def edit_price_save(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await self.cancel_to_edit_menu(update, context)
            return EDIT_PURCHASE_SELECT
        try:
            new_price = float(text.replace(',', '.'))
            purchase_id = context.user_data.get('editing_purchase_id')
            if not purchase_id:
                await update.message.reply_text("❌ Ошибка", reply_markup=self.get_main_keyboard())
                return ConversationHandler.END
            purchase = self.db.get_purchase_by_id(purchase_id)
            if not purchase:
                await update.message.reply_text("❌ Покупка не найдена", reply_markup=self.get_main_keyboard())
                return ConversationHandler.END
            new_amount_usdt = new_price * purchase['quantity']
            if self.db.update_purchase(purchase_id, price=new_price, amount_usdt=new_amount_usdt):
                await update.message.reply_text(f"✅ Цена обновлена: {format_price(new_price, 4)} USDT")
            else:
                await update.message.reply_text("❌ Ошибка при обновлении")
            await self.show_purchase_after_edit(update, context, purchase_id)
            return EDIT_PURCHASE_SELECT
        except ValueError:
            await update.message.reply_text("❌ Ошибка! Введите число.", reply_markup=self.get_cancel_keyboard())
            return EDIT_PRICE
    
    async def edit_amount_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("📊 Введите новое количество:", reply_markup=self.get_cancel_keyboard())
        return EDIT_AMOUNT
    
    async def edit_amount_save(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await self.cancel_to_edit_menu(update, context)
            return EDIT_PURCHASE_SELECT
        try:
            new_quantity = float(text.replace(',', '.'))
            purchase_id = context.user_data.get('editing_purchase_id')
            if not purchase_id:
                await update.message.reply_text("❌ Ошибка", reply_markup=self.get_main_keyboard())
                return ConversationHandler.END
            purchase = self.db.get_purchase_by_id(purchase_id)
            if not purchase:
                await update.message.reply_text("❌ Покупка не найдена", reply_markup=self.get_main_keyboard())
                return ConversationHandler.END
            new_amount_usdt = purchase['price'] * new_quantity
            if self.db.update_purchase(purchase_id, quantity=new_quantity, amount_usdt=new_amount_usdt):
                await update.message.reply_text(f"✅ Количество обновлено: {format_quantity(new_quantity, 6)}")
            else:
                await update.message.reply_text("❌ Ошибка при обновлении")
            await self.show_purchase_after_edit(update, context, purchase_id)
            return EDIT_PURCHASE_SELECT
        except ValueError:
            await update.message.reply_text("❌ Ошибка! Введите число.", reply_markup=self.get_cancel_keyboard())
            return EDIT_AMOUNT
    
    def parse_date(self, date_str: str) -> str:
        date_str = date_str.strip()
        patterns = [
            (r'^(\d{1,2})\.(\d{1,2})\.(\d{4})$', lambda m: (int(m.group(1)), int(m.group(2)), int(m.group(3)))),
            (r'^(\d{1,2})\.(\d{1,2})\.(\d{2})$', lambda m: (int(m.group(1)), int(m.group(2)), 2000 + int(m.group(3)))),
            (r'^(\d{1,2})\.(\d{1,2})$', lambda m: (int(m.group(1)), int(m.group(2)), datetime.now().year)),
        ]
        for pattern, extractor in patterns:
            match = re.match(pattern, date_str)
            if match:
                day, month, year = extractor(match)
                try:
                    dt = datetime(year, month, day)
                    return dt.strftime("%Y-%m-%d")
                except ValueError:
                    raise ValueError("Некорректная дата")
        raise ValueError("Неподдерживаемый формат")
    
    async def edit_date_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        purchase_id = context.user_data.get('editing_purchase_id')
        purchase = self.db.get_purchase_by_id(purchase_id)
        try:
            current_date = datetime.strptime(purchase['date'], "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y")
        except:
            current_date = purchase['date'][:10] if purchase['date'] else "неизвестно"
        await update.message.reply_text(f"📅 Текущая дата: {current_date}\n\nВведите новую дату (ДД.ММ.ГГГГ):", reply_markup=self.get_cancel_keyboard())
        return EDIT_DATE
    
    async def edit_date_save(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await self.cancel_to_edit_menu(update, context)
            return EDIT_PURCHASE_SELECT
        try:
            new_date = self.parse_date(text)
            purchase_id = context.user_data.get('editing_purchase_id')
            if not purchase_id:
                await update.message.reply_text("❌ Ошибка", reply_markup=self.get_main_keyboard())
                return ConversationHandler.END
            purchase = self.db.get_purchase_by_id(purchase_id)
            if not purchase:
                await update.message.reply_text("❌ Покупка не найдена", reply_markup=self.get_main_keyboard())
                return ConversationHandler.END
            old_time = purchase['date'][11:] if purchase['date'] and len(purchase['date']) > 10 else "00:00:00"
            new_date_with_time = f"{new_date} {old_time}"
            if self.db.update_purchase(purchase_id, date=new_date_with_time):
                await update.message.reply_text(f"✅ Дата обновлена: {new_date}")
            else:
                await update.message.reply_text("❌ Ошибка при обновлении")
            await self.show_purchase_after_edit(update, context, purchase_id)
            return EDIT_PURCHASE_SELECT
        except ValueError as e:
            await update.message.reply_text(f"❌ {str(e)}", reply_markup=self.get_cancel_keyboard())
            return EDIT_DATE
    
    async def delete_purchase_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("⚠️ *Удалить эту покупку?*", reply_markup=self.get_confirm_keyboard(), parse_mode='Markdown')
        return DELETE_CONFIRM
    
    async def delete_purchase_execute(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        if text == "❌ Нет, отмена":
            purchase_id = context.user_data.get('editing_purchase_id')
            await self.show_purchase_after_edit(update, context, purchase_id)
            return EDIT_PURCHASE_SELECT
        if text == "✅ Да, удалить":
            purchase_id = context.user_data.get('editing_purchase_id')
            if purchase_id and self.db.delete_purchase(purchase_id):
                await update.message.reply_text("✅ Покупка удалена!", reply_markup=self.get_main_keyboard())
            else:
                await update.message.reply_text("❌ Ошибка при удалении", reply_markup=self.get_main_keyboard())
        return ConversationHandler.END
    
    async def show_purchase_after_edit(self, update: Update, context: ContextTypes.DEFAULT_TYPE, purchase_id):
        purchase = self.db.get_purchase_by_id(purchase_id)
        if not purchase:
            await update.message.reply_text("❌ Покупка не найдена", reply_markup=self.get_main_keyboard())
            return
        try:
            date_display = datetime.strptime(purchase['date'], "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y %H:%M")
        except:
            date_display = purchase['date'][:10] if purchase['date'] else "N/A"
        drop_text = f" (падение {purchase.get('drop_percent', 0):.0f}%)" if purchase.get('drop_percent', 0) > 0 else ""
        await update.message.reply_text(f"✏️ *РЕДАКТИРОВАНИЕ ID: {purchase_id}*\n\n📅 Дата: `{date_display}`\n💰 Цена: `{format_price(purchase['price'], 4)}` USDT\n📊 Количество: `{format_quantity(purchase['quantity'], 6)}`{drop_text}", reply_markup=self.get_edit_purchases_keyboard(), parse_mode='Markdown')
    
    async def cancel_to_edit_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        purchase_id = context.user_data.get('editing_purchase_id')
        if purchase_id:
            await self.show_purchase_after_edit(update, context, purchase_id)
        else:
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_main_keyboard())
    
    # ============= НАСТРОЙКИ =============
    
    async def settings_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        await update.message.reply_text(
            f"⚙️ *Настройки*\n\n🪙 Токен: `{self.db.get_setting('symbol', 'TONUSDT')}`\n💵 Сумма: `{self.db.get_setting('invest_amount', '1.1')}` USDT\n📈 Прибыль: `{self.db.get_setting('profit_percent', '5')}%`\n⏰ Время: `{self.db.get_setting('schedule_time', '09:00')}`\n🔄 Частота: `{self.db.get_setting('frequency_hours', '24')}`ч",
            reply_markup=self.get_settings_keyboard(),
            parse_mode='Markdown'
        )
        return SELECTING_ACTION
    
    async def set_amount_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(f"💵 Введите сумму (текущая: {self.db.get_setting('invest_amount', '1.1')}):\n*Это базовая сумма для лестницы*", reply_markup=self.get_cancel_keyboard(), parse_mode='Markdown')
        return SET_AMOUNT
    
    async def set_amount_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text in ["❌ ОТМЕНА", "❌ Отмена"]:
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_settings_keyboard())
            return SELECTING_ACTION
        try:
            amount = float(text)
            if amount < 1:
                raise ValueError
            self.db.set_setting('invest_amount', str(amount))
            # Обновляем базовую сумму лестницы
            symbol = self.db.get_setting('symbol', 'TONUSDT')
            ladder = self.db.get_ladder_settings(symbol)
            ladder['base_amount'] = amount
            ladder['max_amount'] = amount * 3
            self.db.save_ladder_settings(ladder)
            await update.message.reply_text(f"✅ Сумма изменена на {amount} USDT\n🪜 Базовая сумма лестницы обновлена", reply_markup=self.get_settings_keyboard())
        except ValueError:
            await update.message.reply_text("❌ Некорректное значение", reply_markup=self.get_settings_keyboard())
        return SELECTING_ACTION
    
    async def set_profit_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(f"📊 Введите процент прибыли (текущий: {self.db.get_setting('profit_percent', '5')}%):", reply_markup=self.get_cancel_keyboard())
        return SET_PROFIT_PERCENT
    
    async def set_profit_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text in ["❌ ОТМЕНА", "❌ Отмена"]:
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_settings_keyboard())
            return SELECTING_ACTION
        try:
            percent = float(text)
            if percent < 0.1:
                raise ValueError
            self.db.set_setting('profit_percent', str(percent))
            await update.message.reply_text(f"✅ Процент изменен на {percent}%", reply_markup=self.get_settings_keyboard())
        except ValueError:
            await update.message.reply_text("❌ Некорректное значение", reply_markup=self.get_settings_keyboard())
        return SELECTING_ACTION
    
    async def set_drop_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(f"📉 Введите макс. падение % и множитель (текущие: {self.db.get_setting('max_drop_percent', '60')}% x{self.db.get_setting('max_multiplier', '3')}):\nНапример: 60 3", reply_markup=self.get_cancel_keyboard())
        return SET_MAX_DROP
    
    async def set_drop_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text in ["❌ ОТМЕНА", "❌ Отмена"]:
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_settings_keyboard())
            return SELECTING_ACTION
        try:
            parts = text.split()
            if len(parts) != 2:
                raise ValueError
            max_drop = float(parts[0])
            max_mult = float(parts[1])
            if max_drop < 10 or max_drop > 90 or max_mult < 1.5 or max_mult > 10:
                raise ValueError
            self.db.set_setting('max_drop_percent', str(max_drop))
            self.db.set_setting('max_multiplier', str(max_mult))
            # Обновляем глубину просадки лестницы
            symbol = self.db.get_setting('symbol', 'TONUSDT')
            ladder = self.db.get_ladder_settings(symbol)
            ladder['max_depth'] = max_drop
            self.db.save_ladder_settings(ladder)
            await update.message.reply_text(f"✅ Настройки обновлены: {max_drop}% x{max_mult}\n🪜 Глубина просадки лестницы обновлена", reply_markup=self.get_settings_keyboard())
        except Exception:
            await update.message.reply_text("❌ Ошибка формата. Используйте: 60 3", reply_markup=self.get_settings_keyboard())
        return SELECTING_ACTION
    
    async def set_time_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(f"⏰ Введите время (текущее: {self.db.get_setting('schedule_time', '09:00')}, формат ЧЧ:ММ):", reply_markup=self.get_cancel_keyboard())
        return SET_SCHEDULE_TIME
    
    async def set_time_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        time_str = update.message.text.strip()
        if time_str in ["❌ ОТМЕНА", "❌ Отмена"]:
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_settings_keyboard())
            return SELECTING_ACTION
        try:
            datetime.strptime(time_str, "%H:%M")
            self.db.set_setting('schedule_time', time_str)
            await update.message.reply_text(f"✅ Время изменено на {time_str}", reply_markup=self.get_settings_keyboard())
        except ValueError:
            await update.message.reply_text("❌ Некорректный формат. Используйте ЧЧ:ММ", reply_markup=self.get_settings_keyboard())
        return SELECTING_ACTION
    
    async def set_frequency_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(f"🔄 Введите частоту в часах (текущая: {self.db.get_setting('frequency_hours', '24')}):", reply_markup=self.get_cancel_keyboard())
        return SET_FREQUENCY_HOURS
    
    async def set_frequency_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text in ["❌ ОТМЕНА", "❌ Отмена"]:
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_settings_keyboard())
            return SELECTING_ACTION
        try:
            hours = int(text)
            if hours < 1 or hours > 720:
                raise ValueError
            self.db.set_setting('frequency_hours', str(hours))
            await update.message.reply_text(f"✅ Частота изменена на {hours} часов", reply_markup=self.get_settings_keyboard())
        except ValueError:
            await update.message.reply_text("❌ Введите число от 1 до 720", reply_markup=self.get_settings_keyboard())
        return SELECTING_ACTION
    
    # ============= ВЫБОР ТОКЕНА =============
    
    async def set_symbol_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return SELECTING_ACTION
        await update.message.reply_text(f"🪙 Выберите токен или введите свой\nТекущий: {self.db.get_setting('symbol', 'TONUSDT')}", reply_markup=self.get_symbol_selection_keyboard())
        return SELECTING_SYMBOL
    
    async def process_symbol_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_settings_keyboard())
            return SELECTING_ACTION
        if text == "✏️ Ввести свой токен":
            await update.message.reply_text("✏️ Введите символ токена (например: TONUSDT):", reply_markup=self.get_cancel_keyboard())
            return SET_SYMBOL_MANUAL
        if text in POPULAR_SYMBOLS:
            return await self._validate_and_set_symbol(update, text)
        else:
            await update.message.reply_text("❌ Неверный выбор.", reply_markup=self.get_symbol_selection_keyboard())
            return SELECTING_SYMBOL
    
    async def set_symbol_manual(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        symbol = update.message.text.upper().strip()
        if symbol in ["❌ ОТМЕНА", "❌ Отмена"]:
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_settings_keyboard())
            return SELECTING_ACTION
        return await self._validate_and_set_symbol(update, symbol)
    
    async def _validate_and_set_symbol(self, update: Update, symbol: str) -> int:
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.", reply_markup=self.get_settings_keyboard())
            return SELECTING_ACTION
        price = await self.bybit.get_symbol_price(symbol)
        if not price:
            await update.message.reply_text(f"❌ Символ {symbol} не найден.", reply_markup=self.get_symbol_selection_keyboard())
            return SELECTING_SYMBOL
        self.db.set_setting('symbol', symbol)
        self.db.set_setting('initial_reference_price', str(price))
        ladder = self.db.get_ladder_settings(symbol)
        if ladder['start_price'] <= 0:
            ladder['start_price'] = price
            ladder['last_buy_price'] = price
            self.db.save_ladder_settings(ladder)
        await update.message.reply_text(f"✅ Символ изменен на {symbol}\n💰 Текущая цена: {format_price(price, 4)} USDT", reply_markup=self.get_settings_keyboard())
        return SELECTING_ACTION
    
    # ============= ЭКСПОРТ/ИМПОРТ =============
    
    async def export_database_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return SELECTING_ACTION
        await update.message.reply_text("⏳ Экспортирую...")
        success, count, file_path = self.db.export_database()
        if success:
            await update.message.reply_text(f"✅ Экспортировано! Записей: {count}", reply_markup=self.get_settings_keyboard())
            try:
                with open(file_path, 'rb') as f:
                    await update.message.reply_document(document=InputFile(f, filename=DB_EXPORT_FILE), caption="💾 Файл базы данных")
            except Exception as e:
                await update.message.reply_text(f"❌ Ошибка отправки: {e}", reply_markup=self.get_settings_keyboard())
        else:
            await update.message.reply_text(f"❌ Ошибка экспорта: {file_path}", reply_markup=self.get_settings_keyboard())
        return SELECTING_ACTION
    
    async def import_database_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        await update.message.reply_text("📥 *ИМПОРТ БАЗЫ*\n\nОтправьте файл .json\n⚠️ *Все записи будут заменены!*", reply_markup=self.get_cancel_keyboard(), parse_mode='Markdown')
        return WAITING_IMPORT_FILE
    
    async def import_database_receive(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        if update.message.text and update.message.text.strip() == "❌ Отмена":
            await update.message.reply_text("❌ Импорт отменен", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        if not update.message.document or not update.message.document.file_name.endswith('.json'):
            await update.message.reply_text("❌ Отправьте файл .json", reply_markup=self.get_cancel_keyboard())
            return WAITING_IMPORT_FILE
        try:
            await update.message.reply_text("⏳ Импортирую...")
            file = await context.bot.get_file(update.message.document.file_id)
            temp_file = f"temp_import_{datetime.now().strftime('%Y%m%d%H%M%S')}.json"
            await file.download_to_drive(temp_file)
            success, message = self.db.import_database(temp_file)
            if os.path.exists(temp_file):
                os.remove(temp_file)
            if success:
                await update.message.reply_text(f"✅ {message}", reply_markup=self.get_main_keyboard())
            else:
                await update.message.reply_text(f"❌ Ошибка: {message}", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
    
    async def back_to_main(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data.clear()
        await update.message.reply_text("Главное меню:", reply_markup=self.get_main_keyboard())
        return ConversationHandler.END
    
    async def cancel_conversation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data.clear()
        await update.message.reply_text("Действие отменено", reply_markup=self.get_main_keyboard())
        return ConversationHandler.END
    
    async def handle_unknown(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return
        await update.message.reply_text("Используйте кнопки меню", reply_markup=self.get_main_keyboard())
    
    # ============= ПЛАНИРОВЩИК =============
    
    async def scheduler_loop(self):
        logger.info("Scheduler loop started")
        while self.scheduler_running:
            try:
                await asyncio.sleep(60)
                if self.db.get_setting('dca_active', 'false') != 'true':
                    continue
                if not self.bybit_initialized:
                    self._init_bybit()
                if not self.bybit_initialized:
                    continue
                symbol = self.db.get_setting('symbol', 'TONUSDT')
                profit_percent = float(self.db.get_setting('profit_percent', '5'))
                current_price = await self.bybit.get_symbol_price(symbol)
                if current_price:
                    ladder_info = self.db.calculate_ladder_purchase(current_price, symbol)
                    if ladder_info['should_buy']:
                        logger.info(f"Executing ladder purchase: {ladder_info}")
                        result = await self.strategy.execute_ladder_purchase(symbol, profit_percent)
                        if result['success'] and self.authorized_user_id:
                            msg = f"🪜 *ПОКУПКА!*\n📉 Падение: {result.get('drop_percent', 0):.1f}%\n💰 Сумма: {result['total_usdt']:.2f} USDT\n💵 Цена: {format_price(result['price'], 4)} USDT"
                            try:
                                await self.application.bot.send_message(chat_id=self.authorized_user_id, text=msg, parse_mode='Markdown')
                            except:
                                pass
                await self.strategy.check_and_update_sell_orders(symbol)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
    
    async def post_init(self, application):
        self.scheduler_running = True
        asyncio.create_task(self.scheduler_loop())
    
    def setup_handlers(self):
        logger.info("Setting up handlers...")
        
        self.application.add_handler(CommandHandler("start", self.cmd_start_fast))
        self.application.add_handler(CallbackQueryHandler(self.handle_order_callback, pattern='^order_'))
        
        # Основные кнопки
        self.application.add_handler(MessageHandler(filters.Regex('^(🚀 Запустить Авто DCA|⏹ Остановить Авто DCA)$'), self.toggle_dca))
        self.application.add_handler(MessageHandler(filters.Regex('^(📊 Мой Портфель)$'), self.show_portfolio))
        self.application.add_handler(MessageHandler(filters.Regex('^(📈 Статистика DCA)$'), self.show_dca_stats_detailed))
        self.application.add_handler(MessageHandler(filters.Regex('^(📋 Статус бота)$'), self.show_status))
        self.application.add_handler(MessageHandler(filters.Regex('^(📉 Текущая цена)$'), self.show_price))
        self.application.add_handler(MessageHandler(filters.Regex('^(🔔 Уведомления)$'), self.notification_settings_menu))
        
        # Conversation для лестницы - исправлен для корректной работы
        ladder_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex('^(🪜 Настройка лестницы)$'), self.ladder_settings_menu)],
            states={
                LADDER_MENU: [
                    MessageHandler(filters.Regex('^(💰 Цена старта)$'), self.set_ladder_start_price_start),
                    MessageHandler(filters.Regex('^(📊 Шаг падения \(%\))$'), self.set_ladder_step_percent_start),
                    MessageHandler(filters.Regex('^(📉 Глубина просадки \(%\))$'), self.set_ladder_max_depth_start),
                    MessageHandler(filters.Regex('^(💵 Базовая сумма)$'), self.set_ladder_base_amount_start),
                    MessageHandler(filters.Regex('^(💰 Максимальная сумма)$'), self.set_ladder_max_amount_start),
                    MessageHandler(filters.Regex('^(📋 Текущие настройки)$'), self.show_ladder_settings),
                    MessageHandler(filters.Regex('^(🔄 Сбросить лестницу)$'), self.reset_ladder),
                    MessageHandler(filters.Regex('^(🔙 Назад в меню)$'), self.back_to_main),
                ],
                SET_LADDER_START_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_ladder_start_price_save)],
                SET_LADDER_STEP_PERCENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_ladder_step_percent_save)],
                SET_LADDER_DEPTH: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_ladder_max_depth_save)],
                SET_LADDER_BASE_AMOUNT: [
                    MessageHandler(filters.Regex('^(💰 Максимальная сумма)$'), self.set_ladder_max_amount_save),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_ladder_base_amount_save),
                ],
            },
            fallbacks=[CommandHandler("cancel", self.cancel_conversation)],
            name="ladder_conversation",
            persistent=False,
        )
        self.application.add_handler(ladder_conv)
        
        # Основной ConversationHandler для настроек
        main_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex('^(⚙️ Настройки)$'), self.settings_menu)],
            states={
                SELECTING_ACTION: [
                    MessageHandler(filters.Regex('^(🪙 Выбор токена)$'), self.set_symbol_start),
                    MessageHandler(filters.Regex('^(💵 Сумма покупки)$'), self.set_amount_start),
                    MessageHandler(filters.Regex('^(📊 Процент прибыли)$'), self.set_profit_start),
                    MessageHandler(filters.Regex('^(📉 Настройки падения)$'), self.set_drop_start),
                    MessageHandler(filters.Regex('^(⏰ Время покупки)$'), self.set_time_start),
                    MessageHandler(filters.Regex('^(🔄 Частота покупки)$'), self.set_frequency_start),
                    MessageHandler(filters.Regex('^(🔔 Настройки уведомлений)$'), self.notification_settings_menu),
                    MessageHandler(filters.Regex('^(📤 Экспорт базы)$'), self.export_database_handler),
                    MessageHandler(filters.Regex('^(📥 Импорт базы)$'), self.import_database_start),
                    MessageHandler(filters.Regex('^(🔙 Назад в меню)$'), self.back_to_main),
                ],
                SELECTING_SYMBOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.process_symbol_selection)],
                SET_SYMBOL_MANUAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_symbol_manual)],
                SET_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_amount_done)],
                SET_PROFIT_PERCENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_profit_done)],
                SET_MAX_DROP: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_drop_done)],
                SET_SCHEDULE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_time_done)],
                SET_FREQUENCY_HOURS: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_frequency_done)],
                WAITING_ALERT_PERCENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_alert_percent_save)],
                WAITING_ALERT_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_alert_interval_save)],
                WAITING_IMPORT_FILE: [
                    MessageHandler(filters.Document.FileExtension("json"), self.import_database_receive),
                    MessageHandler(filters.Regex("^❌ Отмена$"), self.import_database_receive),
                ],
            },
            fallbacks=[CommandHandler("cancel", self.cancel_conversation)],
            name="main_conversation",
            persistent=False,
        )
        self.application.add_handler(main_conv)
        
        # Управление ордерами
        orders_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex('^(📝 Управление ордерами)$'), self.orders_menu)],
            states={
                MANAGE_ORDERS: [
                    MessageHandler(filters.Regex('^(📋 Список ордеров)$'), self.list_orders),
                    MessageHandler(filters.Regex('^(❌ Удалить ордер)$'), self.delete_order_start),
                    MessageHandler(filters.Regex('^(✏️ Изменить цену)$'), self.edit_order_start),
                    MessageHandler(filters.Regex('^(🔙 Назад в меню)$'), self.back_to_main),
                ],
                EDIT_ORDER_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.edit_order_done)],
            },
            fallbacks=[CommandHandler("cancel", self.cancel_conversation)],
            name="orders_conversation",
            persistent=False,
        )
        self.application.add_handler(orders_conv)
        
        # Ручная покупка
        manual_limit_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex('^(💰 Ручная покупка \(лимит\))$'), self.manual_buy_start)],
            states={
                MANUAL_BUY_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.manual_buy_price_done)],
                MANUAL_BUY_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.manual_buy_amount_done)],
            },
            fallbacks=[CommandHandler("cancel", self.cancel_conversation)],
            name="manual_buy_conversation",
            persistent=False,
        )
        self.application.add_handler(manual_limit_conv)
        
        # Ручное добавление покупки
        manual_add_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex('^(➕ Добавить покупку вручную)$'), self.manual_add_start)],
            states={
                MANUAL_ADD_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.manual_add_price)],
                MANUAL_ADD_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.manual_add_amount)],
            },
            fallbacks=[CommandHandler("cancel", self.cancel_conversation)],
            name="manual_add_conversation",
            persistent=False,
        )
        self.application.add_handler(manual_add_conv)
        
        # Редактирование покупок
        edit_purchases_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex('^(✏️ Редактировать покупки)$'), self.edit_purchases_list)],
            states={
                EDIT_PURCHASE_SELECT: [
                    MessageHandler(filters.Regex('^(💰 Изменить цену)$'), self.edit_price_start),
                    MessageHandler(filters.Regex('^(📊 Изменить количество)$'), self.edit_amount_start),
                    MessageHandler(filters.Regex('^(📅 Изменить дату)$'), self.edit_date_start),
                    MessageHandler(filters.Regex('^(❌ Удалить покупку)$'), self.delete_purchase_confirm),
                    MessageHandler(filters.Regex('^(🔙 Назад к списку)$'), self.edit_purchases_list),
                    MessageHandler(filters.Regex('^(🏠 Главное меню)$'), self.back_to_main),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.edit_purchase_selected),
                ],
                EDIT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.edit_price_save)],
                EDIT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.edit_amount_save)],
                EDIT_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.edit_date_save)],
                DELETE_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.delete_purchase_execute)],
            },
            fallbacks=[CommandHandler("cancel", self.cancel_conversation)],
            name="edit_purchases_conversation",
            persistent=False,
        )
        self.application.add_handler(edit_purchases_conv)
        
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_unknown))
        
        logger.info("Handlers setup completed")
    
    def run(self):
        print(f"\n{Fore.CYAN}{'='*60}")
        print(f"{Fore.CYAN}🚀 ЗАПУСК DCA BYBIT BOT (МАРТИНГЕЙЛ ЛЕСЕНКОЙ)")
        print(f"{Fore.CYAN}{'='*60}")
        
        if not TELEGRAM_TOKEN:
            print(f"{Fore.RED}❌ TELEGRAM_BOT_TOKEN не найден!")
            return
        
        print(f"{Fore.GREEN}✅ Токен: {TELEGRAM_TOKEN[:10]}...{TELEGRAM_TOKEN[-5:]}")
        print(f"{Fore.WHITE}👤 Пользователь: {AUTHORIZED_USER}")
        print(f"{Fore.WHITE}🌐 Testnet: {'Да' if BYBIT_TESTNET else 'Нет'}")
        print(f"{Fore.WHITE}💾 База данных: dca_bot.db (данные сохраняются)")
        print(f"{Fore.CYAN}{'='*60}\n")
        
        self.application.post_init = self.post_init
        
        try:
            self.application.run_polling(allowed_updates=Update.ALL_TYPES, poll_interval=1.0, timeout=60)
        except Exception as e:
            logger.error(f"Failed to start bot: {e}")
            print(f"{Fore.RED}❌ Ошибка: {e}")


if __name__ == "__main__":
    try:
        import colorama
    except ImportError:
        print("Устанавливаю colorama...")
        os.system(f"{sys.executable} -m pip install colorama")
        import colorama
    
    bot = FastDCABot()
    bot.run()