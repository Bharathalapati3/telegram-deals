# Telegram Deal Watcher → Affiliate Appender → Auto-Poster (India)

This bot reads Indian deal channels on Telegram, keeps only the good deals and
price errors, swaps every link for **your** affiliate link, and posts the deals
to **your** channel in your own format. It runs 24/7 on a small Linux server and
restarts itself after errors.

**It starts with Amazon only.** Flipkart, Myntra, Ajio and other stores are
already recognised. To add one later you change one line of config, or send one
command to the bot (see [Enabling more stores](#enabling-more-stores-later)).

```
source channels ──► reader (your 2nd account) ──► extract ─► expand short links ─► identify store/ASIN
        ─► filter & score ─► dedupe ─► convert to MY affiliate link ─► format ─► auto-post / ask admin
```

> ⚠️ **Use a separate Telegram account for reading** (not your personal one).
> Telegram can limit accounts that join many channels. The `.session` file
> gives full access to that account, so keep it secret.

---

## Assumptions

- Python 3.11+ on a Linux VPS. Docker is recommended; systemd also works.
- The reader account is a normal Telegram user account that you own. It joins the
  source channels itself.
- Your bot (from @BotFather) is an admin of your channel with **Post** and **Edit** rights.
- Times (quiet hours, daily summary, "posts today") use **IST** (`TZ_NAME`).
- Only **Amazon.in** is enabled at first. Amazon links are rebuilt as
  `https://www.amazon.in/dp/{ASIN}?tag=YOURTAG`, so no API is needed.
- EarnKaro and Cuelinks are **stubs**: the endpoint, auth and field names must
  come from your own account's API docs. No endpoint is made up in the code.
- Product pages are never scraped. The bot only follows redirects, and stops as
  soon as a link points at a store.
- The post is rebuilt from the facts it found (title, prices, coupon, bank offer).
  Source text is never copied, so other channels' branding can't get through.
- Discounts are computed conservatively. If "% off" and MRP disagree, the lower
  number is used. MRP is never made up.

---

## 1. Get API_ID and API_HASH

1. Log in at <https://my.telegram.org> with the **reader** account's phone number.
2. Open **API development tools** and create an app (any name, platform "Other").
3. Copy **api_id** and **api_hash**.

## 2. Create the bot

1. In Telegram, open **@BotFather**, send `/newbot` and follow the steps.
2. Copy the **token** (looks like `123456:ABC-…`).
3. Open your new bot and press **Start**. A bot can only message people who
   have started it, so every admin must do this.

## 3. Add the bot to your channel

1. Open your channel → **Admins** → **Add admin** → search for your bot.
2. Turn on **Post messages** and **Edit messages of others**.
3. For a **private** channel, use its numeric id (`-100…`) in `TARGET_CHANNEL`.
   After starting the bot, post any message in the channel once so the bot
   "sees" it.

## 4. Find your admin user id

Send any message to **@userinfobot**. It replies with your numeric id. Put it in
`ADMIN_IDS` (comma-separate several ids).

## 5. Configure

```bash
git clone <your repo> telegram-deals && cd telegram-deals
cp .env.example .env
cp config.example.yaml config.yaml
nano .env          # API_ID, API_HASH, BOT_TOKEN, ADMIN_IDS, TARGET_CHANNEL, AMAZON_TAG
nano config.yaml   # sources, thresholds, brand lists, templates
```

Every setting is explained in the comments in those two files. The most
important ones:

| What | Where |
|---|---|
| Channels to watch | `config.yaml` → `sources` (also `/addsource`) |
| Stores to post | `config.yaml` → `enabled_stores` (also `/enablestore`) |
| Minimum discount | `filters.min_discount` (also `/setmin`) |
| Auto-post brands | `filters.brand_allowlist` (also `/allow`) |
| Posting mode | `posting.mode` (also `/mode`) |
| Post style | `templates` |

Check your config with:

```bash
python main.py --check
```

## 6. First-time login of the reader account (phone + OTP)

Do this once, on the server:

```bash
# Docker
docker compose build
docker compose run --rm dealbot python login.py

# without Docker
python3 -m venv venv && venv/bin/pip install -r requirements.txt
venv/bin/python login.py
```

Enter the phone number (+91…), then the code Telegram sends to that account, then
your 2-step password if you have one. This creates `data/user.session`.

**Code not arriving?** Log in with a QR code instead (no code needed):

```bash
docker compose run --rm dealbot python login.py --qr     # or: venv/bin/python login.py --qr
```

On the phone where the reader account is logged in, open Telegram → **Settings →
Devices → Link Desktop Device** and scan the QR code shown in the terminal. Make
the terminal window big enough to show the whole square.

## 7. Run it

**Docker (recommended)**

```bash
docker compose up -d
docker compose logs -f
```

**systemd**

```bash
sudo useradd -m dealbot          # if you want a dedicated user
# put the project in /home/dealbot/telegram-deals and create the venv there
sudo cp deploy/dealbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dealbot
journalctl -u dealbot -f
```

**Plain (for testing)**: `venv/bin/python main.py`

When the bot starts it messages you: "🤖 Deal bot started".

---

## Using the admin bot

| Command | What it does |
|---|---|
| `/status` | uptime, mode, posts today, queue, pending approvals, errors |
| `/mode auto\|approval\|mixed` | **auto**: post everything that passes. **approval**: ask you every time. **mixed** (default): auto-post high-score deals from allowed brands with converted links, ask about the rest |
| `/pause` / `/resume` | stop or start channel posting (deals are still collected) |
| `/sources`, `/addsource`, `/removesource` | manage source channels (username, invite link or id) |
| `/stores`, `/enablestore`, `/disablestore` | choose which stores get posted |
| `/setmin 60` | minimum % off for normal deals |
| `/block xyz` / `/allow brand` | block a brand/keyword; allow a brand for auto-posting |
| `/stats` | per source: seen / passed / posted, so you can drop bad sources |
| `/test <message>` | run the full pipeline **without posting** and show the result (or reply `/test` to a forwarded message) |
| `/lastskipped` | why recent deals were rejected |
| `/scan [count] [hours]` | read messages **already** in the sources now (default: last 20 per source, up to 12 h old). New messages are read automatically |
| `/queue` | deals waiting to be posted |
| `/over <post id>` | edit a channel post to say **❌ DEAL OVER** |

**Approvals.** You get a preview of the post plus buttons:
**✅ Post | ⚠️ Post as Price Error | ✏️ Edit text | ❌ Skip**.
For *Edit*, send your new text (HTML allowed). If you leave links out, your
affiliate links are added for you. Text containing anyone else's links or
@mentions is refused. Approvals expire (20 min for price errors, 2 h for others)
and the buttons change to "Expired".

**After posting**, the bot DMs you a "❌ Mark DEAL OVER" button for each post.

**Posting rules**: at most `max_posts_per_hour`, at least `min_gap_seconds`
apart. Price errors go to the front of the queue and can skip these limits.
During quiet hours (01:00–07:00 IST by default) normal deals wait; price errors
still post. Queued deals that get too old are dropped instead of posted late.

## Enabling more stores later

1. Get API access from your affiliate network (EarnKaro, Cuelinks…). Fill in its
   block in `.env` from **their** docs and set `AFFILIATE_NETWORK`.
2. Send `/enablestore flipkart` to the bot, or add `flipkart` to
   `enabled_stores` in `config.yaml`.
3. Check with `/test <a flipkart deal>`.

If a link can't be converted, `conversion.on_fail` decides what happens: `skip`,
`approval` (default) or `clean_link`. Someone else's affiliate tag is **never**
posted. Every post is checked before sending, and a post with any link that
isn't yours is blocked.

To add a whole new network, see [docs/ADDING_A_CONVERTER.md](docs/ADDING_A_CONVERTER.md).

---

## Updating

```bash
git pull
docker compose up -d --build          # Docker
# or
venv/bin/pip install -r requirements.txt && sudo systemctl restart dealbot
```

Database changes are applied automatically on start. Your settings, queue and
pending approvals are kept.

## Logs

- `data/logs/bot.log`: everything (rotates at 5 MB, keeps 5 files)
- `data/logs/errors.log`: warnings and errors only
- `docker compose logs -f` or `journalctl -u dealbot -f`
- Set `LOG_LEVEL=DEBUG` in `.env` for more detail, or `LOG_FORMAT=json` for JSON lines.

Each deal is logged on one line with its decision and reasons, for example:
`deal source=Loot Deals msg=1234 action=skip label=SKIP score=0 reasons=discount_20%<min_50%`.

## Troubleshooting

| Problem | Fix |
|---|---|
| `CONFIG: API_ID / API_HASH missing` | Fill in `.env`. With Docker, check `env_file: .env` is next to `docker-compose.yml`. |
| Login code never arrives | Use `python login.py --qr` and scan the code from Telegram → Settings → Devices → Link Desktop Device. |
| Bot says "reader account session is invalid" | Run `login.py` again (step 6), then restart. This happens if you logged that account out of "Active sessions". |
| "I cannot access the target channel" | Make the bot an admin of the channel. For private channels use the `-100…` id and post one message there after the bot starts. |
| You get no DMs from the bot | Open the bot and press **Start**. Check that `ADMIN_IDS` is your numeric id. |
| `/sources` shows ❌ for a channel | Wrong username, an expired invite link, or the reader account was banned from it. |
| No deals posted | Use `/lastskipped` and `/test`. The usual reasons are a minimum discount set too high, `mixed` mode with an empty brand allowlist (deals go to approval instead), quiet hours, or `/pause`. |
| `FloodWaitError` in the logs | Telegram rate limit. The bot waits the required time on its own. If it happens often, watch fewer channels or lower `max_posts_per_hour`. |
| `database is locked` | Two copies of the bot are running. Stop one. |
| Short links "unresolved" | The shortener is down or blocking the server. Results are cached and retried later. You can add domains under `expansion.extra_shortener_domains`. |

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

The tests mock all network calls. They cover price/discount extraction (English,
Hinglish, emojis, fancy fonts), every ASIN URL shape, tracking-param stripping,
removal of other people's affiliate tags, price-error detection, dedupe, post
length limits, the full pipeline, and the posting queue.

### Project layout

```
main.py            app wiring, signals, graceful shutdown
login.py           one-time reader login (phone + OTP)
config.py          .env + config.yaml loading, runtime overrides
reader.py          user client: sources, albums, edits, lazy photo download
extract.py         URLs, prices, MRP, % off, coupons, bank offers, title
links.py           short-link expansion (cached), store detection, ASIN, cleaning
converters/        base.py (interface + generic HTTP API), amazon.py, earnkaro.py, cuelinks.py
filters.py         scoring & labels (PRICE_ERROR / HOT_DEAL / NORMAL / SKIP)
dedupe.py          same product / similar title within a time window
formatter.py       post template, Telegram length limits, "only my links" check
pipeline.py        runs all the steps above (also used by /test)
poster.py          persistent queue, rate limits, quiet hours, DEAL OVER edits
admin.py           admin commands, approval buttons, edit flow, expiry
health.py          daily summary, "no messages" alert, cleanup
db.py              SQLite schema + migrations
utils.py           time zones, error tracker, task supervisor
```

## Legal and safety notes

- **#ad disclosure**: ASCI guidelines in India require it. The default template
  adds "#ad — we may earn a commission". Keep it.
- Follow the Amazon Associates operating agreement and your network's rules.
  Price errors may be cancelled by the store, and the post header says so.
- Never commit `.env`, `config.yaml` or `*.session` files. They are in `.gitignore`.
