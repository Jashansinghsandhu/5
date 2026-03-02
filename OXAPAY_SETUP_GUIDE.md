# OxaPay Deposit System — Setup Guide

This guide explains every manual step you need to take to get the
**newdepositmethods.py** OxaPay integration working with your bot on a
DigitalOcean droplet.

---

## 1. OxaPay Merchant Account

1. Sign up or log in at <https://oxapay.com>.
2. Go to **Merchants → Create Merchant** and note your **Merchant API Key**.
3. Under **Merchant Settings → Webhook**, configure the webhook URL:
   ```
   https://yourdomain.com/oxapay/webhook
   ```
   (Replace `yourdomain.com` with your actual domain.)
4. Make sure the currencies you want to accept (BTC, ETH, USDT, etc.) are
   **enabled** in your merchant settings.

---

## 2. Configure newdepositmethods.py

Open `newdepositmethods.py` and edit the configuration block at the top:

```python
OXAPAY_MERCHANT_KEY  = "YOUR_MERCHANT_KEY_HERE"   # From step 1
OXAPAY_WEBHOOK_HOST  = "https://yourdomain.com"    # Your domain (no trailing /)
OXAPAY_WEBHOOK_PORT  = 8443                        # Port to run the webhook listener on
OXAPAY_WEBHOOK_PATH  = "/oxapay/webhook"           # Must match OxaPay merchant settings
OXAPAY_MIN_DEPOSIT_USD = 5.0                       # Minimum deposit in USD
OXAPAY_SUPPORTED_CURRENCIES = ["BTC", "ETH", "USDT", "LTC", "TRX", "BNB", "SOL"]
```

**Important:** `OXAPAY_SUPPORTED_CURRENCIES` must only include currencies that
are **both** enabled in your OxaPay merchant account **and** present in the
bot's `SUPPORTED_CRYPTOS` list in `bot.py`.

---

## 3. Link newdepositmethods.py into bot.py

Add **two lines** to `bot.py`:

### 3a. Import (near the top of bot.py, after other imports)

```python
from newdepositmethods import register_oxapay_handlers, start_oxapay_webhook
```

### 3b. Register handlers (inside `main()`, right before or after the existing deposit handlers)

Find this block in `main()`:
```python
    # ===== DEPOSIT SYSTEM HANDLERS =====
    app.add_handler(CommandHandler("deposit", deposit_command))
    app.add_handler(CallbackQueryHandler(deposit_method_callback, ...))
    ...
```

Add this line **at the end of that block**:
```python
    register_oxapay_handlers(app)
```

### 3c. Start the webhook server (inside `post_init`)

Find the `post_init` function:
```python
async def post_init(application: Application):
    application.create_task(active_scans_monitor_task(application))
    ...
```

Add this line at the end:
```python
    application.create_task(start_oxapay_webhook(application))
```

### 3d. Add the OxaPay button to the deposit menu

In the `build_deposit_menu()` function in `bot.py`, add one button to
`keyboard_rows` (before the History/Back row):

```python
    # OxaPay button — placed after the existing chain buttons
    keyboard_rows.append([
        apply_button_style(
            InlineKeyboardButton("⚡ OxaPay (Card / Crypto)", callback_data="deposit_oxapay"),
            "primary"
        )
    ])
```

---

## 4. Open the webhook port on your DigitalOcean droplet

The aiohttp server binds to `0.0.0.0:8443` (or whichever port you set).

### UFW (Ubuntu firewall)

```bash
sudo ufw allow 8443/tcp
sudo ufw reload
```

### Without UFW

```bash
sudo iptables -I INPUT -p tcp --dport 8443 -j ACCEPT
```

---

## 5. HTTPS — use Nginx as a reverse proxy (recommended)

OxaPay requires the webhook URL to use **HTTPS**. The easiest approach is:

1. Get a free TLS certificate with Certbot:
   ```bash
   sudo apt install certbot python3-certbot-nginx
   sudo certbot --nginx -d yourdomain.com
   ```

2. Add an Nginx `location` block to forward `/oxapay/webhook` to the local
   aiohttp server:

   ```nginx
   server {
       listen 443 ssl;
       server_name yourdomain.com;
       # ... certbot SSL block ...

       location /oxapay/webhook {
           proxy_pass         http://127.0.0.1:8443;
           proxy_set_header   Host $host;
           proxy_set_header   X-Real-IP $remote_addr;
           proxy_read_timeout 30s;
       }
   }
   ```

3. Reload Nginx:
   ```bash
   sudo nginx -t && sudo systemctl reload nginx
   ```

> **Note:** If you choose to expose port 8443 directly (no Nginx), you must
> configure aiohttp with an SSL context using your certificate files.
> In that case, ensure `OXAPAY_WEBHOOK_PORT` in `newdepositmethods.py` matches
> the port Nginx is forwarding to (default: 8443).

---

## 6. Verify the integration

1. Start the bot: `python bot.py`
2. Look for the log line:
   ```
   [OxaPay] Webhook server listening on 0.0.0.0:8443/oxapay/webhook
   ```
3. Use `/deposit` in Telegram → tap **⚡ OxaPay** → select an amount and
   currency → tap **Pay Now via OxaPay** → complete a test payment using
   OxaPay's sandbox key (`"sandbox"`).
4. Watch the bot logs for:
   ```
   [OxaPay] Webhook received: {...}
   [OxaPay] Credited 0.00012345 BTC ($5.00) to user 123456789
   ```
5. Your Telegram balance should increase.

---

## 7. Production checklist

- [ ] Replace `OXAPAY_MERCHANT_KEY = "sandbox"` with your real key.
- [ ] Replace `OXAPAY_WEBHOOK_HOST` with your actual HTTPS domain.
- [ ] Confirm the webhook URL in OxaPay merchant settings matches exactly.
- [ ] Port 8443 is open in your firewall (UFW / DigitalOcean Cloud Firewall).
- [ ] Nginx is configured to proxy `/oxapay/webhook` → `localhost:8443`.
- [ ] `OXAPAY_SUPPORTED_CURRENCIES` only contains coins enabled in OxaPay.
- [ ] The bot logs show `[OxaPay] Handlers registered.` and
      `[OxaPay] Webhook server listening on ...` at startup.

---

## 8. How balance crediting works

When OxaPay calls your webhook:

1. The HMAC-SHA256 signature is verified against `OXAPAY_MERCHANT_KEY`.
2. Duplicate callbacks (same `orderId`) are silently ignored.
3. `credit_wallet_crypto(telegram_id, pay_amount, currency)` is called —
   this adds the **exact crypto amount** the user paid into their wallet (no
   conversion; same as the on-chain deposit system).
4. `user_stats["unwagered_deposit"]` is incremented for bonus tracking.
5. A referral commission (0.5%) is credited to the referrer if applicable.
6. `save_user_data(telegram_id)` persists the updated wallet to disk.
7. The user receives a Telegram DM confirming the deposit.
