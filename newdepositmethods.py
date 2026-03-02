"""
newdepositmethods.py — OxaPay Deposit System
============================================
Self-contained OxaPay integration for the Telegram casino bot.

Drop this file in the same directory as bot.py.  bot.py only needs two lines:
    from newdepositmethods import register_oxapay_handlers, start_oxapay_webhook
    ... inside main(), after app is built:
    register_oxapay_handlers(app)
    ... inside post_init:
    application.create_task(start_oxapay_webhook(application))

See OXAPAY_SETUP_GUIDE.md (created alongside this file) for configuration.

──────────────────────────────────────────────────────────────────────────────
How balance crediting works (mirrors the existing on-chain deposit flow):
  1. OxaPay POSTs a webhook to our server when a payment is confirmed.
  2. We verify the HMAC-SHA256 signature.
  3. We call credit_wallet_crypto(telegram_id, pay_amount, currency) from bot.py
     — this adds the exact crypto amount the user paid to their wallet.
  4. We update user_stats["unwagered_deposit"] for bonus wagering tracking.
  5. We call save_user_data(telegram_id) to persist.
  6. We send a Telegram DM to the user notifying them of the credit.
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac as hmac_lib
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import aiohttp
import aiohttp.web

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ──────────────────────────────────────────────────────────────────────────────
# ⚙️  OXAPAY CONFIGURATION  — edit these values before running the bot
# ──────────────────────────────────────────────────────────────────────────────
OXAPAY_MERCHANT_KEY: str = "ONJRRF-JIWZG3-PIUVLS-E9ZRDT"          # Replace with your OxaPay merchant key
OXAPAY_WEBHOOK_HOST: str = "https://play-casino.app"  # Publicly reachable base URL (no trailing /)
OXAPAY_WEBHOOK_PORT: int = 8080               # Port to listen on (must be open in firewall / nginx)
OXAPAY_WEBHOOK_PATH: str = "/oxapay/webhook"  # URL path for the webhook endpoint
OXAPAY_MIN_DEPOSIT_USD: float = 5.0           # Minimum invoice amount in USD

# Currencies that OxaPay accepts AND that the bot has in its wallet.
# Adjust this list to match your OxaPay merchant settings.
OXAPAY_SUPPORTED_CURRENCIES: list[str] = [
    "BTC", "ETH", "USDT", "LTC", "TRX", "BNB", "SOL"
]

# ──────────────────────────────────────────────────────────────────────────────
# Internal state — do NOT edit
# ──────────────────────────────────────────────────────────────────────────────
_bot_ref = None                     # Telegram Bot instance (set at startup)
_processed_orders: set[str] = set() # In-memory duplicate-payment guard

# ConversationHandler state keys
_STATE_AMOUNT   = "oxapay_amount_state"
_STATE_CURRENCY = "oxapay_currency_state"
_STATE_CUSTOM   = "oxapay_custom_amount_state"

# ──────────────────────────────────────────────────────────────────────────────
# Helpers — import shared objects from bot.py at call-time to avoid circular
# imports. Python caches modules, so this is fast.
# ──────────────────────────────────────────────────────────────────────────────

def _bot():
    """Lazy import of bot module to avoid circular imports."""
    import bot as _b
    return _b


def _credit(user_id: int, amount: float, coin: str) -> None:
    """Credit crypto to user's wallet and persist."""
    b = _bot()
    b.credit_wallet_crypto(user_id, amount, coin)
    if user_id in b.user_stats:
        price = b.LIVE_PRICES.get(coin, None)
        if price is None or price <= 0:
            logging.warning(f"[OxaPay] No live price for {coin}, unwagered_deposit tracking skipped")
            price = 1.0  # Fallback to avoid divide-by-zero; tracking will be inaccurate
        amount_usd = amount * price
        b.user_stats[user_id]["unwagered_deposit"] = (
            b.user_stats[user_id].get("unwagered_deposit", 0.0) + amount_usd
        )
        # Referral deposit commission (0.5%)
        ref_data = b.user_stats[user_id].get("referral", {})
        referrer_id = ref_data.get("referrer_id")
        if referrer_id and referrer_id in b.user_stats:
            commission = amount * 0.005
            ref_dict = b.user_stats[referrer_id].setdefault("referral", {})
            comm_dict = ref_dict.setdefault("commissions", {})
            comm_dict[coin] = comm_dict.get(coin, 0.0) + commission
            b.save_user_data(referrer_id)
    b.save_user_data(user_id)


def _live_price(coin: str) -> float:
    return _bot().LIVE_PRICES.get(coin, 1.0)


def _apply_style(button, style: str):
    return _bot().apply_button_style(button, style)


def _styled_keyboard(rows):
    return _bot().create_styled_keyboard(rows)


def _check_ownership(query, context) -> bool:
    return _bot().check_menu_ownership(query, context)


def _set_owner(message, user_id: int) -> None:
    _bot().set_menu_owner(message, user_id)


def _safe_edit(query, text, **kwargs):
    return _bot().safe_edit_message(query, text, **kwargs)


def _crypto_symbols() -> dict:
    return _bot().CRYPTO_SYMBOLS


def _supported_cryptos() -> list:
    return _bot().SUPPORTED_CRYPTOS


def _format_amount(amount: float, coin: str) -> str:
    return _bot().format_crypto_amount(amount, coin)


# ──────────────────────────────────────────────────────────────────────────────
# OxaPay API
# ──────────────────────────────────────────────────────────────────────────────

class OxaPayClient:
    """Thin async wrapper around the OxaPay Merchant API."""

    INVOICE_URL = "https://api.oxapay.com/merchants/request"

    def __init__(self, merchant_key: str, callback_url: str):
        self.merchant_key = merchant_key
        self.callback_url = callback_url

    async def create_invoice(
        self,
        amount_usd: float,
        currency: str,
        order_id: str,
        description: str = "Casino Deposit",
    ) -> dict | None:
        """
        Create a payment invoice.

        Returns the full API response dict on success, None on failure.
        On success the dict contains ``payLink`` (or ``payUrl``) for the user.
        """
        payload = {
            "merchant":    self.merchant_key,
            "amount":      round(amount_usd, 2),
            "currency":    currency,
            "orderId":     order_id,
            "callbackUrl": self.callback_url,
            "description": description,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.INVOICE_URL,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    if resp.status != 200:
                        logging.error(f"[OxaPay] Invoice HTTP {resp.status}")
                        return None
                    data = await resp.json()
                    if data.get("result") == 100:
                        return data
                    logging.error(f"[OxaPay] Invoice API error: {data}")
                    return None
        except asyncio.TimeoutError:
            logging.error("[OxaPay] create_invoice timed out")
        except aiohttp.ClientError as e:
            logging.error(f"[OxaPay] create_invoice network error: {e}")
        except Exception as e:
            logging.error(f"[OxaPay] create_invoice exception: {e}")
        return None


# ──────────────────────────────────────────────────────────────────────────────
# HMAC verification
# ──────────────────────────────────────────────────────────────────────────────

def _verify_signature(raw_body: bytes, received_hmac: str) -> bool:
    """Verify the HMAC-SHA256 signature that OxaPay attaches to webhooks."""
    expected = hmac_lib.new(
        OXAPAY_MERCHANT_KEY.encode(),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac_lib.compare_digest(expected, received_hmac)


# ──────────────────────────────────────────────────────────────────────────────
# Webhook HTTP handler (aiohttp)
# ──────────────────────────────────────────────────────────────────────────────

async def _oxapay_webhook(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """
    Receives payment confirmation callbacks from OxaPay.

    Always returns HTTP 200 to prevent OxaPay retries.
    All processing errors are logged server-side.
    """
    global _bot_ref, _processed_orders
    ok = aiohttp.web.Response(text="ok", status=200)

    try:
        raw_body = await request.read()
        data = json.loads(raw_body)
        logging.info(f"[OxaPay] Webhook received: {data}")

        # ── Signature check ──────────────────────────────────────────────────
        received_hmac = data.get("hmac", "")
        if received_hmac:
            verify_data = {k: v for k, v in data.items() if k != "hmac"}
            verify_body = json.dumps(verify_data, separators=(",", ":")).encode()
            if not _verify_signature(verify_body, received_hmac):
                logging.warning("[OxaPay] HMAC mismatch — ignoring")
                return ok

        # ── Only process confirmed payments ──────────────────────────────────
        status = data.get("status", "").lower()
        if status not in ("paid", "confirmed", "completed"):
            logging.info(f"[OxaPay] Ignoring status={status}")
            return ok

        # ── Extract payment fields ────────────────────────────────────────────
        order_id   = data.get("orderId", "")
        track_id   = data.get("trackId", "")
        amount_usd = float(data.get("amount", 0))
        paid_coin  = data.get("currency", "USDT").upper()

        # pay_amount = actual crypto units the user paid (OxaPay field names vary)
        raw_pay = data.get("payAmount")
        if raw_pay is None:
            raw_pay = data.get("pay_amount")
        pay_amount = float(raw_pay) if raw_pay is not None else amount_usd

        # ── Duplicate guard ───────────────────────────────────────────────────
        dedup_key = order_id or track_id
        if dedup_key in _processed_orders:
            logging.info(f"[OxaPay] Duplicate callback {dedup_key} — skipping")
            return ok
        _processed_orders.add(dedup_key)

        if pay_amount <= 0:
            logging.warning(f"[OxaPay] pay_amount={pay_amount} for {order_id} — skipping")
            return ok

        # ── Resolve Telegram user from orderId = "{tg_id}_{timestamp}" ────────
        tg_str = order_id.split("_")[0] if "_" in order_id else ""
        if not tg_str.isdigit():
            logging.warning(f"[OxaPay] Unrecognised orderId: {order_id}")
            return ok
        telegram_id = int(tg_str)

        # ── Load user data if not yet in memory ───────────────────────────────
        b = _bot()
        b.load_user_data_if_missing(telegram_id)

        # ── Credit wallet ─────────────────────────────────────────────────────
        if telegram_id in b.user_wallets:
            _credit(telegram_id, pay_amount, paid_coin)
            logging.info(
                f"[OxaPay] Credited {pay_amount} {paid_coin} (${amount_usd:.2f}) "
                f"to user {telegram_id}"
            )
        else:
            logging.warning(f"[OxaPay] User {telegram_id} not found in user_wallets — deposit not credited")

        # ── Notify user via Telegram ──────────────────────────────────────────
        if _bot_ref:
            formatted = _format_amount(pay_amount, paid_coin)
            sym = _crypto_symbols().get(paid_coin, "")
            try:
                await _bot_ref.send_message(
                    chat_id=telegram_id,
                    text=(
                        f"✅ <b>Deposit Confirmed!</b>\n\n"
                        f"{sym} <b>{formatted} {paid_coin}</b> (≈${amount_usd:.2f})\n\n"
                        f"Your balance has been credited. Good luck! 🎰"
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception as msg_err:
                logging.error(f"[OxaPay] Failed to notify user {telegram_id}: {msg_err}")

    except Exception as exc:
        logging.error(f"[OxaPay] Webhook exception: {exc}", exc_info=True)

    return ok


# ──────────────────────────────────────────────────────────────────────────────
# aiohttp webhook server startup
# ──────────────────────────────────────────────────────────────────────────────

async def start_oxapay_webhook(application: Application) -> None:
    """
    Starts the aiohttp web server that receives OxaPay payment callbacks.

    Pass your Telegram Application so the webhook can notify users via DM.
    Call this from post_init:
        application.create_task(start_oxapay_webhook(application))
    """
    global _bot_ref

    if not OXAPAY_MERCHANT_KEY or OXAPAY_MERCHANT_KEY == "sandbox":
        logging.warning(
            "[OxaPay] OXAPAY_MERCHANT_KEY is not configured. "
            "Webhook server not started."
        )
        return

    if not OXAPAY_WEBHOOK_HOST or OXAPAY_WEBHOOK_HOST == "https://yourdomain.com":
        logging.warning(
            "[OxaPay] OXAPAY_WEBHOOK_HOST is not configured. "
            "Webhook server not started."
        )
        return

    _bot_ref = application.bot

    web_app = aiohttp.web.Application()
    web_app.router.add_post(OXAPAY_WEBHOOK_PATH, _oxapay_webhook)

    runner = aiohttp.web.AppRunner(web_app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "0.0.0.0", OXAPAY_WEBHOOK_PORT)
    await site.start()
    logging.info(
        f"[OxaPay] Webhook server listening on 0.0.0.0:{OXAPAY_WEBHOOK_PORT}{OXAPAY_WEBHOOK_PATH}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Telegram conversation flow
# ──────────────────────────────────────────────────────────────────────────────

# ── Step 1: Entry — show preset amount buttons ────────────────────────────────

async def oxapay_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point: user taps '⚡ OxaPay' in the deposit menu."""
    query = update.callback_query
    if not _check_ownership(query, context):
        await query.answer("This menu is not for you.", show_alert=True)
        return ConversationHandler.END
    await query.answer()

    preset_amounts = [10, 15, 20, 50, 100]

    amount_buttons = []
    row: list = []
    for amt in preset_amounts:
        btn = _apply_style(
            InlineKeyboardButton(f"💲{amt}", callback_data=f"oxapay_amt_{amt}"),
            "primary",
        )
        row.append(btn)
        if len(row) == 3:
            amount_buttons.append(row)
            row = []
    if row:
        amount_buttons.append(row)

    # Custom amount + Back
    amount_buttons.append([
        _apply_style(InlineKeyboardButton("✏️ Custom Amount", callback_data="oxapay_amt_custom"), "primary"),
    ])
    amount_buttons.append([
        _apply_style(InlineKeyboardButton("🔙 Back", callback_data="back_to_deposit_menu"), "danger"),
    ])

    text = (
        "⚡ <b>OxaPay Deposit</b>\n\n"
        "Select how much (USD) you want to deposit:\n\n"
        f"<i>Minimum deposit: ${OXAPAY_MIN_DEPOSIT_USD:.0f}</i>"
    )

    await _safe_edit(query, text, reply_markup=_styled_keyboard(amount_buttons), parse_mode=ParseMode.HTML)
    return _STATE_AMOUNT


# ── Step 2a: Custom amount input ──────────────────────────────────────────────

async def oxapay_custom_amount_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User tapped 'Custom Amount' — ask them to type it."""
    query = update.callback_query
    if not _check_ownership(query, context):
        await query.answer("This menu is not for you.", show_alert=True)
        return ConversationHandler.END
    await query.answer()

    keyboard = [[
        _apply_style(InlineKeyboardButton("🔙 Back", callback_data="oxapay_back_to_amounts"), "danger")
    ]]

    await _safe_edit(
        query,
        f"✏️ <b>Custom Deposit Amount</b>\n\n"
        f"Type the amount in USD you want to deposit.\n"
        f"Minimum: <b>${OXAPAY_MIN_DEPOSIT_USD:.0f}</b>\n\n"
        f"<i>Example: <code>75</code> or <code>250</code></i>",
        reply_markup=_styled_keyboard(keyboard),
        parse_mode=ParseMode.HTML,
    )
    context.user_data["oxapay_awaiting_custom"] = True
    return _STATE_CUSTOM


async def oxapay_receive_custom_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User typed a custom amount."""
    text = (update.message.text or "").strip()
    try:
        amount = float(text.replace(",", ""))
        if amount < OXAPAY_MIN_DEPOSIT_USD:
            raise ValueError("below minimum")
    except ValueError:
        keyboard = [[
            _apply_style(InlineKeyboardButton("🔙 Back", callback_data="oxapay_back_to_amounts"), "danger")
        ]]
        await update.message.reply_text(
            f"❌ Invalid amount. Please enter a number ≥ <b>${OXAPAY_MIN_DEPOSIT_USD:.0f}</b>.",
            parse_mode=ParseMode.HTML,
            reply_markup=_styled_keyboard(keyboard),
        )
        return _STATE_CUSTOM

    context.user_data["oxapay_usd_amount"] = amount
    context.user_data.pop("oxapay_awaiting_custom", None)
    return await _show_currency_selector(update, context, via_message=True)


# ── Step 2b: Preset amount selected ──────────────────────────────────────────

async def oxapay_preset_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User tapped one of the preset amount buttons."""
    query = update.callback_query
    if not _check_ownership(query, context):
        await query.answer("This menu is not for you.", show_alert=True)
        return ConversationHandler.END
    await query.answer()

    amt_str = query.data.replace("oxapay_amt_", "")
    try:
        amount = float(amt_str)
    except ValueError:
        await query.answer("Invalid amount.", show_alert=True)
        return _STATE_AMOUNT

    context.user_data["oxapay_usd_amount"] = amount
    return await _show_currency_selector(update, context, via_message=False)


# ── Step 3: Currency selector ─────────────────────────────────────────────────

async def _show_currency_selector(update: Update, context: ContextTypes.DEFAULT_TYPE, via_message: bool = False):
    """Build and show the currency selection screen."""
    amount = context.user_data.get("oxapay_usd_amount", 0.0)
    symbols = _crypto_symbols()

    # Intersection: currencies both OxaPay and the bot support
    available = [c for c in OXAPAY_SUPPORTED_CURRENCIES if c in _supported_cryptos()]

    rows: list = []
    row: list = []
    for coin in available:
        sym = symbols.get(coin, "")
        btn = _apply_style(
            InlineKeyboardButton(f"{sym} {coin}", callback_data=f"oxapay_cur_{coin}"),
            "primary",
        )
        row.append(btn)
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    rows.append([
        _apply_style(InlineKeyboardButton("🔙 Back", callback_data="oxapay_back_to_amounts"), "danger")
    ])

    text = (
        f"⚡ <b>OxaPay Deposit — ${amount:.2f}</b>\n\n"
        "In which cryptocurrency would you like to pay?\n\n"
        "<i>Your bot balance will be credited in the selected coin.</i>"
    )

    if via_message:
        sent = await update.message.reply_text(
            text,
            reply_markup=_styled_keyboard(rows),
            parse_mode=ParseMode.HTML,
        )
        _set_owner(sent, update.effective_user.id)
    else:
        await _safe_edit(
            update.callback_query,
            text,
            reply_markup=_styled_keyboard(rows),
            parse_mode=ParseMode.HTML,
        )
    return _STATE_CURRENCY


# ── Step 4: Currency selected — create invoice and show payment link ──────────

async def oxapay_currency_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User picked a currency — create OxaPay invoice and present payment link."""
    query = update.callback_query
    if not _check_ownership(query, context):
        await query.answer("This menu is not for you.", show_alert=True)
        return ConversationHandler.END
    await query.answer("Creating invoice…")

    coin = query.data.replace("oxapay_cur_", "").upper()
    amount_usd = context.user_data.get("oxapay_usd_amount", 0.0)
    user_id = query.from_user.id

    if coin not in OXAPAY_SUPPORTED_CURRENCIES:
        await query.answer("Unsupported currency.", show_alert=True)
        return _STATE_CURRENCY

    # Calculate how much crypto the user needs to send
    price = _live_price(coin)
    crypto_needed = amount_usd / price if price > 0 else 0.0
    formatted_crypto = _format_amount(crypto_needed, coin)
    sym = _crypto_symbols().get(coin, "")

    # Build unique orderId
    order_id = f"{user_id}_{int(datetime.now(timezone.utc).timestamp())}"
    callback_url = f"{OXAPAY_WEBHOOK_HOST.rstrip('/')}{OXAPAY_WEBHOOK_PATH}"

    client = OxaPayClient(OXAPAY_MERCHANT_KEY, callback_url)
    result = await client.create_invoice(
        amount_usd=amount_usd,
        currency=coin,
        order_id=order_id,
    )

    if result is None:
        keyboard = [[
            _apply_style(InlineKeyboardButton("🔙 Back", callback_data="oxapay_back_to_amounts"), "danger")
        ]]
        await _safe_edit(
            query,
            "❌ <b>Failed to create invoice.</b>\n\nPlease try again later or contact support.",
            reply_markup=_styled_keyboard(keyboard),
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    pay_link = result.get("payLink") or result.get("payUrl") or result.get("link", "")
    if not pay_link:
        logging.error(f"[OxaPay] No pay link in response: {result}")
        keyboard = [[
            _apply_style(InlineKeyboardButton("🔙 Back", callback_data="oxapay_back_to_amounts"), "danger")
        ]]
        await _safe_edit(
            query,
            "❌ <b>Invoice created but no payment link returned.</b>\n\nPlease contact support.",
            reply_markup=_styled_keyboard(keyboard),
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    # Persist amount & coin for informational purposes
    context.user_data["oxapay_last_order"] = order_id
    context.user_data["oxapay_last_coin"]  = coin

    # Build the message
    text = (
        f"⚡ <b>OxaPay Invoice Ready!</b>\n\n"
        f"💰 <b>Deposit Amount:</b> ${amount_usd:.2f}\n"
        f"{sym} <b>Pay in:</b> {coin}\n"
        f"📊 <b>Amount to Send:</b> <code>{formatted_crypto} {coin}</code>\n\n"
        f"⚠️ <b>Important:</b> On the OxaPay page, select <b>{coin}</b> as "
        f"your payment currency.\n\n"
        f"ℹ️ OxaPay may show a slightly higher amount to cover their processing fee. "
        f"This is normal.\n\n"
        f"<i>Your balance will be automatically credited after OxaPay confirms the payment.</i>"
    )

    keyboard = [
        [_apply_style(InlineKeyboardButton("💳 Pay Now via OxaPay", url=pay_link), "primary")],
        [_apply_style(InlineKeyboardButton("🔙 New Deposit", callback_data="deposit_oxapay"), "danger")],
    ]

    await _safe_edit(
        query,
        text,
        reply_markup=_styled_keyboard(keyboard),
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


# ── Navigation: back to amounts screen ───────────────────────────────────────

async def oxapay_back_to_amounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle 'Back' from currency selector or custom input — return to amount buttons."""
    query = update.callback_query
    await query.answer()
    context.user_data.pop("oxapay_usd_amount", None)
    context.user_data.pop("oxapay_awaiting_custom", None)
    return await oxapay_entry(update, context)


# ── Cancel / fallback ─────────────────────────────────────────────────────────

async def oxapay_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel OxaPay flow (used as ConversationHandler fallback)."""
    if update.message:
        await update.message.reply_text("❌ OxaPay deposit cancelled.")
    context.user_data.pop("oxapay_usd_amount", None)
    context.user_data.pop("oxapay_awaiting_custom", None)
    return ConversationHandler.END


# ──────────────────────────────────────────────────────────────────────────────
# Build and return the ConversationHandler
# ──────────────────────────────────────────────────────────────────────────────

def _build_conversation_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(oxapay_entry, pattern=r"^deposit_oxapay$"),
        ],
        states={
            _STATE_AMOUNT: [
                CallbackQueryHandler(oxapay_preset_amount,         pattern=r"^oxapay_amt_\d+"),
                CallbackQueryHandler(oxapay_custom_amount_prompt,  pattern=r"^oxapay_amt_custom$"),
                CallbackQueryHandler(oxapay_back_to_amounts,       pattern=r"^oxapay_back_to_amounts$"),
            ],
            _STATE_CUSTOM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, oxapay_receive_custom_amount),
                CallbackQueryHandler(oxapay_back_to_amounts,       pattern=r"^oxapay_back_to_amounts$"),
            ],
            _STATE_CURRENCY: [
                CallbackQueryHandler(oxapay_currency_selected,     pattern=r"^oxapay_cur_"),
                CallbackQueryHandler(oxapay_back_to_amounts,       pattern=r"^oxapay_back_to_amounts$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", oxapay_cancel),
            CallbackQueryHandler(oxapay_cancel, pattern=r"^cancel$"),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
        conversation_timeout=timedelta(minutes=10).total_seconds(),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Public registration function — call from bot.py
# ──────────────────────────────────────────────────────────────────────────────

def register_oxapay_handlers(app: Application) -> None:
    """
    Register all OxaPay handlers into the Telegram Application.

    Call this from main() in bot.py after the app is built, BEFORE the
    generic MessageHandler catchall, e.g.:

        from newdepositmethods import register_oxapay_handlers, start_oxapay_webhook
        ...
        register_oxapay_handlers(app)
    """
    app.add_handler(_build_conversation_handler())
    logging.info("[OxaPay] Handlers registered.")
