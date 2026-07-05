"""
Telegram bot: commands + notifications for the trading bot.
Uses python-telegram-bot v20+ (async).
"""
from __future__ import annotations

from typing import Callable

from loguru import logger
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, filters

from src.config import get_config
from src.database import get_session
from src.models import Position as PositionModel, BotLog

# Callbacks to interact with main bot controller
_bot_controller: Callable | None = None  # set from main.py


def set_controller(ctrl):
    global _bot_controller
    _bot_controller = ctrl


def _check_auth(update: Update) -> bool:
    cfg = get_config()
    chat_id = update.effective_chat.id if update.effective_chat else 0
    return chat_id in cfg.bot.telegram_chat_ids


# --- Command handlers ---

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_auth(update):
        await update.message.reply_text("Доступ запрещён.")
        return
    if _bot_controller:
        _bot_controller.start()
        await update.message.reply_text("Бот запущен.")
    else:
        await update.message.reply_text("Контроллер не подключён.")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_auth(update):
        await update.message.reply_text("Доступ запрещён.")
        return
    if _bot_controller:
        _bot_controller.stop()
        await update.message.reply_text("Бот остановлен.")
    else:
        await update.message.reply_text("Контроллер не подключён.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_auth(update):
        await update.message.reply_text("Доступ запрещён.")
        return

    session = get_session()
    positions = session.query(PositionModel).filter(PositionModel.status == "OPEN").all()

    if _bot_controller:
        daily = _bot_controller.get_risk_manager().get_daily_stats()
    else:
        daily = {"trades": 0, "pnl": 0, "consecutive_losses": 0, "breaker": "NONE"}

    msg = f"📊 *Статус*\n"
    msg += f"Сделок сегодня: {daily['trades']}\n"
    msg += f"PnL: {daily['pnl']} USDT\n"
    msg += f"Убытков подряд: {daily['consecutive_losses']}\n"
    msg += f"Breaker: {daily['breaker']}\n\n"

    if positions:
        msg += "*Открытые позиции:*\n"
        for p in positions:
            msg += f"• {p.symbol} | вход {float(p.entry_price):.2f} | SL {float(p.stop_loss or 0):.2f}\n"
    else:
        msg += "Нет открытых позиций."

    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_auth(update):
        await update.message.reply_text("Доступ запрещён.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /close BTCUSDT")
        return

    symbol = context.args[0].upper()
    if _bot_controller:
        success = _bot_controller.manual_close(symbol)
        if success:
            await update.message.reply_text(f"Позиция {symbol} закрыта.")
        else:
            await update.message.reply_text(f"Не удалось закрыть {symbol}.")
    else:
        await update.message.reply_text("Контроллер не подключён.")


async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_auth(update):
        await update.message.reply_text("Доступ запрещён.")
        return

    n = int(context.args[0]) if context.args else 10
    session = get_session()
    logs = session.query(BotLog).order_by(BotLog.created_at.desc()).limit(n).all()

    msg = f"📜 *Последние {n} записей:*\n"
    for log_entry in reversed(logs):
        msg += f"[{log_entry.level}] {log_entry.message[:80]}\n"
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_auth(update):
        await update.message.reply_text("Доступ запрещён.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /config risk 3")
        return

    key, value = context.args[0], context.args[1]
    session = get_session()
    from src.models import BotConfig

    cfg_entry = session.query(BotConfig).filter(BotConfig.config_key == key).first()
    if cfg_entry:
        cfg_entry.config_value = str(value)
    else:
        cfg_entry = BotConfig(config_key=key, config_value=str(value))
        session.add(cfg_entry)
    session.commit()
    await update.message.reply_text(f"✅ {key} = {value}")


# --- Notification helpers ---

async def send_notification(app: Application, text: str):
    """Send message to all authorized chat IDs."""
    cfg = get_config()
    for chat_id in cfg.bot.telegram_chat_ids:
        try:
            await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Failed to send Telegram notification: {e}")


def create_app() -> Application:
    cfg = get_config()
    if not cfg.bot.telegram_token:
        logger.warning("Telegram token not set — bot disabled")
        return None

    app = Application.builder().token(cfg.bot.telegram_token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("close", cmd_close))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("config", cmd_config))

    logger.info("Telegram bot handlers registered")
    return app
