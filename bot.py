"""
Updated Telegram bot with Flask admin and referral handling.
Features in this file:
- Loads secrets from .env via python-dotenv
- Uses PAYMENT_CARD (from env) instead of PAYMENT_INFO
- /start supports referral links: /start ref_<tg_id>
- Records referred_by on new user creation
- When admin confirms a purchase, referrer receives 5% of purchase total to balance and referrals++
- Embedded minimal Flask admin mounted on port 5000 with basic password auth (ADMIN_PASSWORD env)
- All models kept in SQLAlchemy and shared between bot and Flask
- Instructions: set env variables, install requirements, run file

NOTE: This is a development-ready single-file app. For production, run bot and Flask via process manager (systemd / supervisor / docker-compose), use HTTPS for Flask, secure ADMIN_PASSWORD, and migrate to a proper WSGI server for Flask.
"""

import os
import logging
import asyncio
from datetime import datetime, timedelta
from uuid import uuid4

from flask import Flask, render_template_string, request, redirect, url_for, session, abort
from dotenv import load_dotenv

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler,
    MessageHandler, filters
)
from sqlalchemy import (
    create_engine, Column, Integer, String, Text, Float, DateTime, Boolean, ForeignKey
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

# --- Load .env ---
load_dotenv()

# --- Config ---
from dotenv import load_dotenv
import os

load_dotenv(dotenv_path="info.env")  # Явно указываем имя файла
TOKEN = os.getenv("TG_BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("Set TG_BOT_TOKEN in info.env")

DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///gag_bot.db')
OWNER_ID = int(os.getenv('OWNER_ID', '0'))
PAYMENT_CARD = os.getenv('PAYMENT_CARD', '')  # e.g. 2202200220022002
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'changeme')

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- DB setup ---
Base = declarative_base()
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if 'sqlite' in DATABASE_URL else {})
SessionLocal = sessionmaker(bind=engine)

# --- Models ---
class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    tg_id = Column(Integer, unique=True, index=True)
    username = Column(String(200))
    balance = Column(Float, default=0.0)
    referrals = Column(Integer, default=0)
    referred_by = Column(Integer, nullable=True)
    notify_new = Column(Boolean, default=True)
    role = Column(String(50), default='user')  # user, manager, moderator, owner
    created_at = Column(DateTime, default=datetime.utcnow)
    purchases = relationship('Purchase', back_populates='user')

class Pet(Base):
    __tablename__ = 'pets'
    id = Column(Integer, primary_key=True)
    name = Column(String(200))
    rarity = Column(String(50))
    description = Column(Text)
    price = Column(Float)
    image_file_id = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    active = Column(Boolean, default=True)

class CartItem(Base):
    __tablename__ = 'cart_items'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    pet_id = Column(Integer, ForeignKey('pets.id'))
    qty = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.utcnow)

class Purchase(Base):
    __tablename__ = 'purchases'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    pet_snapshot = Column(Text)  # small snapshot as string
    total = Column(Float)
    status = Column(String(50), default='pending')  # pending, awaiting_admin, confirmed, delivered, cancelled
    receipt_file_id = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    user = relationship('User', back_populates='purchases')

class Promo(Base):
    __tablename__ = 'promos'
    id = Column(Integer, primary_key=True)
    code = Column(String(100), unique=True)
    percent = Column(Integer, default=10)
    active = Column(Boolean, default=True)
    uses = Column(Integer, default=0)

Base.metadata.create_all(bind=engine)

# --- Helpers ---

def get_or_create_user(session, tg_user, ref_from_start=None):
    u = session.query(User).filter_by(tg_id=tg_user.id).first()
    if not u:
        u = User(tg_id=tg_user.id, username=tg_user.username or tg_user.full_name)
        if ref_from_start:
            try:
                ref_id = int(ref_from_start)
                u.referred_by = ref_id
            except:
                pass
        session.add(u)
        session.commit()
    return u

async def build_main_menu():
    kb = [
        [InlineKeyboardButton('Купить питомца 🛒', callback_data='buy'), InlineKeyboardButton('Канал 📢', url='https://t.me/your_channel'), InlineKeyboardButton('Отзывы ⭐', url='https://t.me/your_reviews')],
        [InlineKeyboardButton('Поддержка 👨‍💻', callback_data='support'), InlineKeyboardButton('Профиль 👤', callback_data='profile'), InlineKeyboardButton('Топ 🏆', callback_data='top')]
    ]
    return InlineKeyboardMarkup(kb)

async def pets_keyboard(pets, page=0, per_page=8):
    kb = []
    for i in range(0, min(len(pets), per_page)):
        p = pets[i]
        btn = InlineKeyboardButton(f"{p.name} — {int(p.price)}₽", callback_data=f'pet:{p.id}')
        if i % 2 == 0:
            kb.append([btn])
        else:
            kb[-1].append(btn)
    kb.append([InlineKeyboardButton('⬅️', callback_data=f'page:{page-1}'), InlineKeyboardButton('➡️', callback_data=f'page:{page+1}')])
    kb.append([InlineKeyboardButton('Корзина 🧺', callback_data='cart'), InlineKeyboardButton('Назад ↩️', callback_data='back_to_main')])
    return InlineKeyboardMarkup(kb)

# --- Telegram bot handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = SessionLocal()
    tg_user = update.effective_user
    # handle referral arg: /start ref_<id>
    ref_id = None
    if context.args:
        arg = context.args[0]
        if arg.startswith('ref_'):
            ref_id = arg.split('ref_')[1]
    get_or_create_user(session, tg_user, ref_from_start=ref_id)
    menu = await build_main_menu()
    text = f"Привет, {tg_user.first_name}!" "Добро пожаловать в магазин питомцев Grow a Garden 🌱🐾"
    await update.message.reply_text(text, reply_markup=menu)
    session.close()

async def main_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    session = SessionLocal()
    if data == 'buy':
        rarities = ['Common','Uncommon','Rare','Legendary','Mythic','Divine','Prismatic']
        kb = [[InlineKeyboardButton(r, callback_data=f'rar:{r.lower()}')] for r in rarities]
        kb.append([InlineKeyboardButton('Назад ↩️', callback_data='back_to_main')])
        await query.edit_message_text('Выбери редкость:', reply_markup=InlineKeyboardMarkup(kb))
    elif data.startswith('rar:'):
        rarity = data.split(':',1)[1]
        pets = session.query(Pet).filter_by(rarity=rarity, active=True).all()
        if not pets:
            await query.edit_message_text('Питомцев этой редкости пока нет. Назад?', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Назад', callback_data='buy')]]))
        else:
            kb = await pets_keyboard(pets)
            await query.edit_message_text(f'Питомцы {rarity}:', reply_markup=kb)
    elif data.startswith('pet:'):
        pet_id = int(data.split(':',1)[1])
        pet = session.query(Pet).filter_by(id=pet_id).first()
        if not pet:
            await query.edit_message_text('Питомец не найден.')
        else:
            txt = f"""*{pet.name}*
{pet.description}
Цена: {pet.price}₽"""
            kb = InlineKeyboardMarkup([[InlineKeyboardButton('Купить 🛒', callback_data=f'buy_pet:{pet.id}'), InlineKeyboardButton('Назад', callback_data='buy')]])
            if pet.image_file_id:
                await query.message.reply_photo(photo=pet.image_file_id, caption=txt, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
                await query.message.delete()
            else:
                await query.edit_message_text(txt, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    elif data.startswith('buy_pet:'):
        pet_id = int(data.split(':',1)[1])
        pet = session.query(Pet).filter_by(id=pet_id).first()
        user = get_or_create_user(session, update.effective_user)
        ci = CartItem(user_id=user.id, pet_id=pet.id)
        session.add(ci)
        session.commit()
        await query.edit_message_text(f'Питомец {pet.name} добавлен в корзину. Перейти в корзину?', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Перейти в корзину', callback_data='cart'), InlineKeyboardButton('Продолжить покупки', callback_data='buy')]]))
    elif data == 'cart':
        user = get_or_create_user(session, update.effective_user)
        items = session.query(CartItem).filter_by(user_id=user.id).all()
        if not items:
            await query.edit_message_text('Корзина пуста.', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Купить питомца', callback_data='buy'), InlineKeyboardButton('Назад', callback_data='back_to_main')]]))
        else:
            lines = []
            total = 0.0
            for it in items:
                pet = session.query(Pet).filter_by(id=it.pet_id).first()
                if pet:
                    lines.append(f"{pet.name} — {pet.price}₽")
                    total += pet.price
            lines.append(f"""
Итого: {total}₽""")
            kb = InlineKeyboardMarkup([[InlineKeyboardButton('Оплатить 💳', callback_data='checkout'), InlineKeyboardButton('Назад', callback_data='buy')]])
            await query.edit_message_text('\n'
    .join(lines), reply_markup=k)
    elif data == 'checkout':
        user = get_or_create_user(session, update.effective_user)
        items = session.query(CartItem).filter_by(user_id=user.id).all()
        total = 0.0
        snapshot = []
        for it in items:
            pet = session.query(Pet).filter_by(id=it.pet_id).first()
            if pet:
                snapshot.append({'id':pet.id,'name':pet.name,'price':pet.price})
                total += pet.price
        purchase = Purchase(user_id=user.id, pet_snapshot=str(snapshot), total=total)
        session.add(purchase)
        session.commit()
        context.job_queue.run_once(payment_timeout, when=600, data={'purchase_id':purchase.id, 'chat_id':update.effective_chat.id, 'user_id':user.tg_id})
        card_display = ' '.join([PAYMENT_CARD[i:i+4] for i in range(0, min(16,len(PAYMENT_CARD)),4)]) if PAYMENT_CARD else '<установите PAYMENT_CARD в .env>'
        text = f"Оплатите {total}₽ на карту в течение 10 минут:\n💳 Карта: {card_display}\nПосле оплаты прикрепите скриншот или PDF чека."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton('Я оплатил ✅', callback_data=f'paid:{purchase.id}'), InlineKeyboardButton('Назад', callback_data='cart')]])
        await query.edit_message_text(text, reply_markup=kb)
    elif data.startswith('paid:'):
        purchase_id = int(data.split(':',1)[1])
        await query.edit_message_text('Пришлите, пожалуйста, скрин/пдф чека в ответ на это сообщение.', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Назад', callback_data='cart')]]))
        context.user_data['awaiting_receipt_for'] = purchase_id
    elif data == 'back_to_main':
        menu = await build_main_menu()
        await query.edit_message_text('Главное меню', reply_markup=menu)
    elif data == 'support':
        await query.edit_message_text('Связь с поддержкой: @your_support')
    elif data == 'profile':
        user = get_or_create_user(session, update.effective_user)
        txt = f"""Профиль: @{user.username}
Рефералы: {user.referrals}
Роль: {user.role}
Баланс: {int(user.balance)}₽"""
        kb = InlineKeyboardMarkup([[InlineKeyboardButton('История покупок', callback_data='history'), InlineKeyboardButton('Промокоды', callback_data='promos')],[InlineKeyboardButton('Назад', callback_data='back_to_main')]])
        await query.edit_message_text(txt, reply_markup=kb)
    elif data == 'history':
        user = get_or_create_user(session, update.effective_user)
        buys = session.query(Purchase).filter_by(user_id=user.id).order_by(Purchase.created_at.desc()).limit(10).all()
        lines = []
        for b in buys:
            lines.append(f"{b.id} — {b.total}₽ — {b.status} — {b.created_at.date()}")
        await query.edit_message_text('\n'.join(lines) if lines else 'Покупок нет.', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Назад', callback_data='profile')]]))
    elif data == 'promos':
        await query.edit_message_text('Введи промокод в чат (текстом).', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Назад', callback_data='profile')]]))
        context.user_data['awaiting_promo'] = True
    elif data == 'top':
        users = session.query(User).order_by(User.balance.desc()).limit(10).all()
        lines = [f"@{u.username} — {int(u.balance)}₽" for u in users]
        await query.edit_message_text('\n'.join(lines) if lines else 'Пока нет данных.', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Назад', callback_data='back_to_main')]]))
    session.close()

async def payment_timeout(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    data = job.data
    session = SessionLocal()
    purchase = session.query(Purchase).filter_by(id=data['purchase_id']).first()
    if purchase and purchase.status == 'pending':
        purchase.status = 'cancelled'
        session.commit()
        try:
            await context.bot.send_message(chat_id=data['chat_id'], text=f'Платёж {purchase.id} не был завершён — заказ отменён.')
        except Exception as e:
            logger.exception('Failed to notify about timeout')
    session.close()

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = SessionLocal()
    if 'awaiting_receipt_for' in context.user_data:
        pid = context.user_data.pop('awaiting_receipt_for')
        purchase = session.query(Purchase).filter_by(id=pid).first()
        if not purchase:
            await update.message.reply_text('Заказ не найден.')
            session.close()
            return
        file_id = update.message.photo[-1].file_id if update.message.photo else (update.message.document.file_id if update.message.document else None)
        purchase.receipt_file_id = file_id
        purchase.status = 'awaiting_admin'
        session.commit()
        await update.message.reply_text('Чек отправлен администратору — ожидайте подтверждения.')
        managers = session.query(User).filter(User.role.in_(['manager','owner','moderator'])).all()
        for m in managers:
            try:
                await context.bot.send_message(chat_id=m.tg_id, text=f'Новый чек: purchase_id={purchase.id} от @{update.effective_user.username} (use /confirm {purchase.id} to confirm)')
                if file_id:
                    await context.bot.send_photo(chat_id=m.tg_id, photo=file_id)
            except Exception as e:
                logger.exception('notify manager failed')
    else:
        await update.message.reply_text('Я не жду файл. Если это чек — сначала нажмите "Я оплатил" в боте.')
    session.close()

# --- Admin commands in bot ---
async def admin_add_pet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = SessionLocal()
    user = session.query(User).filter_by(tg_id=update.effective_user.id).first()
    if not user or user.role not in ('owner','manager','moderator'):
        await update.message.reply_text('Нет доступа')
        session.close()
        return
    text = update.message.text[len('/addpet'):].strip()
    parts = [p.strip() for p in text.split('|')]
    if len(parts) < 4:
        await update.message.reply_text('Использование: /addpet имя|редкость|цена|описание')
        session.close()
        return
    name, rarity, price, desc = parts[0], parts[1].lower(), float(parts[2]), parts[3]
    pet = Pet(name=name, rarity=rarity, description=desc, price=price)
    session.add(pet)
    session.commit()
    await update.message.reply_text(f'Питомец добавлен с id {pet.id}. Пришлите фото как ответ на эту команду с /setpetphoto {pet.id}')
    session.close()

async def admin_set_pet_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = SessionLocal()
    user = session.query(User).filter_by(tg_id=update.effective_user.id).first()
    if not user or user.role not in ('owner','manager','moderator'):
        await update.message.reply_text('Нет доступа')
        session.close()
        return
    parts = update.message.text.split()
    if len(parts) < 2:
        await update.message.reply_text('Использование: /setpetphoto <id> (ответьте фото на это сообщение)')
        session.close()
        return
    pet_id = int(parts[1])
    pet = session.query(Pet).filter_by(id=pet_id).first()
    if not pet:
        await update.message.reply_text('Питомец не найден')
        session.close()
        return
    if not update.message.reply_to_message or not update.message.reply_to_message.photo:
        await update.message.reply_text('Ответьте фото на эту команду')
        session.close()
        return
    file_id = update.message.reply_to_message.photo[-1].file_id
    pet.image_file_id = file_id
    session.commit()
    await update.message.reply_text('Фото сохранено')
    session.close()

async def admin_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = SessionLocal()
    user = session.query(User).filter_by(tg_id=update.effective_user.id).first()
    if not user or user.role not in ('owner','manager','moderator'):
        await update.message.reply_text('Нет доступа')
        session.close()
        return
    parts = update.message.text.split()
    if len(parts) < 2:
        await update.message.reply_text('Использование: /confirm <purchase_id>')
        session.close()
        return
    pid = int(parts[1])
    purchase = session.query(Purchase).filter_by(id=pid).first()
    if not purchase:
        await update.message.reply_text('Заказ не найден')
        session.close()
        return
    purchase.status = 'confirmed'
    session.commit()
    # reward referrer if exists
    try:
        buyer = purchase.user
        if buyer and buyer.referred_by:
            ref = session.query(User).filter_by(tg_id=buyer.referred_by).first()
            if ref:
                bonus = round(purchase.total * 0.05, 2)  # 5% reward
                ref.balance += bonus
                ref.referrals += 1
                session.commit()
                await context.bot.send_message(chat_id=ref.tg_id, text=f'Вы получили {bonus}₽ за приглашение друга (@{buyer.username}).')
    except Exception:
        logger.exception('ref reward failed')
    try:
        await context.bot.send_message(chat_id=purchase.user.tg_id, text=f'Админ подтвердил оплату заказа {purchase.id}. Питомец(ы) скоро будут переданы.')
    except:
        pass
    await update.message.reply_text('Подтверждено')
    session.close()

# --- Text handler ---
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = SessionLocal()
    user = get_or_create_user(session, update.effective_user)
    txt = update.message.text.strip()
    if context.user_data.get('awaiting_promo'):
        code = txt.upper()
        promo = session.query(Promo).filter_by(code=code, active=True).first()
        if not promo:
            await update.message.reply_text('Промокод не найден или неактивен')
        else:
            promo.uses += 1
            session.commit()
            await update.message.reply_text(f'Промокод принят — скидка {promo.percent}% будет применена к следующей покупке')
            context.user_data['applied_promo'] = promo.id
        context.user_data.pop('awaiting_promo', None)
        session.close()
        return
    if txt.startswith('/search'):
        q = txt[len('/search'):].strip().lower()
        pets = session.query(Pet).filter(Pet.name.ilike(f'%{q}%'), Pet.active==True).all()
        if not pets:
            await update.message.reply_text('Не найдено')
        else:
            kb = await pets_keyboard(pets)
            await update.message.reply_text('Результаты по поиску:', reply_markup=kb)
        session.close()
        return
    await update.message.reply_text('Не понимаю. Попробуй /start или /search <имя>')
    session.close()

# --- Flask admin ---
flask_app = Flask(__name__)
flask_app.secret_key = os.getenv('FLASK_SECRET', 'supersecret')

ADMIN_BASE_TEMPLATE = """
<!doctype html>
<title>GAG Bot Admin</title>
<h2>Админка</h2>
{% if not session.get('admin') %}
<form method=post action="{{ url_for('login') }}">
  <input type=password name=password placeholder="Password">
  <input type=submit value=Login>
</form>
{% else %}
<p><a href="{{ url_for('logout') }}">Logout</a></p>
<p><a href="{{ url_for('pets') }}">Pets</a> | <a href="{{ url_for('orders') }}">Orders</a> | <a href="{{ url_for('admins') }}">Admins</a></p>
<hr>
{% block body %}{% endblock %}
{% endif %}
"""

@flask_app.route('/admin', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASSWORD:
            session['admin'] = True
            return redirect(url_for('pets'))
        else:
            return render_template_string(ADMIN_BASE_TEMPLATE + '<p>Wrong password</p>')
    return render_template_string(ADMIN_BASE_TEMPLATE)

@flask_app.route('/admin/logout')
def logout():
    session.pop('admin', None)
    return redirect(url_for('login'))

@flask_app.route('/admin/pets')
def pets():
    if not session.get('admin'):
        return redirect(url_for('login'))
    db = SessionLocal()
    pets = db.query(Pet).order_by(Pet.created_at.desc()).all()
    body = '<h3>Pets</h3><a href="/admin/pets/add">Add pet</a><ul>'
    for p in pets:
        body += f'<li>{p.id} - {p.name} ({p.rarity}) - {p.price} - <a href="/admin/pets/edit/{p.id}">edit</a> <a href="/admin/pets/delete/{p.id}">del</a></li>'
    body += '</ul>'
    return render_template_string(ADMIN_BASE_TEMPLATE.replace('{% block body %}{% endblock %}','') + body)

@flask_app.route('/admin/pets/add', methods=['GET','POST'])
def add_pet():
    if not session.get('admin'):
        return redirect(url_for('login'))
    if request.method == 'POST':
        name = request.form.get('name')
        rarity = request.form.get('rarity')
        price = float(request.form.get('price') or 0)
        desc = request.form.get('description')
        db = SessionLocal()
        pet = Pet(name=name, rarity=rarity, price=price, description=desc)
        db.add(pet)
        db.commit()
        return redirect(url_for('pets'))
    form = '''<form method=post><input name=name placeholder="name"><input name=rarity placeholder="rarity"><input name=price placeholder="price"><br><textarea name=description placeholder="description"></textarea><br><input type=submit></form>'''
    return render_template_string(ADMIN_BASE_TEMPLATE.replace('{% block body %}{% endblock %}','') + form)

@flask_app.route('/admin/pets/edit/<int:pid>', methods=['GET','POST'])
def edit_pet(pid):
    if not session.get('admin'):
        return redirect(url_for('login'))
    db = SessionLocal()
    pet = db.query(Pet).filter_by(id=pid).first()
    if not pet:
        return 'not found'
    if request.method == 'POST':
        pet.name = request.form.get('name')
        pet.rarity = request.form.get('rarity')
        pet.price = float(request.form.get('price') or pet.price)
        pet.description = request.form.get('description')
        db.commit()
        return redirect(url_for('pets'))
    form = f'''<form method=post><input name=name value="{pet.name}"><input name=rarity value="{pet.rarity}"><input name=price value="{pet.price}"><br><textarea name=description>{pet.description}</textarea><br><input type=submit></form>'''
    return render_template_string(ADMIN_BASE_TEMPLATE.replace('{% block body %}{% endblock %}','') + form)

@flask_app.route('/admin/pets/delete/<int:pid>')
def delete_pet(pid):
    if not session.get('admin'):
        return redirect(url_for('login'))
    db = SessionLocal()
    pet = db.query(Pet).filter_by(id=pid).first()
    if pet:
        db.delete(pet)
        db.commit()
    return redirect(url_for('pets'))

@flask_app.route('/admin/orders')
def orders():
    if not session.get('admin'):
        return redirect(url_for('login'))
    db = SessionLocal()
    orders = db.query(Purchase).order_by(Purchase.created_at.desc()).all()
    body = '<h3>Orders</h3><ul>'
    for o in orders:
        body += f'<li>{o.id} - user_id:{o.user_id} - {o.total}₽ - {o.status} - <a href="/admin/orders/confirm/{o.id}">confirm</a></li>'
    body += '</ul>'
    return render_template_string(ADMIN_BASE_TEMPLATE.replace('{% block body %}{% endblock %}','') + body)

@flask_app.route('/admin/orders/confirm/<int:oid>')
def web_confirm_order(oid):
    if not session.get('admin'):
        return redirect(url_for('login'))
    db = SessionLocal()
    o = db.query(Purchase).filter_by(id=oid).first()
    if o:
        o.status = 'confirmed'
        db.commit()
        # reward referrer
        try:
            buyer = o.user
            if buyer and buyer.referred_by:
                ref = db.query(User).filter_by(tg_id=buyer.referred_by).first()
                if ref:
                    bonus = round(o.total * 0.05, 2)
                    ref.balance += bonus
                    ref.referrals += 1
                    db.commit()
        except Exception:
            logger.exception('ref reward failed (web)')
    return redirect(url_for('orders'))

@flask_app.route('/admin/admins', methods=['GET','POST'])
def admins():
    if not session.get('admin'):
        return redirect(url_for('login'))
    db = SessionLocal()
    if request.method == 'POST':
        tg_id = int(request.form.get('tg_id'))
        role = request.form.get('role')
        u = db.query(User).filter_by(tg_id=tg_id).first()
        if u:
            u.role = role
            db.commit()
    users = db.query(User).order_by(User.id.desc()).limit(200).all()
    body = '<h3>Admins</h3><form method=post>tg_id:<input name=tg_id> role:<input name=role> <input type=submit value=Set></form><ul>'
    for u in users:
        body += f'<li>{u.tg_id} - @{u.username} - {u.role}</li>'
    body += '</ul>'
    return render_template_string(ADMIN_BASE_TEMPLATE.replace('{% block body %}{% endblock %}','') + body)

# --- Boot both apps ---
async def run_bot():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler('start', start))
    app.add_handler(CallbackQueryHandler(main_callback))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.PDF, photo_handler))
    app.add_handler(CommandHandler('addpet', admin_add_pet))
    app.add_handler(CommandHandler('setpetphoto', admin_set_pet_photo))
    app.add_handler(CommandHandler('confirm', admin_confirm))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), text_handler))

    print('Telegram bot started')
    await app.run_polling()

def run_flask():
    print('Flask admin started on http://0.0.0.0:5000/admin')
    flask_app.run(host='0.0.0.0', port=int(os.getenv('FLASK_PORT', '5000')))

if __name__ == '__main__':
    # Run flask in a thread and bot in asyncio
    import threading
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    asyncio.run(run_bot())
