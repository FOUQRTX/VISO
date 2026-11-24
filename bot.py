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

# --- ЛОГИРОВАНИЕ ---
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger(__name__)

# --- ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ---
TOKEN = os.getenv("BOT_TOKEN")
AI_API_KEY = os.getenv("AI_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID") or "0")

if not TOKEN:
    logger.error("❌ Не задан BOT_TOKEN!")
    sys.exit(1)

bot = Bot(token=TOKEN)
dp = Dispatcher()

PROXYAPI_URL = "https://" + "api.proxyapi.ru" + "/openai/v1"
# --- БАЗА ДАННЫХ ---
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
        balance INTEGER DEFAULT 1,
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
conn.commit()


# --- ХЕЛПЕРЫ БД ---
def get_user(user_id: int):
    cursor.execute("SELECT balance, referrals FROM users WHERE user_id = ?", (user_id,))
    return cursor.fetchone()


def add_user(user_id: int, referrer_id: int | None = None) -> bool:
    if not get_user(user_id):
        cursor.execute("INSERT INTO users (user_id, balance, referrals) VALUES (?, 1, 0)", (user_id,))
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


# --- СОСТОЯНИЯ ---
class BotStates(StatesGroup):
    waiting_for_prompt = State()
    waiting_for_photo = State()
    waiting_for_edit_prompt = State()
    waiting_for_promo = State()


# --- КЛАВИАТУРА ---
main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🎨 Создать картинку"), KeyboardButton(text="✏️ Редактировать фото")],
        [KeyboardButton(text="👤 Мой профиль"), KeyboardButton(text="💎 Прайс и Пополнение")],
        [KeyboardButton(text="🎁 Пригласить друга"), KeyboardButton(text="🎟 Ввести бонус-код")]
    ],
    resize_keyboard=True
)


# --- /START ---
@dp.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    referrer_id = int(command.args) if (command.args and command.args.isdigit()) else None

    if add_user(user_id, referrer_id) and referrer_id:
        try:
            await bot.send_message(
                referrer_id,
                "🎉 По вашей ссылке зарегистрировался друг! Вам начислена <b>+1 генерация</b>.",
                parse_mode="HTML"
            )
        except Exception:
            pass

    await message.answer(
        "👋 Добро пожаловать в <b>VISO AI</b>!\n\n"
        "🎁 Вам доступно <b>3 бесплатные генерации</b>.\n"
        "Выберите действие ниже 👇",
        reply_markup=main_kb,
        parse_mode="HTML"
    )
# --- АДМИНКА: СОЗДАНИЕ ПРОМОКОДА ---
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
        code, reward, uses = parts[0].upper(), int(parts[1]), int(parts[2])
        if reward <= 0 or uses <= 0:
            await message.answer("❌ Награда и активации должны быть больше 0!")
            return
        cursor.execute("DELETE FROM used_promos WHERE code = ?", (code,))
        cursor.execute(
            "INSERT OR REPLACE INTO promocodes (code, reward, uses_left) VALUES (?, ?, ?)",
            (code, reward, uses)
        )
        conn.commit()
        await message.answer(f"✅ Код <code>{code}</code> создан (+{reward} ген., {uses} акт.)", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Пример: <code>/addpromo BONUS5 5 50</code>\nОшибка: {html.escape(str(e))}", parse_mode="HTML")


# --- ПРОФИЛЬ ---
@dp.message(F.text == "👤 Мой профиль")
async def profile_handler(message: types.Message, state: FSMContext):
    await state.clear()
    balance, refs = ensure_user(message.from_user.id)
    await message.answer(
        f"👤 <b>Профиль</b>\n\n"
        f"🆔 ID: <code>{message.from_user.id}</code>\n"
        f"⚡ Баланс: <b>{balance} шт.</b>\n"
        f"👥 Друзей: <b>{refs}</b>",
        parse_mode="HTML"
    )


# --- ПРАЙС ---
@dp.message(F.text == "💎 Прайс и Пополнение")
async def price_handler(message: types.Message, state: FSMContext):
    await state.clear()
    pay_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="10 генераций — 390 ₽", callback_data="buy_10")],
        [InlineKeyboardButton(text="🔥 30 генераций — 990 ₽", callback_data="buy_30")],
        [InlineKeyboardButton(text="100 генераций — 2 490 ₽", callback_data="buy_100")]
    ])
    await message.answer("💎 <b>Тарифы VISO AI</b>\nВыберите пакет ниже 👇", reply_markup=pay_kb, parse_mode="HTML")


@dp.callback_query(F.data.startswith("buy_"))
async def process_buy(callback: types.CallbackQuery):
    _, pack = callback.data.split("_", 1)
    await callback.answer()
    await callback.message.answer(
        f"💳 Выбран пакет на <b>{html.escape(pack)} генераций</b>. Оплата скоро будет подключена!",
        parse_mode="HTML"
    )


# --- РЕФЕРАЛКА ---
@dp.message(F.text == "🎁 Пригласить друга")
async def referral_handler(message: types.Message, state: FSMContext):
    await state.clear()
    bot_info = await bot.get_me()
    ref_link = f"https://t.me{bot_info.username}?start={message.from_user.id}"
    await message.answer(
        f"🎁 За каждого друга по ссылке вы получите <b>+1 генерацию</b>!\n\n"
        f"🔗 Ваша ссылка (нажмите, чтобы скопировать):\n<code>{ref_link}</code>",
        parse_mode="HTML"
    )


# --- БОНУС-КОД ---
@dp.message(F.text == "🎟 Ввести бонус-код")
async def promo_start(message: types.Message, state: FSMContext):
    await state.clear()
    ensure_user(message.from_user.id)
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
        await message.answer("❌ Лимит активаций этого бонус-кода исчерпан.")
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
# --- ГЕНЕРАЦИЯ КАРТИНКИ ---
@dp.message(F.text == "🎨 Создать картинку")
async def create_image_start(message: types.Message, state: FSMContext):
    await state.clear()
    balance, _ = ensure_user(message.from_user.id)
    if balance <= 0:
        await message.answer("❌ Закончились генерации! Пригласите друга или введите бонус-код.")
        return
    await state.set_state(BotStates.waiting_for_prompt)
    await message.answer("✍️ <b>Опишите на русском, что нужно нарисовать:</b>", parse_mode="HTML")


@dp.message(BotStates.waiting_for_prompt)
async def create_image_process(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("❌ Опишите картинку текстом.")
        return

    balance, _ = ensure_user(message.from_user.id)
    if balance <= 0:
        await message.answer("❌ Закончились генерации!")
        await state.clear()
        return

    wait_msg = await message.answer("⏳ Рисую (15–20 сек)...")
    try:
        prompt_text = message.text.strip()[:500]
        headers = {
            "Authorization": f"Bearer {AI_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "gpt-image-1.5",
            "prompt": prompt_text,
            "size": "1024x1024",
            "quality": "medium",
            "n": 1
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{PROXYAPI_URL}/images/generations", json=payload, headers=headers) as resp:
                if resp.status != 200:
                    err_text = await resp.text()
                    raise RuntimeError(f"API {resp.status}: {err_text}")
                result = await resp.json()

        image_bytes = base64.b64decode(result["data"][0]["b64_json"])
        await message.answer_photo(
            BufferedInputFile(image_bytes, filename="result.png"),
            caption=f"✨ <b>Готово!</b>\n📝 <i>{html.escape(prompt_text)}</i>",
            parse_mode="HTML"
        )
        change_balance(message.from_user.id, -1)
        await wait_msg.delete()
    except Exception:
        logger.exception("Ошибка генерации")
        await wait_msg.edit_text("❌ Не удалось сгенерировать картинку. Попробуйте изменить запрос или повторить позже.")
    await state.clear()


# --- РЕДАКТИРОВАНИЕ ФОТО ---
@dp.message(F.text == "✏️ Редактировать фото")
async def edit_photo_start(message: types.Message, state: FSMContext):
    await state.clear()
    balance, _ = ensure_user(message.from_user.id)
    if balance <= 0:
        await message.answer("❌ Закончились генерации! Пригласите друга или введите бонус-код.")
        return
    await state.set_state(BotStates.waiting_for_photo)
    await message.answer("📸 Отправьте фото для редактирования:")


@dp.message(BotStates.waiting_for_photo, F.photo)
async def edit_photo_get_image(message: types.Message, state: FSMContext):
    await state.update_data(photo_id=message.photo[-1].file_id)
    await state.set_state(BotStates.waiting_for_edit_prompt)
    await message.answer("✍️ <b>Напишите, что нужно изменить на фото:</b>", parse_mode="HTML")


@dp.message(BotStates.waiting_for_photo, ~F.photo)
async def edit_photo_wrong(message: types.Message):
    await message.answer("❌ Отправьте именно <b>фото</b> (не файлом).", parse_mode="HTML")


@dp.message(BotStates.waiting_for_edit_prompt)
async def edit_photo_process(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("❌ Напишите задачу текстом.")
        return

    balance, _ = ensure_user(message.from_user.id)
    if balance <= 0:
        await message.answer("❌ Закончились генерации!")
        await state.clear()
        return

    data = await state.get_data()
    edit_prompt = message.text.strip()[:500]
    wait_msg = await message.answer("⏳ Редактирую фото (15–25 сек)...")

    try:
        file_info = await bot.get_file(data.get("photo_id"))
        photo_io = BytesIO()
        await bot.download_file(file_info.file_path, photo_io)
        photo_io.seek(0)

        form = aiohttp.FormData()
        form.add_field("model", "gpt-image-1.5")
        form.add_field("prompt", edit_prompt)
        form.add_field("size", "1024x1024")
        form.add_field("image", photo_io, filename="image.jpg", content_type="image/jpeg")

        headers = {"Authorization": f"Bearer {AI_API_KEY}"}
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{PROXYAPI_URL}/images/edits", data=form, headers=headers) as resp:
                if resp.status != 200:
                    err_text = await resp.text()
                    raise RuntimeError(f"API {resp.status}: {err_text}")
                result = await resp.json()

        result_bytes = base64.b64decode(result["data"][0]["b64_json"])
        await message.answer_photo(
            BufferedInputFile(result_bytes, filename="edited.png"),
            caption=f"✨ <b>Готово!</b>\n📝 <i>{html.escape(edit_prompt)}</i>",
            parse_mode="HTML"
        )
        change_balance(message.from_user.id, -1)
        await wait_msg.delete()
    except Exception:
        logger.exception("Ошибка редактирования")
        await wait_msg.edit_text("❌ Не удалось отредактировать фото. Попробуйте другое фото или запрос.")
    await state.clear()


# --- ЗАПУСК ---
async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    me = await bot.get_me()
    logger.info(f"✅ Бот @{me.username} запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
