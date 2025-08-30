import asyncio
import json
import logging
import re
import os
from datetime import datetime, time
from bs4 import BeautifulSoup
import aiohttp
from telegram import Update, Bot, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from dotenv import load_dotenv

# Загрузка переменных окружения из .env файла
load_dotenv()

# Настройки
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')  # Изменено на загрузку из .env
CHECK_INTERVAL_HOURS = 3  # Изменено на 3 часа
TARGET_PRICE = 1400.0
BASE_URL = "https://dns-shop.by"
SEARCH_URL = "https://dns-shop.by/ru/category/17a89aab16404e77/videokarty/"
DATA_FILE = 'graphic_cards.json'
NIGHT_MODE_FILE = 'night_mode.json'

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class NightModeManager:
    def __init__(self, filename=NIGHT_MODE_FILE):
        self.filename = filename
        self.ensure_file_exists()

    def ensure_file_exists(self):
        """Создает файл ночного режима, если он не существует"""
        if not os.path.exists(self.filename):
            with open(self.filename, 'w', encoding='utf-8') as f:
                json.dump({"enabled": False}, f, indent=2, ensure_ascii=False)
            logger.info(f"Created night mode file: {self.filename}")

    def load_night_mode(self):
        """Загружает настройки ночного режима"""
        try:
            with open(self.filename, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.error(f"Error loading night mode: {e}")
            return {"enabled": False}

    def save_night_mode(self, data):
        """Сохраняет настройки ночного режима"""
        try:
            with open(self.filename, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error saving night mode: {e}")

    def is_night_mode_enabled(self):
        """Проверяет, включен ли ночной режим"""
        return self.load_night_mode()["enabled"]

    def toggle_night_mode(self, user_id):
        """Переключает ночной режим"""
        data = self.load_night_mode()
        data["enabled"] = not data["enabled"]
        data["last_toggled_by"] = user_id
        data["last_toggled_at"] = datetime.now().isoformat()
        self.save_night_mode(data)
        return data["enabled"]

    def is_night_time(self):
        """Проверяет, сейчас ночное время (с 00:00 до 08:00)"""
        now = datetime.now().time()
        night_start = time(0, 0)  # 00:00
        night_end = time(8, 0)    # 08:00
        
        if night_start <= now <= night_end:
            return True
        return False

    def should_send_notifications(self):
        """Определяет, нужно ли отправлять уведомления"""
        if not self.is_night_mode_enabled():
            return True
        
        # Если ночной режим включен, проверяем время
        if self.is_night_time():
            logger.info("Night mode active: not sending notifications")
            return False
        return True

class DataManager:
    def __init__(self, filename=DATA_FILE):
        self.filename = filename
        self.ensure_file_exists()

    def ensure_file_exists(self):
        """Создает файл данных, если он не существует"""
        if not os.path.exists(self.filename):
            with open(self.filename, 'w', encoding='utf-8') as f:
                json.dump({"graphic_cards": {}}, f, indent=2, ensure_ascii=False)
            logger.info(f"Created data file: {self.filename}")

    def load_data(self):
        """Загружает данные из файла"""
        try:
            with open(self.filename, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.error(f"Error loading data: {e}")
            return {"graphic_cards": {}}

    def save_data(self, data):
        """Сохраняет данные в файл"""
        try:
            with open(self.filename, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error saving data: {e}")

    def get_product_key(self, title, price):
        """Создает уникальный ключ для товара"""
        return f"{title}_{price}"

class DNSParser:
    def __init__(self):
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }

    async def fetch_page(self, session, url):
        try:
            async with session.get(url, headers=self.headers) as response:
                if response.status == 200:
                    return await response.text()
                else:
                    logger.error(f"HTTP error {response.status} for {url}")
                    return None
        except Exception as e:
            logger.error(f"Error fetching {url}: {e}")
            return None

    def parse_price(self, price_text):
        """Парсит цену из текста, учитывая десятичные числа"""
        try:
            # Удаляем все нечисловые символы, кроме точки и запятой
            cleaned_text = re.sub(r'[^\d,.]', '', price_text)
            
            # Заменяем запятую на точку для корректного преобразования в float
            cleaned_text = cleaned_text.replace(',', '.')
            
            # Удаляем лишние точки (оставляем только первую)
            if cleaned_text.count('.') > 1:
                parts = cleaned_text.split('.')
                cleaned_text = parts[0] + '.' + ''.join(parts[1:])
            
            return float(cleaned_text)
        except (ValueError, AttributeError) as e:
            logger.warning(f"Error parsing price '{price_text}': {e}")
            return None

    def parse_products(self, html, selectors):
        if not html:
            return []
            
        soup = BeautifulSoup(html, 'html.parser')
        products = soup.select(selectors['product'])
        results = []
        
        for product in products:
            try:
                title_elem = product.select_one(selectors['title'])
                price_elem = product.select_one(selectors['price'])
                image_elem = product.select_one(selectors['image'])
                
                if not all([title_elem, price_elem, image_elem]):
                    continue

                title = title_elem.text.strip()
                price_text = price_elem.text.strip()
                
                # Парсим цену с учетом десятичных чисел
                price = self.parse_price(price_text)
                if price is None:
                    continue
                
                # Получаем URL изображения
                image_url = image_elem.get('src', '') or image_elem.get('data-src', '')
                if image_url and not image_url.startswith('http'):
                    image_url = BASE_URL + image_url

                if price < TARGET_PRICE:
                    results.append({
                        'title': title,
                        'price': price,
                        'price_text': price_text.strip(),
                        'image_url': image_url,
                        'timestamp': datetime.now().isoformat()
                    })
            except (ValueError, AttributeError, TypeError) as e:
                logger.warning(f"Error parsing product: {e}")
                continue
        
        return results

    async def parse_all_pages(self, url, selectors):
        async with aiohttp.ClientSession() as session:
            all_products = []
            page = 1
            
            while True:
                paginated_url = f"{url}?avail=now%2Ctod%2Ctom%2Clat%2Cinw%2Cuna&sqctg=rtx+5060&page={page}"
                logger.info(f"Parsing page {page}: {paginated_url}")
                
                html = await self.fetch_page(session, paginated_url)
                if not html:
                    break

                products = self.parse_products(html, selectors)
                if not products:
                    logger.info(f"No more products found on page {page}")
                    break

                all_products.extend(products)
                page += 1
                await asyncio.sleep(1)
            
            logger.info(f"Found {len(all_products)} products below {TARGET_PRICE} BYN")
            return all_products

class SubscriptionManager:
    def __init__(self, filename='subscriptions.json'):
        self.filename = filename
        self.ensure_file_exists()

    def ensure_file_exists(self):
        if not os.path.exists(self.filename):
            with open(self.filename, 'w', encoding='utf-8') as f:
                json.dump({"users": []}, f, indent=2, ensure_ascii=False)

    def load_subscriptions(self):
        try:
            with open(self.filename, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"users": []}

    def save_subscriptions(self, data):
        with open(self.filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def add_user(self, user_id):
        data = self.load_subscriptions()
        if user_id not in data['users']:
            data['users'].append(user_id)
            self.save_subscriptions(data)
            logger.info(f"User {user_id} subscribed")
            return True
        return False

    def get_all_users(self):
        return self.load_subscriptions()['users']

# Глобальные объекты
parser = DNSParser()
subscription_manager = SubscriptionManager()
data_manager = DataManager()
night_mode_manager = NightModeManager()

SELECTORS = {
    'product': 'li.catalog-category-products__product',
    'title': 'a.catalog-category-product__title',
    'price': 'div.catalog-product-purchase__current-price',
    'image': 'div.catalog-category-product__image img'
}

# Клавиатура с кнопками
def get_main_keyboard():
    keyboard = [
        [KeyboardButton("🔄 Проверить сейчас")],
        [KeyboardButton("📊 Статус подписки")],
        [KeyboardButton("📈 Статистика")],
        [KeyboardButton("🌙 Ночной режим")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def compare_products(current_products):
    """Сравнивает текущие товары с сохраненными и возвращает изменения"""
    saved_data = data_manager.load_data()
    saved_products = saved_data.get('graphic_cards', {})
    
    current_keys = set()
    new_products = []
    updated_products = []
    
    # Создаем ключи для текущих товаров
    for product in current_products:
        key = data_manager.get_product_key(product['title'], product['price'])
        current_keys.add(key)
        
        if key not in saved_products:
            # Новый товар
            new_products.append(product)
            # Добавляем в сохраненные
            saved_products[key] = {
                'title': product['title'],
                'price': product['price'],
                'price_text': product['price_text'],
                'image_url': product['image_url'],
                'first_seen': datetime.now().isoformat(),
                'last_updated': datetime.now().isoformat()
            }
        else:
            # Товар уже есть, обновляем время
            saved_products[key]['last_updated'] = datetime.now().isoformat()
            updated_products.append(product)
    
    # Ищем удаленные товары
    removed_products = []
    saved_keys = set(saved_products.keys())
    removed_keys = saved_keys - current_keys
    
    for key in removed_keys:
        removed_products.append(saved_products[key])
        # Удаляем из сохраненных
        del saved_products[key]
    
    # Сохраняем обновленные данные
    saved_data['graphic_cards'] = saved_products
    data_manager.save_data(saved_data)
    
    return {
        'new': new_products,
        'updated': updated_products,
        'removed': removed_products
    }

async def send_product_message(bot, user_id, product, message_type="new"):
    """Отправляет сообщение о товаре"""
    try:
        if message_type == "new":
            caption = (
                f"🆕 НОВЫЙ ТОВАР\n"
                f"🎮 {product['title']}\n"
                f"💰 Цена: {product['price_text']}\n"
                f"⏰ Добавлен: {datetime.now().strftime('%H:%M %d.%m.%Y')}"
            )
        elif message_type == "removed":
            caption = (
                f"❌ ТОВАР УДАЛЕН\n"
                f"🎮 {product['title']}\n"
                f"💰 Цена: {product['price_text']}\n"
                f"⏰ Удален: {datetime.now().strftime('%H:%M %d.%m.%Y')}"
            )
        else:
            return
        
        await bot.send_photo(
            chat_id=user_id,
            photo=product['image_url'],
            caption=caption
        )
        await asyncio.sleep(0.5)
        
    except Exception as e:
        logger.error(f"Error sending {message_type} product to {user_id}: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user = update.effective_user
    night_mode_status = "включен" if night_mode_manager.is_night_mode_enabled() else "выключен"
    
    welcome_text = (
        f"Привет, {user.first_name}! 👋\n\n"
        "Я бот для отслеживания видеокарт на DNS-Shop.\n"
        f"Я буду присылать уведомления о видеокартах дешевле {TARGET_PRICE} BYN.\n\n"
        f"📊 Интервал проверки: {CHECK_INTERVAL_HOURS} часа\n"
        f"🌙 Ночной режим: {night_mode_status}\n\n"
        "Доступные команды:\n"
        "/start_mail - Подписаться на рассылку\n"
        "/check_now - Принудительная проверка\n"
        "/stats - Статистика отслеживания\n"
        "/night_mode - Переключить ночной режим\n"
        "Или используйте кнопки ниже ⬇️"
    )
    await update.message.reply_text(welcome_text, reply_markup=get_main_keyboard())

async def start_mail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start_mail"""
    user_id = update.effective_user.id
    if subscription_manager.add_user(user_id):
        await update.message.reply_text(
            f"✅ Вы успешно подписались на рассылку! Буду присылать уведомления о видеокартах дешевле {TARGET_PRICE} BYN.",
            reply_markup=get_main_keyboard()
        )
    else:
        await update.message.reply_text(
            "ℹ️ Вы уже подписаны на рассылку.",
            reply_markup=get_main_keyboard()
        )

async def check_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /check_now"""
    user_id = update.effective_user.id
    users = subscription_manager.get_all_users()
    
    if user_id not in users:
        await update.message.reply_text(
            "❌ Вы не подписаны на рассылку. Сначала используйте /start_mail",
            reply_markup=get_main_keyboard()
        )
        return
    
    await update.message.reply_text(
        "🔍 Запускаю принудительную проверку... Это может занять несколько секунд.",
        reply_markup=get_main_keyboard()
    )
    
    # Запускаем проверку
    products = await parser.parse_all_pages(SEARCH_URL, SELECTORS)
    changes = await compare_products(products)
    
    # Отправляем результаты пользователю
    sent_new = 0
    sent_removed = 0
    
    for product in changes['new']:
        await send_product_message(context.bot, user_id, product, "new")
        sent_new += 1
    
    for product in changes['removed']:
        await send_product_message(context.bot, user_id, product, "removed")
        sent_removed += 1
    
    summary = []
    if sent_new > 0:
        summary.append(f"🆕 Новых: {sent_new}")
    if sent_removed > 0:
        summary.append(f"❌ Удаленных: {sent_removed}")
    if not summary:
        summary.append("ℹ️ Изменений не обнаружено")
    
    await update.message.reply_text(
        f"✅ Проверка завершена:\n" + "\n".join(summary),
        reply_markup=get_main_keyboard()
    )

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает статистику отслеживания"""
    data = data_manager.load_data()
    products_count = len(data.get('graphic_cards', {}))
    
    night_mode_status = "включен" if night_mode_manager.is_night_mode_enabled() else "выключен"
    night_time_status = "ночное время" if night_mode_manager.is_night_time() else "дневное время"
    
    await update.message.reply_text(
        f"📊 Статистика отслеживания:\n\n"
        f"• Отслеживается товаров: {products_count}\n"
        f"• Целевая цена: {TARGET_PRICE} BYN\n"
        f"• Интервал проверки: {CHECK_INTERVAL_HOURS} часа\n"
        f"• Ночной режим: {night_mode_status}\n"
        f"• Сейчас: {night_time_status}",
        reply_markup=get_main_keyboard()
    )

async def night_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключает ночной режим"""
    user_id = update.effective_user.id
    is_enabled = night_mode_manager.toggle_night_mode(user_id)
    
    status = "включен" if is_enabled else "выключен"
    description = (
        "🌙 Ночной режим ВКЛЮЧЕН\n\n"
        "С 00:00 до 08:00 бот будет:\n"
        "• Парсить сайт каждые 3 часа\n"
        "• Обновлять базу данных\n"
        "• НЕ отправлять уведомления\n\n"
        "Уведомления будут отправлены утром, когда появятся новые изменения!"
        if is_enabled else
        "☀️ Ночной режим ВЫКЛЮЧЕН\n\n"
        "Бот будет работать в обычном режиме и отправлять уведомления в любое время."
    )
    
    await update.message.reply_text(
        f"✅ Ночной режим {status}!\n\n{description}",
        reply_markup=get_main_keyboard()
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатий кнопок"""
    text = update.message.text
    user_id = update.effective_user.id
    
    if text == "🔄 Проверить сейчас":
        await check_now(update, context)
    elif text == "📊 Статус подписки":
        users = subscription_manager.get_all_users()
        if user_id in users:
            await update.message.reply_text(
                "✅ Вы подписаны на рассылку\n\n"
                f"Целевая цена: {TARGET_PRICE} BYN\n"
                f"Следующая автоматическая проверка через {CHECK_INTERVAL_HOURS} часа",
                reply_markup=get_main_keyboard()
            )
        else:
            await update.message.reply_text(
                "❌ Вы не подписаны на рассылку\n\n"
                f"Целевая цена: {TARGET_PRICE} BYN\n"
                "Используйте /start_mail для подписки",
                reply_markup=get_main_keyboard()
            )
    elif text == "📈 Статистика":
        await stats(update, context)
    elif text == "🌙 Ночной режим":
        await night_mode(update, context)

async def send_notifications():
    """Функция для автоматической рассылки"""
    try:
        # Проверяем, нужно ли отправлять уведомления
        if not night_mode_manager.should_send_notifications():
            logger.info("Skipping notifications due to night mode")
            return
            
        users = subscription_manager.get_all_users()
        if not users:
            logger.info("No subscribers found for automatic notification")
            return

        logger.info(f"Sending automatic notifications to {len(users)} users")
        products = await parser.parse_all_pages(SEARCH_URL, SELECTORS)
        changes = await compare_products(products)
        
        if not changes['new'] and not changes['removed']:
            logger.info("No changes detected for automatic notification")
            return

        # Создаем Application для отправки
        application = Application.builder().token(TOKEN).build()
        
        for user_id in users:
            sent_new = 0
            sent_removed = 0
            
            for product in changes['new']:
                await send_product_message(application.bot, user_id, product, "new")
                sent_new += 1
            
            for product in changes['removed']:
                await send_product_message(application.bot, user_id, product, "removed")
                sent_removed += 1
            
            summary = []
            if sent_new > 0:
                summary.append(f"🆕 Новых: {sent_new}")
            if sent_removed > 0:
                summary.append(f"❌ Удаленных: {sent_removed}")
            
            if summary:
                await application.bot.send_message(
                    chat_id=user_id,
                    text=f"📢 Автоматическое обновление:\n" + "\n".join(summary),
                    reply_markup=get_main_keyboard()
                )
                    
    except Exception as e:
        logger.error(f"Error in automatic send_notifications: {e}")

async def scheduled_task():
    """Задача для периодической проверки"""
    while True:
        try:
            # Всегда парсим и обновляем базу, даже в ночном режиме
            logger.info("Starting scheduled parsing...")
            products = await parser.parse_all_pages(SEARCH_URL, SELECTORS)
            await compare_products(products)
            logger.info("Scheduled parsing completed")
            
            # Отправляем уведомления только если разрешено
            await send_notifications()
            
            logger.info(f"Next check in {CHECK_INTERVAL_HOURS} hours")
            await asyncio.sleep(CHECK_INTERVAL_HOURS * 3600)
            
        except Exception as e:
            logger.error(f"Error in scheduled task: {e}")
            await asyncio.sleep(300)

def main():
    # Проверка наличия токена
    if not TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not found in environment variables")
        logger.error("Please create .env file with TELEGRAM_BOT_TOKEN=your_token")
        return
    
    # Создаем Application
    application = Application.builder().token(TOKEN).build()
    
    # Добавляем обработчики команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("start_mail", start_mail))
    application.add_handler(CommandHandler("check_now", check_now))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("night_mode", night_mode))
    
    # Добавляем обработчик кнопок
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, button_handler))

    # Запускаем планировщик в отдельной задаче
    loop = asyncio.get_event_loop()
    loop.create_task(scheduled_task())

    # Запускаем бота
    logger.info("Bot started with night mode and 3-hour interval")
    application.run_polling()

if __name__ == '__main__':
    main()