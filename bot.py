import os
import sys
import html
import asyncio
import sqlite3
import logging
import base64
import uuid
import zipfile
import io
from io import BytesIO
from pathlib import Path
from typing import Optional

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

if not TOKEN or not AI_API_KEY:
    logger.error("❌ Не заданы BOT_TOKEN или AI_API_KEY!")
    sys.exit(1)

bot = Bot(token=TOKEN)
dp = Dispatcher()

PROXYAPI_URL = "https://api.proxyapi.ru/openai/v1"
MODEL = "gpt-image-1.5"  # Если будет ошибка API, поменяй на "gpt-image-1"
FREE_GENERATIONS = 3

# ============ ЦЕНЫ ============
PRICE_SINGLE = 1
PRICE_PACK_3 = 2
PRICE_PACK_5 = 4
PRICE_REDO = 1
FREE_REDO_PER_PACK = 1

# ============ БАЗА ДАННЫХ ============
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    DATA_DIR = Path("data")
    DATA_DIR.mkdir(parents=True, exist_ok=True)

conn = sqlite3.connect(DATA_DIR / "users.db", check_same_thread=False)
cursor = conn.cursor()

# Создаем таблицы
cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        balance INTEGER DEFAULT 3,
        referrals INTEGER DEFAULT 0,
        onboarded INTEGER DEFAULT 0
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
cursor.execute("""
    CREATE TABLE IF NOT EXISTS packs (
        pack_id TEXT PRIMARY KEY,
        user_id INTEGER,
        size INTEGER,
        category TEXT,
        style TEXT,
        redo_used INTEGER DEFAULT 0,
        file_ids TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
""")
conn.commit()

# ============ АВТОМАТИЧЕСКАЯ МИГРАЦИЯ БД ============
# Это защитит от ошибки "no such column: onboarded", если база старая
def check_db_columns():
    cursor.execute("PRAGMA table_info(users)")
    columns = [row[1] for row in cursor.fetchall()]
    if "onboarded" not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN onboarded INTEGER DEFAULT 0")
        logger.info("🔧 Миграция БД: добавлена колонка onboarded")
    if "referrals" not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN referrals INTEGER DEFAULT 0")
        logger.info("🔧 Миграция БД: добавлена колонка referrals")
    if "balance" not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN balance INTEGER DEFAULT 3")
        logger.info("🔧 Миграция БД: добавлена колонка balance")
    conn.commit()

check_db_columns()

# ============ ХЕЛПЕРЫ БД ============
def get_user(user_id: int):
    cursor.execute("SELECT balance, referrals, onboarded FROM users WHERE user_id = ?", (user_id,))
    return cursor.fetchone()

def add_user(user_id: int, referrer_id: Optional[int] = None) -> bool:
    if not get_user(user_id):
        cursor.execute(
            "INSERT INTO users (user_id, balance, referrals, onboarded) VALUES (?, ?, 0, 0)",
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

def try_deduct_balance(user_id: int, amount: int) -> bool:
    cursor.execute(
        "UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
        (amount, user_id, amount)
    )
    conn.commit()
    return cursor.rowcount > 0

def refund_balance(user_id: int, amount: int):
    cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()

def set_onboarded(user_id: int):
    cursor.execute("UPDATE users SET onboarded = 1 WHERE user_id = ?", (user_id,))
    conn.commit()

# ============ СОСТОЯНИЯ ============
class BotStates(StatesGroup):
    waiting_for_prompt = State()
    waiting_for_photo = State()
    waiting_for_promo = State()
    waiting_for_support = State()

# ============ КЛАВИАТУРЫ ============
main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🎨 Создать пак"), KeyboardButton(text="👤 Мой профиль")],
        [KeyboardButton(text="💎 Тарифы"), KeyboardButton(text="🎁 Пригласить друга")],
        [KeyboardButton(text="🎟 Бонус-код"), KeyboardButton(text="🆘 Помощь и Правила")]
    ],
    resize_keyboard=True
)

def get_pack_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 карточка — 1 генерация", callback_data="pack:1")],
        [InlineKeyboardButton(text="3 карточки — 2 генерации (выгода)", callback_data="pack:3")],
        [InlineKeyboardButton(text="5 карточек — 4 генерации (макс. выгода)", callback_data="pack:5")]
    ])

# ============ /START И ТУР ============
@dp.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    referrer_id = int(command.args) if (command.args and command.args.isdigit()) else None

    is_new = add_user(user_id, referrer_id)
    user = get_user(user_id)

    if is_new and referrer_id:
        try:
            await bot.send_message(
                referrer_id,
                "🎉 По вашей ссылке зарегистрировался друг! Вам начислена <b>+1 генерация</b>.",
                parse_mode="HTML"
            )
        except Exception:
            pass

    if not user[2]:  # onboarded == 0
        await message.answer(
            "👋 Добро пожаловать в <b>VISO AI</b>!\n\n"
            "Мы создаём продающие карточки для Ozon и WB.\n"
            f"🎁 Вам доступно <b>{FREE_GENERATIONS} бесплатные генерации</b>.\n\n"
            "Давайте создадим первую карточку? 👇",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="▶️ Начать тур", callback_data="start_tour")],
                [InlineKeyboardButton(text="❌ Пропустить", callback_data="skip_tour")]
            ]),
            parse_mode="HTML"
        )
    else:
        await message.answer("👋 С возвращением! Выберите действие ниже 👇", reply_markup=main_kb)

@dp.callback_query(F.data == "start_tour")
async def start_tour(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    set_onboarded(cb.from_user.id)
    # ИСПРАВЛЕНИЕ: Удаляем старое сообщение и отправляем новое, чтобы повесить Reply-кнопки
    try:
        await cb.message.delete()
    except Exception:
        pass
    await cb.message.answer(
        "🎨 <b>Шаг 1 из 2:</b> Нажмите «🎨 Создать пак» в меню ниже и загрузите фото.",
        reply_markup=main_kb,
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "skip_tour")
async def skip_tour(cb: types.CallbackQuery):
    await cb.answer()
    set_onboarded(cb.from_user.id)
    # ИСПРАВЛЕНИЕ: Удаляем старое сообщение и отправляем новое
    try:
        await cb.message.delete()
    except Exception:
        pass
    await cb.message.answer(
        "👌 Ок! Вы всегда можете вернуться к инструкции в разделе «🆘 Помощь».",
        reply_markup=main_kb
    )

# ============ СОЗДАНИЕ ПАКА ============
@dp.message(F.text == "🎨 Создать пак")
async def create_pack_start(message: types.Message, state: FSMContext):
    await state.clear()
    user = ensure_user(message.from_user.id)
    if user[0] <= 0:
        await message.answer("❌ Закончились генерации! Пополните баланс в разделе «💎 Тарифы».")
        return
    await message.answer("📦 Выберите размер пака:", reply_markup=get_pack_kb())

@dp.callback_query(F.data.startswith("pack:"))
async def process_pack_size(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    size = int(cb.data.split(":")[1])
    price = {1: PRICE_SINGLE, 3: PRICE_PACK_3, 5: PRICE_PACK_5}.get(size, size)
    
    user = ensure_user(cb.from_user.id)
    if user[0] < price:
        await cb.message.edit_text(f"❌ Недостаточно генераций для пака на {size} шт. Нужно: {price}. У вас: {user[0]}.")
        return
    
    await state.update_data(pack_size=size, pack_price=price)
    await state.set_state(BotStates.waiting_for_photo)
    await cb.message.edit_text(
        f"✅ Выбран пак: {size} карточек.\n"
        f"Спишется: {price} генераций.\n\n"
        f"📸 Теперь загрузите фото вашего товара:"
    )

@dp.message(BotStates.waiting_for_photo, F.photo)
async def process_photo(message: types.Message, state: FSMContext):
    await state.update_data(photo_id=message.photo[-1].file_id)
    await state.set_state(BotStates.waiting_for_prompt)
    await message.answer(
        "✍️ Напишите описание стиля.\n"
        "<i>Например: «на светлом мраморном столике, мягкий свет, эко-стиль».</i>",
        parse_mode="HTML"
    )

@dp.message(BotStates.waiting_for_photo, ~F.photo)
async def wrong_photo(message: types.Message):
    await message.answer("❌ Отправьте именно <b>фотографию</b> (не файлом).", parse_mode="HTML")

# ============ ГЕНЕРАЦИЯ ============
@dp.message(BotStates.waiting_for_prompt)
async def process_generation(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("❌ Напишите описание текстом.")
        return

    data = await state.get_data()
    pack_size = data.get("pack_size", 1)
    pack_price = data.get("pack_price", 1)
    photo_id = data.get("photo_id")

    if not photo_id:
        await message.answer("❌ Фото не найдено. Начните заново.")
        await state.clear()
        return

    if not try_deduct_balance(message.from_user.id, pack_price):
        await message.answer("❌ Закончились генерации! Пополните баланс в «💎 Тарифы».")
        await state.clear()
        return

    wait_msg = await message.answer(f"⏳ Создаю {pack_size} карточек. Это займёт 20-40 секунд...")
    
    try:
        # Скачиваем фото пользователя
        file_info = await bot.get_file(photo_id)
        photo_bytes = BytesIO()
        await bot.download_file(file_info.file_path, photo_bytes)
        photo_bytes.seek(0)

        backgrounds = []
        user_prompt = message.text.strip()[:400]
        
        headers = {"Authorization": f"Bearer {AI_API_KEY}"}
        timeout = aiohttp.ClientTimeout(total=180)
        
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for i in range(pack_size):
                angle_prompts = [
                    "Главный кадр. Товар по центру.",
                    "Ракурс 3/4, виден объем товара.",
                    "Товар в интерьере, лайфстайл.",
                    "Вид сверху (флэтлей) с небольшим декором.",
                    "Крупный план детали товара."
                ]
                prompt = (
                    f"Коммерческая карточка для маркетплейса. Сохрани сам товар без изменений. "
                    f"Замени только фон и освещение: {user_prompt}. {angle_prompts[i % len(angle_prompts)]}. "
                    f"Без текста, надписей и логотипов."
                )
                
                form = aiohttp.FormData()
                form.add_field("model", MODEL)
                form.add_field("prompt", prompt)
                form.add_field("size", "1024x1536")
                form.add_field("quality", "medium")
                form.add_field("image", photo_bytes, filename="image.jpg", content_type="image/jpeg")
                
                async with session.post(f"{PROXYAPI_URL}/images/edits", data=form, headers=headers) as resp:
                    if resp.status != 200:
                        err = await resp.text()
                        raise RuntimeError(f"API {resp.status}: {err[:200]}")
                    res_json = await resp.json()
                
                b64_img = res_json["data"][0]["b64_json"]
                backgrounds.append(BytesIO(base64.b64decode(b64_img)))

        media_group = []
        for i, bg in enumerate(backgrounds):
            media_group.append(types.InputMediaPhoto(media=BufferedInputFile(bg.read(), filename=f"card_{i+1}.jpg")))

        sent_messages = await message.answer_media_group(media_group)
        
        pack_id = str(uuid.uuid4())[:8]
        file_ids = ",".join([m.photo[-1].file_id for m in sent_messages])
        cursor.execute(
            "INSERT INTO packs (pack_id, user_id, size, category, style, file_ids) VALUES (?, ?, ?, ?, ?, ?)",
            (pack_id, message.from_user.id, pack_size, "custom", user_prompt, file_ids)
        )
        conn.commit()

        result_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Переделать", callback_data=f"redo:{pack_id}")],
            [InlineKeyboardButton(text="📦 Собрать ZIP", callback_data=f"zip:{pack_id}")],
            [InlineKeyboardButton(text="➕ Создать ещё", callback_data="create_more")]
        ])
        
        await message.answer(
            f"✨ <b>Пак готов!</b>\n"
            f"📦 Карточек: {pack_size}\n"
            f"💳 Списано: {pack_price} генераций\n"
            f"📝 <i>{html.escape(user_prompt[:100])}...</i>",
            reply_markup=result_kb,
            parse_mode="HTML"
        )
        await wait_msg.delete()

    except Exception as e:
        logger.exception("Ошибка генерации")
        refund_balance(message.from_user.id, pack_price)
        try:
            await wait_msg.edit_text("❌ Ошибка при генерации. Генерации возвращены на баланс. Попробуйте позже.")
        except Exception:
            await message.answer("❌ Ошибка при генерации. Генерации возвращены на баланс.")
    finally:
        await state.clear()

# ============ ZIP И ПЕРЕДЕЛКА ============
@dp.callback_query(F.data.startswith("zip:"))
async def send_zip(cb: types.CallbackQuery):
    await cb.answer()
    pack_id = cb.data.split(":")[1]
    cursor.execute("SELECT file_ids FROM packs WHERE pack_id = ? AND user_id = ?", (pack_id, cb.from_user.id))
    row = cursor.fetchone()
    if not row:
        await cb.message.answer("❌ Пак не найден.")
        return
    
    file_ids = row[0].split(",")
    zip_buffer = io.BytesIO()
    
    with zipfile.ZipFile(zip_buffer, "w") as zf:
        for i, fid in enumerate(file_ids):
            try:
                file_info = await bot.get_file(fid)
                file_bytes = BytesIO()
                await bot.download_file(file_info.file_path, file_bytes)
                zf.writestr(f"card_{i+1}.jpg", file_bytes.getvalue())
            except Exception as e:
                logger.error(f"Ошибка скачивания для ZIP: {e}")
    
    zip_buffer.seek(0)
    await cb.message.answer_document(
        BufferedInputFile(zip_buffer.read(), filename=f"viso_pack_{pack_id}.zip"),
        caption="📦 Ваш архив с карточками готов!"
    )

@dp.callback_query(F.data.startswith("redo:"))
async def process_redo(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    pack_id = cb.data.split(":")[1]
    
    cursor.execute("SELECT redo_used, size FROM packs WHERE pack_id = ? AND user_id = ?", (pack_id, cb.from_user.id))
    row = cursor.fetchone()
    if not row:
        await cb.message.answer("❌ Пак не найден.")
        return
    
    redo_used, size = row
    user = ensure_user(cb.from_user.id)
    
    if redo_used < FREE_REDO_PER_PACK:
        cursor.execute("UPDATE packs SET redo_used = redo_used + 1 WHERE pack_id = ?", (pack_id,))
        conn.commit()
        await cb.message.answer("🔄 Первая переделка бесплатна. Загрузите новое фото или описание заново.")
    else:
        if user[0] < PRICE_REDO:
            await cb.message.answer("❌ Недостаточно генераций для переделки. Нужно: 1.")
            return
        try_deduct_balance(cb.from_user.id, PRICE_REDO)
        cursor.execute("UPDATE packs SET redo_used = redo_used + 1 WHERE pack_id = ?", (pack_id,))
        conn.commit()
        await cb.message.answer("🔄 Списан 1 генерация. Загрузите фото и описание заново.")
    
    await state.update_data(pack_size=size, pack_price=1)
    await state.set_state(BotStates.waiting_for_photo)
    await cb.message.answer("📸 Загрузите фото товара:")

@dp.callback_query(F.data == "create_more")
async def create_more(cb: types.CallbackQuery):
    await cb.answer()
    await cb.message.answer("Выберите размер нового пака:", reply_markup=get_pack_kb())

# ============ ПРОФИЛЬ И ТАРИФЫ ============
@dp.message(F.text == "👤 Мой профиль")
async def profile_handler(message: types.Message, state: FSMContext):
    await state.clear()
    balance, refs, _ = ensure_user(message.from_user.id)
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={message.from_user.id}"
    await message.answer(
        f"👤 <b>Кабинет дизайнера</b>\n\n"
        f"🆔 ID: <code>{message.from_user.id}</code>\n"
        f"⚡ Баланс: <b>{balance} генераций</b> (карточек)\n"
        f"👥 Приглашено друзей: <b>{refs}</b>\n\n"
        f"🎁 <b>Партнёрская ссылка:</b>\n<code>{ref_link}</code>",
        parse_mode="HTML"
    )

@dp.message(F.text == "💎 Тарифы")
async def price_handler(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "💎 <b>Тарифы VISO AI</b>\n\n"
        "⚡ 1 генерация = 1 карточка\n\n"
        "• 10 генераций — 390 ₽\n"
        "• 30 генераций — 990 ₽ (выгоднее)\n"
        "• 100 генераций — 2490 ₽ (максимум)\n\n"
        "🎁 <b>Скидки на паки:</b>\n"
        "• Пак 3 карточки = 2 генерации\n"
        "• Пак 5 карточек = 4 генерации\n\n"
        "<i>Оплата скоро будет подключена. Пока используйте промокоды.</i>",
        parse_mode="HTML"
    )

@dp.message(F.text == "🎁 Пригласить друга")
async def referral_handler(message: types.Message, state: FSMContext):
    await state.clear()
    ensure_user(message.from_user.id)
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={message.from_user.id}"
    await message.answer(
        f"🎁 За каждого друга вы получите <b>+1 генерацию</b>!\n\n"
        f"🔗 <code>{ref_link}</code>",
        parse_mode="HTML"
    )

# ============ ПРОМОКОД ============
@dp.message(F.text == "🎟 Бонус-код")
async def promo_start(message: types.Message, state: FSMContext):
    await state.clear()
    await state.set_state(BotStates.waiting_for_promo)
    await message.answer("🎟 Введите ваш бонус-код:")

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

    await message.answer(f"🎉 Код <b>{html.escape(code)}</b> активирован! Начислено: <b>+{reward} генераций</b>.", parse_mode="HTML")
    await state.clear()

# ============ ПОМОЩЬ И ПОДДЕРЖКА ============
@dp.message(F.text == "🆘 Помощь и Правила")
async def help_handler(message: types.Message, state: FSMContext):
    await state.clear()
    help_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 Примеры запросов", callback_data="help_examples")],
        [InlineKeyboardButton(text="📜 Правила и возвраты", callback_data="help_terms")],
        [InlineKeyboardButton(text="💬 Написать в поддержку", callback_data="help_support")]
    ])
    await message.answer("🆘 <b>Раздел помощи</b>\nВыберите нужный пункт 👇", reply_markup=help_kb, parse_mode="HTML")

@dp.callback_query(F.data == "help_examples")
async def help_examples_cb(cb: types.CallbackQuery):
    await cb.answer()
    await cb.message.answer(
        "📖 <b>Примеры запросов:</b>\n\n"
        "⬜ <b>Минимализм:</b>\n"
        "<code>Товар на светлом бетонном подиуме, мягкий утренний свет, слева место под текст</code>\n\n"
        "🌿 <b>Эко:</b>\n"
        "<code>Товар на деревянной столешнице, уютная светлая кухня на фоне, тёплый студийный свет</code>\n\n"
        "💎 <b>Премиум:</b>\n"
        "<code>Товар на тёмном матовом камне, контровой холодный свет, рекламная съёмка</code>",
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "help_terms")
async def help_terms_cb(cb: types.CallbackQuery):
    await cb.answer()
    await cb.message.answer(
        "📜 <b>Правила и возвраты:</b>\n\n"
        "1. <b>Предмет:</b> доступ к ИИ для генерации изображений.\n"
        "2. <b>Результат:</b> качество зависит от фото и описания.\n"
        "3. <b>Права:</b> пользователь гарантирует наличие прав на загружаемые фото.\n"
        "4. <b>Возврат:</b>\n"
        "• Сбой сервера — генерации не списываются (возвращаются автоматически).\n"
        "• Возврат средств — только за неиспользованный остаток.\n"
        "• Использованные генерации возврату не подлежат.",
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "help_support")
async def help_support_cb(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    if not ADMIN_ID:
        await cb.message.answer("❌ Поддержка временно недоступна.")
        return
    await state.set_state(BotStates.waiting_for_support)
    await cb.message.answer("💬 <b>Напишите ваш вопрос поддержке:</b>", parse_mode="HTML")

@dp.message(BotStates.waiting_for_support)
async def support_receive(message: types.Message, state: FSMContext):
    if not ADMIN_ID:
        await message.answer("❌ Поддержка недоступна.")
        await state.clear()
        return

    user_info = f"@{message.from_user.username}" if message.from_user.username else "без юзернейма"
    header = (
        f"📩 <b>Обращение в поддержку</b>\n"
        f"👤 От: {html.escape(message.from_user.full_name)} ({user_info})\n"
        f"🆔 ID: <code>{message.from_user.id}</code>\n"
        f"<i>Ответьте на это сообщение (Reply)</i>"
    )
    try:
        admin_msg = await bot.send_message(ADMIN_ID, header, parse_mode="HTML")
        await message.forward(ADMIN_ID)
        cursor.execute("INSERT OR REPLACE INTO support_tickets (admin_msg_id, user_id) VALUES (?, ?)", (admin_msg.message_id, message.from_user.id))
        conn.commit()
        await message.answer("✅ Передано в поддержку. Ответ придёт сюда. Для выхода — /cancel.")
    except Exception:
        await message.answer("❌ Не удалось отправить.")

# ============ АДМИНКА ============
@dp.message(Command("addpromo"))
async def add_promo_cmd(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args:
        await message.answer("Формат: <code>/addpromo КОД НАГРАДА АКТИВАЦИИ</code>", parse_mode="HTML")
        return
    try:
        parts = command.args.split()
        if len(parts) != 3: raise ValueError
        code, reward, uses = parts[0].upper(), int(parts[1]), int(parts[2])
        if reward <= 0 or uses <= 0: raise ValueError
        cursor.execute("INSERT OR REPLACE INTO promocodes (code, reward, uses_left) VALUES (?, ?, ?)", (code, reward, uses))
        conn.commit()
        await message.answer(f"✅ Код <code>{code}</code> создан (+{reward} ген., {uses} акт.)", parse_mode="HTML")
    except Exception:
        await message.answer("❌ Ошибка формата.")

@dp.message(Command("addbalance"))
async def add_balance_cmd(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID or not command.args: return
    try:
        raw_id, raw_amount = command.args.split()
        target_id, amount = int(raw_id), int(raw_amount)
        ensure_user(target_id)
        change_balance(target_id, amount)
        await message.answer(f"✅ Начислено {amount} ген. пользователю {target_id}.")
        try: await bot.send_message(target_id, f"⚡ Администратор начислил вам <b>+{amount} генераций</b>!", parse_mode="HTML")
        except Exception: pass
    except Exception:
        await message.answer("❌ Формат: <code>/addbalance USER_ID КОЛИЧЕСТВО</code>", parse_mode="HTML")

@dp.message(Command("cancel"))
async def cancel_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Действие отменено.", reply_markup=main_kb)

# --- ОТВЕТ АДМИНА В ПОДДЕРЖКУ ---
async def is_support_reply(message: types.Message) -> bool:
    if message.from_user.id != ADMIN_ID or not message.reply_to_message: return False
    cursor.execute("SELECT 1 FROM support_tickets WHERE admin_msg_id = ?", (message.reply_to_message.message_id,))
    return cursor.fetchone() is not None

@dp.message(is_support_reply)
async def support_admin_reply(message: types.Message):
    cursor.execute("SELECT user_id FROM support_tickets WHERE admin_msg_id = ?", (message.reply_to_message.message_id,))
    row = cursor.fetchone()
    if not row: return
    try:
        await message.copy_to(row[0])
        await message.reply("✅ Ответ отправлен.")
    except Exception:
        await message.reply("❌ Ошибка отправки.")

# ============ ЗАПУСК ============
async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    me = await bot.get_me()
    logger.info(f"✅ Бот @{me.username} запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
