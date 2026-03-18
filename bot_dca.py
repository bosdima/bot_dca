#!/usr/bin/env python3
"""
DCA Bybit Trading Bot - ИСПРАВЛЕННАЯ ВЕРСИЯ с экспортом/импортом
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
from functools import lru_cache
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
) = range(23)  # 23 состояния

# Файл для экспорта/импорта
DB_EXPORT_FILE = 'dca_data_export.json'

# ==================== ПРОВЕРКА TELEGRAM API ====================

async def check_telegram_connection() -> Tuple[bool, float, str]:
    """
    Проверяет подключение к Telegram Bot API
    Возвращает: (успех, пинг в мс, сообщение)
    """
    print(f"\n{Fore.CYAN}{'='*60}")
    print(f"{Fore.CYAN}ПРОВЕРКА ПОДКЛЮЧЕНИЯ К TELEGRAM BOT API")
    print(f"{Fore.CYAN}{'='*60}")
    
    if not TELEGRAM_TOKEN:
        print(f"{Fore.RED}✗ TELEGRAM_BOT_TOKEN не найден в .env файле!")
        return False, 0, "Токен не найден"
    
    print(f"{Fore.WHITE}Токен: {TELEGRAM_TOKEN[:10]}...{TELEGRAM_TOKEN[-5:]}")
    
    try:
        # Проверяем общее подключение к интернету
        print(f"{Fore.WHITE}Проверка интернет-соединения...", end='')
        try:
            socket.create_connection(("8.8.8.8", 53), timeout=3)
            print(f"{Fore.GREEN} ✓ OK")
        except OSError:
            print(f"{Fore.RED} ✗ НЕТ ИНТЕРНЕТА")
            return False, 0, "Нет подключения к интернету"
        
        # Проверяем подключение к Telegram API
        print(f"{Fore.WHITE}Подключение к Telegram API...", end='')
        start_time = time.time()
        
        timeout = aiohttp.ClientTimeout(total=10)
        connector = aiohttp.TCPConnector(ssl=False)
        
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async with session.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe") as response:
                elapsed = (time.time() - start_time) * 1000
                
                if response.status == 200:
                    data = await response.json()
                    if data.get('ok'):
                        bot_info = data.get('result', {})
                        bot_name = bot_info.get('first_name', 'Unknown')
                        bot_username = bot_info.get('username', 'Unknown')
                        
                        print(f"{Fore.GREEN} ✓ ПОДКЛЮЧЕНО")
                        print(f"{Fore.WHITE}Бот: @{bot_username} ({bot_name})")
                        print(f"{Fore.WHITE}Пинг: {Fore.YELLOW}{elapsed:.1f}ms")
                        print(f"{Fore.GREEN}{'='*60}\n")
                        
                        return True, elapsed, f"Подключено к @{bot_username}"
                    else:
                        print(f"{Fore.RED} ✗ ОШИБКА API")
                        return False, 0, "Ошибка API Telegram"
                else:
                    print(f"{Fore.RED} ✗ HTTP {response.status}")
                    return False, 0, f"HTTP ошибка {response.status}"
                    
    except asyncio.TimeoutError:
        print(f"{Fore.RED} ✗ ТАЙМАУТ")
        return False, 0, "Таймаут подключения"
    except aiohttp.ClientConnectorError as e:
        print(f"{Fore.RED} ✗ ОШИБКА СОЕДИНЕНИЯ")
        return False, 0, f"Ошибка соединения: {str(e)[:50]}"
    except Exception as e:
        print(f"{Fore.RED} ✗ НЕИЗВЕСТНАЯ ОШИБКА")
        return False, 0, str(e)[:50]

# ==================== DATABASE ====================

class Database:
    def __init__(self, db_file: str = "dca_bot.db"):
        self.db_file = db_file
        self.settings_cache = {}
        self.cache_ttl = 60
        self.cache_timestamp = 0
        self.init_db()
    
    def init_db(self):
        """Быстрая инициализация БД"""
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
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
            
            # Дефолтные настройки
            defaults = [
                ('symbol', 'BTCUSDT'),
                ('invest_amount', '1'),
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
            ]
            
            for key, value in defaults:
                cursor.execute('''
                    INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)
                ''', (key, value))
            
            # Дефолтные настройки уведомлений
            cursor.execute('''
                INSERT OR IGNORE INTO notifications (id, enabled, alert_percent, alert_interval_minutes, last_check)
                VALUES (1, 1, 10.0, 30, CURRENT_TIMESTAMP)
            ''')
            
            conn.commit()
            conn.close()
            logger.info("Database initialized successfully")
        except Exception as e:
            logger.error(f"DB init error: {e}")
    
    def get_setting(self, key: str, default: str = '') -> str:
        """Получить настройку с кэшированием"""
        now = time.time()
        if now - self.cache_timestamp < self.cache_ttl and key in self.settings_cache:
            return self.settings_cache[key]
        
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('SELECT value FROM settings WHERE key = ?', (key,))
            result = cursor.fetchone()
            conn.close()
            
            value = result[0] if result else default
            self.settings_cache[key] = value
            self.cache_timestamp = now
            return value
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
            self.settings_cache[key] = value
            self.cache_timestamp = time.time()
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
                return {
                    'enabled': bool(row[0]),
                    'alert_percent': row[1],
                    'alert_interval_minutes': row[2]
                }
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
                values.append(1)  # для WHERE id = 1
                cursor.execute(f"UPDATE notifications SET {', '.join(updates)}, last_check = CURRENT_TIMESTAMP WHERE id = ?", values)
                conn.commit()
            
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Error updating notification settings: {e}")
            return False
    
    def add_purchase(self, symbol: str, amount_usdt: float, price: float, 
                     quantity: float, multiplier: float = 1.0, drop_percent: float = 0,
                     date: str = None):
        """Добавить покупку"""
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO dca_purchases 
                (symbol, amount_usdt, price, quantity, multiplier, drop_percent, date)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (symbol, amount_usdt, price, quantity, multiplier, drop_percent, date))
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
                cursor.execute('''
                    SELECT * FROM dca_purchases 
                    WHERE symbol = ? 
                    ORDER BY date ASC
                ''', (symbol,))
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
        allowed_fields = ['symbol', 'amount_usdt', 'price', 'quantity', 'multiplier', 'drop_percent', 'date']
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
            
            # Переиндексируем ID после удаления
            if success:
                self.reindex_purchases()
            
            return success
        except Exception as e:
            logger.error(f"Error deleting purchase {purchase_id}: {e}")
            return False
    
    def reindex_purchases(self):
        """Переиндексировать ID покупок по порядку дат"""
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            
            # Получаем все покупки отсортированные по дате
            cursor.execute("SELECT symbol, amount_usdt, price, quantity, multiplier, drop_percent, date FROM dca_purchases ORDER BY date ASC")
            purchases = cursor.fetchall()
            
            # Создаем временную таблицу
            cursor.execute("DROP TABLE IF EXISTS dca_purchases_temp")
            cursor.execute('''
                CREATE TABLE dca_purchases_temp (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    amount_usdt REAL NOT NULL,
                    price REAL NOT NULL,
                    quantity REAL NOT NULL,
                    multiplier REAL DEFAULT 1.0,
                    drop_percent REAL DEFAULT 0,
                    date TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Вставляем данные в правильном порядке
            for purchase in purchases:
                cursor.execute('''
                    INSERT INTO dca_purchases_temp 
                    (symbol, amount_usdt, price, quantity, multiplier, drop_percent, date)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', purchase)
            
            # Переименовываем таблицы
            cursor.execute("DROP TABLE dca_purchases")
            cursor.execute("ALTER TABLE dca_purchases_temp RENAME TO dca_purchases")
            
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Error reindexing purchases: {e}")
            return False
    
    def get_dca_stats(self, symbol: str) -> Dict:
        """Получить статистику DCA"""
        purchases = self.get_purchases(symbol)
        if not purchases:
            return None
        
        total_usdt = sum(p['amount_usdt'] for p in purchases)
        total_qty = sum(p['quantity'] for p in purchases)
        avg_price = total_usdt / total_qty if total_qty > 0 else 0
        
        prices = [p['price'] for p in purchases]
        min_price = min(prices) if prices else 0
        max_price = max(prices) if prices else 0
        last_purchases = purchases[-5:] if len(purchases) > 5 else purchases
        
        return {
            'total_purchases': len(purchases),
            'total_usdt': total_usdt,
            'total_quantity': total_qty,
            'avg_price': avg_price,
            'min_price': min_price,
            'max_price': max_price,
            'first_purchase': purchases[0]['date'] if purchases else None,
            'last_purchase': purchases[-1]['date'] if purchases else None,
            'last_purchases': last_purchases,
        }
    
    def add_sell_order(self, symbol: str, order_id: str, quantity: float, 
                       target_price: float, profit_percent: float):
        """Добавить ордер на продажу"""
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            try:
                cursor.execute('''
                    INSERT INTO sell_orders 
                    (symbol, order_id, quantity, target_price, profit_percent)
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
        """Получить активные ордера"""
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            if symbol:
                cursor.execute('''
                    SELECT * FROM sell_orders 
                    WHERE symbol = ? AND status = 'active'
                    ORDER BY created_at DESC
                ''', (symbol,))
            else:
                cursor.execute('''
                    SELECT * FROM sell_orders 
                    WHERE status = 'active'
                    ORDER BY created_at DESC
                ''')
            
            rows = cursor.fetchall()
            conn.close()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Error getting active sell orders: {e}")
            return []
    
    def get_order_by_id(self, order_id: str) -> Optional[Dict]:
        """Получить ордер по ID"""
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM sell_orders WHERE order_id = ?', (order_id,))
            row = cursor.fetchone()
            conn.close()
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"Error getting order {order_id}: {e}")
            return None
    
    def update_sell_order_status(self, order_id: str, status: str):
        """Обновить статус ордера"""
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE sell_orders SET status = ? WHERE order_id = ?
            ''', (status, order_id))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error updating order status: {e}")
    
    def update_order_price(self, order_id: str, new_price: float, new_profit_percent: float):
        """Обновить цену ордера"""
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE sell_orders SET target_price = ?, profit_percent = ? WHERE order_id = ?
            ''', (new_price, new_profit_percent, order_id))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error updating order price: {e}")
    
    def delete_order(self, order_id: str):
        """Удалить ордер"""
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM sell_orders WHERE order_id = ?', (order_id,))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error deleting order: {e}")
    
    def log_action(self, action: str, symbol: str = None, details: str = None):
        """Логировать действие"""
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO history (action, symbol, details) VALUES (?, ?, ?)
            ''', (action, symbol, details))
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
            cursor.execute('''
                INSERT INTO dca_start (id, start_date, symbol, initial_price)
                VALUES (1, CURRENT_TIMESTAMP, ?, ?)
            ''', (symbol, initial_price))
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
    
    def export_database(self) -> Tuple[bool, int, str]:
        """Экспортировать базу данных в JSON"""
        try:
            purchases = self.get_purchases()
            sell_orders = self.get_active_sell_orders()
            settings = {}
            
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('SELECT key, value FROM settings')
            settings_rows = cursor.fetchall()
            for key, value in settings_rows:
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
            
            conn.close()
            
            export_data = {
                'export_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'version': '1.0',
                'purchases': purchases,
                'sell_orders': sell_orders,
                'settings': settings,
                'notifications': notifications,
                'dca_start': dca_start
            }
            
            with open(DB_EXPORT_FILE, 'w', encoding='utf-8') as f:
                json.dump(export_data, f, indent=2, ensure_ascii=False, default=str)
            
            return True, len(purchases), DB_EXPORT_FILE
        except Exception as e:
            logger.error(f"Error exporting database: {e}")
            return False, 0, str(e)
    
    def import_database(self, file_path: str) -> Tuple[bool, str]:
        """Импортировать базу данных из JSON"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            
            # Очищаем таблицы
            cursor.execute("DELETE FROM dca_purchases")
            cursor.execute("DELETE FROM sell_orders")
            cursor.execute("DELETE FROM settings")
            cursor.execute("DELETE FROM dca_start")
            
            # Импортируем покупки
            purchases_imported = 0
            for purchase in data.get('purchases', []):
                cursor.execute('''
                    INSERT INTO dca_purchases 
                    (id, symbol, amount_usdt, price, quantity, multiplier, drop_percent, date, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    purchase.get('id'),
                    purchase.get('symbol', 'BTCUSDT'),
                    purchase.get('amount_usdt', 0),
                    purchase.get('price', 0),
                    purchase.get('quantity', 0),
                    purchase.get('multiplier', 1.0),
                    purchase.get('drop_percent', 0),
                    purchase.get('date', datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                    purchase.get('created_at', datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                ))
                purchases_imported += 1
            
            # Импортируем ордера
            orders_imported = 0
            for order in data.get('sell_orders', []):
                try:
                    cursor.execute('''
                        INSERT OR IGNORE INTO sell_orders 
                        (id, symbol, order_id, quantity, target_price, profit_percent, created_at, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        order.get('id'),
                        order.get('symbol', 'BTCUSDT'),
                        order.get('order_id', f"imported_{order.get('id', 0)}"),
                        order.get('quantity', 0),
                        order.get('target_price', 0),
                        order.get('profit_percent', 5),
                        order.get('created_at', datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                        order.get('status', 'active')
                    ))
                    orders_imported += 1
                except:
                    pass
            
            # Импортируем настройки
            settings_imported = 0
            for key, value in data.get('settings', {}).items():
                cursor.execute('''
                    INSERT OR REPLACE INTO settings (key, value, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                ''', (key, value))
                settings_imported += 1
            
            # Импортируем DCA start
            dca_start = data.get('dca_start')
            if dca_start and dca_start.get('start_date'):
                cursor.execute('''
                    INSERT OR REPLACE INTO dca_start (id, start_date, symbol, initial_price)
                    VALUES (1, ?, ?, ?)
                ''', (dca_start['start_date'], dca_start['symbol'], dca_start['initial_price']))
            
            # Импортируем настройки уведомлений
            notifications = data.get('notifications', {})
            if notifications:
                cursor.execute('''
                    UPDATE notifications SET 
                        enabled = ?,
                        alert_percent = ?,
                        alert_interval_minutes = ?,
                        last_check = CURRENT_TIMESTAMP
                    WHERE id = 1
                ''', (
                    1 if notifications.get('enabled', True) else 0,
                    notifications.get('alert_percent', 10.0),
                    notifications.get('alert_interval_minutes', 30)
                ))
            
            conn.commit()
            conn.close()
            
            # Сбрасываем кэш
            self.cache_timestamp = 0
            self.settings_cache = {}
            
            return True, f"Импортировано: {purchases_imported} покупок, {orders_imported} ордеров, {settings_imported} настроек"
            
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
        self.account_type = None
        self._init_session()
    
    def _init_session(self):
        """Инициализация сессии"""
        try:
            self.session = HTTP(
                testnet=self.testnet,
                api_key=self.api_key,
                api_secret=self.api_secret,
                recv_window=5000,
            )
            logger.info("Bybit session initialized")
        except Exception as e:
            logger.error(f"Session init error: {e}")
    
    async def test_connection(self) -> Tuple[bool, str]:
        """Проверка соединения"""
        try:
            if not self.session:
                self._init_session()
            response = self.session.get_account_info()
            if response['retCode'] == 0:
                return True, "✅ Bybit API подключен"
            else:
                return False, f"❌ Bybit API ошибка: {response['retMsg']}"
        except Exception as e:
            return False, f"❌ Bybit API ошибка подключения: {str(e)}"
    
    @lru_cache(maxsize=10)
    async def get_symbol_price(self, symbol: str) -> Optional[float]:
        """Получить текущую цену (с кэшированием)"""
        try:
            if not self.session:
                self._init_session()
            response = self.session.get_tickers(category="spot", symbol=symbol)
            if response['retCode'] == 0 and response['result']['list']:
                return float(response['result']['list'][0]['lastPrice'])
            return None
        except Exception as e:
            logger.error(f"Error getting price for {symbol}: {e}")
            return None
    
    async def get_balance(self, coin: str = None) -> Dict:
        """Получение баланса"""
        try:
            if not self.session:
                self._init_session()
            
            try:
                # Получаем баланс UNIFIED аккаунта
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
                                    
                                    return {
                                        'coin': coin,
                                        'equity': equity or wallet_balance,
                                        'walletBalance': wallet_balance,
                                        'available': available,
                                        'usdValue': usd_value,
                                    }
                            
                            return {'coin': coin, 'equity': 0, 'available': 0, 'usdValue': 0}
                        else:
                            total_equity = float(account_data.get('totalEquity', 0) or 0)
                            return {
                                'total_equity': total_equity,
                                'coins': coins,
                            }
            except Exception as e:
                logger.warning(f"wallet_balance failed: {e}")
            
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
    
    async def cancel_order(self, symbol: str, order_id: str) -> Dict:
        """Отменить ордер"""
        try:
            if not self.session:
                self._init_session()
            
            response = self.session.cancel_order(
                category="spot",
                symbol=symbol,
                orderId=order_id
            )
            if response['retCode'] == 0:
                return {'success': True, 'result': response['result']}
            else:
                return {'success': False, 'error': response['retMsg']}
        except Exception as e:
            logger.error(f"Error cancelling order: {e}")
            return {'success': False, 'error': str(e)}
    
    async def amend_order_price(self, symbol: str, order_id: str, new_price: float) -> Dict:
        """Изменить цену ордера"""
        try:
            if not self.session:
                self._init_session()
            
            response = self.session.amend_order(
                category="spot",
                symbol=symbol,
                orderId=order_id,
                price=str(new_price)
            )
            if response['retCode'] == 0:
                return {'success': True, 'result': response['result']}
            else:
                return {'success': False, 'error': response['retMsg']}
        except Exception as e:
            logger.error(f"Error amending order: {e}")
            return {'success': False, 'error': str(e)}
    
    async def place_limit_sell(self, symbol: str, quantity: float, price: float) -> Dict:
        """Лимитный ордер на продажу"""
        try:
            if not self.session:
                self._init_session()
            
            response = self.session.place_order(
                category="spot",
                symbol=symbol,
                side="Sell",
                orderType="Limit",
                qty=str(quantity),
                price=str(price),
                timeInForce="GTC",
            )
            
            if response['retCode'] == 0:
                return {
                    'success': True,
                    'order_id': response['result']['orderId'],
                    'quantity': quantity,
                    'price': price,
                }
            else:
                return {'success': False, 'error': response['retMsg']}
                
        except Exception as e:
            logger.error(f"Error in place_limit_sell: {e}")
            return {'success': False, 'error': str(e)}
    
    async def place_market_buy(self, symbol: str, amount_usdt: float) -> Dict:
        """Рыночная покупка"""
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
                return {'success': False, 'error': f'Минимальная сумма ордера: {min_order_amt} USDT'}
            
            price = await self.get_symbol_price(symbol)
            if not price:
                return {'success': False, 'error': 'Не удалось получить цену'}
            
            quantity = amount_usdt / price
            quantity = Decimal(str(quantity)).quantize(
                Decimal(str(lot_size)), rounding=ROUND_DOWN
            )
            
            if float(quantity) < min_qty:
                return {'success': False, 'error': f'Минимальное количество: {min_qty}'}
            
            response = self.session.place_order(
                category="spot",
                symbol=symbol,
                side="Buy",
                orderType="Market",
                qty=str(quantity),
            )
            
            if response['retCode'] == 0:
                order_id = response['result']['orderId']
                await asyncio.sleep(1)
                order_details = self.session.get_order_history(
                    category="spot",
                    orderId=order_id
                )
                
                avg_price = price
                if order_details['retCode'] == 0 and order_details['result']['list']:
                    avg_price = float(order_details['result']['list'][0].get('avgPrice', price))
                
                return {
                    'success': True,
                    'order_id': order_id,
                    'quantity': float(quantity),
                    'price': avg_price,
                    'total_usdt': amount_usdt,
                }
            else:
                return {'success': False, 'error': response['retMsg']}
                
        except Exception as e:
            logger.error(f"Error in place_market_buy: {e}")
            return {'success': False, 'error': str(e)}
    
    async def place_limit_buy(self, symbol: str, price: float, amount_usdt: float) -> Dict:
        """Лимитный ордер на покупку"""
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
                return {'success': False, 'error': f'Минимальная сумма ордера: {min_order_amt} USDT'}
            
            quantity = amount_usdt / price
            quantity = Decimal(str(quantity)).quantize(
                Decimal(str(lot_size)), rounding=ROUND_DOWN
            )
            
            if float(quantity) < min_qty:
                return {'success': False, 'error': f'Минимальное количество: {min_qty}'}
            
            response = self.session.place_order(
                category="spot",
                symbol=symbol,
                side="Buy",
                orderType="Limit",
                qty=str(quantity),
                price=str(price),
                timeInForce="GTC",
            )
            
            if response['retCode'] == 0:
                return {
                    'success': True,
                    'order_id': response['result']['orderId'],
                    'quantity': float(quantity),
                    'price': price,
                    'total_usdt': amount_usdt,
                }
            else:
                return {'success': False, 'error': response['retMsg']}
                
        except Exception as e:
            logger.error(f"Error in place_limit_buy: {e}")
            return {'success': False, 'error': str(e)}

# ==================== DCA STRATEGY ====================

class DCAStrategy:
    def __init__(self, db: Database, bybit: BybitClient):
        self.db = db
        self.bybit = bybit
    
    def calculate_multiplier(self, drop_percent: float, max_drop: float = 60, 
                           max_multiplier: float = 3.0) -> float:
        """Расчет коэффициента покупки"""
        if drop_percent <= 0:
            return 1.0
        
        if drop_percent >= max_drop:
            return max_multiplier
        
        multiplier = 1.0 + (drop_percent / max_drop) * (max_multiplier - 1.0)
        return round(multiplier, 2)
    
    async def get_drop_percent(self, symbol: str) -> float:
        """Рассчитать процент падения"""
        initial_price_str = self.db.get_setting('initial_reference_price', '0')
        last_price_str = self.db.get_setting('last_purchase_price', '0')
        
        current_price = await self.bybit.get_symbol_price(symbol)
        if not current_price:
            return 0.0
        
        if float(initial_price_str) > 0:
            initial_price = float(initial_price_str)
            drop_percent = ((initial_price - current_price) / initial_price) * 100
            return max(0, drop_percent)
        
        if float(last_price_str) > 0:
            last_price = float(last_price_str)
            drop_percent = ((last_price - current_price) / last_price) * 100
            return max(0, drop_percent)
        
        self.db.set_setting('initial_reference_price', str(current_price))
        return 0.0
    
    async def execute_dca_purchase(self, symbol: str, base_amount: float, 
                                   profit_percent: float) -> Dict:
        """Выполнить DCA покупку"""
        max_drop = float(self.db.get_setting('max_drop_percent', '60'))
        max_multiplier = float(self.db.get_setting('max_multiplier', '3'))
        
        drop_percent = await self.get_drop_percent(symbol)
        multiplier = self.calculate_multiplier(drop_percent, max_drop, max_multiplier)
        
        final_amount = base_amount * multiplier
        
        result = await self.bybit.place_market_buy(symbol, final_amount)
        
        if result['success']:
            current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            self.db.add_purchase(
                symbol=symbol,
                amount_usdt=result['total_usdt'],
                price=result['price'],
                quantity=result['quantity'],
                multiplier=multiplier,
                drop_percent=drop_percent,
                date=current_date
            )
            
            self.db.set_setting('last_purchase_price', str(result['price']))
            self.db.set_setting('last_purchase_time', str(datetime.now().timestamp()))
            
            target_price = result['price'] * (1 + profit_percent / 100)
            sell_result = await self.bybit.place_limit_sell(
                symbol, result['quantity'], target_price
            )
            
            if sell_result['success']:
                self.db.add_sell_order(
                    symbol=symbol,
                    order_id=sell_result['order_id'],
                    quantity=result['quantity'],
                    target_price=target_price,
                    profit_percent=profit_percent
                )
                result['sell_order_id'] = sell_result['order_id']
                result['target_price'] = target_price
            
            result['multiplier'] = multiplier
            result['drop_percent'] = drop_percent
            
            self.db.log_action(
                'DCA_PURCHASE',
                symbol,
                f"Куплено {result['quantity']} {symbol} за {result['total_usdt']} USDT "
                f"(множитель: {multiplier}, падение: {drop_percent:.2f}%)"
            )
        
        return result
    
    async def check_and_update_sell_orders(self, symbol: str):
        """Проверить статус ордеров"""
        active_orders = self.db.get_active_sell_orders(symbol)
        open_orders = await self.bybit.get_open_orders(symbol)
        open_order_ids = {o['orderId'] for o in open_orders}
        
        for order in active_orders:
            if order['order_id'] not in open_order_ids:
                self.db.update_sell_order_status(order['order_id'], 'completed')
                self.db.log_action(
                    'SELL_COMPLETED',
                    symbol,
                    f"Продано по цене {order['target_price']} (прибыль {order['profit_percent']}%)"
                )
                
                stats = self.db.get_dca_stats(symbol)
                if stats and stats['total_quantity'] <= 0:
                    self.db.set_setting('initial_reference_price', '0')
                    self.db.set_setting('last_purchase_price', '0')

# ==================== FAST DCA BOT ====================

class FastDCABot:
    def __init__(self):
        self.db = Database()
        self.bybit = None
        self.strategy = None
        self.bybit_initialized = False
        
        # Увеличиваем таймауты для стабильности
        request_kwargs = {
            'connect_timeout': 30.0,
            'read_timeout': 30.0,
            'write_timeout': 30.0,
            'pool_timeout': 30.0,
        }
        
        request = HTTPXRequest(**request_kwargs)
        self.application = Application.builder().token(TELEGRAM_TOKEN).request(request).build()
        
        self.scheduler_task = None
        self.authorized_user_id = None
        self.pending_order_action = {}
        self.last_purchase_minute = None
        self.scheduler_running = False
        
        self.setup_handlers()
    
    def _init_bybit(self):
        """Ленивая инициализация Bybit"""
        if not self.bybit_initialized and BYBIT_API_KEY and BYBIT_API_SECRET:
            try:
                self.bybit = BybitClient(BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_TESTNET)
                self.strategy = DCAStrategy(self.db, self.bybit)
                self.bybit_initialized = True
                logger.info("Bybit client initialized successfully")
            except Exception as e:
                logger.error(f"Bybit init error: {e}")
    
    def get_main_keyboard(self):
        """Главная клавиатура"""
        is_active = self.db.get_setting('dca_active', 'false') == 'true'
        dca_button = "⏹ Остановить Авто DCA" if is_active else "🚀 Запустить Авто DCA"
        
        keyboard = [
            [KeyboardButton("📊 Мой Портфель"), KeyboardButton(dca_button)],
            [KeyboardButton("💰 Ручная покупка (лимит)"), KeyboardButton("📈 Статистика DCA")],
            [KeyboardButton("➕ Добавить покупку вручную"), KeyboardButton("✏️ Редактировать покупки")],
            [KeyboardButton("⚙️ Настройки"), KeyboardButton("📋 Статус бота")],
            [KeyboardButton("📉 Текущая цена"), KeyboardButton("📝 Управление ордерами")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_settings_keyboard(self):
        """Клавиатура настроек"""
        keyboard = [
            [KeyboardButton("🪙 Выбор токена"), KeyboardButton("💵 Сумма покупки")],
            [KeyboardButton("📊 Процент прибыли"), KeyboardButton("📉 Настройки падения")],
            [KeyboardButton("⏰ Время покупки"), KeyboardButton("🔄 Частота покупки")],
            [KeyboardButton("🔔 Настройки уведомлений"), KeyboardButton("📤 Экспорт базы")],
            [KeyboardButton("📥 Импорт базы"), KeyboardButton("🔙 Назад в меню")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_notification_settings_keyboard(self):
        """Клавиатура настроек уведомлений"""
        keyboard = [
            [KeyboardButton("📊 Процент для уведомления"), KeyboardButton("⏱ Частота проверки")],
            [KeyboardButton("🔔 Вкл/Выкл уведомления"), KeyboardButton("📋 Текущие настройки")],
            [KeyboardButton("🔙 Назад в меню")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_orders_management_keyboard(self):
        """Клавиатура управления ордерами"""
        keyboard = [
            [KeyboardButton("📋 Список ордеров"), KeyboardButton("❌ Удалить ордер")],
            [KeyboardButton("✏️ Изменить цену"), KeyboardButton("🔙 Назад в меню")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_edit_purchases_keyboard(self):
        """Клавиатура для редактирования покупок"""
        keyboard = [
            [KeyboardButton("💰 Изменить цену"), KeyboardButton("📊 Изменить количество")],
            [KeyboardButton("📅 Изменить дату"), KeyboardButton("❌ Удалить покупку")],
            [KeyboardButton("🔙 Назад к списку"), KeyboardButton("🏠 Главное меню")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_cancel_keyboard(self):
        """Клавиатура с кнопкой отмены"""
        return ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
    
    def get_confirm_keyboard(self):
        """Клавиатура подтверждения"""
        keyboard = [
            [KeyboardButton("✅ Да, удалить"), KeyboardButton("❌ Нет, отмена")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_purchases_list_keyboard(self, purchases):
        """Клавиатура со списком покупок"""
        keyboard = []
        for p in purchases:
            try:
                date_display = datetime.strptime(p['date'], "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y")
            except:
                try:
                    date_display = datetime.strptime(p['date'], "%Y-%m-%d").strftime("%d.%m.%Y")
                except:
                    date_display = p['date'][:10] if p['date'] else "N/A"
            
            btn_text = f"ID{p['id']}: {date_display} - {p['quantity']:.4f} по {p['price']:.2f}"
            keyboard.append([KeyboardButton(btn_text)])
        keyboard.append([KeyboardButton("🏠 Главное меню")])
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_manual_buy_keyboard(self):
        """Клавиатура для ручной покупки"""
        return ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
    
    async def _check_user_fast(self, update: Update) -> bool:
        """Быстрая проверка пользователя"""
        user = update.effective_user
        username = f"@{user.username}" if user.username else f"ID:{user.id}"
        
        logger.info(f"Checking user: {username}")
        
        if self.authorized_user_id is None:
            if username == AUTHORIZED_USER:
                self.authorized_user_id = user.id
                logger.info(f"User authorized: {username}")
                return True
        elif user.id == self.authorized_user_id:
            return True
        
        logger.warning(f"Access denied for user: {username}")
        await update.message.reply_text("⛔ Доступ запрещен")
        return False
    
    async def _background_checks(self, update: Update):
        """Фоновые проверки после быстрого ответа"""
        try:
            await asyncio.sleep(1)
            
            if not TELEGRAM_TOKEN:
                await self.application.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="⚠️ Ошибка: TELEGRAM_BOT_TOKEN не найден в .env"
                )
                return
            
            if not BYBIT_API_KEY or not BYBIT_API_SECRET:
                await self.application.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="⚠️ Ошибка: Bybit API ключи не найдены в .env"
                )
                return
            
            self._init_bybit()
            
            if self.bybit_initialized:
                bybit_ok, msg = await self.bybit.test_connection()
                if not bybit_ok:
                    await self.application.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=f"⚠️ {msg}"
                    )
                else:
                    await self.application.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text="✅ Все подключения работают!"
                    )
        except Exception as e:
            logger.error(f"Background check error: {e}")
    
    async def cmd_start_fast(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """СУПЕР-БЫСТРЫЙ СТАРТ"""
        logger.info(f"Received /start command from user: {update.effective_user.username}")
        
        if not await self._check_user_fast(update):
            return
        
        # Мгновенный ответ
        await update.message.reply_text(
            f"👋 Привет, {update.effective_user.first_name}!\n\n"
            f"🤖 DCA Bybit Bot\n"
            f"Главное меню:",
            reply_markup=self.get_main_keyboard()
        )
        
        # Фоновые проверки
        asyncio.create_task(self._background_checks(update))
        
        print(f"\n{'='*50}")
        print(f"🤖 DCA Bot запущен для {update.effective_user.first_name}")
        print(f"{'='*50}\n")
    
    async def show_portfolio(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показать портфель"""
        logger.info("show_portfolio called")
        
        if not await self._check_user_fast(update):
            return
        
        self._init_bybit()
        
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован. Проверьте API ключи в .env файле.")
            return
        
        await update.message.reply_text("⏳ Загрузка данных портфеля...")
        
        try:
            symbol = self.db.get_setting('symbol', 'BTCUSDT')
            coin = symbol.replace('USDT', '')
            
            # Получаем баланс
            coin_balance = await self.bybit.get_balance(coin)
            usdt_balance = await self.bybit.get_balance('USDT')
            current_price = await self.bybit.get_symbol_price(symbol)
            
            message = f"📊 *Мой Портфель*\n\n"
            
            # Информация о USDT
            if usdt_balance and 'equity' in usdt_balance:
                available_usdt = usdt_balance.get('available', usdt_balance.get('equity', 0))
                message += f"💵 USDT доступно: `{available_usdt:.2f}`\n\n"
            
            # Информация о монете
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
                message += f"Количество: `{equity:.6f}`\n"
                message += f"Доступно: `{available:.6f}`\n"
                message += f"Стоимость: `{usd_value:.2f}` USDT\n"
                if avg_price > 0:
                    message += f"Средняя цена входа: `{avg_price:.2f}` USDT\n"
                if current_price:
                    message += f"Текущая цена: `{current_price:.2f}` USDT\n"
                    message += f"{emoji} PnL: `{pnl_percent:+.2f}%` ({pnl_usd:+.2f} USDT)\n\n"
            
            # Открытые ордера
            open_orders = await self.bybit.get_open_orders(symbol)
            
            if open_orders:
                message += f"📋 *Открытые ордера ({len(open_orders)})*\n"
                for order in open_orders[:5]:
                    side = order.get('side', 'Unknown')
                    price = float(order.get('price', 0))
                    qty = float(order.get('qty', 0))
                    
                    if side == 'Sell':
                        message += f"🔴 Продажа: `{qty:.4f}` @ `{price:.2f}`\n"
                
                if len(open_orders) > 5:
                    message += f"_...и еще {len(open_orders) - 5}_\n"
            else:
                message += f"📋 *Нет открытых ордеров*"
            
            await update.message.reply_text(message, parse_mode='Markdown')
            
        except Exception as e:
            logger.error(f"Error in show_portfolio: {e}")
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")
    
    async def show_dca_stats_detailed(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показать детальную статистику DCA"""
        logger.info("show_dca_stats_detailed called")
        
        if not await self._check_user_fast(update):
            return
        
        self._init_bybit()
        
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован. Проверьте API ключи в .env файле.")
            return
        
        symbol = self.db.get_setting('symbol', 'BTCUSDT')
        coin = symbol.replace('USDT', '')
        
        stats = self.db.get_dca_stats(symbol)
        current_price = await self.bybit.get_symbol_price(symbol)
        dca_start = self.db.get_dca_start()
        
        if not stats:
            await update.message.reply_text(
                "📈 *Статистика DCA*\n\nПокупок пока нет.",
                parse_mode='Markdown'
            )
            return
        
        total_amount = stats['total_quantity']
        total_cost = stats['total_usdt']
        avg_price = stats['avg_price']
        current_value = total_amount * current_price if current_price else 0
        pnl = current_value - total_cost
        pnl_percent = (pnl / total_cost * 100) if total_cost > 0 else 0
        
        start_date_str = "Неизвестно"
        if dca_start:
            try:
                start_date = datetime.fromisoformat(dca_start['start_date'].replace('Z', '+00:00'))
                start_date_str = start_date.strftime('%d.%m.%Y')
            except:
                start_date_str = dca_start['start_date']
        
        pnl_emoji = "📈" if pnl >= 0 else "📉"
        pnl_sign = "+" if pnl >= 0 else ""
        
        text = f"📊 *ДЕТАЛЬНАЯ СТАТИСТИКА DCA*\n\n"
        text += f"📅 Начало: `{start_date_str}`\n"
        text += f"💰 Куплено: `{total_amount:.6f}` {coin}\n"
        text += f"💵 Средняя цена: `{avg_price:.2f}` USDT\n"
        text += f"💵 Инвестировано: `{total_cost:.2f}` USDT\n"
        
        if current_price:
            text += f"📈 Текущая цена: `{current_price:.2f}` USDT\n"
            text += f"💰 Текущая стоимость: `{current_value:.2f}` USDT\n"
            text += f"{pnl_emoji} Текущий PnL: `{pnl:.2f}` USDT ({pnl_sign}{pnl_percent:.2f}%)\n"
        
        text += f"📊 Всего сделок: `{stats['total_purchases']}`\n"
        
        profit_percent = float(self.db.get_setting('profit_percent', '5'))
        target_price = avg_price * (1 + profit_percent / 100)
        target_value = total_amount * target_price
        target_pnl = target_value - total_cost
        
        text += f"\n🎯 *ЦЕЛЕВАЯ ПРИБЫЛЬ {profit_percent}%:*\n"
        text += f"Продать `{total_amount:.6f}` {coin} по цене `{target_price:.2f}` USDT\n"
        text += f"Получите: `{target_value:.2f}` USDT\n"
        text += f"Прибыль: `{target_pnl:.2f}` USDT"
        
        await update.message.reply_text(text, parse_mode='Markdown')
    
    async def show_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показать статус бота"""
        logger.info("show_status called")
        
        if not await self._check_user_fast(update):
            return
        
        symbol = self.db.get_setting('symbol', 'BTCUSDT')
        is_active = self.db.get_setting('dca_active', 'false') == 'true'
        
        message = f"📋 *Статус бота*\n\n"
        message += f"🤖 Статус: {'✅ Активен' if is_active else '⏹ Остановлен'}\n"
        message += f"🪙 Токен: `{symbol}`\n"
        message += f"💵 Сумма: `{self.db.get_setting('invest_amount', '1')}` USDT\n"
        message += f"⏰ Время: `{self.db.get_setting('schedule_time', '09:00')}`\n"
        message += f"🔄 Частота: `{self.db.get_setting('frequency_hours', '24')}`ч\n"
        message += f"📈 Цель: `{self.db.get_setting('profit_percent', '5')}%`\n"
        
        stats = self.db.get_dca_stats(symbol)
        if stats:
            message += f"\n📊 Всего покупок: `{stats['total_purchases']}`\n"
            message += f"💰 Вложено: `{stats['total_usdt']:.2f}` USDT\n"
        
        await update.message.reply_text(message, parse_mode='Markdown')
    
    async def show_price(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показать текущую цену"""
        logger.info("show_price called")
        
        if not await self._check_user_fast(update):
            return
        
        self._init_bybit()
        
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован. Проверьте API ключи в .env файле.")
            return
        
        symbol = self.db.get_setting('symbol', 'BTCUSDT')
        price = await self.bybit.get_symbol_price(symbol)
        
        if price:
            await update.message.reply_text(f"💹 *{symbol}*: `{price:.2f}` USDT", parse_mode='Markdown')
        else:
            await update.message.reply_text("❌ Не удалось получить цену. Проверьте символ или подключение к Bybit.")
    
    async def toggle_dca(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Переключение DCA"""
        logger.info("toggle_dca called")
        
        if not await self._check_user_fast(update):
            return
        
        self._init_bybit()
        
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован. Проверьте API ключи в .env файле.")
            return
        
        is_active = self.db.get_setting('dca_active', 'false') == 'true'
        
        if is_active:
            self.db.set_setting('dca_active', 'false')
            await update.message.reply_text(
                "⏹ DCA остановлен",
                reply_markup=self.get_main_keyboard()
            )
            self.db.log_action('DCA_STOPPED')
        else:
            symbol = self.db.get_setting('symbol', 'BTCUSDT')
            current_price = await self.bybit.get_symbol_price(symbol)
            
            if not current_price:
                await update.message.reply_text("❌ Не удалось получить цену")
                return
            
            self.db.set_setting('dca_active', 'true')
            self.db.set_setting('initial_reference_price', str(current_price))
            self.db.set_dca_start(symbol, current_price)
            
            await update.message.reply_text(
                f"✅ DCA запущен!\n\n"
                f"🪙 {symbol}\n"
                f"💵 Начальная цена: {current_price:.2f} USDT",
                reply_markup=self.get_main_keyboard()
            )
            self.db.log_action('DCA_STARTED', symbol, f"Цена: {current_price}")
    
    # ============= ЭКСПОРТ/ИМПОРТ =============
    
    async def export_database_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Экспорт базы данных"""
        logger.info("export_database_handler called")
        
        if not await self._check_user_fast(update):
            return SELECTING_ACTION
        
        await update.message.reply_text("⏳ Экспортирую базу данных...")
        
        success, count, file_path = self.db.export_database()
        
        if success:
            await update.message.reply_text(
                f"✅ База экспортирована!\n📊 Записей: {count}",
                reply_markup=self.get_settings_keyboard()
            )
            try:
                with open(file_path, 'rb') as f:
                    await update.message.reply_document(
                        document=InputFile(f, filename=DB_EXPORT_FILE),
                        caption="💾 Файл базы данных для скачивания"
                    )
            except Exception as e:
                logger.error(f"Error sending file: {e}")
                await update.message.reply_text(
                    f"❌ Ошибка отправки файла: {str(e)}",
                    reply_markup=self.get_settings_keyboard()
                )
        else:
            await update.message.reply_text(
                f"❌ Ошибка экспорта: {file_path}",
                reply_markup=self.get_settings_keyboard()
            )
        
        return SELECTING_ACTION
    
    async def import_database_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Начать импорт базы данных"""
        logger.info("import_database_start called")
        
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        
        await update.message.reply_text(
            "📥 *ИМПОРТ БАЗЫ ДАННЫХ*\n\n"
            "Отправьте файл базы данных (.json)\n"
            "⚠️ *Все текущие записи будут заменены!*",
            reply_markup=self.get_cancel_keyboard(),
            parse_mode='Markdown'
        )
        return WAITING_IMPORT_FILE
    
    async def import_database_receive(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Получить и импортировать файл"""
        logger.info("import_database_receive called")
        
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        
        # Проверяем, есть ли документ
        if not update.message.document:
            await update.message.reply_text(
                "❌ Пожалуйста, отправьте файл .json",
                reply_markup=self.get_cancel_keyboard()
            )
            return WAITING_IMPORT_FILE
        
        document = update.message.document
        
        # Проверяем расширение файла
        if not document.file_name.endswith('.json'):
            await update.message.reply_text(
                "❌ Файл должен быть в формате .json",
                reply_markup=self.get_cancel_keyboard()
            )
            return WAITING_IMPORT_FILE
        
        try:
            await update.message.reply_text("⏳ Импортирую базу данных...")
            
            # Скачиваем файл
            file = await context.bot.get_file(document.file_id)
            temp_file = f"temp_import_{datetime.now().strftime('%Y%m%d%H%M%S')}.json"
            await file.download_to_drive(temp_file)
            
            # Импортируем
            success, message = self.db.import_database(temp_file)
            
            # Удаляем временный файл
            if os.path.exists(temp_file):
                os.remove(temp_file)
            
            if success:
                await update.message.reply_text(
                    f"✅ {message}",
                    reply_markup=self.get_settings_keyboard()
                )
                return SELECTING_ACTION
            else:
                await update.message.reply_text(
                    f"❌ Ошибка импорта: {message}",
                    reply_markup=self.get_settings_keyboard()
                )
                return SELECTING_ACTION
                
        except Exception as e:
            logger.error(f"Error in import: {e}")
            await update.message.reply_text(
                f"❌ Ошибка: {str(e)}",
                reply_markup=self.get_settings_keyboard()
            )
            return SELECTING_ACTION
    
    # ============= НАСТРОЙКИ УВЕДОМЛЕНИЙ =============
    
    async def notification_settings_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Меню настроек уведомлений"""
        logger.info("notification_settings_menu called")
        
        if not await self._check_user_fast(update):
            return SELECTING_ACTION
        
        await update.message.reply_text(
            "🔔 *Настройки уведомлений*",
            reply_markup=self.get_notification_settings_keyboard(),
            parse_mode='Markdown'
        )
        return SELECTING_ACTION
    
    async def show_current_notification_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показать текущие настройки уведомлений"""
        if not await self._check_user_fast(update):
            return SELECTING_ACTION
        
        settings = self.db.get_notification_settings()
        status = "✅ Включены" if settings['enabled'] else "❌ Выключены"
        
        text = (
            f"📋 *ТЕКУЩИЕ НАСТРОЙКИ УВЕДОМЛЕНИЙ*\n\n"
            f"📊 Процент для уведомления: `{settings['alert_percent']}%`\n"
            f"⏱ Частота проверки: `{settings['alert_interval_minutes']}` минут\n"
            f"🔔 Уведомления: {status}"
        )
        
        await update.message.reply_text(text, parse_mode='Markdown', reply_markup=self.get_notification_settings_keyboard())
        return SELECTING_ACTION
    
    async def toggle_notifications(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Включить/выключить уведомления"""
        if not await self._check_user_fast(update):
            return SELECTING_ACTION
        
        settings = self.db.get_notification_settings()
        new_status = not settings['enabled']
        
        self.db.update_notification_settings(enabled=new_status)
        
        status = "✅ включены" if new_status else "❌ выключены"
        await update.message.reply_text(
            f"🔔 Уведомления {status}!",
            reply_markup=self.get_notification_settings_keyboard()
        )
        return SELECTING_ACTION
    
    async def set_alert_percent_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Начать изменение процента уведомлений"""
        if not await self._check_user_fast(update):
            return SELECTING_ACTION
        
        settings = self.db.get_notification_settings()
        
        await update.message.reply_text(
            f"📊 Текущий процент: {settings['alert_percent']}%\n\n"
            f"Введите новый процент (например: 5, 10, 15):",
            reply_markup=self.get_cancel_keyboard()
        )
        return WAITING_ALERT_PERCENT
    
    async def set_alert_percent_save(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Сохранить процент уведомлений"""
        text = update.message.text.strip()
        
        if text == "❌ Отмена":
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_settings_keyboard())
            return SELECTING_ACTION
        
        try:
            new_percent = float(text.replace(',', '.'))
            
            if new_percent <= 0:
                await update.message.reply_text(
                    "❌ Процент должен быть больше 0:",
                    reply_markup=self.get_cancel_keyboard()
                )
                return WAITING_ALERT_PERCENT
            
            self.db.update_notification_settings(alert_percent=new_percent)
            
            await update.message.reply_text(
                f"✅ Процент изменен на {new_percent}%!",
                reply_markup=self.get_settings_keyboard()
            )
            return SELECTING_ACTION
            
        except ValueError:
            await update.message.reply_text(
                "❌ Ошибка! Введите число (например: 10):",
                reply_markup=self.get_cancel_keyboard()
            )
            return WAITING_ALERT_PERCENT
    
    async def set_alert_interval_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Начать изменение интервала проверки"""
        if not await self._check_user_fast(update):
            return SELECTING_ACTION
        
        settings = self.db.get_notification_settings()
        
        await update.message.reply_text(
            f"⏱ Текущая частота: {settings['alert_interval_minutes']} минут\n\n"
            f"Введите новую частоту в минутах (например: 5, 15, 30, 60):",
            reply_markup=self.get_cancel_keyboard()
        )
        return WAITING_ALERT_INTERVAL
    
    async def set_alert_interval_save(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Сохранить интервал проверки"""
        text = update.message.text.strip()
        
        if text == "❌ Отмена":
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_settings_keyboard())
            return SELECTING_ACTION
        
        try:
            new_interval = int(float(text.replace(',', '.')))
            
            if new_interval < 1:
                await update.message.reply_text(
                    "❌ Интервал должен быть не менее 1 минуты:",
                    reply_markup=self.get_cancel_keyboard()
                )
                return WAITING_ALERT_INTERVAL
            
            self.db.update_notification_settings(alert_interval_minutes=new_interval)
            
            await update.message.reply_text(
                f"✅ Частота изменена на {new_interval} минут!",
                reply_markup=self.get_settings_keyboard()
            )
            return SELECTING_ACTION
            
        except ValueError:
            await update.message.reply_text(
                "❌ Ошибка! Введите целое число (например: 30):",
                reply_markup=self.get_cancel_keyboard()
            )
            return WAITING_ALERT_INTERVAL
    
    # ============= УПРАВЛЕНИЕ ОРДЕРАМИ =============
    
    async def orders_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Меню управления ордерами"""
        logger.info("orders_menu called")
        
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        
        self._init_bybit()
        
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован. Проверьте API ключи в .env файле.")
            return ConversationHandler.END
        
        symbol = self.db.get_setting('symbol', 'BTCUSDT')
        open_orders = await self.bybit.get_open_orders(symbol)
        
        await update.message.reply_text(
            f"📝 *Управление ордерами*\n\n"
            f"Токен: `{symbol}`\n"
            f"Открытых ордеров: `{len(open_orders)}`",
            reply_markup=self.get_orders_management_keyboard(),
            parse_mode='Markdown'
        )
        return MANAGE_ORDERS
    
    async def list_orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показать список ордеров"""
        logger.info("list_orders called")
        
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        
        self._init_bybit()
        
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.")
            return MANAGE_ORDERS
        
        symbol = self.db.get_setting('symbol', 'BTCUSDT')
        open_orders = await self.bybit.get_open_orders(symbol)
        
        if not open_orders:
            await update.message.reply_text(
                "📋 Нет открытых ордеров",
                reply_markup=self.get_orders_management_keyboard()
            )
            return MANAGE_ORDERS
        
        message = f"📋 *Открытые ордера ({len(open_orders)})*\n\n"
        
        for i, order in enumerate(open_orders[:10], 1):
            price = float(order.get('price', 0))
            qty = float(order.get('qty', 0))
            short_id = order.get('orderId', 'N/A')[:8]
            
            message += f"*{i}.* `{short_id}` - `{qty:.6f}` @ `{price:.2f}`\n"
        
        await update.message.reply_text(message, parse_mode='Markdown')
        return MANAGE_ORDERS
    
    async def delete_order_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Начать удаление ордера"""
        logger.info("delete_order_start called")
        
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        
        self._init_bybit()
        
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.")
            return MANAGE_ORDERS
        
        symbol = self.db.get_setting('symbol', 'BTCUSDT')
        open_orders = await self.bybit.get_open_orders(symbol)
        
        if not open_orders:
            await update.message.reply_text(
                "❌ Нет открытых ордеров",
                reply_markup=self.get_orders_management_keyboard()
            )
            return MANAGE_ORDERS
        
        keyboard = []
        for i, order in enumerate(open_orders[:10], 1):
            order_id = order.get('orderId', 'N/A')
            keyboard.append([InlineKeyboardButton(
                f"❌ Удалить ордер #{i}", 
                callback_data=f"order_delete_{order_id}"
            )])
        
        keyboard.append([InlineKeyboardButton("🔙 Отмена", callback_data="order_cancel")])
        
        await update.message.reply_text(
            "❌ *Выберите ордер для удаления:*",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        return MANAGE_ORDERS
    
    async def edit_order_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Начать редактирование цены ордера"""
        logger.info("edit_order_start called")
        
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        
        self._init_bybit()
        
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.")
            return MANAGE_ORDERS
        
        symbol = self.db.get_setting('symbol', 'BTCUSDT')
        open_orders = await self.bybit.get_open_orders(symbol)
        
        if not open_orders:
            await update.message.reply_text(
                "❌ Нет открытых ордеров",
                reply_markup=self.get_orders_management_keyboard()
            )
            return MANAGE_ORDERS
        
        keyboard = []
        for i, order in enumerate(open_orders[:10], 1):
            order_id = order.get('orderId', 'N/A')
            price = float(order.get('price', 0))
            qty = float(order.get('qty', 0))
            keyboard.append([InlineKeyboardButton(
                f"✏️ Изменить #{i} ({price:.2f})", 
                callback_data=f"order_edit_{order_id}_{price}_{qty}"
            )])
        
        keyboard.append([InlineKeyboardButton("🔙 Отмена", callback_data="order_cancel")])
        
        await update.message.reply_text(
            "✏️ *Выберите ордер для изменения цены:*",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        return MANAGE_ORDERS
    
    async def handle_order_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка callback запросов"""
        query = update.callback_query
        await query.answer()
        
        data = query.data
        logger.info(f"Callback received: {data}")
        
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
        """Удалить ордер"""
        self._init_bybit()
        
        if not self.bybit_initialized:
            await update.callback_query.edit_message_text("❌ Bybit API не инициализирован.")
            return
        
        symbol = self.db.get_setting('symbol', 'BTCUSDT')
        
        await update.callback_query.edit_message_text(f"⏳ Удаляю ордер...")
        
        result = await self.bybit.cancel_order(symbol, order_id)
        
        if result['success']:
            self.db.update_sell_order_status(order_id, 'cancelled')
            await update.callback_query.edit_message_text(f"✅ Ордер удален!")
        else:
            await update.callback_query.edit_message_text(f"❌ Ошибка: {result.get('error', 'Unknown')}")
    
    async def process_order_edit_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE, 
                                       order_id: str, current_price: float, qty: float):
        """Начать изменение цены"""
        context.user_data['editing_order_id'] = order_id
        context.user_data['editing_order_qty'] = qty
        context.user_data['editing_order_current_price'] = current_price
        
        await update.callback_query.edit_message_text(
            f"✏️ Введите новую цену (текущая: {current_price:.2f}):"
        )
        context.user_data['current_state'] = EDIT_ORDER_PRICE
    
    async def edit_order_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Завершить изменение цены"""
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
            qty = context.user_data.get('editing_order_qty', 0)
            old_price = context.user_data.get('editing_order_current_price', 0)
            symbol = self.db.get_setting('symbol', 'BTCUSDT')
            
            if not order_id:
                await update.message.reply_text("❌ Ошибка", reply_markup=self.get_orders_management_keyboard())
                return MANAGE_ORDERS
            
            result = await self.bybit.amend_order_price(symbol, order_id, new_price)
            
            if result['success']:
                self.db.update_order_price(order_id, new_price, 0)
                await update.message.reply_text(
                    f"✅ Цена изменена: {old_price:.2f} -> {new_price:.2f}",
                    reply_markup=self.get_orders_management_keyboard()
                )
            else:
                await update.message.reply_text(
                    f"❌ Ошибка: {result.get('error', 'Unknown')}",
                    reply_markup=self.get_orders_management_keyboard()
                )
        
        except ValueError:
            await update.message.reply_text("❌ Некорректная цена", reply_markup=self.get_orders_management_keyboard())
            return EDIT_ORDER_PRICE
        
        context.user_data.pop('editing_order_id', None)
        return MANAGE_ORDERS
    
    # ============= РУЧНАЯ ПОКУПКА =============
    
    async def manual_buy_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Начать ручную покупку"""
        logger.info("manual_buy_start called")
        
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        
        self._init_bybit()
        
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован. Проверьте API ключи в .env файле.")
            return ConversationHandler.END
        
        symbol = self.db.get_setting('symbol', 'BTCUSDT')
        current_price = await self.bybit.get_symbol_price(symbol)
        
        if not current_price:
            await update.message.reply_text("❌ Не удалось получить цену", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        
        context.user_data['manual_buy_current_price'] = current_price
        context.user_data['manual_buy_symbol'] = symbol
        
        await update.message.reply_text(
            f"💰 Текущая цена {symbol}: `{current_price:.2f}` USDT\n\nВведите цену лимитного ордера в USDT (или нажмите Отмена):",
            reply_markup=self.get_manual_buy_keyboard(),
            parse_mode='Markdown'
        )
        return MANUAL_BUY_PRICE
    
    async def manual_buy_price_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка цены"""
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
            
            await update.message.reply_text(
                f"💰 Введите сумму покупки в USDT (минимум 1.1, или нажмите Отмена):",
                reply_markup=self.get_manual_buy_keyboard()
            )
            return MANUAL_BUY_AMOUNT
            
        except ValueError:
            await update.message.reply_text("❌ Некорректная цена. Введите число или нажмите Отмена:", reply_markup=self.get_manual_buy_keyboard())
            return MANUAL_BUY_PRICE
    
    async def manual_buy_amount_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка суммы и создание ордера"""
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
            symbol = context.user_data.get('manual_buy_symbol', 'BTCUSDT')
            
            if not price:
                await update.message.reply_text("❌ Ошибка", reply_markup=self.get_main_keyboard())
                return ConversationHandler.END
            
            await update.message.reply_text("⏳ Создаю ордер...")
            
            result = await self.bybit.place_limit_buy(symbol, price, amount)
            
            if result['success']:
                profit_percent = float(self.db.get_setting('profit_percent', '5'))
                target_price = price * (1 + profit_percent / 100)
                
                current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.db.add_purchase(
                    symbol=symbol,
                    amount_usdt=amount,
                    price=price,
                    quantity=result['quantity'],
                    multiplier=1.0,
                    drop_percent=0,
                    date=current_date
                )
                
                sell_result = await self.bybit.place_limit_sell(symbol, result['quantity'], target_price)
                
                if sell_result['success']:
                    self.db.add_sell_order(
                        symbol=symbol,
                        order_id=sell_result['order_id'],
                        quantity=result['quantity'],
                        target_price=target_price,
                        profit_percent=profit_percent
                    )
                
                await update.message.reply_text(
                    f"✅ *Лимитный ордер создан!*\n\n"
                    f"Цена: `{price:.2f}` USDT\n"
                    f"Сумма: `{amount:.2f}` USDT\n"
                    f"Количество: `{result['quantity']:.6f}`\n"
                    f"Цель продажи: `{target_price:.2f}` USDT ({profit_percent}%)",
                    reply_markup=self.get_main_keyboard(),
                    parse_mode='Markdown'
                )
            else:
                await update.message.reply_text(
                    f"❌ Ошибка: {result.get('error', 'Unknown')}",
                    reply_markup=self.get_main_keyboard()
                )
        
        except ValueError as e:
            await update.message.reply_text(f"❌ {str(e)}. Введите число или нажмите Отмена:", reply_markup=self.get_manual_buy_keyboard())
            return MANUAL_BUY_AMOUNT
        
        return ConversationHandler.END
    
    # ============= РУЧНОЕ ДОБАВЛЕНИЕ ПОКУПКИ =============
    
    async def manual_add_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Начать ручное добавление покупки"""
        logger.info("manual_add_start called")
        
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        
        symbol = self.db.get_setting('symbol', 'BTCUSDT')
        
        await update.message.reply_text(
            f"➕ Введите цену покупки в USDT (или нажмите Отмена):",
            reply_markup=self.get_cancel_keyboard()
        )
        return MANUAL_ADD_PRICE
    
    async def manual_add_price(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка цены"""
        text = update.message.text.strip()
        
        if text == "❌ Отмена":
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        
        try:
            price = float(text.replace(',', '.'))
            if price <= 0:
                raise ValueError
            
            context.user_data['manual_price'] = price
            
            await update.message.reply_text(
                f"✅ Цена {price} USDT\n\nВведите количество монет (или нажмите Отмена):",
                reply_markup=self.get_cancel_keyboard()
            )
            return MANUAL_ADD_AMOUNT
            
        except ValueError:
            await update.message.reply_text("❌ Ошибка! Введите число или нажмите Отмена:", reply_markup=self.get_cancel_keyboard())
            return MANUAL_ADD_PRICE
    
    async def manual_add_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка количества и сохранение"""
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
            
            symbol = self.db.get_setting('symbol', 'BTCUSDT')
            amount_usdt = price * quantity
            
            purchase_id = self.db.add_purchase(
                symbol=symbol,
                amount_usdt=amount_usdt,
                price=price,
                quantity=quantity,
                multiplier=1.0,
                drop_percent=0,
                date=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            )
            
            if purchase_id:
                await update.message.reply_text(
                    f"✅ *Покупка добавлена!*\n\n"
                    f"ID: `{purchase_id}`\n"
                    f"Цена: `{price:.2f}` USDT\n"
                    f"Количество: `{quantity:.6f}`\n"
                    f"Сумма: `{amount_usdt:.2f}` USDT",
                    reply_markup=self.get_main_keyboard(),
                    parse_mode='Markdown'
                )
            else:
                await update.message.reply_text("❌ Ошибка сохранения", reply_markup=self.get_main_keyboard())
            
            return ConversationHandler.END
            
        except ValueError:
            await update.message.reply_text("❌ Ошибка! Введите число или нажмите Отмена:", reply_markup=self.get_cancel_keyboard())
            return MANUAL_ADD_AMOUNT
    
    # ============= РЕДАКТИРОВАНИЕ ПОКУПОК =============
    
    async def edit_purchases_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показать список покупок"""
        logger.info("edit_purchases_list called")
        
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        
        symbol = self.db.get_setting('symbol', 'BTCUSDT')
        purchases = self.db.get_purchases(symbol)
        
        if not purchases:
            await update.message.reply_text("Нет покупок", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        
        context.user_data.pop('editing_purchase_id', None)
        
        await update.message.reply_text(
            "✏️ Выберите покупку:",
            reply_markup=self.get_purchases_list_keyboard(purchases)
        )
        return EDIT_PURCHASE_SELECT
    
    async def edit_purchase_selected(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Выбрана покупка"""
        text = update.message.text
        
        if text == "🏠 Главное меню":
            await self.back_to_main(update, context)
            return ConversationHandler.END
        
        try:
            purchase_id = int(text.split(":")[0].replace("ID", ""))
            purchase = self.db.get_purchase_by_id(purchase_id)
            
            if not purchase:
                await update.message.reply_text("❌ Покупка не найдена", reply_markup=self.get_main_keyboard())
                return ConversationHandler.END
            
            context.user_data['editing_purchase_id'] = purchase_id
            
            try:
                date_display = datetime.strptime(purchase['date'], "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y %H:%M")
            except:
                try:
                    date_display = datetime.strptime(purchase['date'], "%Y-%m-%d").strftime("%d.%m.%Y")
                except:
                    date_display = purchase['date']
            
            await update.message.reply_text(
                f"✏️ *РЕДАКТИРОВАНИЕ ID: {purchase_id}*\n\n"
                f"📅 Дата: `{date_display}`\n"
                f"💰 Цена: `{purchase['price']:.2f}` USDT\n"
                f"📊 Количество: `{purchase['quantity']:.6f}`",
                reply_markup=self.get_edit_purchases_keyboard(),
                parse_mode='Markdown'
            )
            return EDIT_PURCHASE_SELECT
            
        except (ValueError, IndexError) as e:
            await update.message.reply_text("❌ Ошибка выбора", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
    
    async def edit_price_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Начать редактирование цены"""
        await update.message.reply_text("💰 Введите новую цену (или нажмите Отмена):", reply_markup=self.get_cancel_keyboard())
        return EDIT_PRICE
    
    async def edit_price_save(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Сохранить новую цену"""
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
                await update.message.reply_text(f"✅ Цена обновлена: {new_price:.2f} USDT")
            else:
                await update.message.reply_text("❌ Ошибка при обновлении")
            
            await self.show_purchase_after_edit(update, context, purchase_id)
            return EDIT_PURCHASE_SELECT
            
        except ValueError:
            await update.message.reply_text("❌ Ошибка! Введите число или нажмите Отмена:", reply_markup=self.get_cancel_keyboard())
            return EDIT_PRICE
    
    async def edit_amount_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Начать редактирование количества"""
        await update.message.reply_text("📊 Введите новое количество (или нажмите Отмена):", reply_markup=self.get_cancel_keyboard())
        return EDIT_AMOUNT
    
    async def edit_amount_save(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Сохранить новое количество"""
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
                await update.message.reply_text(f"✅ Количество обновлено: {new_quantity:.6f}")
            else:
                await update.message.reply_text("❌ Ошибка при обновлении")
            
            await self.show_purchase_after_edit(update, context, purchase_id)
            return EDIT_PURCHASE_SELECT
            
        except ValueError:
            await update.message.reply_text("❌ Ошибка! Введите число или нажмите Отмена:", reply_markup=self.get_cancel_keyboard())
            return EDIT_AMOUNT
    
    def parse_date(self, date_str: str) -> str:
        """Парсинг даты"""
        date_str = date_str.strip()
        current_year = datetime.now().year
        
        patterns = [
            (r'^(\d{1,2})\.(\d{1,2})\.(\d{4})$', lambda m: (int(m.group(1)), int(m.group(2)), int(m.group(3)))),
            (r'^(\d{1,2})\.(\d{1,2})\.(\d{2})$', lambda m: (int(m.group(1)), int(m.group(2)), 2000 + int(m.group(3)))),
            (r'^(\d{1,2})-(\d{1,2})-(\d{4})$', lambda m: (int(m.group(1)), int(m.group(2)), int(m.group(3)))),
            (r'^(\d{1,2})-(\d{1,2})-(\d{2})$', lambda m: (int(m.group(1)), int(m.group(2)), 2000 + int(m.group(3)))),
            (r'^(\d{1,2})\.(\d{1,2})$', lambda m: (int(m.group(1)), int(m.group(2)), current_year)),
            (r'^(\d{1,2})-(\d{1,2})$', lambda m: (int(m.group(1)), int(m.group(2)), current_year)),
        ]
        
        for pattern, extractor in patterns:
            match = re.match(pattern, date_str)
            if match:
                day, month, year = extractor(match)
                try:
                    dt = datetime(year, month, day)
                    return dt.strftime("%Y-%m-%d")
                except ValueError:
                    raise ValueError(f"Некорректная дата")
        
        raise ValueError("Неподдерживаемый формат даты")
    
    async def edit_date_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Начать редактирование даты"""
        purchase_id = context.user_data.get('editing_purchase_id')
        purchase = self.db.get_purchase_by_id(purchase_id)
        
        try:
            current_date = datetime.strptime(purchase['date'], "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y")
        except:
            try:
                current_date = datetime.strptime(purchase['date'], "%Y-%m-%d").strftime("%d.%m.%Y")
            except:
                current_date = purchase['date'][:10] if purchase['date'] else "неизвестно"
        
        await update.message.reply_text(
            f"📅 Текущая дата: {current_date}\n\nВведите новую дату (ДД.ММ.ГГГГ или нажмите Отмена):",
            reply_markup=self.get_cancel_keyboard()
        )
        return EDIT_DATE
    
    async def edit_date_save(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Сохранить новую дату"""
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
                display_date = new_date
                await update.message.reply_text(f"✅ Дата обновлена: {display_date}")
            else:
                await update.message.reply_text("❌ Ошибка при обновлении")
            
            await self.show_purchase_after_edit(update, context, purchase_id)
            return EDIT_PURCHASE_SELECT
            
        except ValueError as e:
            await update.message.reply_text(f"❌ {str(e)}. Попробуйте снова или нажмите Отмена:", reply_markup=self.get_cancel_keyboard())
            return EDIT_DATE
    
    async def delete_purchase_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Подтверждение удаления"""
        await update.message.reply_text(
            "⚠️ *Удалить эту покупку?*",
            reply_markup=self.get_confirm_keyboard(),
            parse_mode='Markdown'
        )
        return DELETE_CONFIRM
    
    async def delete_purchase_execute(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Выполнить удаление"""
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
        """Показать покупку после редактирования"""
        purchase = self.db.get_purchase_by_id(purchase_id)
        
        if not purchase:
            await update.message.reply_text("❌ Покупка не найдена", reply_markup=self.get_main_keyboard())
            return
        
        try:
            date_display = datetime.strptime(purchase['date'], "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y %H:%M")
        except:
            try:
                date_display = datetime.strptime(purchase['date'], "%Y-%m-%d").strftime("%d.%m.%Y")
            except:
                date_display = purchase['date']
        
        await update.message.reply_text(
            f"✏️ *РЕДАКТИРОВАНИЕ ID: {purchase_id}*\n\n"
            f"📅 Дата: `{date_display}`\n"
            f"💰 Цена: `{purchase['price']:.2f}` USDT\n"
            f"📊 Количество: `{purchase['quantity']:.6f}`",
            reply_markup=self.get_edit_purchases_keyboard(),
            parse_mode='Markdown'
        )
    
    async def cancel_to_edit_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Отмена и возврат к меню"""
        purchase_id = context.user_data.get('editing_purchase_id')
        if purchase_id:
            await self.show_purchase_after_edit(update, context, purchase_id)
        else:
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_main_keyboard())
    
    # ============= НАСТРОЙКИ =============
    
    async def settings_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Меню настроек"""
        logger.info("settings_menu called")
        
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        
        freq_hours = int(self.db.get_setting('frequency_hours', '24'))
        
        await update.message.reply_text(
            "⚙️ *Настройки*\n\n"
            f"🪙 Токен: `{self.db.get_setting('symbol', 'BTCUSDT')}`\n"
            f"💵 Сумма: `{self.db.get_setting('invest_amount', '1')}` USDT\n"
            f"📈 Прибыль: `{self.db.get_setting('profit_percent', '5')}%`\n"
            f"⏰ Время: `{self.db.get_setting('schedule_time', '09:00')}`\n"
            f"🔄 Частота: `{freq_hours}`ч",
            reply_markup=self.get_settings_keyboard(),
            parse_mode='Markdown'
        )
        return SELECTING_ACTION
    
    async def set_symbol_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Начать изменение символа"""
        await update.message.reply_text(
            f"🪙 Введите символ (текущий: {self.db.get_setting('symbol', 'BTCUSDT')}, или нажмите Отмена):",
            reply_markup=self.get_cancel_keyboard()
        )
        return SET_SYMBOL
    
    async def set_symbol_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Завершить изменение символа"""
        symbol = update.message.text.upper().strip()
        
        if symbol == "❌ ОТМЕНА" or symbol == "❌ Отмена":
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_settings_keyboard())
            return SELECTING_ACTION
        
        self._init_bybit()
        
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.")
            return SELECTING_ACTION
        
        price = await self.bybit.get_symbol_price(symbol)
        if not price:
            await update.message.reply_text(f"❌ Символ {symbol} не найден", reply_markup=self.get_settings_keyboard())
            return SELECTING_ACTION
        
        self.db.set_setting('symbol', symbol)
        self.db.set_setting('initial_reference_price', str(price))
        
        await update.message.reply_text(
            f"✅ Символ изменен на {symbol}\nЦена: {price:.2f} USDT",
            reply_markup=self.get_settings_keyboard()
        )
        return SELECTING_ACTION
    
    async def set_amount_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Начать изменение суммы"""
        await update.message.reply_text(
            f"💵 Введите сумму (текущая: {self.db.get_setting('invest_amount', '1')}, или нажмите Отмена):",
            reply_markup=self.get_cancel_keyboard()
        )
        return SET_AMOUNT
    
    async def set_amount_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Завершить изменение суммы"""
        text = update.message.text.strip()
        
        if text == "❌ ОТМЕНА" or text == "❌ Отмена":
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_settings_keyboard())
            return SELECTING_ACTION
        
        try:
            amount = float(text)
            if amount < 1:
                raise ValueError
            
            self.db.set_setting('invest_amount', str(amount))
            await update.message.reply_text(f"✅ Сумма изменена на {amount} USDT", reply_markup=self.get_settings_keyboard())
        except ValueError:
            await update.message.reply_text("❌ Некорректное значение", reply_markup=self.get_settings_keyboard())
        
        return SELECTING_ACTION
    
    async def set_profit_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Начать изменение процента прибыли"""
        await update.message.reply_text(
            f"📊 Введите процент прибыли (текущий: {self.db.get_setting('profit_percent', '5')}%, или нажмите Отмена):",
            reply_markup=self.get_cancel_keyboard()
        )
        return SET_PROFIT_PERCENT
    
    async def set_profit_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Завершить изменение процента прибыли"""
        text = update.message.text.strip()
        
        if text == "❌ ОТМЕНА" or text == "❌ Отмена":
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
        """Начать изменение настроек падения"""
        await update.message.reply_text(
            f"📉 Введите макс. падение % и множитель (текущие: {self.db.get_setting('max_drop_percent', '60')}% x{self.db.get_setting('max_multiplier', '3')}):\nНапример: 60 3 (или нажмите Отмена)",
            reply_markup=self.get_cancel_keyboard()
        )
        return SET_MAX_DROP
    
    async def set_drop_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Завершить изменение настроек падения"""
        text = update.message.text.strip()
        
        if text == "❌ ОТМЕНА" or text == "❌ Отмена":
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_settings_keyboard())
            return SELECTING_ACTION
        
        try:
            parts = text.split()
            if len(parts) != 2:
                raise ValueError
            
            max_drop = float(parts[0])
            max_mult = float(parts[1])
            
            if max_drop < 10 or max_drop > 90:
                raise ValueError
            if max_mult < 1.5 or max_mult > 10:
                raise ValueError
            
            self.db.set_setting('max_drop_percent', str(max_drop))
            self.db.set_setting('max_multiplier', str(max_mult))
            
            await update.message.reply_text(
                f"✅ Настройки обновлены: {max_drop}% x{max_mult}",
                reply_markup=self.get_settings_keyboard()
            )
        except Exception:
            await update.message.reply_text("❌ Ошибка формата. Используйте: 60 3", reply_markup=self.get_settings_keyboard())
        
        return SELECTING_ACTION
    
    async def set_time_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Начать изменение времени"""
        await update.message.reply_text(
            f"⏰ Введите время (текущее: {self.db.get_setting('schedule_time', '09:00')}, формат ЧЧ:ММ, или нажмите Отмена):",
            reply_markup=self.get_cancel_keyboard()
        )
        return SET_SCHEDULE_TIME
    
    async def set_time_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Завершить изменение времени"""
        time_str = update.message.text.strip()
        
        if time_str == "❌ ОТМЕНА" or time_str == "❌ Отмена":
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
        """Начать изменение частоты"""
        await update.message.reply_text(
            f"🔄 Введите частоту в часах (текущая: {self.db.get_setting('frequency_hours', '24')}, или нажмите Отмена):",
            reply_markup=self.get_cancel_keyboard()
        )
        return SET_FREQUENCY_HOURS
    
    async def set_frequency_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Завершить изменение частоты"""
        text = update.message.text.strip()
        
        if text == "❌ ОТМЕНА" or text == "❌ Отмена":
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
    
    async def back_to_main(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Вернуться в главное меню"""
        context.user_data.clear()
        await update.message.reply_text("Главное меню:", reply_markup=self.get_main_keyboard())
        return ConversationHandler.END
    
    async def cancel_conversation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Отмена conversation"""
        context.user_data.clear()
        await update.message.reply_text("Действие отменено", reply_markup=self.get_main_keyboard())
        return ConversationHandler.END
    
    # ============= ПЛАНИРОВЩИК =============
    
    async def scheduler_loop(self):
        """Основной цикл планировщика"""
        logger.info("Scheduler loop started")
        while self.scheduler_running:
            try:
                await asyncio.sleep(60)
                if self.db.get_setting('dca_active', 'false') != 'true':
                    continue
                
                if not self.bybit_initialized:
                    self._init_bybit()
                
                # Здесь будет логика DCA покупок
                
            except asyncio.CancelledError:
                logger.info("Scheduler cancelled")
                break
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
    
    def setup_handlers(self):
        """Настройка всех обработчиков"""
        logger.info("Setting up handlers...")
        
        # Старт - самый важный обработчик, ставим первым
        self.application.add_handler(CommandHandler("start", self.cmd_start_fast))
        
        # Callback обработчик
        self.application.add_handler(CallbackQueryHandler(self.handle_order_callback, pattern='^order_'))
        
        # Основные кнопки (без conversation) - ставим их перед ConversationHandler
        self.application.add_handler(MessageHandler(filters.Regex('^(🚀 Запустить Авто DCA|⏹ Остановить Авто DCA)$'), self.toggle_dca))
        self.application.add_handler(MessageHandler(filters.Regex('^(📊 Мой Портфель)$'), self.show_portfolio))
        self.application.add_handler(MessageHandler(filters.Regex('^(📈 Статистика DCA)$'), self.show_dca_stats_detailed))
        self.application.add_handler(MessageHandler(filters.Regex('^(📋 Статус бота)$'), self.show_status))
        self.application.add_handler(MessageHandler(filters.Regex('^(📉 Текущая цена)$'), self.show_price))
        
        # Настройки (основной ConversationHandler)
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
                SET_SYMBOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_symbol_done)],
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
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.import_database_receive),
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
        
        # Ручная лимитная покупка
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
                    MessageHandler(filters.Regex('^ID\\d+:'), self.edit_purchase_selected),
                    MessageHandler(filters.Regex('^(💰 Изменить цену)$'), self.edit_price_start),
                    MessageHandler(filters.Regex('^(📊 Изменить количество)$'), self.edit_amount_start),
                    MessageHandler(filters.Regex('^(📅 Изменить дату)$'), self.edit_date_start),
                    MessageHandler(filters.Regex('^(❌ Удалить покупку)$'), self.delete_purchase_confirm),
                    MessageHandler(filters.Regex('^(🔙 Назад к списку)$'), self.edit_purchases_list),
                    MessageHandler(filters.Regex('^(🏠 Главное меню)$'), self.back_to_main),
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
        
        # Обработчик для всех остальных сообщений (чтобы не было ошибок)
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_unknown))
        
        logger.info("Handlers setup completed")
    
    async def handle_unknown(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка неизвестных сообщений"""
        if not await self._check_user_fast(update):
            return
        await update.message.reply_text("Используйте кнопки меню", reply_markup=self.get_main_keyboard())
    
    async def post_init(self, application):
        """Запускается после инициализации приложения"""
        self.scheduler_running = True
        asyncio.create_task(self.scheduler_loop())
    
    def run(self):
        """Запуск бота"""
        print(f"\n{Fore.CYAN}{'='*60}")
        print(f"{Fore.CYAN}🚀 ЗАПУСК FAST DCA BYBIT BOT")
        print(f"{Fore.CYAN}{'='*60}")
        
        # Проверяем токен
        if not TELEGRAM_TOKEN:
            print(f"{Fore.RED}❌ TELEGRAM_BOT_TOKEN не найден в .env файле!")
            return
        
        print(f"{Fore.GREEN}✅ Токен найден: {TELEGRAM_TOKEN[:10]}...{TELEGRAM_TOKEN[-5:]}")
        print(f"{Fore.WHITE}👤 Пользователь: {AUTHORIZED_USER}")
        print(f"{Fore.WHITE}🌐 Testnet: {'Да' if BYBIT_TESTNET else 'Нет'}")
        print(f"{Fore.CYAN}{'='*60}\n")
        
        # Добавляем обработчик post_init
        self.application.post_init = self.post_init
        
        # Запускаем бота
        self.application.run_polling(allowed_updates=Update.ALL_TYPES)

# ==================== MAIN ====================

if __name__ == "__main__":
    # Проверяем наличие необходимых зависимостей
    try:
        import colorama
    except ImportError:
        print("Устанавливаю colorama...")
        os.system(f"{sys.executable} -m pip install colorama")
        import colorama
    
    # Создаем и запускаем бота
    bot = FastDCABot()
    bot.run()