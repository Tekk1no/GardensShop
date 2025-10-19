"""
Telegram Pet Shop Bot
Single-file example implementation with sqlite and aiogram (v2).
Features implemented (core):
- /start with referral handling and agreement flow
- main menu (ReplyKeyboard) with: Купить питомца, Канал, Отзывы, Поддержка, Профиль
- "Купить питомца" -> rarity selection -> paginated inline pet lists (8 per page)
- pet detail view with photo, description, price and inline: <<, Купить, >>, Назад
- buy flow: shows payment instructions; user presses "Я оплатил" -> asked to send proof (photo/file) -> forwarded to admin chat
- simple cart accessible from pet lists: add/remove/pay
- admin panel inside the bot: /admin for admins (add pet, broadcast, confirm purchases)
- warnings and ban after 3 warnings
- promo codes structure and discount mechanics (basic)
- checks for channel subscription (two channels) before allowing access

NOTES:
- This is a working skeleton. Some production hardening, security, rate-limits, i18n and payment gateway hooking are left as exercises.
- Dependencies: aiogram (v2.x), SQLAlchemy, aiosqlite, python-dotenv, pillow (optional)

Configure via a .env file or environment variables:
BOT_TOKEN=your_bot_token
ADMIN_IDS=123456789,987654321
CHANNEL_ID=@your_channel or -1001234567890
REVIEWS_CHAT_ID=@your_reviews_chat or -1001234567890
SUPPORT_CHAT_ID=@your_support_chat or -1001234567890
DATABASE=./shop.db

Run: python telegram_pet_shop_bot.py
"""
# -*- coding: utf-8 -*-

import os
import logging
import asyncio
from functools import wraps
from decimal import Decimal
from typing import Optional, List

from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.utils.callback_data import CallbackData

from sqlalchemy import create_engine, Column, Integer, String, Boolean, ForeignKey, Numeric, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship

from dotenv import load_dotenv
import os

# загружаем переменные из my.env
load_dotenv('my.env')

# теперь можно использовать
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_IDS = [int(x) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip().isdigit()]
CHANNEL_ID = os.getenv('CHANNEL_ID')
REVIEWS_CHAT_ID = os.getenv('REVIEWS_CHAT_ID')
SUPPORT_CHAT_ID = os.getenv('SUPPORT_CHAT_ID')
DATABASE = os.getenv('DATABASE', './shop.db')


if BOT_TOKEN == 'PASTE_TOKEN_HERE':
    raise RuntimeError('Please set BOT_TOKEN in environment or .env file')

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Bot init ---
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# --- DB setup ---
Base = declarative_base()
engine = create_engine(f'sqlite:///{DATABASE}', connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)

# --- Models ---
class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True, index=True)
    tg_id = Column(Integer, unique=True, index=True)
    username = Column(String, nullable=True)
    balance = Column(Numeric(10, 2), default=0)
    purchases = Column(Integer, default=0)
    referrals = Column(Integer, default=0)
    referred_by = Column(Integer, nullable=True)
    agreed = Column(Boolean, default=False)
    active = Column(Boolean, default=False)  # passed subscription checks
    warnings = Column(Integer, default=0)
    banned = Column(Boolean, default=False)

class Pet(Base):
    __tablename__ = 'pets'
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String)
    rarity = Column(String)
    price = Column(Numeric(10, 2))
    desc = Column(Text)
    photo_file_id = Column(String, nullable=True)  # Telegram file_id stored

class CartItem(Base):
    __tablename__ = 'cart'
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.tg_id'))
    pet_id = Column(Integer, ForeignKey('pets.id'))

class Purchase(Base):
    __tablename__ = 'purchases'
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer)
    pet_id = Column(Integer)
    status = Column(String, default='pending')  # pending, accepted, rejected
    proof_file_id = Column(String, nullable=True)
    price_paid = Column(Numeric(10,2), default=0)

class Promo(Base):
    __tablename__ = 'promos'
    id = Column(Integer, primary_key=True)
    code = Column(String, unique=True)
    discount_percent = Column(Integer)
    permanent = Column(Boolean, default=False)
    uses_left = Column(Integer, default=0)  # 0 for unlimited

Base.metadata.create_all(bind=engine)

# --- Simple helpers ---
def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()

# callback data factories
pet_list_cb = CallbackData('plist', 'rarity', 'page')
pet_cb = CallbackData('pet', 'pet_id', 'page')
pet_action_cb = CallbackData('pact', 'action', 'pet_id')
cart_cb = CallbackData('cart', 'action', 'pet_id')
admin_purchase_cb = CallbackData('ap', 'action', 'purchase_id')

RARITIES = ['common','uncommon','rare','legendary','mythic','divine','prismatic']
ITEMS_PER_PAGE = 8

# --- Keyboards ---
back_button = KeyboardButton('Назад')
main_kb = ReplyKeyboardMarkup(resize_keyboard=True)
main_kb.add(KeyboardButton('Купить питомца'))
main_kb.add(KeyboardButton('Канал'))
main_kb.add(KeyboardButton('Отзывы'))
main_kb.add(KeyboardButton('Поддержка'))
main_kb.add(KeyboardButton('Профиль'))

# Inline: rarity selection
def rarity_inline():
    ik = InlineKeyboardMarkup(row_width=2)
    for r in RARITIES:
        ik.insert(InlineKeyboardButton(r.capitalize(), callback_data=pet_list_cb.new(rarity=r, page=0)))
    ik.add(InlineKeyboardButton('Назад', callback_data='back_to_main'))
    return ik

# generic back inline
def back_inline(cb='back_to_main'):
    ik = InlineKeyboardMarkup()
    ik.add(InlineKeyboardButton('Назад', callback_data=cb))
    return ik

# --- Decorators ---
def admin_only(handler):
    @wraps(handler)
    async def wrapper(message: types.Message):
        if message.from_user.id not in ADMIN_IDS:
            await message.reply('Ты не админ. Тут нельзя. Хотя бы попробуй себя вести.')
            return
        return await handler(message)
    return wrapper

# --- Start flow & referral handling ---
@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    args = message.get_args()
    ref = None
    if args and args.isdigit():
        ref = int(args)
    session = next(db_session())
    user = session.query(User).filter_by(tg_id=message.from_user.id).first()
    if not user:
        user = User(tg_id=message.from_user.id, username=message.from_user.username or '')
        if ref and ref != message.from_user.id:
            # credit referral only if exists and not same
            ref_user = session.query(User).filter_by(tg_id=ref).first()
            if ref_user:
                user.referred_by = ref
        session.add(user)
        session.commit()
# храним, кто согласился (потом можно заменить на БД)
agreed_users = set()

@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    # текст с кликабельной ссылкой
    text = 'Перед использованием нашего сервиса, ознакомьтесь с <a href="https://telegra.ph/Polzovatelskoe-soglashenie-10-19-18">пользовательским соглашением</a>.'
    
    # кнопка для подтверждения
    ik = InlineKeyboardMarkup()
    ik.add(InlineKeyboardButton('Я ознакомился и согласен', callback_data='agreed'))
    
    # отправка сообщения
    await message.reply(text, parse_mode='HTML', reply_markup=ik)


# обработчик нажатия кнопки
@dp.callback_query_handler(lambda c: c.data == 'agreed')
async def process_agreed(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    agreed_users.add(user_id)
    await bot.answer_callback_query(callback_query.id, text="Спасибо за согласие!")
    await bot.send_message(user_id, "Вы согласились с пользовательским соглашением. Теперь вы можете пользоваться сервисом.")

@dp.callback_query_handler(lambda c: c.data == 'agreed')
async def agreed_cb(query: types.CallbackQuery):
    session = next(db_session())
    user = session.query(User).filter_by(tg_id=query.from_user.id).first()
    if not user:
        user = User(tg_id=query.from_user.id, username=query.from_user.username or '', agreed=True)
        session.add(user)
    else:
        user.agreed = True
    session.commit()
    # Check subscription to two channels
    ok = await check_subscriptions(query.from_user.id)
    if not ok:
        await query.message.answer('Подпишись на канал и чат с отзывами, затем нажми /start снова.')
        await query.answer()
        session.close()
        return
    user.active = True
    session.commit()
    session.close()
    await query.message.answer('Отлично. Доступ открыт. Вот меню.', reply_markup=main_kb)
    await query.answer()

async def check_subscriptions(tg_user_id: int) -> bool:
    # Attempt to check if user is a member of CHANNEL_ID and REVIEWS_CHAT_ID
    # If config is missing, assume OK (for development)
    if not CHANNEL_ID or not REVIEWS_CHAT_ID:
        return True
    try:
        me1 = await bot.get_chat_member(CHANNEL_ID, tg_user_id)
        me2 = await bot.get_chat_member(REVIEWS_CHAT_ID, tg_user_id)
        allowed = me1.status not in ('left', 'kicked') and me2.status not in ('left', 'kicked')
        return allowed
    except Exception as e:
        logger.info('Subscription check failed: %s', e)
        return False

# --- Main menu handlers ---
@dp.message_handler(lambda m: m.text == 'Купить питомца')
async def buy_menu(message: types.Message):
    session = next(db_session())
    u = session.query(User).filter_by(tg_id=message.from_user.id).first()
    if u and u.banned:
        await message.reply('Ты забанен. Обращайся к админам через другой аккаунт.')
        session.close()
        return
    await message.reply('Выбери редкость питомцев (и не пропускай кнопку назад).', reply_markup=types.ReplyKeyboardRemove())
    await message.answer('Редкости:', reply_markup=rarity_inline())
    session.close()

@dp.message_handler(lambda m: m.text == 'Канал')
async def open_channel(message: types.Message):
    await message.reply('Переход на канал:')
    if CHANNEL_ID:
        await message.answer(f'Перейти: {CHANNEL_ID}', reply_markup=main_kb)
    else:
        await message.answer('Канал не настроен.', reply_markup=main_kb)

@dp.message_handler(lambda m: m.text == 'Отзывы')
async def open_reviews(message: types.Message):
    await message.reply('Чат с отзывами:', reply_markup=main_kb)
    if REVIEWS_CHAT_ID:
        await message.answer(f'Перейти: {REVIEWS_CHAT_ID}')

@dp.message_handler(lambda m: m.text == 'Поддержка')
async def open_support(message: types.Message):
    await message.reply('Поддержка бота:', reply_markup=main_kb)
    if SUPPORT_CHAT_ID:
        await message.answer(f'Перейти: {SUPPORT_CHAT_ID}')

@dp.message_handler(lambda m: m.text == 'Профиль')
async def profile(message: types.Message):
    session = next(db_session())
    user = session.query(User).filter_by(tg_id=message.from_user.id).first()
    if not user:
        await message.reply('Профиль не найден, нажми /start')
        session.close()
        return
    text = f"Профиль @{user.username or 'no_username'}\nБаланс: {user.balance}₽\nСовершено покупок: {user.purchases}\nКоличество рефералов: {user.referrals}"
    ik = InlineKeyboardMarkup()
    ik.add(InlineKeyboardButton('Пополнить баланс', callback_data='topup'))
    ik.add(InlineKeyboardButton('Применить промокод', callback_data='apply_promo'))
    invite_link = f'https://t.me/{(await bot.get_me()).username}?start={message.from_user.id}'
    ik.add(InlineKeyboardButton('Пригласить рефералов', url=invite_link))
    ik.add(InlineKeyboardButton('Назад', callback_data='back_to_main'))
    await message.reply(text, reply_markup=main_kb)
    await message.answer('Фото профиля (пусто).')
    await message.answer('Данные ниже:', reply_markup=ik)
    session.close()

# --- Pets browsing and pagination ---
@dp.callback_query_handler(pet_list_cb.filter())
async def list_pets_cb(query: types.CallbackQuery, callback_data: dict):
    rarity = callback_data['rarity']
    page = int(callback_data['page'])
    session = next(db_session())
    pets = session.query(Pet).filter_by(rarity=rarity).all()
    total = len(pets)
    pages = (total - 1) // ITEMS_PER_PAGE + 1 if total else 1
    start = page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    page_pets = pets[start:end]
    ik = InlineKeyboardMarkup(row_width=2)
    # Cart button
    ik.add(InlineKeyboardButton('Корзина 🛒', callback_data='open_cart'))
    for pet in page_pets:
        ik.insert(InlineKeyboardButton(pet.name, callback_data=pet_cb.new(pet_id=pet.id, page=page)))
    # navigation
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton('<<', callback_data=pet_list_cb.new(rarity=rarity, page=page-1)))
    if page < pages-1:
        nav.append(InlineKeyboardButton('>>', callback_data=pet_list_cb.new(rarity=rarity, page=page+1)))
    if nav:
        ik.row(*nav)
    ik.add(InlineKeyboardButton('Назад', callback_data='back_to_main'))
    await query.message.answer(f'Редкость: {rarity} — страница {page+1}/{pages}')
    await query.message.answer('Питомцы:', reply_markup=ik)
    await query.answer()
    session.close()

@dp.callback_query_handler(pet_cb.filter())
async def pet_detail_cb(query: types.CallbackQuery, callback_data: dict):
    pet_id = int(callback_data['pet_id'])
    page = int(callback_data['page'])
    session = next(db_session())
    pet = session.query(Pet).get(pet_id)
    if not pet:
        await query.answer('Питомец не найден', show_alert=True)
        session.close()
        return
    text = f"{pet.name}\nРедкость: {pet.rarity}\nЦена: {pet.price}₽\nОписание: {pet.desc}"
    ik = InlineKeyboardMarkup()
    ik.add(InlineKeyboardButton('<<', callback_data=pet_list_cb.new(rarity=pet.rarity, page=max(0,page-1))))
    ik.add(InlineKeyboardButton('Купить', callback_data=pet_action_cb.new(action='buy', pet_id=pet.id)))
    ik.add(InlineKeyboardButton('>>', callback_data=pet_list_cb.new(rarity=pet.rarity, page=page+1)))
    ik.add(InlineKeyboardButton('Назад', callback_data=pet_list_cb.new(rarity=pet.rarity, page=page)))
    if pet.photo_file_id:
        try:
            await bot.send_photo(query.from_user.id, pet.photo_file_id, caption=text, reply_markup=ik)
        except Exception:
            await query.message.answer(text, reply_markup=ik)
    else:
        await query.message.answer(text, reply_markup=ik)
    await query.answer()
    session.close()

@dp.callback_query_handler(pet_action_cb.filter(action='buy'))
async def pet_buy_cb(query: types.CallbackQuery, callback_data: dict):
    pet_id = int(callback_data['pet_id'])
    session = next(db_session())
    pet = session.query(Pet).get(pet_id)
    if not pet:
        await query.answer('Питомец пропал.')
        session.close()
        return
    # send payment instructions and create pending purchase record
    text = f'Покупка: {pet.name} — {pet.price}₽\nПереведите деньги по реквизитам: <здесь ваши реквизиты>\nПосле перевода нажмите кнопку "Я оплатил" и пришлите чек.'
    ik = InlineKeyboardMarkup()
    ik.add(InlineKeyboardButton('Я оплатил', callback_data='paid_' + str(pet.id)))
    ik.add(InlineKeyboardButton('Назад', callback_data='back_to_main'))
    # create purchase record with status pending (will be updated when proof is sent / admin acts)
    purchase = Purchase(user_id=query.from_user.id, pet_id=pet.id, status='pending', price_paid=pet.price)
    session.add(purchase)
    session.commit()
    await query.message.answer(text, reply_markup=ik)
    await query.answer()
    session.close()

@dp.callback_query_handler(lambda c: c.data and c.data.startswith('paid_'))
async def paid_cb(query: types.CallbackQuery):
    pet_id = int(query.data.split('_',1)[1])
    session = next(db_session())
    # find last pending purchase for this user and pet
    purchase = session.query(Purchase).filter_by(user_id=query.from_user.id, pet_id=pet_id, status='pending').order_by(Purchase.id.desc()).first()
    if not purchase:
        await query.answer('Нет активной покупки.', show_alert=True)
        session.close()
        return
    # ask for proof
    await query.message.answer('Пришли чек в виде фото или файла сюда. Отправлю администратору.')
    # store in FSM state that we're waiting for proof
    state = dp.current_state(user=query.from_user.id)
    await state.set_state('waiting_proof')
    await state.update_data(purchase_id=purchase.id)
    await query.answer()
    session.close()

# handle proof upload
@dp.message_handler(content_types=types.ContentTypes.PHOTO, state='waiting_proof')
async def handle_proof_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    pid = data.get('purchase_id')
    session = next(db_session())
    purchase = session.query(Purchase).get(pid)
    if not purchase:
        await message.reply('Покупка не найдена. Отправь /start и попробуй снова.')
        await state.finish()
        session.close()
        return
    file_id = message.photo[-1].file_id
    purchase.proof_file_id = file_id
    session.commit()
    await message.reply('Чек получен и отправлен администратору. Ожидай подтверждения.')
    # forward proof to admin(s) with inline accept/reject
    for admin in ADMIN_IDS:
        ik = InlineKeyboardMarkup()
        ik.add(InlineKeyboardButton('Принять', callback_data=admin_purchase_cb.new(action='accept', purchase_id=purchase.id)))
        ik.add(InlineKeyboardButton('Отклонить', callback_data=admin_purchase_cb.new(action='reject', purchase_id=purchase.id)))
        await bot.send_photo(admin, file_id, caption=f'Покупка #{purchase.id} от {message.from_user.id} на {purchase.price_paid}₽', reply_markup=ik)
    await state.finish()
    session.close()

@dp.callback_query_handler(admin_purchase_cb.filter())
async def admin_purchase_action(query: types.CallbackQuery, callback_data: dict):
    action = callback_data['action']
    pid = int(callback_data['purchase_id'])
    session = next(db_session())
    purchase = session.query(Purchase).get(pid)
    if not purchase:
        await query.answer('Покупка не найдена')
        session.close()
        return
    if query.from_user.id not in ADMIN_IDS:
        await query.answer('Только админ может принять решение', show_alert=True)
        session.close()
        return
    buyer = session.query(User).filter_by(tg_id=purchase.user_id).first()
    pet = session.query(Pet).get(purchase.pet_id)
    if action == 'accept':
        purchase.status = 'accepted'
        buyer.purchases += 1
        session.commit()
        await bot.send_message(purchase.user_id, f'Покупка #{pid} подтверждена. С вами свяжется админ.')
        await query.answer('Принято')
    else:
        purchase.status = 'rejected'
        # warn user
        buyer.warnings += 1
        session.commit()
        await bot.send_message(purchase.user_id, f'Покупка #{pid} отклонена. Вам выдано предупреждение ({buyer.warnings}/3).')
        if buyer.warnings >= 3:
            buyer.banned = True
            session.commit()
            await bot.send_message(purchase.user_id, 'Вы получили 3 предупреждения и заблокированы в боте.')
        await query.answer('Отклонено')
    session.close()

# Cart simple handlers (open cart, add, remove, checkout)
@dp.callback_query_handler(lambda c: c.data == 'open_cart')
async def open_cart_cb(query: types.CallbackQuery):
    session = next(db_session())
    items = session.query(CartItem).filter_by(user_id=query.from_user.id).all()
    if not items:
        await query.answer('Корзина пуста', show_alert=True)
        session.close()
        return
    text = 'Корзина:\n'
    total = Decimal('0')
    ik = InlineKeyboardMarkup()
    for it in items:
        pet = session.query(Pet).get(it.pet_id)
        if pet:
            text += f'{pet.name} — {pet.price}₽\n'
            total += Decimal(str(pet.price))
            ik.add(InlineKeyboardButton(f'Убрать {pet.name}', callback_data=cart_cb.new(action='remove', pet_id=pet.id)))
    ik.add(InlineKeyboardButton('Оплатить всё', callback_data=cart_cb.new(action='checkout', pet_id=0)))
    ik.add(InlineKeyboardButton('Назад', callback_data='back_to_main'))
    await query.message.answer(text + f'Итого: {total}₽', reply_markup=ik)
    await query.answer()
    session.close()

@dp.callback_query_handler(cart_cb.filter(action='remove'))
async def cart_remove_cb(query: types.CallbackQuery, callback_data: dict):
    pet_id = int(callback_data['pet_id'])
    session = next(db_session())
    item = session.query(CartItem).filter_by(user_id=query.from_user.id, pet_id=pet_id).first()
    if item:
        session.delete(item)
        session.commit()
    await query.answer('Удалено')
    await query.message.delete()
    await open_cart_cb(query)
    session.close()

@dp.callback_query_handler(cart_cb.filter(action='checkout'))
async def cart_checkout_cb(query: types.CallbackQuery, callback_data: dict):
    session = next(db_session())
    items = session.query(CartItem).filter_by(user_id=query.from_user.id).all()
    if not items:
        await query.answer('Пусто', show_alert=True)
        session.close()
        return
    total = Decimal('0')
    for it in items:
        pet = session.query(Pet).get(it.pet_id)
        if pet:
            total += Decimal(str(pet.price))
            purchase = Purchase(user_id=query.from_user.id, pet_id=pet.id, status='pending', price_paid=pet.price)
            session.add(purchase)
    session.query(CartItem).filter_by(user_id=query.from_user.id).delete()
    session.commit()
    await query.message.answer(f'Созданы покупки на сумму {total}₽. Пришлите чеки по каждой покупке (по отдельности).')
    await query.answer()
    session.close()

# Admin: add pet flow
class AddPet(StatesGroup):
    photo = State()
    name = State()
    rarity = State()
    price = State()
    desc = State()

@dp.message_handler(commands=['admin'])
async def admin_panel(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.reply('Нет доступа')
        return
    ik = InlineKeyboardMarkup()
    ik.add(InlineKeyboardButton('Добавить питомца', callback_data='admin_add_pet'))
    ik.add(InlineKeyboardButton('Разослать всем', callback_data='admin_broadcast'))
    await message.reply('Панель админа', reply_markup=ik)

@dp.callback_query_handler(lambda c: c.data == 'admin_add_pet')
async def admin_add_pet_cb(query: types.CallbackQuery):
    if query.from_user.id not in ADMIN_IDS:
        await query.answer('Нет доступа')
        return
    await query.message.answer('Отправь фото питомца')
    await AddPet.photo.set()
    await query.answer()

@dp.message_handler(content_types=types.ContentType.PHOTO, state=AddPet.photo)
async def addpet_photo(message: types.Message, state: FSMContext):
    file_id = message.photo[-1].file_id
    await state.update_data(photo=file_id)
    await message.answer('Имя?')
    await AddPet.next()

@dp.message_handler(state=AddPet.name)
async def addpet_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    # ask rarity
    ik = InlineKeyboardMarkup(row_width=3)
    for r in RARITIES:
        ik.insert(InlineKeyboardButton(r, callback_data='rar_' + r))
    await message.answer('Выбери редкость', reply_markup=ik)
    await AddPet.next()

@dp.callback_query_handler(lambda c: c.data and c.data.startswith('rar_'), state=AddPet.rarity)
async def addpet_rarity(query: types.CallbackQuery, state: FSMContext):
    r = query.data.split('_',1)[1]
    await state.update_data(rarity=r)
    await query.message.answer('Цена в рублях (число):')
    await AddPet.next()
    await query.answer()

@dp.message_handler(state=AddPet.price)
async def addpet_price(message: types.Message, state: FSMContext):
    try:
        p = Decimal(message.text)
    except Exception:
        await message.reply('Введи число')
        return
    await state.update_data(price=str(p))
    await message.answer('Описание:')
    await AddPet.next()

@dp.message_handler(state=AddPet.desc)
async def addpet_desc(message: types.Message, state: FSMContext):
    data = await state.get_data()
    session = next(db_session())
    pet = Pet(name=data['name'], rarity=data['rarity'], price=Decimal(data['price']), desc=message.text, photo_file_id=data['photo'])
    session.add(pet)
    session.commit()
    await message.answer(f'Питомец {pet.name} добавлен.')
    await state.finish()
    session.close()

# Broadcast
@dp.callback_query_handler(lambda c: c.data == 'admin_broadcast')
async def admin_broadcast_cb(query: types.CallbackQuery):
    if query.from_user.id not in ADMIN_IDS:
        await query.answer('Нет доступа')
        return
    await query.message.answer('Отправь текст рассылки')
    await dp.current_state(user=query.from_user.id).set_state('waiting_broadcast')
    await query.answer()

@dp.message_handler(state='waiting_broadcast')
async def handle_broadcast(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.reply('Нет доступа')
        await state.finish()
        return
    text = message.text
    session = next(db_session())
    users = session.query(User).all()
    count = 0
    for u in users:
        try:
            await bot.send_message(u.tg_id, text)
            count += 1
        except Exception:
            pass
    await message.reply(f'Разослано {count} пользователям')
    await state.finish()
    session.close()

# fallback back_to_main handler
@dp.callback_query_handler(lambda c: c.data == 'back_to_main')
async def back_to_main_cb(query: types.CallbackQuery):
    await query.message.answer('Меню', reply_markup=main_kb)
    await query.answer()

# generic text handler for unknowns
@dp.message_handler()
async def fallback(message: types.Message):
    await message.reply('Не понял команду. Выбери из меню.', reply_markup=main_kb)

if __name__ == '__main__':
    print('Bot starting...')
    executor.start_polling(dp, skip_updates=True)

# PROMOCODES & REFERRALS IMPLEMENTATION (SCHEMA DRAFT)

## DB TABLES

### promo_codes
- id (PK)
- code (TEXT, UNIQUE)
- discount_percent (INTEGER)
- expires_at (DATETIME)
- max_uses (INTEGER)
# - used_count (INTEGER, default 0)
- created_by_admin (INTEGER)

### user_promocodes
- id (PK)
- user_id (INTEGER)
- promo_id (INTEGER)
- is_used (BOOLEAN)
- activated_at (DATETIME)

### referral_relations
- id (PK)
- referrer_id (INTEGER)
- invited_id (INTEGER, UNIQUE)
- confirmed (BOOLEAN)
- confirmed_at (DATETIME)

## PROMOCODE LOGIC
# - Promocode can only be applied if discount_percent > user's referral discount
# - If weaker -> show error "promo lower than current discount"
# - On purchase -> mark as used and remove from active user session
# - No stacking with referrals

## REFERRAL LOGIC
# - 5 inviters -> 5%
# - 25 inviters -> 15%
# - 50 inviters -> 30%
# - always active unless promo is stronger

## DISPLAY
# - show original price strikethrough + discounted next to it
# - show active promo in profile

# (Implementation code incoming next)


# --- PROMO & REFERRAL: Дополнения (handlers + логика) ---
from datetime import datetime, timedelta
from sqlalchemy import DateTime

# Дополнительные модели (если их нет) — берём за основу уже существующей Promo
# Но добавим таблицу user_promos и referral_relations если не были созданы ранее
class UserPromo(Base):
    __tablename__ = 'user_promos'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True)
    promo_id = Column(Integer, ForeignKey('promos.id'))
    is_used = Column(Boolean, default=False)
    activated_at = Column(DateTime, default=datetime.utcnow)

class ReferralRelation(Base):
    __tablename__ = 'referral_relations'
    id = Column(Integer, primary_key=True)
    referrer_id = Column(Integer, index=True)
    invited_id = Column(Integer, unique=True, index=True)
    confirmed = Column(Boolean, default=False)
    confirmed_at = Column(DateTime, nullable=True)

# create new tables if needed
Base.metadata.create_all(bind=engine)

# helpers
def get_referral_discount(session, user: User) -> int:
    # counts confirmed referrals for this user
    cnt = session.query(ReferralRelation).filter_by(referrer_id=user.tg_id, confirmed=True).count()
    if cnt >= 50:
        return 30
    if cnt >= 25:
        return 15
    if cnt >= 5:
        return 5
    return 0

def find_active_user_promo(session, user_id: int) -> Optional[UserPromo]:
    up = session.query(UserPromo).filter_by(user_id=user_id, is_used=False).order_by(UserPromo.activated_at.desc()).first()
    return up

@dp.callback_query_handler(lambda c: c.data == 'apply_promo')
async def start_apply_promo(query: types.CallbackQuery):
    # start FSM to accept promo code
    await query.message.answer('Введи промокод (например: SAVE10) — он одноразовый и действует на следующую покупку:')
    await dp.current_state(user=query.from_user.id).set_state('waiting_promo_code')
    await query.answer()

@dp.message_handler(state='waiting_promo_code')
async def handle_promo_input(message: types.Message, state: FSMContext):
    code = message.text.strip()
    session = next(db_session())
    promo = session.query(Promo).filter_by(code=code).first()
    if not promo:
        await message.reply('❌ Промокод не найден')
        await state.finish()
        session.close()
        return
    # check expiration / uses
    now = datetime.utcnow()
    if getattr(promo, 'expires_at', None) and promo.expires_at < now:
        await message.reply('❌ Промокод истёк')
        await state.finish()
        session.close()
        return
    if getattr(promo, 'uses_left', None) is not None and promo.uses_left <= 0:
        await message.reply('❌ У промокода закончились активации')
        await state.finish()
        session.close()
        return
    # compare with referral discount
    user = session.query(User).filter_by(tg_id=message.from_user.id).first()
    ref_disc = get_referral_discount(session, user)
    if promo.discount_percent <= ref_disc:
        await message.reply('❌ Промокод не может быть применён, потому что ваша текущая скидка выше.')
        await state.finish()
        session.close()
        return
    # register user_promo (one-time)
    up = UserPromo(user_id=message.from_user.id, promo_id=promo.id, is_used=False, activated_at=now)
    session.add(up)
    # decrement global uses if limited
    if getattr(promo, 'uses_left', None) is not None:
        promo.uses_left = promo.uses_left - 1
    session.commit()
    await message.reply(f'✅ Промокод применён: -{promo.discount_percent}% (сработает на следующую покупку).')
    await state.finish()
    session.close()

# modify price after purchase creation: apply promo or referral
def apply_discounts_to_purchase(purchase_id: int):
    session = SessionLocal()
    purchase = session.query(Purchase).get(purchase_id)
    if not purchase:
        session.close()
        return
    user = session.query(User).filter_by(tg_id=purchase.user_id).first()
    pet = session.query(Pet).get(purchase.pet_id)
    base = Decimal(str(pet.price))
    # check user_promo
    up = find_active_user_promo(session, user.tg_id)
    final_price = base
    applied = None
    if up:
        promo = session.query(Promo).get(up.promo_id)
        if promo:
            # apply promo
            discount = Decimal(promo.discount_percent) / Decimal(100)
            final_price = (base * (Decimal(1) - discount)).quantize(Decimal('0.01'))
            up.is_used = True
            applied = ('promo', promo.id, promo.discount_percent)
    else:
        # apply referral discount
        ref_disc = get_referral_discount(session, user)
        if ref_disc > 0:
            discount = Decimal(ref_disc) / Decimal(100)
            final_price = (base * (Decimal(1) - discount)).quantize(Decimal('0.01'))
            applied = ('ref', None, ref_disc)
    # enforce global cap at 50%
    if applied is not None:
        maxcap = Decimal('0.5')
        if (base - final_price) / base > maxcap:
            final_price = (base * (Decimal(1) - maxcap)).quantize(Decimal('0.01'))
    purchase.price_paid = final_price
    session.commit()
    session.close()

# Ensure pet_buy_cb calls apply_discounts_to_purchase AFTER creating pending purchase
# We'll append a small note to the handler earlier: after session.commit() where purchase created, call apply_discounts_to_purchase(purchase.id)
# (If you edit code manually: insert call to apply_discounts_to_purchase immediately after purchase.commit())

# Update profile view to show active promo
# Replace earlier profile function's part where it prepared inline keyboard: add active promo display
@dp.message_handler(lambda m: m.text == 'Профиль')
async def profile_with_promo(message: types.Message):
    session = next(db_session())
    user = session.query(User).filter_by(tg_id=message.from_user.id).first()
    if not user:
        await message.reply('Профиль не найден, нажми /start')
        session.close()
        return
    up = find_active_user_promo(session, user.tg_id)
    promo_text = ''
    if up:
        promo = session.query(Promo).get(up.promo_id)
        if promo:
            promo_text = f"Активный промокод: -{promo.discount_percent}% (на следующую покупку)"
    ref_disc = get_referral_discount(session, user)
    text = f"""Профиль @{user.username or 'no_username'}
Баланс: {user.balance}₽
Совершено покупок: {user.purchases_count}
Количество рефералов: {user.referrals_count}"""

async def show_profile(message):
    text = f"Текущая скидка от рефералов: {ref_disc}% {promo_text}"

    ik = InlineKeyboardMarkup()
    ik.add(InlineKeyboardButton('Пополнить баланс', callback_data='topup'))
    ik.add(InlineKeyboardButton('Применить промокод', callback_data='apply_promo'))
    invite_link = f'https://t.me/{(await bot.get_me()).username}?start={message.from_user.id}'
    ik.add(InlineKeyboardButton('Пригласить рефералов', url=invite_link))
    ik.add(InlineKeyboardButton('Назад', callback_data='back_to_main'))

    await message.reply(text, reply_markup=main_kb)
    await message.answer('Фото профиля (пусто).')
    await message.answer('Данные ниже:', reply_markup=ik)

    session.close()


# Referral confirmation: when user finishes agreement and subscription, mark referral
@dp.callback_query_handler(lambda c: c.data == 'agreed')
async def agreed_referral_cb(query: types.CallbackQuery):
    session = next(db_session())
    user = session.query(User).filter_by(tg_id=query.from_user.id).first()
    if not user:
        await query.answer()
        session.close()
        return
    # find if this user was invited by someone
    rel = session.query(ReferralRelation).filter_by(invited_id=user.tg_id).first()
    if rel and not rel.confirmed:
        rel.confirmed = True
        rel.confirmed_at = datetime.utcnow()
        session.commit()
        # increment referrer counter (for quick access/store)
        referrer = session.query(User).filter_by(tg_id=rel.referrer_id).first()
        if referrer:
            referrer.referrals = session.query(ReferralRelation).filter_by(referrer_id=referrer.tg_id, confirmed=True).count()
            session.commit()
    # continue existing agreed flow
    user.agreed = True
    ok = await check_subscriptions(query.from_user.id)
    if not ok:
        await query.message.answer('Подпишись на канал и чат с отзывами, затем нажми /start снова.')
        await query.answer()
        session.close()
        return
    user.active = True
    session.commit()
    session.close()
    await query.message.answer('Отлично. Доступ открыт. Вот меню.', reply_markup=main_kb)
    await query.answer()

# Utility: when handling /start with referral id, register relation explicitly
@dp.message_handler(commands=['start'])
async def cmd_start_ref(message: types.Message):
    args = message.get_args()
    ref = None
    if args and args.isdigit():
        ref = int(args)
    session = next(db_session())
    user = session.query(User).filter_by(tg_id=message.from_user.id).first()
    if not user:
        user = User(tg_id=message.from_user.id, username=message.from_user.username or '')
        session.add(user)
        session.commit()
    if ref and ref != message.from_user.id:
        # create referral relation only if not exists
        existing = session.query(ReferralRelation).filter_by(invited_id=user.tg_id).first()
        if not existing:
            rel = ReferralRelation(referrer_id=ref, invited_id=user.tg_id, confirmed=False)
            session.add(rel)
            session.commit()
    # then proceed as before (show agreement)
    await message.reply('Условия пользовательского соглашения: прочитал - жми кнопку "Я прочитал". Ты обязан подписаться на канал и отзывы.')
    ik = InlineKeyboardMarkup()
    ik.add(InlineKeyboardButton('Я прочитал', callback_data='agreed'))
    await message.answer('Фото условий ниже (пустышка).')
    await message.answer('Нажми кнопку:', reply_markup=ik)
    session.close()

# Note for integration:
# - Insert call apply_discounts_to_purchase(purchase.id) immediately after creating Purchase in pet_buy_cb
# - Ensure Promo model has fields: code, discount_percent, expires_at (optional), uses_left (optional)
# - The system uses one-time user_promos; promo global usage decremented when user activates
# - UI shows promo in profile and informs user on activation

# End of promo/referral additions
