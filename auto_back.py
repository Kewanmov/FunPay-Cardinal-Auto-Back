from __future__ import annotations

import json
import logging
import os
import threading
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from cardinal import Cardinal

from FunPayAPI.common.enums import MessageTypes, OrderStatuses
from FunPayAPI.common.utils import RegularExpressions
from FunPayAPI.updater.events import NewMessageEvent, NewOrderEvent
from telebot.types import (
    CallbackQuery,
    InlineKeyboardButton as B,
    InlineKeyboardMarkup as K,
    Message,
)
from tg_bot import CBT as _CBT, static_keyboards as skb
from Utils.cardinal_tools import cache_blacklist


NAME = "Auto Back"
VERSION = "1.0.0"
DESCRIPTION = "Плагин добавляет новую функцию для автоматического возврата средств по отзывам и заказам от пользователей из чёрного списка."
CREDITS = "@kewanmov"
UUID = "d185c26a-466c-4dac-8644-62a949a51a80"
SETTINGS_PAGE = True

logger = logging.getLogger("FPC.AutoBack")

CBT_MAIN = "AB_Main"
CBT_SWITCH = "AB_Switch"
CBT_BL_TEXT_SHOW = "AB_BLTextShow"
CBT_BL_TEXT_EDIT = "AB_BLTextEdit"
CBT_BL_TEXT_EDITED = "AB_BLTextEdited"
CBT_BL_PRICE_EDIT = "AB_BLPriceEdit"
CBT_BL_PRICE_EDITED = "AB_BLPriceEdited"
CBT_STARS_LIST = "AB_StarsList"
CBT_STAR_OPEN = "AB_StarOpen"
CBT_STAR_SWITCH = "AB_StarSwitch"
CBT_STAR_TEXT_SHOW = "AB_StarTextShow"
CBT_STAR_TEXT_EDIT = "AB_StarTextEdit"
CBT_STAR_TEXT_EDITED = "AB_StarTextEdited"
CBT_STAR_PRICE_EDIT = "AB_StarPriceEdit"
CBT_STAR_PRICE_EDITED = "AB_StarPriceEdited"

_STORAGE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "storage", "plugins", "auto_back"
)
os.makedirs(_STORAGE_PATH, exist_ok=True)
_SETTINGS_FILE = os.path.join(_STORAGE_PATH, "settings.json")
_ORDER_ID_RE = RegularExpressions().ORDER_ID


class StarsConfig:
    __slots__ = ("stars", "refund", "send_msg", "text", "add_bl", "min_price", "max_price")

    def __init__(
        self,
        stars: int = 1,
        refund: bool = False,
        send_msg: bool = False,
        text: str = "",
        add_bl: bool = False,
        min_price: float = 1,
        max_price: float = 10,
    ):
        self.stars = stars
        self.refund = refund
        self.send_msg = send_msg
        self.text = text or ""
        self.add_bl = add_bl
        self.min_price = min_price
        self.max_price = max_price

    def to_dict(self) -> dict:
        return {
            "stars": self.stars,
            "refund": self.refund,
            "send_msg": self.send_msg,
            "text": self.text,
            "add_bl": self.add_bl,
            "min_price": self.min_price,
            "max_price": self.max_price,
        }

    @classmethod
    def from_dict(cls, data: dict) -> StarsConfig:
        return cls(
            stars=data.get("stars", 1),
            refund=data.get("refund", False),
            send_msg=data.get("send_msg", False),
            text=data.get("text", ""),
            add_bl=data.get("add_bl", False),
            min_price=data.get("min_price", 1),
            max_price=data.get("max_price", 10),
        )

    def in_price_range(self, price: float) -> bool:
        return self.min_price <= price <= self.max_price


class Settings:
    __slots__ = (
        "on", "stars_configs", "refund_bl", "send_msg", "text",
        "refund_bl_min", "refund_bl_max", "_lock",
    )

    def __init__(
        self,
        on: bool = True,
        stars_configs: Optional[dict[int, StarsConfig]] = None,
        refund_bl: bool = False,
        send_msg: bool = False,
        text: str = "Вы в чёрном списке нашего магазина!",
        refund_bl_min: float = 1,
        refund_bl_max: float = 10,
    ):
        self.on = on
        self.stars_configs = stars_configs or {i: StarsConfig(stars=i) for i in range(1, 6)}
        self.refund_bl = refund_bl
        self.send_msg = send_msg
        self.text = text
        self.refund_bl_min = refund_bl_min
        self.refund_bl_max = refund_bl_max
        self._lock = threading.Lock()

    def save(self):
        with self._lock:
            data = {
                "on": self.on,
                "stars_configs": {str(k): v.to_dict() for k, v in self.stars_configs.items()},
                "refund_bl": self.refund_bl,
                "send_msg": self.send_msg,
                "text": self.text,
                "refund_bl_min": self.refund_bl_min,
                "refund_bl_max": self.refund_bl_max,
            }
            tmp = _SETTINGS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, _SETTINGS_FILE)

    @classmethod
    def load(cls) -> Settings:
        if os.path.exists(_SETTINGS_FILE):
            try:
                with open(_SETTINGS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                stars_configs = {}
                raw_stars = data.get("stars_configs", {})
                for k, v in raw_stars.items():
                    stars_configs[int(k)] = StarsConfig.from_dict(v)
                for i in range(1, 6):
                    if i not in stars_configs:
                        stars_configs[i] = StarsConfig(stars=i)
                return cls(
                    on=data.get("on", True),
                    stars_configs=stars_configs,
                    refund_bl=data.get("refund_bl", False),
                    send_msg=data.get("send_msg", False),
                    text=data.get("text", "Вы в чёрном списке нашего магазина!"),
                    refund_bl_min=data.get("refund_bl_min", 1),
                    refund_bl_max=data.get("refund_bl_max", 10),
                )
            except Exception:
                logger.exception("Ошибка загрузки настроек, используются дефолтные")
        return cls()

    def bl_in_price_range(self, price: float) -> bool:
        return self.refund_bl_min <= price <= self.refund_bl_max


SETTINGS: Optional[Settings] = None
_cardinal_ref: Optional[Cardinal] = None


def _extract_order_id(text: str) -> Optional[str]:
    match = _ORDER_ID_RE.search(text)
    if not match:
        return None
    order_id = match.group(0)
    if order_id.startswith("#"):
        order_id = order_id[1:]
    return order_id


def _get_notify_chat_id() -> Optional[int]:
    if _cardinal_ref is None or not _cardinal_ref.telegram:
        return None
    tg = _cardinal_ref.telegram
    if hasattr(tg, "authorized_users") and tg.authorized_users:
        if isinstance(tg.authorized_users, dict):
            return next(iter(tg.authorized_users.keys()), None)
        if isinstance(tg.authorized_users, list):
            return tg.authorized_users[0] if tg.authorized_users else None
    return None


def _send_notification(text: str):
    chat_id = _get_notify_chat_id()
    if not chat_id or not _cardinal_ref or not _cardinal_ref.telegram:
        return
    try:
        _cardinal_ref.telegram.bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception:
        logger.debug("Не удалось отправить уведомление", exc_info=True)


def _is_on(val: bool) -> str:
    return "🟢" if val else "🔴"


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _main_text() -> str:
    s = SETTINGS
    return (
        f"⚙️ <b>Настройки Auto Back</b>\n\n"
        f"∟ Авто-возвраты: {_is_on(s.on)} {'Включены' if s.on else 'Выключены'}\n"
        f"∟ Возврат при покупке из ЧС: {_is_on(s.refund_bl)}\n"
    )


def _main_kb() -> K:
    s = SETTINGS
    kb = K()
    kb.add(B(f"{_is_on(s.on)} Авто-возвраты", callback_data=f"{CBT_SWITCH}:on"))
    kb.add(B(f"{_is_on(s.send_msg)} Отправлять сообщение из ЧС", callback_data=f"{CBT_SWITCH}:send_msg"))
    kb.add(B(f"{_is_on(s.refund_bl)} Возврат при покупке из ЧС", callback_data=f"{CBT_SWITCH}:refund_bl"))
    kb.add(B("💬 Текст сообщения ЧС", callback_data=CBT_BL_TEXT_SHOW))
    kb.add(B("⭐ Настройки по оценкам", callback_data=CBT_STARS_LIST))
    kb.row(
        B(f"Мин: {s.refund_bl_min} ₽", callback_data=f"{CBT_BL_PRICE_EDIT}:min"),
        B(f"Макс: {s.refund_bl_max} ₽", callback_data=f"{CBT_BL_PRICE_EDIT}:max"),
    )
    kb.add(B("⬅️ Назад", callback_data=f"{_CBT.EDIT_PLUGIN}:{UUID}:0"))
    return kb


def _stars_list_text() -> str:
    lines = ["⭐ <b>Настройки по оценкам</b>\n"]
    for i in range(1, 6):
        cfg = SETTINGS.stars_configs[i]
        parts = []
        if cfg.refund:
            parts.append("возврат")
        if cfg.send_msg:
            parts.append("сообщение")
        if cfg.add_bl:
            parts.append("ЧС")
        status = ", ".join(parts) if parts else "выключено"
        lines.append(f"∟ {'⭐️' * i} — {status}")
    lines.append("\nВыберите оценку для настройки:")
    return "\n".join(lines)


def _stars_list_kb() -> K:
    kb = K()
    for i in range(1, 6):
        kb.add(B(f"{'⭐️' * i}", callback_data=f"{CBT_STAR_OPEN}:{i}"))
    kb.add(B("⬅️ Назад", callback_data=CBT_MAIN))
    return kb


def _star_text(cfg: StarsConfig) -> str:
    return (
        f"♻️ <b>Настройки на {'⭐️' * cfg.stars}</b>\n\n"
        f"∟ Авто-возврат: {_is_on(cfg.refund)}\n"
        f"∟ Добавлять в ЧС: {_is_on(cfg.add_bl)}\n"
        f"∟ Отправлять сообщение: {_is_on(cfg.send_msg)}\n"
        f"∟ Диапазон цен: <b>{cfg.min_price}–{cfg.max_price} ₽</b>"
    )


def _star_kb(cfg: StarsConfig) -> K:
    i = cfg.stars
    kb = K()
    kb.add(B(f"{_is_on(cfg.refund)} Авто-возврат", callback_data=f"{CBT_STAR_SWITCH}:{i}:refund"))
    kb.add(B(f"{_is_on(cfg.add_bl)} Добавлять в ЧС", callback_data=f"{CBT_STAR_SWITCH}:{i}:add_bl"))
    kb.add(B(f"{_is_on(cfg.send_msg)} Отправлять сообщение", callback_data=f"{CBT_STAR_SWITCH}:{i}:send_msg"))
    kb.add(B("💬 Текст сообщения", callback_data=f"{CBT_STAR_TEXT_SHOW}:{i}"))
    kb.row(
        B(f"Мин: {cfg.min_price} ₽", callback_data=f"{CBT_STAR_PRICE_EDIT}:{i}:min"),
        B(f"Макс: {cfg.max_price} ₽", callback_data=f"{CBT_STAR_PRICE_EDIT}:{i}:max"),
    )
    kb.add(B("⬅️ Назад", callback_data=CBT_STARS_LIST))
    return kb


def _star_msg_text(cfg: StarsConfig) -> str:
    if cfg.text and cfg.text.strip():
        return (
            f"💬 <b>Текст сообщения для {'⭐️' * cfg.stars}:</b>\n\n"
            f"<code>{_escape_html(cfg.text)}</code>"
        )
    return f"❌ Текст сообщения для {'⭐️' * cfg.stars} не установлен."


def _bl_msg_text() -> str:
    if SETTINGS.text and SETTINGS.text.strip():
        return f"💬 <b>Текст сообщения ЧС:</b>\n\n<code>{_escape_html(SETTINGS.text)}</code>"
    return "❌ Текст сообщения ЧС не установлен."


def _cleanup(cardinal: Cardinal, *args):
    global SETTINGS, _cardinal_ref
    SETTINGS = None
    _cardinal_ref = None


def init_commands(cardinal: Cardinal, *args):
    global SETTINGS, _cardinal_ref
    SETTINGS = Settings.load()
    _cardinal_ref = cardinal

    if not cardinal.telegram:
        return

    tg = cardinal.telegram
    bot = tg.bot

    def _edit(c: CallbackQuery, text: str, kb: K):
        try:
            bot.edit_message_text(
                text, c.message.chat.id, c.message.id,
                reply_markup=kb, parse_mode="HTML",
            )
        except Exception:
            pass

    def open_main(c: CallbackQuery):
        _edit(c, _main_text(), _main_kb())
        bot.answer_callback_query(c.id)

    def switch_main(c: CallbackQuery):
        param = c.data.split(":")[-1]
        if param == "on":
            SETTINGS.on = not SETTINGS.on
        elif param == "send_msg":
            SETTINGS.send_msg = not SETTINGS.send_msg
        elif param == "refund_bl":
            SETTINGS.refund_bl = not SETTINGS.refund_bl
        SETTINGS.save()
        _edit(c, _main_text(), _main_kb())
        bot.answer_callback_query(c.id)

    def show_bl_text(c: CallbackQuery):
        kb = K()
        kb.row(
            B("⬅️ Назад", callback_data=CBT_MAIN),
            B("✏️ Изменить", callback_data=CBT_BL_TEXT_EDIT),
        )
        _edit(c, _bl_msg_text(), kb)
        bot.answer_callback_query(c.id)

    def act_bl_text_edit(c: CallbackQuery):
        msg = bot.send_message(
            c.message.chat.id,
            "💬 <b>Отправьте новый текст сообщения для покупателей из ЧС.</b>\n\n"
            "Отправьте <code>-</code> чтобы очистить текст.",
            reply_markup=skb.CLEAR_STATE_BTN(),
            parse_mode="HTML",
        )
        tg.set_state(c.message.chat.id, msg.id, c.from_user.id, CBT_BL_TEXT_EDITED, {})
        bot.answer_callback_query(c.id)

    def on_bl_text_edited(m: Message):
        tg.clear_state(m.chat.id, m.from_user.id, True)
        try:
            bot.delete_message(m.chat.id, m.id)
        except Exception:
            pass
        if m.text and m.text.strip() == "-":
            SETTINGS.text = ""
        else:
            SETTINGS.text = m.text or ""
        SETTINGS.save()
        kb = K()
        kb.row(
            B("⬅️ Назад", callback_data=CBT_MAIN),
            B("💬 Посмотреть", callback_data=CBT_BL_TEXT_SHOW),
        )
        bot.send_message(m.chat.id, "✅ Текст сообщения обновлён!", reply_markup=kb)

    def act_bl_price_edit(c: CallbackQuery):
        bound = c.data.split(":")[-1]
        label = "минимальную" if bound == "min" else "максимальную"
        current = SETTINGS.refund_bl_min if bound == "min" else SETTINGS.refund_bl_max
        msg = bot.send_message(
            c.message.chat.id,
            f"💰 <b>Введите {label} цену для ЧС:</b>\nТекущая: <b>{current} ₽</b>",
            reply_markup=skb.CLEAR_STATE_BTN(),
            parse_mode="HTML",
        )
        tg.set_state(c.message.chat.id, msg.id, c.from_user.id, CBT_BL_PRICE_EDITED, {"bound": bound})
        bot.answer_callback_query(c.id)

    def on_bl_price_edited(m: Message):
        data = tg.get_state(m.chat.id, m.from_user.id)
        bound = data.get("data", {}).get("bound", "min") if data else "min"
        tg.clear_state(m.chat.id, m.from_user.id, True)
        try:
            bot.delete_message(m.chat.id, m.id)
        except Exception:
            pass
        try:
            val = float(m.text.strip())
        except (ValueError, TypeError):
            bot.send_message(m.chat.id, "❌ <b>Неверный формат цены</b>", parse_mode="HTML")
            return
        if bound == "min":
            SETTINGS.refund_bl_min = val
        else:
            SETTINGS.refund_bl_max = val
        SETTINGS.save()
        bot.send_message(m.chat.id, _main_text(), reply_markup=_main_kb(), parse_mode="HTML")

    def open_stars_list(c: CallbackQuery):
        _edit(c, _stars_list_text(), _stars_list_kb())
        bot.answer_callback_query(c.id)

    def open_star(c: CallbackQuery):
        star = int(c.data.split(":")[-1])
        cfg = SETTINGS.stars_configs[star]
        _edit(c, _star_text(cfg), _star_kb(cfg))
        bot.answer_callback_query(c.id)

    def switch_star(c: CallbackQuery):
        parts = c.data.split(":")
        star = int(parts[1])
        param = parts[2]
        cfg = SETTINGS.stars_configs[star]
        if hasattr(cfg, param):
            setattr(cfg, param, not getattr(cfg, param))
            SETTINGS.save()
        _edit(c, _star_text(cfg), _star_kb(cfg))
        bot.answer_callback_query(c.id)

    def show_star_text(c: CallbackQuery):
        star = int(c.data.split(":")[-1])
        cfg = SETTINGS.stars_configs[star]
        kb = K()
        kb.row(
            B("⬅️ Назад", callback_data=f"{CBT_STAR_OPEN}:{star}"),
            B("✏️ Изменить", callback_data=f"{CBT_STAR_TEXT_EDIT}:{star}"),
        )
        _edit(c, _star_msg_text(cfg), kb)
        bot.answer_callback_query(c.id)

    def act_star_text_edit(c: CallbackQuery):
        star = c.data.split(":")[-1]
        msg = bot.send_message(
            c.message.chat.id,
            f"💬 <b>Отправьте новый текст сообщения для {'⭐️' * int(star)}.</b>\n\n"
            f"Отправьте <code>-</code> чтобы очистить текст.",
            reply_markup=skb.CLEAR_STATE_BTN(),
            parse_mode="HTML",
        )
        tg.set_state(c.message.chat.id, msg.id, c.from_user.id, CBT_STAR_TEXT_EDITED, {"star": star})
        bot.answer_callback_query(c.id)

    def on_star_text_edited(m: Message):
        data = tg.get_state(m.chat.id, m.from_user.id)
        star = int(data["data"]["star"])
        tg.clear_state(m.chat.id, m.from_user.id, True)
        try:
            bot.delete_message(m.chat.id, m.id)
        except Exception:
            pass
        cfg = SETTINGS.stars_configs[star]
        if m.text and m.text.strip() == "-":
            cfg.text = ""
        else:
            cfg.text = m.text or ""
        SETTINGS.save()
        kb = K()
        kb.row(
            B("⬅️ Назад", callback_data=f"{CBT_STAR_OPEN}:{star}"),
            B("💬 Посмотреть", callback_data=f"{CBT_STAR_TEXT_SHOW}:{star}"),
        )
        bot.send_message(m.chat.id, f"✅ Текст для {'⭐️' * star} обновлён!", reply_markup=kb)

    def act_star_price_edit(c: CallbackQuery):
        parts = c.data.split(":")
        star, bound = parts[1], parts[2]
        label = "минимальную" if bound == "min" else "максимальную"
        cfg = SETTINGS.stars_configs[int(star)]
        current = cfg.min_price if bound == "min" else cfg.max_price
        msg = bot.send_message(
            c.message.chat.id,
            f"💰 <b>Введите {label} цену для {'⭐️' * int(star)}:</b>\nТекущая: <b>{current} ₽</b>",
            reply_markup=skb.CLEAR_STATE_BTN(),
            parse_mode="HTML",
        )
        tg.set_state(c.message.chat.id, msg.id, c.from_user.id, CBT_STAR_PRICE_EDITED, {"star": star, "bound": bound})
        bot.answer_callback_query(c.id)

    def on_star_price_edited(m: Message):
        data = tg.get_state(m.chat.id, m.from_user.id)
        star = int(data["data"]["star"])
        bound = data["data"]["bound"]
        tg.clear_state(m.chat.id, m.from_user.id, True)
        try:
            bot.delete_message(m.chat.id, m.id)
        except Exception:
            pass
        try:
            val = float(m.text.strip())
        except (ValueError, TypeError):
            bot.send_message(m.chat.id, "❌ <b>Неверный формат цены</b>", parse_mode="HTML")
            return
        cfg = SETTINGS.stars_configs[star]
        if bound == "min":
            cfg.min_price = val
        else:
            cfg.max_price = val
        SETTINGS.save()
        bot.send_message(m.chat.id, _star_text(cfg), reply_markup=_star_kb(cfg), parse_mode="HTML")

    def cmd_menu(m: Message):
        bot.send_message(m.chat.id, _main_text(), reply_markup=_main_kb(), parse_mode="HTML")

    tg.cbq_handler(open_main, lambda c: f"{_CBT.PLUGIN_SETTINGS}:{UUID}" in c.data)
    tg.cbq_handler(open_main, lambda c: c.data == CBT_MAIN)
    tg.cbq_handler(switch_main, lambda c: c.data.startswith(f"{CBT_SWITCH}:"))
    tg.cbq_handler(show_bl_text, lambda c: c.data == CBT_BL_TEXT_SHOW)
    tg.cbq_handler(act_bl_text_edit, lambda c: c.data == CBT_BL_TEXT_EDIT)
    tg.msg_handler(on_bl_text_edited, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, CBT_BL_TEXT_EDITED))
    tg.cbq_handler(act_bl_price_edit, lambda c: c.data.startswith(f"{CBT_BL_PRICE_EDIT}:"))
    tg.msg_handler(on_bl_price_edited, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, CBT_BL_PRICE_EDITED))
    tg.cbq_handler(open_stars_list, lambda c: c.data == CBT_STARS_LIST)
    tg.cbq_handler(open_star, lambda c: c.data.startswith(f"{CBT_STAR_OPEN}:"))
    tg.cbq_handler(switch_star, lambda c: c.data.startswith(f"{CBT_STAR_SWITCH}:"))
    tg.cbq_handler(show_star_text, lambda c: c.data.startswith(f"{CBT_STAR_TEXT_SHOW}:"))
    tg.cbq_handler(act_star_text_edit, lambda c: c.data.startswith(f"{CBT_STAR_TEXT_EDIT}:"))
    tg.msg_handler(on_star_text_edited, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, CBT_STAR_TEXT_EDITED))
    tg.cbq_handler(act_star_price_edit, lambda c: c.data.startswith(f"{CBT_STAR_PRICE_EDIT}:"))
    tg.msg_handler(on_star_price_edited, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, CBT_STAR_PRICE_EDITED))
    tg.msg_handler(cmd_menu, commands=["auto_back"])
    cardinal.add_telegram_commands(UUID, [("auto_back", "открыть настройки авто-возврата", True)])


def on_new_message(cardinal: Cardinal, event: NewMessageEvent):
    if not SETTINGS or not SETTINGS.on:
        return

    msg = event.message
    if msg.type not in (MessageTypes.NEW_FEEDBACK, MessageTypes.FEEDBACK_CHANGED):
        return

    order_id = _extract_order_id(msg.text or "")
    if not order_id:
        logger.warning("Не удалось извлечь order_id из сообщения: %s", msg.text)
        return

    try:
        order = cardinal.account.get_order(order_id)
    except Exception:
        logger.error("Не удалось получить заказ #%s", order_id, exc_info=True)
        return

    if order.status in (OrderStatuses.REFUNDED, OrderStatuses.PARTIALLY_REFUNDED):
        logger.debug("Заказ #%s уже возвращён, пропускаю", order_id)
        return

    if not order.review or order.review.stars is None:
        logger.debug("Заказ #%s: отзыв отсутствует или без оценки", order_id)
        return

    stars = order.review.stars
    cfg = SETTINGS.stars_configs.get(stars)
    if not cfg:
        return

    if not cfg.in_price_range(order.sum):
        logger.debug(
            "Заказ #%s: цена %s вне диапазона [%s, %s] для %d★",
            order_id, order.sum, cfg.min_price, cfg.max_price, stars,
        )
        return

    actions_taken = []

    if cfg.refund:
        try:
            cardinal.account.refund(order.id)
            actions_taken.append("возврат")
            logger.info("Возврат по заказу #%s (%d★)", order.id, stars)
        except Exception:
            logger.error("Ошибка возврата по заказу #%s", order.id, exc_info=True)

    if cfg.send_msg and cfg.text:
        try:
            cardinal.send_message(order.chat_id, cfg.text)
            actions_taken.append("сообщение")
            logger.info("Сообщение отправлено в чат %s", order.chat_id)
        except Exception:
            logger.error("Ошибка отправки сообщения в чат %s", order.chat_id, exc_info=True)

    if cfg.add_bl and order.buyer_username:
        if order.buyer_username not in cardinal.blacklist:
            cardinal.blacklist.append(order.buyer_username)
            cache_blacklist(cardinal.blacklist)
            actions_taken.append("ЧС")
            logger.info("%s добавлен в ЧС (%d★)", order.buyer_username, stars)

    if actions_taken:
        _send_notification(
            f"♻️ <b>Auto Back</b>\n\n"
            f"Заказ: <code>#{order.id}</code>\n"
            f"Оценка: {'⭐️' * stars}\n"
            f"Покупатель: {order.buyer_username or '?'}\n"
            f"Сумма: <b>{order.sum} {order.currency}</b>\n"
            f"Действия: {', '.join(actions_taken)}"
        )


def on_new_order(cardinal: Cardinal, event: NewOrderEvent):
    if not SETTINGS or not SETTINGS.on:
        return

    order_shortcut = event.order
    buyer = order_shortcut.buyer_username

    if buyer not in cardinal.blacklist:
        return

    if not SETTINGS.bl_in_price_range(order_shortcut.price):
        logger.debug(
            "Заказ #%s от %s (ЧС): цена %s вне диапазона [%s, %s]",
            order_shortcut.id, buyer, order_shortcut.price,
            SETTINGS.refund_bl_min, SETTINGS.refund_bl_max,
        )
        return

    actions_taken = []

    if SETTINGS.refund_bl:
        try:
            full_order = cardinal.account.get_order(order_shortcut.id)
        except Exception:
            logger.error("Не удалось получить заказ #%s для проверки автовыдачи", order_shortcut.id, exc_info=True)
            return

        if full_order.order_secrets:
            logger.info("Заказ #%s от %s (ЧС) содержит автовыдачу — возврат не выполнен", order_shortcut.id, buyer)
        else:
            try:
                cardinal.account.refund(order_shortcut.id)
                actions_taken.append("возврат")
                logger.info("Возврат по заказу #%s (покупатель в ЧС)", order_shortcut.id)
            except Exception:
                logger.error("Ошибка возврата по заказу #%s", order_shortcut.id, exc_info=True)

    if SETTINGS.send_msg and SETTINGS.text:
        try:
            cardinal.send_message(order_shortcut.chat_id, SETTINGS.text)
            actions_taken.append("сообщение")
            logger.info("Сообщение отправлено в чат %s", order_shortcut.chat_id)
        except Exception:
            logger.error("Ошибка отправки сообщения в чат %s", order_shortcut.chat_id, exc_info=True)

    if actions_taken:
        _send_notification(
            f"🚫 <b>Auto Back — ЧС</b>\n\n"
            f"Заказ: <code>#{order_shortcut.id}</code>\n"
            f"Покупатель: {buyer}\n"
            f"Сумма: <b>{order_shortcut.price} {order_shortcut.currency}</b>\n"
            f"Действия: {', '.join(actions_taken)}"
        )


BIND_TO_PRE_INIT = [init_commands]
BIND_TO_DELETE = [_cleanup]
BIND_TO_NEW_MESSAGE = [on_new_message]
BIND_TO_NEW_ORDER = [on_new_order]
