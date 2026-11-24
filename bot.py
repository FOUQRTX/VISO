import os
import sys
import html
import asyncio
import sqlite3
import logging
import base64
from io import BytesIO
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, CommandObject, Command
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# ============ ЛОГИРОВАНИЕ ============
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger(__name__)

# ============ ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ============
TOKEN = os.getenv("BOT_TOKEN")
AI_API_KEY = os.getenv("AI_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID") or "0")

if not TOKEN:
    logger.error("❌ Не задан BOT_TOKEN!")
    sys.exit(1)
if not AI_API_KEY:
    logger.error("❌ Не задан AI_API_KEY!")
    sys.exit(1)

bot = Bot(token=TOKEN)
dp = Dispatcher()

PROXYAPI_URL = "https://api.proxyapi.ru/openai/v1"
MODEL = "gpt-image-1.5"          # если ProxyAPI не поддерживает — поменяйте на "gpt-image-1"
FREE_GENERATIONS = 3             # стартовый баланс

# ============ БАЗА ДАННЫХ ============
DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    DATA_DIR = Path("data")
    DATA_DIR.mkdir(parents=True, exist_ok=True)

conn = sqlite3.connect(DATA_DIR / "users.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        balance INTEGER DEFAULT 3,
        referrals INTEGER DEFAULT 0
    )
""")
cursor.execute("""
    CREATE TABLE IF NOT EXISTS promocodes (
        code TEXT PRIMARY KEY,
        reward INTEGER,
        uses_left INTEGER
    )
""")
cursor.execute("""
    CREATE TABLE IF NOT EXISTS used_promos (
        user_id INTEGER,
        code TEXT,
        PRIMARY KEY (user_id, code)
    )
""")
cursor.execute("""
    CREATE TABLE IF NOT EXISTS support_tickets (
        admin_msg_id INTEGER PRIMARY KEY,
        user_id INTEGER
    )
""")
conn.commit()


# ============ ХЕЛПЕРЫ БД ============
def get_user(user_id: int):
    cursor.execute("SELECT balance, referrals FROM users WHERE user_id = ?", (user_id,))
    return cursor.fetchone()


def add_user(user_id: int, referrer_id: int | None = None) -> bool:
    if not get_user(user_id):
        cursor.execute(
            "INSERT INTO users (user_id, balance, referrals) VALUES (?, ?, 0)",
            (user_id, FREE_GENERATIONS)
        )
        if referrer_id and referrer_id != user_id and get_user(referrer_id):
            cursor.execute(
                "UPDATE users SET balance = balance + 1, referrals = referrals + 1 WHERE user_id = ?",
                (referrer_id,)
            )
        conn.commit()
        return True
    return False


def ensure_user(user_id: int):
    user = get_user(user_id)
    if not user:
        add_user(user_id)
        user = get_user(user_id)
    return user


def change_balance(user_id: int, amount: int):
    cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()


def try_deduct_balance(user_id: int) -> bool:
    """Атомарно списывает 1 генерацию. Возвращает True при успехе."""
    cursor.execute(
        "UPDATE users SET balance = balance - 1 WHERE user_id = ? AND balance > 0",
        (user_id,)
    )
    conn.commit()
    return cursor.rowcount > 0


def refund_balance(user_id: int):
    """Возвращает 1 генерацию при ошибке API."""
    cursor.execute("UPDATE users SET balance = balance + 1 WHERE user_id = ?", (user_id,))
    conn.commit()


# ============ СОСТОЯНИЯ ============
class BotStates(StatesGroup):
    waiting_for_prompt = State()
    waiting_for_photo = State()
    waiting_for_edit_prompt = State()
    waiting_for_promo = State()
    waiting_for_support = State()


# ============ КЛАВИАТУРА ============
main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📦 Карточка товара"), KeyboardButton(text="🎨 Создать картинку")],
        [KeyboardButton(text="👤 Мой профиль"),     KeyboardButton(text="💎 Тарифы")],
        [KeyboardButton(text="🎁 Пригласить друга"), KeyboardButton(text="🎟 Бонус-код")],
        [KeyboardButton(text="🆘 Помощь и Правила")]
    ],
    resize_keyboard=True
)


# ============ КОМАНДЫ ============
@dp.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    referrer_id = int(command.args) if (command.args and command.args.isdigit()) else None

    is_new = add_user(user_id, referrer_id)
    if is_new and referrer_id:
        try:
            await bot.send_message(
                referrer_id,
                "🎉 По вашей ссылке зарегистрировался друг! Вам начислена <b>+1 генерация</b>.",
                parse_mode="HTML"
            )
        except Exception:
            pass

    await message.answer(
        f"👋 Добро пожаловать в <b>VISO AI</b> — ИИ-студию для селлеров Ozon и Wildberries!\n\n"
        f"📸 Превращаем фото товара в продающую карточку 3:4 за 20 секунд.\n"
        f"🎁 Вам доступны <b>{FREE_GENERATIONS} бесплатные генерации</b>.\n\n"
        f"Выберите действие в меню ниже 👇",
        reply_markup=main_kb,
        parse_mode="HTML"
    )


@dp.message(Command("cancel"))
async def cancel_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Действие отменено.", reply_markup=main_kb)


@dp.message(Command("help"))
async def help_cmd(message: types.Message, state: FSMContext):
    await help_handler(message, state)


@dp.message(Command("addpromo"))
async def add_promo_cmd(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return
    if not command.args:
        await message.answer(
            "Формат: <code>/addpromo КОД НАГРАДА АКТИВАЦИИ</code>\n"
            "Пример: <code>/addpromo BONUS5 5 50</code>",
            parse_mode="HTML"
        )
        return
    try:
        parts = command.args.split()
        if len(parts) != 3:
            raise ValueError("нужно 3 аргумента")
        code, reward, uses = parts[0].upper(), int(parts[1]), int(parts[2])
        if reward <= 0 or uses <= 0:
            await message.answer("❌ Награда и активации должны быть > 0!")
            return
        cursor.execute(
            "INSERT OR REPLACE INTO promocodes (code, reward, uses_left) VALUES (?, ?, ?)",
            (code, reward, uses)
        )
        conn.commit()
        await message.answer(
            f"✅ Код <code>{code}</code> создан (+{reward} ген., {uses} акт.)",
            parse_mode="HTML"
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {html.escape(str(e))}", parse_mode="HTML")


@dp.message(Command("addbalance"))
async def add_balance_cmd(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID or not command.args:
        return
    try:
        raw_id, raw_amount = command.args.split()
        target_id, amount = int(raw_id), int(raw_amount)
        ensure_user(target_id)
        change_balance(target_id, amount)
        await message.answer(
            f"✅ Пользователю <code>{target_id}</code> начислено {amount} ген.",
            parse_mode="HTML"
        )
        try:
            await bot.send_message(
                target_id,
                f"⚡ Администратор начислил вам <b>+{amount} генераций</b>!",
                parse_mode="HTML"
            )
        except Exception:
            pass
    except Exception as e:
        await message.answer(
            f"❌ Формат: <code>/addbalance USER_ID КОЛИЧЕСТВО</code>\nОшибка: {html.escape(str(e))}",
            parse_mode="HTML"
        )


# ============ ОТВЕТ АДМИНА В ПОДДЕРЖКУ ============
async def is_support_reply(message: types.Message) -> bool:
    """Фильтр: сообщение от админа, являющееся Reply на тикет поддержки."""
    if message.from_user.id != ADMIN_ID:
        return False
    if not message.reply_to_message:
        return False
    cursor.execute(
        "SELECT 1 FROM support_tickets WHERE admin_msg_id = ?",
        (message.reply_to_message.message_id,)
    )
    return cursor.fetchone() is not None


@dp.message(is_support_reply)
async def support_admin_reply(message: types.Message):
    cursor.execute(
        "SELECT user_id FROM support_tickets WHERE admin_msg_id = ?",
        (message.reply_to_message.message_id,)
    )
    row = cursor.fetchone()
    if not row:
        return
    user_id = row[0]
    try:
        await message.copy_to(user_id)
        await message.reply("✅ Ответ отправлен пользователю.")
    except Exception as e:
        logger.exception("Ошибка отправки ответа пользователю")
        await message.reply(f"❌ Не удалось отправить: {html.escape(str(e))}")


# ============ КНОПКИ ГЛАВНОГО МЕНЮ ============
@dp.message(F.text == "👤 Мой профиль")
async def profile_handler(message: types.Message, state: FSMContext):
    await state.clear()
    balance, refs = ensure_user(message.from_user.id)
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={message.from_user.id}"
    await message.answer(
        f"👤 <b>Кабинет продавца</b>\n\n"
        f"🆔 Ваш ID: <code>{message.from_user.id}</code>\n"
        f"⚡ Доступно генераций: <b>{balance} шт.</b>\n"
        f"👥 Приглашено друзей: <b>{refs}</b>\n\n"
        f"🎁 <b>Партнёрская ссылка (+1 генерация за друга):</b>\n<code>{ref_link}</code>",
        parse_mode="HTML"
    )


@dp.message(F.text == "💎 Тарифы")
async def price_handler(message: types.Message, state: FSMContext):
    await state.clear()
    pay_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="10 генераций — 390 ₽",   callback_data="buy_10")],
        [InlineKeyboardButton(text="🔥 30 генераций — 990 ₽", callback_data="buy_30")],
        [InlineKeyboardButton(text="100 генераций — 2 490 ₽", callback_data="buy_100")]
    ])
    await message.answer(
        "💎 <b>Тарифы VISO AI</b>\n\n"
        "• <b>10 генераций</b> — 390 ₽ (39 ₽/шт.)\n"
        "• <b>30 генераций</b> — 990 ₽ (33 ₽/шт.)\n"
        "• <b>100 генераций</b> — 2 490 ₽ (25 ₽/шт.)\n\n"
        "<i>Оплачивая тариф, вы соглашаетесь с правилами (раздел «🆘 Помощь и Правила»).</i>",
        reply_markup=pay_kb,
        parse_mode="HTML"
    )


@dp.message(F.text == "🎁 Пригласить друга")
async def referral_handler(message: types.Message, state: FSMContext):
    await state.clear()
    ensure_user(message.from_user.id)
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={message.from_user.id}"
    await message.answer(
        f"🎁 За каждого друга по вашей ссылке вы получите <b>+1 генерацию</b>!\n\n"
        f"🔗 Ваша ссылка (нажмите, чтобы скопировать):\n<code>{ref_link}</code>",
        parse_mode="HTML"
    )


@dp.message(F.text == "🎟 Бонус-код")
async def promo_start(message: types.Message, state: FSMContext):
    await state.clear()
    ensure_user(message.from_user.id)
    await state.set_state(BotStates.waiting_for_promo)
    await message.answer("🎟 Введите ваш бонус-код:")


@dp.message(F.text == "🆘 Помощь и Правила")
async def help_handler(message: types.Message, state: FSMContext):
    await state.clear()
    help_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 Примеры запросов",    callback_data="help_examples")],
        [InlineKeyboardButton(text="📜 Правила и возвраты",   callback_data="help_terms")],
        [InlineKeyboardButton(text="💬 Написать в поддержку", callback_data="help_support")]
    ])
    await message.answer(
        "🆘 <b>Как получить идеальную карточку:</b>\n\n"
        "1️⃣ <b>Фото:</b> снимайте товар при хорошем свете, чтобы этикетка читалась.\n"
        "2️⃣ <b>Описание:</b> пишите конкретно, где стоит товар, какой фон и свет.\n"
        "3️⃣ <b>Инфографика:</b> добавьте в запрос <i>«товар сдвинут вправо, слева место для текста»</i>.\n\n"
        "Выберите раздел ниже 👇",
        reply_markup=help_kb,
        parse_mode="HTML"
    )


@dp.message(F.text == "📦 Карточка товара")
async def edit_photo_start(message: types.Message, state: FSMContext):
    await state.clear()
    balance, _ = ensure_user(message.from_user.id)
    if balance <= 0:
        await message.answer("❌ Закончились генерации! Пополните баланс в разделе «💎 Тарифы».")
        return
    await state.set_state(BotStates.waiting_for_photo)
    await message.answer("📸 <b>Шаг 1 из 2:</b> Отправьте фото вашего товара:", parse_mode="HTML")


@dp.message(F.text == "🎨 Создать картинку")
async def create_image_start(message: types.Message, state: FSMContext):
    await state.clear()
    balance, _ = ensure_user(message.from_user.id)
    if balance <= 0:
        await message.answer("❌ Закончились генерации! Пополните баланс в разделе «💎 Тарифы».")
        return
    await state.set_state(BotStates.waiting_for_prompt)
    await message.answer(
        "✍️ <b>Опишите на русском, что нужно нарисовать:</b>",
        parse_mode="HTML"
    )


# ============ CALLBACK ============
@dp.callback_query(F.data.startswith("buy_"))
async def process_buy(callback: types.CallbackQuery):
    _, pack = callback.data.split("_", 1)
    await callback.answer()
    await callback.message.answer(
        f"💳 Выбран пакет на <b>{html.escape(pack)} генераций</b>.\n"
        f"Автоматическая оплата скоро будет подключена.\n"
        f"Для ручного пополнения напишите в поддержку через раздел «🆘 Помощь».",
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "help_examples")
async def help_examples_cb(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "📖 <b>Готовые шаблоны (нажмите, чтобы скопировать):</b>\n\n"
        "⬜ <b>Минимализм для косметики:</b>\n"
        "<code>Товар на светлом бетонном подиуме, мягкий утренний свет, лёгкие тени, фон размыт, слева место под текст</code>\n\n"
        "🌿 <b>Эко-стиль для дома и кухни:</b>\n"
        "<code>Товар на деревянной столешнице, уютная светлая кухня на фоне, тёплый студийный свет, коммерческая съёмка</code>\n\n"
        "💎 <b>Премиум для электроники:</b>\n"
        "<code>Товар на тёмном матовом камне, контровой холодный свет, глубокие тени, рекламная съёмка</code>",
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "help_terms")
async def help_terms_cb(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "📜 <b>Правила и возвраты (Оферта VISO AI):</b>\n\n"
        "1. <b>Предмет:</b> доступ к алгоритмам ИИ для генерации изображений «как есть».\n"
        "2. <b>Результат:</b> качество зависит от фото и описания. Артефакты не являются сбоем.\n"
        "3. <b>Права:</b> пользователь гарантирует наличие прав на загружаемые фото.\n"
        "4. <b>Возврат:</b>\n"
        "• Сбой сервера — попытка не списывается.\n"
        "• Возврат средств — только за неиспользованный остаток пакета.\n"
        "• Использованные генерации возврату не подлежат.",
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "help_support")
async def help_support_cb(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    if not ADMIN_ID:
        await callback.message.answer("❌ Поддержка временно недоступна.")
        return
    await state.set_state(BotStates.waiting_for_support)
    await callback.message.answer(
        "💬 <b>Чат с поддержкой</b>\n\n"
        "Опишите проблему одним сообщением (можно со скриншотом). "
        "Оператор ответит сюда. Для выхода — /cancel.",
        parse_mode="HTML"
    )


# ============ FSM: ПРОМОКОД ============
@dp.message(BotStates.waiting_for_promo)
async def promo_process(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("❌ Отправьте код текстом.")
        return
    code = message.text.strip().upper()
    user_id = message.from_user.id

    cursor.execute("SELECT reward, uses_left FROM promocodes WHERE code = ?", (code,))
    promo = cursor.fetchone()
    if not promo:
        await message.answer("❌ Бонус-код не найден.")
        await state.clear()
        return

    reward, uses_left = promo
    if uses_left <= 0:
        await message.answer("❌ Лимит активаций этого кода исчерпан.")
        await state.clear()
        return

    cursor.execute("SELECT 1 FROM used_promos WHERE user_id = ? AND code = ?", (user_id, code))
    if cursor.fetchone():
        await message.answer("❌ Вы уже активировали этот код!")
        await state.clear()
        return

    cursor.execute("INSERT INTO used_promos (user_id, code) VALUES (?, ?)", (user_id, code))
    cursor.execute("UPDATE promocodes SET uses_left = uses_left - 1 WHERE code = ?", (code,))
    change_balance(user_id, reward)
    conn.commit()

    await message.answer(
        f"🎉 Код <b>{html.escape(code)}</b> активирован! Начислено: <b>+{reward} генераций</b>.",
        parse_mode="HTML"
    )
    await state.clear()


# ============ FSM: КАРТОЧКА ТОВАРА 3:4 ============
@dp.message(BotStates.waiting_for_photo, F.photo)
async def edit_photo_get_image(message: types.Message, state: FSMContext):
    await state.update_data(photo_id=message.photo[-1].file_id)
    await state.set_state(BotStates.waiting_for_edit_prompt)
    await message.answer(
        "✍️ <b>Шаг 2 из 2:</b> Опишите фон и сцену.\n"
        "<i>Например: «на светлом мраморном подиуме, мягкий утренний свет, справа место под текст»</i>",
        parse_mode="HTML"
    )


@dp.message(BotStates.waiting_for_photo, ~F.photo)
async def edit_photo_wrong(message: types.Message):
    await message.answer("❌ Отправьте именно <b>фотографию</b> (не файлом).", parse_mode="HTML")


@dp.message(BotStates.waiting_for_edit_prompt)
async def edit_photo_process(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("❌ Опишите фон текстом.")
        return

    data = await state.get_data()
    photo_id = data.get("photo_id")
    if not photo_id:
        await message.answer("❌ Фото потеряно. Начните заново — «📦 Карточка товара».")
        await state.clear()
        return

    # атомарное списание
    if not try_deduct_balance(message.from_user.id):
        await message.answer("❌ Закончились генерации! Пополните баланс в «💎 Тарифы».")
        await state.clear()
        return

    user_scene = message.text.strip()[:500]
    full_prompt = (
        f"Коммерческая карточка товара для маркетплейса. Сохрани сам товар, его форму, цвет, "
        f"логотипы и текст на этикетке полностью без изменений. Замени только фон и освещение: {user_scene}. "
        f"Профессиональная предметная студийная съёмка, реалистичные тени, высокая детализация."
    )

    wait_msg = await message.answer("⏳ Создаю карточку 3:4 (15–25 сек)...")
    success = False
    try:
        file_info = await bot.get_file(photo_id)
        photo_io = BytesIO()
        await bot.download_file(file_info.file_path, photo_io)
        photo_io.seek(0)

        form = aiohttp.FormData()
        form.add_field("model", MODEL)
        form.add_field("prompt", full_prompt)
        form.add_field("size", "1024x1536")
        form.add_field("quality", "medium")
        form.add_field("image", photo_io, filename="image.jpg", content_type="image/jpeg")

        headers = {"Authorization": f"Bearer {AI_API_KEY}"}
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{PROXYAPI_URL}/images/edits", data=form, headers=headers) as resp:
                if resp.status != 200:
                    err_text = await resp.text()
                    raise RuntimeError(f"API {resp.status}: {err_text[:300]}")
                result = await resp.json()

        (first_item,) = result["data"]
        result_bytes = base64.b64decode(first_item["b64_json"])
        await message.answer_photo(
            BufferedInputFile(result_bytes, filename="card_3x4.png"),
            caption=f"✨ <b>Карточка готова (3:4)!</b>\n📝 <i>{html.escape(user_scene)}</i>",
            parse_mode="HTML"
        )
        success = True
    except Exception:
        logger.exception("Ошибка создания карточки")
    finally:
        if not success:
            refund_balance(message.from_user.id)
        try:
            await wait_msg.delete()
        except Exception:
            pass
        await state.clear()


# ============ FSM: СОЗДАТЬ КАРТИНКУ ============
@dp.message(BotStates.waiting_for_prompt)
async def create_image_process(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("❌ Опишите картинку текстом.")
        return

    if not try_deduct_balance(message.from_user.id):
        await message.answer("❌ Закончились генерации! Пополните баланс в «💎 Тарифы».")
        await state.clear()
        return

    prompt_text = message.text.strip()[:500]
    wait_msg = await message.answer("⏳ Рисую (15–20 сек)...")
    success = False
    try:
        headers = {
            "Authorization": f"Bearer {AI_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": MODEL,
            "prompt": prompt_text,
            "size": "1024x1536",
            "quality": "medium",
            "n": 1
        }
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{PROXYAPI_URL}/images/generations", json=payload, headers=headers) as resp:
                if resp.status != 200:
                    err_text = await resp.text()
                    raise RuntimeError(f"API {resp.status}: {err_text[:300]}")
                result = await resp.json()

        (first_item,) = result["data"]
        image_bytes = base64.b64decode(first_item["b64_json"])
        await message.answer_photo(
            BufferedInputFile(image_bytes, filename="image.png"),
            caption=f"✨ <b>Готово!</b>\n📝 <i>{html.escape(prompt_text)}</i>",
            parse_mode="HTML"
        )
        success = True
    except Exception:
        logger.exception("Ошибка генерации")
    finally:
        if not success:
            refund_balance(message.from_user.id)
        try:
            await wait_msg.delete()
        except Exception:
            pass
        await state.clear()


# ============ FSM: ПОДДЕРЖКА ============
@dp.message(BotStates.waiting_for_support)
async def support_receive_from_user(message: types.Message, state: FSMContext):
    if not ADMIN_ID:
        await message.answer("❌ Поддержка временно недоступна.")
        await state.clear()
        return

    user_info = f"@{message.from_user.username}" if message.from_user.username else "без юзернейма"
    header = (
        f"📩 <b>Новое обращение в поддержку</b>\n"
        f"👤 От: {html.escape(message.from_user.full_name)} ({user_info})\n"
        f"🆔 ID: <code>{message.from_user.id}</code>\n"
        f"<i>Ответьте на это сообщение (Reply), чтобы написать клиенту</i>"
    )

    try:
        admin_msg = await bot.send_message(ADMIN_ID, header, parse_mode="HTML")
        await message.forward(ADMIN_ID)
        cursor.execute(
            "INSERT OR REPLACE INTO support_tickets (admin_msg_id, user_id) VALUES (?, ?)",
            (admin_msg.message_id, message.from_user.id)
        )
        conn.commit()
        await message.answer(
            "✅ Передано в поддержку. Ответ придёт сюда.\n"
            "Можете отправить ещё сообщение или /cancel для выхода."
        )
    except Exception:
        logger.exception("Ошибка пересылки в поддержку")
        await message.answer("❌ Не удалось отправить. Попробуйте позже.")


# ============ ЗАПУСК ============
async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    me = await bot.get_me()
    logger.info(f"✅ Бот @{me.username} запущен!")
    if ADMIN_ID:
        logger.info(f"👑 Admin ID: {ADMIN_ID}")
    else:
        logger.warning("⚠️ ADMIN_ID не задан — админ-функции и поддержка отключены")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
