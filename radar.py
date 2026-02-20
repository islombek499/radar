"""
╔══════════════════════════════════════════════════════════════════╗
║              🚦 RADAR YPX BOT — Single File Edition             ║
║         Uzbekiston haydovchilari uchun Telegram bot             ║
╚══════════════════════════════════════════════════════════════════╝

Ishga tushirish:
    pip install python-telegram-bot==21.6 sqlalchemy==2.0.35 asyncpg==0.30.0 python-dotenv==1.0.1
    python radar_ypx_bot.py

.env fayl namunasi:
    BOT_TOKEN=your_token_here
    ADMIN_IDS=123456789,987654321
    DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/radar_ypx
"""

# ══════════════════════════════════════════════════════════════════
# IMPORTS
# ══════════════════════════════════════════════════════════════════

import asyncio
import hashlib
import logging
import math
import os
import random
import re
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum as PyEnum

from dotenv import load_dotenv
from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Float,
    ForeignKey, Integer, String, Enum, func, select,
)
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, relationship
from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters,
)

# ══════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════

load_dotenv()


class Config:
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    ADMIN_IDS: list[int] = [
        int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()
    ]
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://postgres:postgres@localhost:5432/radar_ypx"
    )
    NEARBY_RADIUS_KM: float = float(os.getenv("NEARBY_RADIUS_KM", "5"))
    PREMIUM_DAYS: int = int(os.getenv("PREMIUM_DAYS", "30"))
    FREE_SEARCH_LIMIT: int = int(os.getenv("FREE_SEARCH_LIMIT", "5"))
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")


config = Config()

# ══════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
# MODELS (SQLAlchemy ORM)
# ══════════════════════════════════════════════════════════════════


class Base(DeclarativeBase):
    pass


class RadarType(str, PyEnum):
    SPEED_CAMERA   = "speed_camera"
    YPX_POST       = "ypx_post"
    MOBILE_PATROL  = "mobile_patrol"


class User(Base):
    """Foydalanuvchilar jadvali."""
    __tablename__ = "users"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id   = Column(BigInteger, unique=True, nullable=False, index=True)
    username      = Column(String(255), nullable=True)
    full_name     = Column(String(255), nullable=True)
    premium_until = Column(DateTime, nullable=True)
    created_at    = Column(DateTime, default=func.now(), nullable=False)

    reported_radars = relationship("Radar", back_populates="reporter")

    @property
    def is_premium(self) -> bool:
        if self.premium_until is None:
            return False
        return self.premium_until > datetime.utcnow()

    def __repr__(self) -> str:
        return f"<User tg={self.telegram_id} premium={self.is_premium}>"


class Radar(Base):
    """Radarlar va nazorat nuqtalari jadvali."""
    __tablename__ = "radars"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    lat         = Column(Float, nullable=False)
    lon         = Column(Float, nullable=False)
    type        = Column(Enum(RadarType), nullable=False, default=RadarType.SPEED_CAMERA)
    description = Column(String(500), nullable=True)
    is_active   = Column(Boolean, default=True, nullable=False)
    created_by  = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at  = Column(DateTime, default=func.now(), nullable=False)

    reporter = relationship("User", back_populates="reported_radars")

    def __repr__(self) -> str:
        return f"<Radar id={self.id} type={self.type}>"


class YPXPost(Base):
    """YPX postlari jadvali."""
    __tablename__ = "ypx_posts"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    lat          = Column(Float, nullable=False)
    lon          = Column(Float, nullable=False)
    name         = Column(String(255), nullable=True)
    active_until = Column(DateTime, nullable=True)   # None = doimiy
    is_active    = Column(Boolean, default=True, nullable=False)
    created_at   = Column(DateTime, default=func.now(), nullable=False)

    @property
    def currently_active(self) -> bool:
        if not self.is_active:
            return False
        if self.active_until is None:
            return True
        return self.active_until > datetime.utcnow()

    def __repr__(self) -> str:
        return f"<YPXPost id={self.id} name={self.name}>"


# ══════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════

engine = create_async_engine(
    config.DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def init_db() -> None:
    """Barcha jadvallarni avtomatik yaratadi."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("✅ Database jadvallar tayyor.")


# ══════════════════════════════════════════════════════════════════
# GEO UTILITY — Haversine formulasi
# ══════════════════════════════════════════════════════════════════

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Ikki nuqta orasidagi masofani km da hisoblaydi (Haversine)."""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi       = math.radians(lat2 - lat1)
    dlambda    = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def format_distance(km: float) -> str:
    """450 m yoki 1.2 km ko'rinishida masofa."""
    if km < 1.0:
        return f"{int(km * 1000)} m"
    return f"{km:.1f} km"


# ══════════════════════════════════════════════════════════════════
# SERVICES — User
# ══════════════════════════════════════════════════════════════════

async def get_or_create_user(
    session: AsyncSession,
    telegram_id: int,
    username: str | None = None,
    full_name: str | None = None,
) -> User:
    """Foydalanuvchini topadi yoki yangi yaratadi."""
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()

    if user is None:
        user = User(telegram_id=telegram_id, username=username, full_name=full_name)
        session.add(user)
        await session.commit()
        await session.refresh(user)
    else:
        changed = False
        if username and user.username != username:
            user.username = username
            changed = True
        if full_name and user.full_name != full_name:
            user.full_name = full_name
            changed = True
        if changed:
            await session.commit()

    return user


async def activate_premium(session: AsyncSession, user: User) -> User:
    """Premium muddatini PREMIUM_DAYS kunga uzaytiradi."""
    now  = datetime.utcnow()
    base = user.premium_until if (user.premium_until and user.premium_until > now) else now
    user.premium_until = base + timedelta(days=config.PREMIUM_DAYS)
    await session.commit()
    await session.refresh(user)
    return user


# ══════════════════════════════════════════════════════════════════
# SERVICES — Radar
# ══════════════════════════════════════════════════════════════════

RADAR_TYPE_LABELS: dict[RadarType, str] = {
    RadarType.SPEED_CAMERA:  "🚦 Tezlik radari",
    RadarType.YPX_POST:      "🚓 YPX posti",
    RadarType.MOBILE_PATROL: "🚔 Mobil patrul",
}


async def find_nearby_radars(
    session: AsyncSession,
    lat: float,
    lon: float,
    radius_km: float | None = None,
) -> list[tuple[Radar, float]]:
    """Berilgan nuqtaga yaqin (radius_km ichidagi) radarlarni qaytaradi."""
    if radius_km is None:
        radius_km = config.NEARBY_RADIUS_KM

    result = await session.execute(select(Radar).where(Radar.is_active == True))
    radars = result.scalars().all()

    nearby: list[tuple[Radar, float]] = []
    for radar in radars:
        dist = haversine_km(lat, lon, radar.lat, radar.lon)
        if dist <= radius_km:
            nearby.append((radar, dist))

    nearby.sort(key=lambda x: x[1])
    return nearby


async def report_radar(
    session: AsyncSession,
    lat: float,
    lon: float,
    radar_type: RadarType,
    user: User,
    description: str | None = None,
) -> Radar:
    """Foydalanuvchi tomonidan yangi radar xabarini saqlaydi."""
    radar = Radar(lat=lat, lon=lon, type=radar_type, description=description, created_by=user.id)
    session.add(radar)
    await session.commit()
    await session.refresh(radar)
    return radar


# ══════════════════════════════════════════════════════════════════
# SERVICES — YPX Posts
# ══════════════════════════════════════════════════════════════════

async def get_active_posts(session: AsyncSession) -> list[YPXPost]:
    """Faol YPX postlarini qaytaradi."""
    result = await session.execute(select(YPXPost).where(YPXPost.is_active == True))
    posts  = result.scalars().all()
    return [p for p in posts if p.currently_active]


async def get_posts_with_distance(
    session: AsyncSession, lat: float, lon: float
) -> list[tuple[YPXPost, float]]:
    """YPX postlarini masofaga qarab saralaydi."""
    posts = await get_active_posts(session)
    result = [(p, haversine_km(lat, lon, p.lat, p.lon)) for p in posts]
    result.sort(key=lambda x: x[1])
    return result


async def add_ypx_post(
    session: AsyncSession,
    lat: float,
    lon: float,
    name: str | None = None,
    active_until: datetime | None = None,
) -> YPXPost:
    """Admin: yangi YPX posti qo'shadi."""
    post = YPXPost(lat=lat, lon=lon, name=name, active_until=active_until)
    session.add(post)
    await session.commit()
    await session.refresh(post)
    return post


async def deactivate_ypx_post(session: AsyncSession, post_id: int) -> bool:
    """Admin: YPX postini o'chiradi. True → topildi va o'chirildi."""
    result = await session.execute(select(YPXPost).where(YPXPost.id == post_id))
    post   = result.scalar_one_or_none()
    if post is None:
        return False
    post.is_active = False
    await session.commit()
    return True


# ══════════════════════════════════════════════════════════════════
# SERVICES — Fine Check (Mock)
# ══════════════════════════════════════════════════════════════════

@dataclass
class FineResult:
    plate:           str
    fine_count:      int
    total_amount_uzs: int
    has_fines:       bool
    message:         str


async def check_fines(plate: str) -> FineResult:
    """
    Jarima tekshiradi.
    Hozircha MOCK — real API tayyor bo'lganda _call_real_api() ni to'ldiring.
    """
    plate_clean = plate.upper().replace(" ", "").replace("-", "")
    return await _mock_api_call(plate_clean)


async def _mock_api_call(plate: str) -> FineResult:
    """
    Bir xil davlat raqami → har doim bir xil natija (MD5 seed asosida).
    Demo uchun qulay.
    """
    seed = int(hashlib.md5(plate.encode()).hexdigest(), 16) % 10000
    rng  = random.Random(seed)

    fine_count       = rng.randint(0, 4)
    amount_per_fine  = rng.choice([74_000, 148_000, 222_000, 370_000, 740_000])
    total            = fine_count * amount_per_fine

    if fine_count == 0:
        msg = "✅ Jarimalar topilmadi."
    else:
        msg = (
            f"⚠️ {fine_count} ta jarima topildi.\n"
            f"Jami: {total:,} so'm\n\n"
            "Iltimos, traffic.uz orqali to'lang yoki eng yaqin yo'l xavfsizligi bo'limiga boring."
        )

    return FineResult(
        plate=plate,
        fine_count=fine_count,
        total_amount_uzs=total,
        has_fines=fine_count > 0,
        message=msg,
    )


# TODO: Real API integratsiyasi
# async def _call_real_api(plate: str) -> dict:
#     async with aiohttp.ClientSession() as s:
#         r = await s.get("https://api.traffic.uz/fines",
#                         params={"plate": plate, "key": config.FINE_API_KEY})
#         return await r.json()


# ══════════════════════════════════════════════════════════════════
# KEYBOARDS
# ══════════════════════════════════════════════════════════════════

def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Asosiy menyu tugmalari."""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📍 Yaqin radarlar"), KeyboardButton("🚓 YPX postlari")],
            [KeyboardButton("➕ Radar xabar berish"), KeyboardButton("🧾 Jarima tekshirish")],
            [KeyboardButton("⭐ Premium")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def request_location_keyboard(text: str = "📍 Joylashuvni ulashish") -> ReplyKeyboardMarkup:
    """Joylashuv so'rash tugmasi."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton(text, request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def radar_type_keyboard() -> InlineKeyboardMarkup:
    """Radar turini tanlash."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚦 Tezlik radari",  callback_data="rtype:speed_camera")],
        [InlineKeyboardButton("🚓 YPX posti",      callback_data="rtype:ypx_post")],
        [InlineKeyboardButton("🚔 Mobil patrul",   callback_data="rtype:mobile_patrol")],
        [InlineKeyboardButton("❌ Bekor qilish",   callback_data="rtype:cancel")],
    ])


def premium_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Premium sotib olish — 30 kun", callback_data="premium:buy")],
        [InlineKeyboardButton("ℹ️ Nima kiradi?",                 callback_data="premium:info")],
    ])


def admin_ypx_keyboard(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Postni o'chirish", callback_data=f"admin_ypx:deactivate:{post_id}")]
    ])


# ══════════════════════════════════════════════════════════════════
# HANDLERS — Start / Help
# ══════════════════════════════════════════════════════════════════

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — foydalanuvchini ro'yxatdan o'tkazadi va menyuni ko'rsatadi."""
    tg = update.effective_user
    async with AsyncSessionLocal() as session:
        user = await get_or_create_user(session, tg.id, tg.username, tg.full_name)

    status = "⭐ Premium" if user.is_premium else "Bepul"
    await update.message.reply_text(
        f"👋 <b>Radar YPX Bot</b>ga xush kelibsiz, {tg.first_name}!\n\n"
        f"Yo'lda xavfsiz haydang. Hisob holati: <b>{status}</b>\n\n"
        "Quyidagi tugmalardan birini tanlang:",
        reply_markup=main_menu_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — yordam xabari."""
    await update.message.reply_text(
        "ℹ️ <b>Radar YPX Bot — Yordam</b>\n\n"
        "📍 <b>Yaqin radarlar</b> — Joylashuvingizni ulashing, 5 km ichidagi kameralar ko'rinadi.\n"
        "➕ <b>Radar xabar berish</b> — Yangi radar ko'rdingizmi? Xabar bering!\n"
        "🚓 <b>YPX postlari</b> — Faol YPX postlari ro'yxati.\n"
        "🧾 <b>Jarima tekshirish</b> — Davlat raqamingizni kiriting.\n"
        "⭐ <b>Premium</b> — Cheksiz qidiruv va ustuvor ogohlantirishlar.\n\n"
        "/start — bosh menyuga qaytish.",
        parse_mode=ParseMode.HTML,
    )


# ══════════════════════════════════════════════════════════════════
# HANDLERS — Nearby Radars
# ══════════════════════════════════════════════════════════════════

async def nearby_radars_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Joylashuvni so'raydi."""
    context.user_data["location_mode"] = "nearby"
    await update.message.reply_text(
        f"📍 <b>Yaqin radarlar</b>\n\n"
        f"Joylashuvingizni ulashing — {config.NEARBY_RADIUS_KM:.0f} km ichidagi"
        f" radarlarni topaman.",
        reply_markup=request_location_keyboard("📍 Joylashuvni ulashish"),
        parse_mode=ParseMode.HTML,
    )


async def nearby_radars_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Joylashuv keldi → radarlar ro'yxatini qaytaradi."""
    loc = update.message.location
    lat, lon = loc.latitude, loc.longitude

    async with AsyncSessionLocal() as session:
        results = await find_nearby_radars(session, lat, lon)

    if not results:
        await update.message.reply_text(
            f"✅ {config.NEARBY_RADIUS_KM:.0f} km ichida radar topilmadi. Xavfsiz haydang!",
            reply_markup=main_menu_keyboard(),
        )
        return

    lines = ["🗺 <b>Sizga yaqin radarlar:</b>\n"]
    for radar, dist_km in results:
        label = RADAR_TYPE_LABELS.get(radar.type, "📡 Noma'lum")
        lines.append(f"📍 {format_distance(dist_km)} — {label}")

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=main_menu_keyboard(),
        parse_mode=ParseMode.HTML,
    )


# ══════════════════════════════════════════════════════════════════
# HANDLERS — Report Radar (ConversationHandler)
# ══════════════════════════════════════════════════════════════════

REPORT_WAITING_LOCATION = 1
REPORT_WAITING_TYPE     = 2
_PENDING_LOC            = "pending_radar_location"


async def report_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """1-qadam: joylashuvni so'raydi."""
    await update.message.reply_text(
        "➕ <b>Radar xabar berish</b>\n\nRadar turgan joyning joylashuvini ulashing:",
        reply_markup=request_location_keyboard("📍 Radar joylashuvini ulashish"),
        parse_mode=ParseMode.HTML,
    )
    return REPORT_WAITING_LOCATION


async def report_got_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """2-qadam: joylashuv saqlandi, tur tanlash."""
    loc = update.message.location
    context.user_data[_PENDING_LOC] = (loc.latitude, loc.longitude)
    await update.message.reply_text(
        "📌 Joylashuv saqlandi. Radar turini tanlang:",
        reply_markup=radar_type_keyboard(),
    )
    return REPORT_WAITING_TYPE


async def report_got_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """3-qadam: DB ga saqlaydi."""
    query = update.callback_query
    await query.answer()

    _, value = query.data.split(":", 1)

    if value == "cancel":
        context.user_data.pop(_PENDING_LOC, None)
        await query.edit_message_text("❌ Bekor qilindi.")
        await query.message.reply_text("Menyu:", reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    try:
        radar_type = RadarType(value)
    except ValueError:
        await query.edit_message_text("❌ Noma'lum tur. Qaytadan urinib ko'ring.")
        return ConversationHandler.END

    loc = context.user_data.pop(_PENDING_LOC, None)
    if loc is None:
        await query.edit_message_text("⚠️ Joylashuv yo'qoldi. Qaytadan boshlang.")
        return ConversationHandler.END

    lat, lon   = loc
    tg         = update.effective_user

    async with AsyncSessionLocal() as session:
        user  = await get_or_create_user(session, tg.id, tg.username, tg.full_name)
        radar = await report_radar(session, lat, lon, radar_type, user)

    label = RADAR_TYPE_LABELS[radar_type]
    await query.edit_message_text(
        f"✅ <b>Rahmat!</b> {label} xabar berildi:\n"
        f"🌐 Lat: {lat:.5f}, Lon: {lon:.5f}\n\n"
        "Xabaringiz boshqa haydovchilarga yordam beradi! 🙏",
        parse_mode=ParseMode.HTML,
    )
    await query.message.reply_text("Menyu:", reply_markup=main_menu_keyboard())
    return ConversationHandler.END


async def report_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop(_PENDING_LOC, None)
    await update.message.reply_text("❌ Bekor qilindi.", reply_markup=main_menu_keyboard())
    return ConversationHandler.END


def build_report_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[MessageHandler(filters.Text(["➕ Radar xabar berish"]), report_start)],
        states={
            REPORT_WAITING_LOCATION: [MessageHandler(filters.LOCATION, report_got_location)],
            REPORT_WAITING_TYPE:     [CallbackQueryHandler(report_got_type, pattern=r"^rtype:")],
        },
        fallbacks=[MessageHandler(filters.Text(["/cancel"]), report_cancel)],
        per_user=True,
        per_chat=True,
    )


# ══════════════════════════════════════════════════════════════════
# HANDLERS — YPX Posts
# ══════════════════════════════════════════════════════════════════

async def ypx_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Joylashuv so'raydi."""
    context.user_data["location_mode"] = "ypx"
    await update.message.reply_text(
        "🚓 <b>YPX postlari</b>\n\nMasofaga qarab saralash uchun joylashuvingizni ulashing:",
        reply_markup=request_location_keyboard("📍 Joylashuvni ulashish"),
        parse_mode=ParseMode.HTML,
    )


async def ypx_with_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Joylashuv keldi → YPX postlar ro'yxati."""
    loc      = update.message.location
    lat, lon = loc.latitude, loc.longitude
    is_admin = update.effective_user.id in config.ADMIN_IDS

    async with AsyncSessionLocal() as session:
        posts = await get_posts_with_distance(session, lat, lon)

    if not posts:
        await update.message.reply_text(
            "✅ Hozirda faol YPX postlar mavjud emas.",
            reply_markup=main_menu_keyboard(),
        )
        return

    lines = ["🚓 <b>Sizga yaqin YPX postlari:</b>\n"]
    for i, (post, dist_km) in enumerate(posts, 1):
        name = post.name or f"Post #{post.id}"
        lines.append(f"{i}. 📍 {format_distance(dist_km)} — {name}")

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=main_menu_keyboard(),
        parse_mode=ParseMode.HTML,
    )

    # Adminlarga boshqaruv tugmalari
    if is_admin:
        for post, _ in posts:
            name = post.name or f"Post #{post.id}"
            await update.message.reply_text(
                f"🔧 Admin: {name}",
                reply_markup=admin_ypx_keyboard(post.id),
            )


async def admin_add_ypx(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Admin buyrug'i: /addypx <lat> <lon> [nom]
    Misol: /addypx 41.2995 69.2401 Chilonzor posti
    """
    if update.effective_user.id not in config.ADMIN_IDS:
        await update.message.reply_text("⛔ Faqat adminlar uchun.")
        return

    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text(
            "Foydalanish: /addypx <lat> <lon> [nom]\n"
            "Misol: /addypx 41.2995 69.2401 Chilonzor"
        )
        return

    try:
        lat = float(args[0])
        lon = float(args[1])
    except ValueError:
        await update.message.reply_text("❌ Noto'g'ri koordinatlar.")
        return

    name = " ".join(args[2:]) if len(args) > 2 else None

    async with AsyncSessionLocal() as session:
        post = await add_ypx_post(session, lat, lon, name=name)

    await update.message.reply_text(
        f"✅ YPX posti qo'shildi (id={post.id}):\n"
        f"📍 {lat}, {lon} — {name or 'Nomsiz'}",
        reply_markup=main_menu_keyboard(),
    )


async def admin_deactivate_ypx_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback: admin_ypx:deactivate:<post_id>"""
    query = update.callback_query
    await query.answer()

    if update.effective_user.id not in config.ADMIN_IDS:
        await query.answer("⛔ Faqat adminlar uchun.", show_alert=True)
        return

    _, _action, post_id_str = query.data.split(":")
    post_id = int(post_id_str)

    async with AsyncSessionLocal() as session:
        found = await deactivate_ypx_post(session, post_id)

    if found:
        await query.edit_message_text(f"✅ YPX post #{post_id} o'chirildi.")
    else:
        await query.edit_message_text(f"❌ Post #{post_id} topilmadi.")


# ══════════════════════════════════════════════════════════════════
# HANDLERS — Fine Check (ConversationHandler)
# ══════════════════════════════════════════════════════════════════

FINE_WAITING_PLATE  = 10
_PLATE_PATTERN      = re.compile(r"^[A-Z0-9]{4,10}$", re.IGNORECASE)


async def fine_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Davlat raqamini so'raydi."""
    await update.message.reply_text(
        "🧾 <b>Jarima tekshirish</b>\n\nDavlat raqamingizni kiriting:\n"
        "Misol: <code>01A123BC</code> yoki <code>30B999AA</code>",
        parse_mode=ParseMode.HTML,
    )
    return FINE_WAITING_PLATE


async def fine_got_plate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Raqamni qabul qiladi va jarimalarni tekshiradi."""
    plate_raw = update.message.text.strip()

    if not _PLATE_PATTERN.match(plate_raw):
        await update.message.reply_text(
            "❌ Raqam formati noto'g'ri. Iltimos, to'g'ri Uzbekiston davlat raqamini kiriting."
        )
        return FINE_WAITING_PLATE

    await update.message.reply_text("🔍 Jarimalar tekshirilmoqda, kuting…")

    try:
        result = await check_fines(plate_raw)
    except Exception as exc:
        logger.error("Jarima tekshirish xatosi: %s", exc, exc_info=True)
        await update.message.reply_text(
            "⚠️ Xizmat vaqtincha mavjud emas. Keyinroq urinib ko'ring.",
            reply_markup=main_menu_keyboard(),
        )
        return ConversationHandler.END

    summary = (
        f"🚗 Davlat raqami: <b>{result.plate}</b>\n"
        f"📋 Jarimalar soni: <b>{result.fine_count}</b>\n"
        f"💰 Jami summa: <b>{result.total_amount_uzs:,} so'm</b>\n\n"
        f"{result.message}"
    )

    await update.message.reply_text(summary, reply_markup=main_menu_keyboard(), parse_mode=ParseMode.HTML)
    return ConversationHandler.END


async def fine_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Bekor qilindi.", reply_markup=main_menu_keyboard())
    return ConversationHandler.END


def build_fine_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[MessageHandler(filters.Text(["🧾 Jarima tekshirish"]), fine_start)],
        states={
            FINE_WAITING_PLATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, fine_got_plate)],
        },
        fallbacks=[MessageHandler(filters.Text(["/cancel"]), fine_cancel)],
        per_user=True,
        per_chat=True,
    )


# ══════════════════════════════════════════════════════════════════
# HANDLERS — Premium
# ══════════════════════════════════════════════════════════════════

async def premium_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Premium holati va sotib olish tugmasi."""
    tg = update.effective_user
    async with AsyncSessionLocal() as session:
        user = await get_or_create_user(session, tg.id, tg.username, tg.full_name)

    if user.is_premium:
        expiry = user.premium_until.strftime("%d.%m.%Y")
        await update.message.reply_text(
            f"⭐ Sizda allaqachon <b>Premium</b> bor — <b>{expiry}</b> gacha!\n\n"
            "Cheksiz qidiruv va ustuvor ogohlantirishlardan foydalaning.",
            parse_mode=ParseMode.HTML,
            reply_markup=premium_keyboard(),
        )
    else:
        await update.message.reply_text(
            "⭐ <b>Radar YPX Premium</b>\n\n"
            "✅ Cheksiz radar qidiruvi\n"
            "✅ Ustuvor yangi radar ogohlantirishlari\n"
            "✅ Marshrut bo'yicha skanerlash\n"
            "✅ Batafsil tarix\n\n"
            "💰 <b>30 kun — 19 900 so'm</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=premium_keyboard(),
        )


async def premium_buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """'Sotib olish' tugmasi — mock to'lov muvaffaqiyatli."""
    query = update.callback_query
    await query.answer("To'lov amalga oshirilmoqda… ✅")

    tg = update.effective_user
    async with AsyncSessionLocal() as session:
        user = await get_or_create_user(session, tg.id)
        user = await activate_premium(session, user)

    expiry = user.premium_until.strftime("%d.%m.%Y")
    await query.edit_message_text(
        f"🎉 <b>Premium faollashtirildi!</b>\n\n"
        f"Premium muddati: <b>{expiry}</b> gacha.\n"
        "Cheksiz imkoniyatlardan foydalaning! 🚀",
        parse_mode=ParseMode.HTML,
    )
    await query.message.reply_text("Menyu:", reply_markup=main_menu_keyboard())


async def premium_info_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Premium imkoniyatlari haqida ma'lumot."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "⭐ <b>Premium imkoniyatlari</b>\n\n"
        "🔓 <b>Cheksiz qidiruv</b> — Bepul foydalanuvchilar kuniga 5 ta qidiruv.\n"
        "📢 <b>Real vaqt ogohlantirishlari</b> — Marshruting bo'yicha yangi radarlar.\n"
        "🗺 <b>Marshrut skaneri</b> — Butun yo'lni kameralar uchun tekshirish.\n"
        "📊 <b>Tarix</b> — O'tgan qidiruvlar va xabarlar.\n\n"
        "Har 30 kunda bir to'lanadi.",
        parse_mode=ParseMode.HTML,
        reply_markup=premium_keyboard(),
    )


async def premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/premium buyrug'i."""
    await premium_menu(update, context)


# ══════════════════════════════════════════════════════════════════
# HANDLERS — Global Error Handler
# ══════════════════════════════════════════════════════════════════

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Barcha tutilmagan xatolarni loglaydi va foydalanuvchiga xabar beradi."""
    logger.error("Update ichida xato:", exc_info=context.error)
    tb = "".join(traceback.format_exception(None, context.error, context.error.__traceback__))
    logger.debug("To'liq traceback:\n%s", tb)

    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Ichki xato yuz berdi. Iltimos, qaytadan urinib ko'ring yoki /start bosing."
            )
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════
# LOCATION ROUTER
# Joylashuv xabarini context.user_data["location_mode"] ga qarab
# to'g'ri handler ga yo'naltiradi.
# ══════════════════════════════════════════════════════════════════

async def location_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mode = context.user_data.pop("location_mode", None)
    if mode == "nearby":
        await nearby_radars_location(update, context)
    elif mode == "ypx":
        await ypx_with_location(update, context)
    else:
        await update.message.reply_text(
            "📍 Joylashuv qabul qilindi, lekin nima qilishni bilmadim.\n"
            "Iltimos, quyidagi menyu tugmalaridan foydalaning.",
            reply_markup=main_menu_keyboard(),
        )


# ══════════════════════════════════════════════════════════════════
# APPLICATION BUILDER
# ══════════════════════════════════════════════════════════════════

def build_application() -> Application:
    app = Application.builder().token(config.BOT_TOKEN).build()

    # ── Buyruqlar ──────────────────────────────────────────────────
    app.add_handler(CommandHandler("start",   start_handler))
    app.add_handler(CommandHandler("help",    help_handler))
    app.add_handler(CommandHandler("premium", premium_command))
    app.add_handler(CommandHandler("addypx",  admin_add_ypx))

    # ── Suhbat (Conversation) handlerlari — menyu tugmalaridan oldin ──
    app.add_handler(build_report_conversation())
    app.add_handler(build_fine_conversation())

    # ── Menyu tugmalari ────────────────────────────────────────────
    app.add_handler(MessageHandler(filters.Text(["📍 Yaqin radarlar"]),       nearby_radars_menu))
    app.add_handler(MessageHandler(filters.Text(["🚓 YPX postlari"]),         ypx_menu))
    app.add_handler(MessageHandler(filters.Text(["⭐ Premium"]),              premium_menu))

    # ── Joylashuv xabarlari ────────────────────────────────────────
    app.add_handler(MessageHandler(filters.LOCATION, location_router))

    # ── Inline tugma callbacklari ──────────────────────────────────
    app.add_handler(CallbackQueryHandler(premium_buy_callback,          pattern=r"^premium:buy$"))
    app.add_handler(CallbackQueryHandler(premium_info_callback,         pattern=r"^premium:info$"))
    app.add_handler(CallbackQueryHandler(admin_deactivate_ypx_callback, pattern=r"^admin_ypx:deactivate:"))

    # ── Global xato handler ────────────────────────────────────────
    app.add_error_handler(error_handler)

    return app


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════

async def main() -> None:
    if not config.BOT_TOKEN:
        logger.critical("❌ BOT_TOKEN o'rnatilmagan. .env faylini tekshiring.")
        sys.exit(1)

    logger.info("🗄  Database jadvallar yaratilmoqda…")
    await init_db()

    logger.info("🤖 Radar YPX Bot ishga tushmoqda…")
    app = build_application()

    await app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
