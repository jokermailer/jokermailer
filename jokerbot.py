import os
import json
import smtplib
import ssl
import logging
import re
from pathlib import Path
from html.parser import HTMLParser
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.error import NetworkError
from telegram.helpers import escape_markdown

load_dotenv()

TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN")
TEMPLATES_DIR      = Path("templates")
ALLOWED_USERS_FILE = Path("allowed_users.json")
USER_REGISTRY_FILE = Path("user_registry.json")

# ── Multi-SMTP: reads SMTP_1_HOST/USER/PASS, SMTP_2_HOST/USER/PASS, etc. ──
def _load_smtp_servers() -> list:
    servers = []
    i = 1
    while True:
        host = os.getenv(f"SMTP_{i}_HOST")
        user = os.getenv(f"SMTP_{i}_USER")
        pw   = os.getenv(f"SMTP_{i}_PASS")
        name = os.getenv(f"SMTP_{i}_NAME", f"SMTP {i}")
        if not host:
            break
        servers.append({"name": name, "host": host, "user": user, "pass": pw,
                        "online": False, "last_err": ""})
        i += 1
    # Backwards compat: single MAILGUN_* vars
    if not servers:
        h = os.getenv("MAILGUN_SMTP")
        u = os.getenv("MAILGUN_USER")
        p = os.getenv("MAILGUN_PASS")
        if h:
            servers.append({"name": "Mailgun", "host": h, "user": u, "pass": p,
                            "online": False, "last_err": ""})
    return servers

SMTP_SERVERS: list = _load_smtp_servers()

ADMIN_ID = 6197474466   # HBXX8 — always has access, cannot be removed

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

user_data = {}


# ═══════════════════════════════════════════
# BRAND
# ═══════════════════════════════════════════

JM = "🃏 *JOKER MAILER*\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
JM_DIV = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"


# ═══════════════════════════════════════════
# SMTP HEALTH CHECK
# ═══════════════════════════════════════════

def test_smtp(server: dict) -> bool:
    """Try to login to an SMTP server. Returns True if healthy."""
    for port, use_ssl in [(587, False), (465, True)]:
        conn = None
        try:
            if use_ssl:
                ctx  = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                conn = smtplib.SMTP_SSL(server["host"], port, timeout=10, context=ctx)
            else:
                conn = smtplib.SMTP(server["host"], port, timeout=10)
                conn.ehlo(); conn.starttls()
            conn.ehlo()
            conn.login(server["user"], server["pass"])
            conn.quit()
            return True
        except Exception as e:
            server["last_err"] = str(e)
            try: conn.close()
            except Exception: pass
    return False


def check_all_smtp() -> None:
    """Test every configured SMTP and update their .online status."""
    for srv in SMTP_SERVERS:
        srv["online"] = test_smtp(srv)
        status = "✅ online" if srv["online"] else f"❌ offline ({srv['last_err'][:60]})"
        logger.info(f"  SMTP [{srv['name']}] {status}")


def smtp_status_line() -> str:
    """One-line summary: '2/3 SMTP online'"""
    online = sum(1 for s in SMTP_SERVERS if s["online"])
    return f"{online}/{len(SMTP_SERVERS)} SMTP online"


def smtp_status_block() -> str:
    """Multi-line block for the startup screen."""
    if not SMTP_SERVERS:
        return "⚠️ No SMTP servers configured"
    lines = []
    for s in SMTP_SERVERS:
        icon = "✅" if s["online"] else "❌"
        lines.append(f"  {icon} `{s['name']}`")
    return "\n".join(lines)


# ═══════════════════════════════════════════
# ALLOWED USERS
# ═══════════════════════════════════════════

def load_allowed_users() -> set:
    try:
        if ALLOWED_USERS_FILE.exists():
            data = json.loads(ALLOWED_USERS_FILE.read_text(encoding='utf-8'))
            users = set(int(uid) for uid in data)
            users.add(ADMIN_ID)
            return users
    except Exception as e:
        logger.error(f"Could not load allowed_users.json: {e}")
    return {ADMIN_ID}

def save_allowed_users() -> None:
    try:
        ALLOWED_USERS_FILE.write_text(
            json.dumps(sorted(list(allowed_users))), encoding='utf-8')
    except Exception as e:
        logger.error(f"Could not save allowed_users.json: {e}")

allowed_users: set = load_allowed_users()

def is_allowed(user_id: int) -> bool:
    return user_id in allowed_users

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


# ═══════════════════════════════════════════
# USER REGISTRY
# ═══════════════════════════════════════════

def load_user_registry() -> dict:
    try:
        if USER_REGISTRY_FILE.exists():
            return json.loads(USER_REGISTRY_FILE.read_text(encoding='utf-8'))
    except Exception as e:
        logger.error(f"Could not load user_registry.json: {e}")
    return {}

def save_user_registry() -> None:
    try:
        USER_REGISTRY_FILE.write_text(
            json.dumps(user_registry, indent=2), encoding='utf-8')
    except Exception as e:
        logger.error(f"Could not save user_registry.json: {e}")

user_registry: dict = load_user_registry()

def register_user(user) -> None:
    uid   = str(user.id)
    label = (f"@{user.username}" if user.username
             else (user.first_name or str(user.id)))
    if user_registry.get(uid) != label:
        user_registry[uid] = label
        save_user_registry()

def user_label(uid: int) -> str:
    name = user_registry.get(str(uid))
    return f"{name} ({uid})" if name else str(uid)


# ═══════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════

def md_safe(text: str) -> str:
    return escape_markdown(str(text), version=1)

def get_user_data(user_id: int) -> dict:
    if user_id not in user_data:
        user_data[user_id] = {'favorites': []}
    return user_data[user_id]


class _TextExtractor(HTMLParser):
    SKIP  = {'script', 'style', 'head', 'meta', 'link', 'title'}
    BLOCK = {'p', 'div', 'tr', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'td', 'br', 'hr'}

    def __init__(self):
        super().__init__()
        self._skip  = False
        self._parts = []

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t in self.SKIP:    self._skip = True
        elif t in self.BLOCK: self._parts.append('\n')

    def handle_endtag(self, tag):
        if tag.lower() in self.SKIP:
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            s = data.strip()
            if s: self._parts.append(s)


def html_to_text(html: str) -> str:
    try:
        p = _TextExtractor()
        p.feed(html)
        raw = '\n'.join(p._parts)
        raw = re.sub(r' *\n +', '\n', raw)
        raw = re.sub(r'\n{3,}', '\n\n', raw)
        return raw.strip()
    except Exception:
        return re.sub(r'<[^>]+>', ' ', html).strip()


def format_template_name(filename: str) -> str:
    name = Path(filename).stem.replace('_', ' ').replace('-', ' ')
    return ' '.join(w.capitalize() for w in name.split())


# ═══════════════════════════════════════════
# TEMPLATE LOADING
# ═══════════════════════════════════════════

def load_templates_from_files():
    templates = {
        "BANKS":     {"display_name": "🏦 BANKS",     "countries": {}},
        "CRYPTO":    {"display_name": "🪙 CRYPTO",    "types": {}},
        "AUTHORITY": {"display_name": "🏛️ AUTHORITY", "items": {}},
    }

    def load_file(f):
        try:
            if f.suffix == '.json':
                data = json.loads(f.read_text(encoding='utf-8'))
                return {"id": data.get('id', f.stem),
                        "name": data.get('name', format_template_name(f.name)),
                        "body": data.get('body', '')}
            else:
                return {"id": f.stem,
                        "name": format_template_name(f.name),
                        "body": f.read_text(encoding='utf-8')}
        except Exception as e:
            logger.error(f"Failed to load {f}: {e}")
            return None

    banks_path = TEMPLATES_DIR / "banks"
    if banks_path.exists():
        for country_folder in banks_path.iterdir():
            if not country_folder.is_dir(): continue
            cn    = country_folder.name
            emoji = ("🇳🇿" if "New Zealand" in cn else
                     "🇺🇸" if "USA"         in cn else
                     "🇬🇧" if "UK"          in cn else "🇦🇺")
            templates["BANKS"]["countries"].setdefault(cn, {"emoji": emoji, "items": {}})
            for bank_folder in country_folder.iterdir():
                if not bank_folder.is_dir(): continue
                bn = bank_folder.name
                templates["BANKS"]["countries"][cn]["items"].setdefault(bn, {"templates": []})
                for tf in bank_folder.glob("*"):
                    if tf.is_file() and tf.suffix in ('.json', '.html', '.txt'):
                        t = load_file(tf)
                        if t and t.get('body'):
                            templates["BANKS"]["countries"][cn]["items"][bn]["templates"].append(t)

    crypto_path = TEMPLATES_DIR / "crypto"
    if crypto_path.exists():
        for type_folder in crypto_path.iterdir():
            if not type_folder.is_dir(): continue
            ct = type_folder.name
            templates["CRYPTO"]["types"].setdefault(ct, {"display_name": f"💱 {ct}", "items": {}})
            for item_folder in type_folder.iterdir():
                if not item_folder.is_dir(): continue
                itn = item_folder.name
                templates["CRYPTO"]["types"][ct]["items"].setdefault(itn, {"templates": []})
                for tf in item_folder.glob("*"):
                    if tf.is_file() and tf.suffix in ('.json', '.html', '.txt'):
                        t = load_file(tf)
                        if t and t.get('body'):
                            templates["CRYPTO"]["types"][ct]["items"][itn]["templates"].append(t)

    auth_path = TEMPLATES_DIR / "authority"
    if auth_path.exists():
        for svc_folder in auth_path.iterdir():
            if not svc_folder.is_dir(): continue
            sn = svc_folder.name
            templates["AUTHORITY"]["items"].setdefault(sn, {"templates": []})
            for tf in svc_folder.glob("*"):
                if tf.is_file() and tf.suffix in ('.json', '.html', '.txt'):
                    t = load_file(tf)
                    if t and t.get('body'):
                        templates["AUTHORITY"]["items"][sn]["templates"].append(t)

    return templates


TEMPLATES = load_templates_from_files()


def find_template(template_id: str):
    for cat_key, cat in TEMPLATES.items():
        if cat_key == "BANKS":
            for cd in cat["countries"].values():
                for itd in cd["items"].values():
                    for t in itd["templates"]:
                        if t['id'] == template_id: return t
        elif cat_key == "CRYPTO":
            for td in cat["types"].values():
                for itd in td["items"].values():
                    for t in itd["templates"]:
                        if t['id'] == template_id: return t
        elif cat_key == "AUTHORITY":
            for itd in cat["items"].values():
                for t in itd["templates"]:
                    if t['id'] == template_id: return t
    return None


# ═══════════════════════════════════════════
# ACCESS GUARD
# ═══════════════════════════════════════════

async def deny(update: Update):
    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if update.callback_query:
        try:
            await update.callback_query.answer("🔒 Access denied", show_alert=True)
        except Exception:
            pass
    if msg:
        await msg.reply_text(
            f"{JM}"
            "🔒 *Access Denied*\n\n"
            "This bot is invite-only.\n"
            "Contact the admin to request access.",
            parse_mode='Markdown')


# ═══════════════════════════════════════════
# EMAIL VALIDATION
# ═══════════════════════════════════════════

def validate_sender_email(email: str):
    if '@' not in email:
        return False, "Must contain @ — e.g. security@anz"
    local, domain = email.rsplit('@', 1)
    if not local:  return False, "Missing local part — e.g. security@anz"
    if not domain: return False, "Missing domain — e.g. security@anz"
    return True, "Valid"


# ═══════════════════════════════════════════
# CANCEL EMAIL FLOW
# ═══════════════════════════════════════════

def cancel_email_flow(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data['awaiting_email'] = False
    context.user_data.pop('admin_step', None)
    for k in ['email_step', 'email_recipient', 'email_subject', 'email_body',
              'sender_name', 'sender_email', 'reply_to_email',
              'selected_template_id', 'raw_body', 'pending_vars', 'filled_vars',
              'template_list_back_cb', 'custom_email_mode']:
        # note: 'edit_snapshot' is intentionally NOT cleared here —
        # cancel_edit needs it after calling cancel_email_flow
        context.user_data.pop(k, None)


# ═══════════════════════════════════════════
# CONFIRM SCREEN
# ═══════════════════════════════════════════

async def show_confirm_screen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show a full review of all email details. Always called before sending."""
    cd          = context.user_data
    template_id = cd.get('selected_template_id', '')
    template    = find_template(template_id)
    if cd.get('custom_email_mode'):
        tname = "✍️ Custom Email"
    else:
        tname = md_safe(template['name'] if template else template_id)

    sender      = md_safe(cd.get('sender_name',    '—'))
    sender_mail = md_safe(cd.get('sender_email',   '—'))
    reply_to    = md_safe(cd.get('reply_to_email', '—'))
    recipient   = md_safe(cd.get('email_recipient','—'))
    subject     = md_safe(cd.get('email_subject',  '—'))

    filled = cd.get('filled_vars', {})
    extra_block = ""
    if cd.get('custom_email_mode'):
        # Show a preview of the custom body
        raw_body  = cd.get('email_body', '')
        preview   = raw_body[:200].strip()
        if len(raw_body) > 200:
            preview += "…"
        extra_block = f"\n\n📝 *Body preview:*\n`{md_safe(preview)}`"
    elif filled:
        vars_lines  = "\n".join(f"  `{md_safe(k)}` → `{md_safe(v)}`" for k, v in filled.items())
        extra_block = f"\n\n📝 *Placeholders filled:*\n{vars_lines}"

    # cancel goes back to mailer hub for custom email, template detail otherwise
    cancel_cb = "cancel_custom_email" if cd.get('custom_email_mode') else f"cancel_email_{template_id}"
    edit_cb   = "confirm_edit_custom" if cd.get('custom_email_mode') else f"confirm_edit_{template_id}"

    text = (
        f"{JM}"
        "📋 *Review Before Sending*\n\n"
        f"📧 *Type:*          `{tname}`\n"
        f"{JM_DIV}\n"
        f"👤 *Sender name:*   `{sender}`\n"
        f"📤 *Sender email:*  `{sender_mail}`\n"
        f"↩️  *Reply-to:*      `{reply_to}`\n"
        f"📬 *Recipient:*     `{recipient}`\n"
        f"📌 *Subject:*       `{subject}`"
        f"{extra_block}\n\n"
        f"{JM_DIV}\n"
        "Does everything look correct?\n\n"
        "✅ Tap *Send* to fire it off\n"
        "✏️  Tap *Edit* to start over\n"
        "❌ Tap *Cancel* to abort"
    )

    kb = [
        [InlineKeyboardButton("✅  Send Now",  callback_data="confirm_send")],
        [InlineKeyboardButton("✏️  Edit",       callback_data=edit_cb)],
        [InlineKeyboardButton("❌  Cancel",     callback_data=cancel_cb)],
    ]

    if update.message:
        await update.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
    else:
        await update.callback_query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')


# ═══════════════════════════════════════════
# MAIN MENU
# ═══════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_allowed(user.id):
        await deny(update)
        return

    register_user(user)
    cancel_email_flow(context)

    uname      = md_safe(user.username or str(user.id))
    first_name = md_safe(user.first_name or "User")
    admin_tag  = " 👑" if is_admin(user.id) else ""

    text = (
        f"{JM}"
        f"👋 Welcome back, *{first_name}*{admin_tag}\n"
        f"🪪 `@{uname}`\n\n"
        f"{JM_DIV}\n"
        "📖 *HOW TO USE*\n"
        f"{JM_DIV}\n\n"
        "1️⃣  Tap *📧 Mailer* below\n"
        "2️⃣  Choose *🏦 Banks*, *🪙 Crypto* or *🏛️ Authority*\n"
        "3️⃣  Pick a template and preview it\n"
        "4️⃣  Tap *Send Email* and fill in each step\n"
        "5️⃣  Fill in any template placeholders\n"
        "6️⃣  *Review all details* on the confirm screen\n"
        "7️⃣  Tap *Send Now* — done ✅\n\n"
        "⭐ Star any template to save it in *Favourites*\n\n"
        f"{JM_DIV}\n"
        f"📡 *{smtp_status_line()}*\n"
        "🔒 *Invite-only* — keep your access private\n"
        f"{JM_DIV}\n\n"
        "👇 *Select an option to get started:*"
    )

    kb = [[InlineKeyboardButton("📧  Mailer", callback_data="mailer")]]
    if is_admin(user.id):
        kb.append([InlineKeyboardButton("👑  Admin Panel", callback_data="admin_panel")])

    rm = InlineKeyboardMarkup(kb)
    if update.message:
        await update.message.reply_text(text, reply_markup=rm, parse_mode='Markdown')
    else:
        await update.callback_query.edit_message_text(text, reply_markup=rm, parse_mode='Markdown')


# ═══════════════════════════════════════════
# ADMIN PANEL
# ═══════════════════════════════════════════

async def show_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(update.effective_user.id):
        await deny(update)
        return

    non_admin = [uid for uid in sorted(allowed_users) if uid != ADMIN_ID]
    user_list = (
        "\n".join(f"  • `{md_safe(user_label(uid))}`" for uid in non_admin)
        if non_admin else "  _No users added yet_"
    )

    text = (
        f"{JM}"
        "👑 *Admin Panel*\n\n"
        f"{JM_DIV}\n"
        f"*Allowed Users ({len(non_admin)}):*\n"
        f"{user_list}\n\n"
        f"{JM_DIV}\n"
        "➕ *Add User* — grant access by Telegram ID\n"
        "➖ *Remove User* — revoke access\n"
        f"{JM_DIV}"
    )
    kb = [
        [InlineKeyboardButton("➕  Add User",     callback_data="admin_add")],
        [InlineKeyboardButton("➖  Remove User",  callback_data="admin_remove")],
        [InlineKeyboardButton("⬅️  Back to Menu", callback_data="back")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')


async def show_admin_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(update.effective_user.id):
        await deny(update)
        return
    context.user_data['admin_step'] = 'add_user'
    await query.edit_message_text(
        f"{JM}"
        "👑 *Admin · Add User*\n\n"
        "Send me the Telegram user ID to grant access.\n\n"
        "_Tip: they can find their ID using @userinfobot_",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌  Cancel", callback_data="admin_panel")]
        ]),
        parse_mode='Markdown')


async def show_admin_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(update.effective_user.id):
        await deny(update)
        return

    non_admin = [uid for uid in sorted(allowed_users) if uid != ADMIN_ID]
    if not non_admin:
        await query.answer("No users to remove.", show_alert=True)
        await show_admin_panel(update, context)
        return

    kb = [
        [InlineKeyboardButton(f"🗑  {user_label(uid)}", callback_data=f"admin_del_{uid}")]
        for uid in non_admin
    ]
    kb.append([InlineKeyboardButton("⬅️  Back", callback_data="admin_panel")])

    await query.edit_message_text(
        f"{JM}"
        "👑 *Admin · Remove User*\n\n"
        "Tap a user to revoke their access:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode='Markdown')


async def admin_delete_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(update.effective_user.id):
        await deny(update)
        return
    uid_str = query.data.replace("admin_del_", "")
    try:
        uid = int(uid_str)
        if uid == ADMIN_ID:
            await query.answer("Cannot remove the admin.", show_alert=True)
        elif uid in allowed_users:
            allowed_users.discard(uid)
            save_allowed_users()
            label = user_registry.get(str(uid), str(uid))
            await query.answer(f"Removed {label}")
        else:
            await query.answer("User not found.")
    except ValueError:
        await query.answer("Invalid ID.")
    await show_admin_panel(update, context)


# ═══════════════════════════════════════════
# MAILER HUB
# ═══════════════════════════════════════════

async def show_mailer_hub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await deny(update)
        return

    user  = update.effective_user
    uname = md_safe(user.first_name or user.username or str(user.id))

    text = (
        f"{JM}"
        "📧 *Mailer*\n\n"
        f"👤 *{uname}* — ready to send\n\n"
        f"{JM_DIV}\n"
        "📋 *Templates* — browse by Banks, Crypto & Authority\n\n"
        "⭐ *Favourites* — your starred templates\n\n"
        "✍️ *Custom Email* — write your own from scratch\n"
        f"{JM_DIV}\n\n"
        "👇 *What would you like to do?*"
    )
    kb = [
        [InlineKeyboardButton("📋  Templates",    callback_data="mailer_templates")],
        [InlineKeyboardButton("⭐  Favourites",   callback_data="mailer_favorites")],
        [InlineKeyboardButton("✍️  Custom Email",  callback_data="custom_email")],
        [InlineKeyboardButton("⬅️  Back to Menu", callback_data="back")],
    ]
    await update.callback_query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')


# ═══════════════════════════════════════════
# CATEGORIES / COUNTRIES / TYPES / ITEMS
# ═══════════════════════════════════════════

async def show_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await deny(update)
        return
    kb = []

    if TEMPLATES["BANKS"]["countries"]:
        bc = sum(len(c["items"]) for c in TEMPLATES["BANKS"]["countries"].values())
        tc = sum(len(i.get("templates", []))
                 for c in TEMPLATES["BANKS"]["countries"].values()
                 for i in c["items"].values())
        kb.append([InlineKeyboardButton(
            f"🏦 Banks  ·  {bc} banks  ·  {tc} templates",
            callback_data="cat_BANKS")])

    if TEMPLATES["CRYPTO"]["types"]:
        pc = sum(len(td["items"]) for td in TEMPLATES["CRYPTO"]["types"].values())
        tc = sum(len(i.get("templates", []))
                 for td in TEMPLATES["CRYPTO"]["types"].values()
                 for i in td["items"].values())
        kb.append([InlineKeyboardButton(
            f"🪙 Crypto  ·  {pc} platforms  ·  {tc} templates",
            callback_data="cat_CRYPTO")])

    if TEMPLATES["AUTHORITY"]["items"]:
        sc = len(TEMPLATES["AUTHORITY"]["items"])
        tc = sum(len(i.get("templates", [])) for i in TEMPLATES["AUTHORITY"]["items"].values())
        kb.append([InlineKeyboardButton(
            f"🏛️ Authority  ·  {sc} services  ·  {tc} templates",
            callback_data="cat_AUTHORITY")])

    kb.append([InlineKeyboardButton("⬅️  Back", callback_data="mailer")])
    await update.callback_query.edit_message_text(
        f"{JM}"
        "📋 *Templates* — pick a category:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode='Markdown')


async def show_crypto_types(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['current_category'] = "CRYPTO"
    kb = []
    for ctype, tdata in TEMPLATES["CRYPTO"]["types"].items():
        plat  = len(tdata["items"])
        tmpls = sum(len(i.get("templates", [])) for i in tdata["items"].values())
        if plat > 0:
            kb.append([InlineKeyboardButton(
                f"{tdata['display_name']}  ·  {plat} platforms  ·  {tmpls} templates",
                callback_data=f"type_{ctype}")])
    kb.append([InlineKeyboardButton("⬅️  Back", callback_data="mailer_templates")])
    await update.callback_query.edit_message_text(
        f"{JM}"
        "🪙 *Crypto* — pick a type:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode='Markdown')


async def show_bank_countries(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['current_category'] = "BANKS"
    kb = []
    for country, cdata in TEMPLATES["BANKS"]["countries"].items():
        banks = len(cdata["items"])
        tmpls = sum(len(i.get("templates", [])) for i in cdata["items"].values())
        if banks > 0:
            kb.append([InlineKeyboardButton(
                f"{cdata['emoji']} {country}  ·  {banks} banks  ·  {tmpls} templates",
                callback_data=f"country_BANKS_{country}")])
    kb.append([InlineKeyboardButton("⬅️  Back", callback_data="mailer_templates")])
    await update.callback_query.edit_message_text(
        f"{JM}"
        "🏦 *Banks* — pick a country:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode='Markdown')


async def show_items(update: Update, context: ContextTypes.DEFAULT_TYPE,
                     cb: str = None):
    query = update.callback_query
    kb    = []
    category    = None
    crypto_type = None
    data = cb if cb is not None else query.data

    if data.startswith("cat_"):
        category = data.replace("cat_", "")
        context.user_data['current_category'] = category
    elif data.startswith("type_"):
        crypto_type = data.replace("type_", "")
        context.user_data['current_type'] = crypto_type
        category = "CRYPTO"
    elif data.startswith("country_"):
        country  = data.replace("country_BANKS_", "")
        context.user_data['current_country'] = country
        category = "BANKS"

    if category == "BANKS":
        country     = context.user_data.get('current_country', 'USA')
        cdata       = TEMPLATES["BANKS"]["countries"][country]
        header_text = (
            f"{JM}"
            f"{cdata['emoji']} *{country} Banks* — pick a bank:"
        )
        for name, idata in cdata["items"].items():
            n = len(idata.get("templates", []))
            if n:
                kb.append([InlineKeyboardButton(f"🏦 {name}  ·  {n} templates",
                                                callback_data=f"item_BANKS_{country}_{name}")])
        kb.append([InlineKeyboardButton("⬅️  Back", callback_data="cat_BANKS")])

    elif category == "AUTHORITY":
        header_text = (
            f"{JM}"
            "🏛️ *Authority* — pick a service:"
        )
        for name, idata in TEMPLATES["AUTHORITY"]["items"].items():
            n = len(idata["templates"])
            if n:
                kb.append([InlineKeyboardButton(f"🏛️ {name}  ·  {n} templates",
                                                callback_data=f"item_AUTHORITY_{name}")])
        kb.append([InlineKeyboardButton("⬅️  Back", callback_data="mailer_templates")])

    elif category == "CRYPTO" and crypto_type:
        tdata       = TEMPLATES["CRYPTO"]["types"][crypto_type]
        header_text = (
            f"{JM}"
            f"{tdata['display_name']} — pick a platform:"
        )
        for name, idata in tdata["items"].items():
            n = len(idata.get("templates", []))
            if n:
                kb.append([InlineKeyboardButton(f"💱 {name}  ·  {n} templates",
                                                callback_data=f"item_CRYPTO_{crypto_type}_{name}")])
        kb.append([InlineKeyboardButton("⬅️  Back", callback_data="cat_CRYPTO")])

    else:
        header_text = f"{JM}Pick a category:"

    await query.edit_message_text(header_text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')


# ═══════════════════════════════════════════
# TEMPLATE LIST
# ═══════════════════════════════════════════

async def show_templates(update: Update, context: ContextTypes.DEFAULT_TYPE,
                         cb: str = None):
    query = update.callback_query
    uinfo = get_user_data(update.effective_user.id)
    data  = cb if cb is not None else query.data

    if data.startswith("item_BANKS_"):
        parts   = data.replace("item_BANKS_", "").split("_", 1)
        country = parts[0]
        bank    = parts[1] if len(parts) > 1 else ""
        context.user_data['current_item']    = bank
        context.user_data['current_country'] = country
        templates = TEMPLATES["BANKS"]["countries"][country]["items"][bank]["templates"]
        header    = f"🏦 *{md_safe(bank)}*  ·  {country}"
        back_cb   = f"country_BANKS_{country}"

    elif data.startswith("item_AUTHORITY_"):
        svc       = data.replace("item_AUTHORITY_", "")
        context.user_data['current_item'] = svc
        templates = TEMPLATES["AUTHORITY"]["items"][svc]["templates"]
        header    = f"🏛️ *{md_safe(svc)}*"
        back_cb   = "cat_AUTHORITY"

    elif data.startswith("item_CRYPTO_"):
        parts       = data.replace("item_CRYPTO_", "").split("_", 1)
        crypto_type = parts[0]
        item_name   = parts[1] if len(parts) > 1 else ""
        context.user_data['current_type'] = crypto_type
        context.user_data['current_item'] = item_name
        templates = TEMPLATES["CRYPTO"]["types"][crypto_type]["items"][item_name]["templates"]
        header    = f"💱 *{md_safe(item_name)}*"
        back_cb   = f"type_{crypto_type}"

    else:
        await query.answer("Unknown item.", show_alert=True)
        return

    context.user_data['template_list_back_cb'] = back_cb

    kb = []
    for t in templates:
        star = "⭐" if t["id"] in uinfo['favorites'] else "☆"
        name = t['name'] if t['name'] and t['name'].strip() else t['id']
        kb.append([InlineKeyboardButton(f"{star} {name}", callback_data=f"view_template_{t['id']}")])
    kb.append([InlineKeyboardButton("⬅️  Back", callback_data=back_cb)])

    await query.edit_message_text(
        f"{JM}"
        f"{header}\n\n"
        "Pick a template:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode='Markdown')


# ═══════════════════════════════════════════
# TEMPLATE DETAIL
# ═══════════════════════════════════════════

async def view_template(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        template_id: str = None):
    query       = update.callback_query
    uinfo       = get_user_data(update.effective_user.id)
    if template_id is None:
        template_id = query.data.replace("view_template_", "")
    template    = find_template(template_id)

    if not template:
        await query.answer("Template not found!", show_alert=True)
        return

    is_fav = template_id in uinfo['favorites']
    name   = template['name'] if template['name'] and template['name'].strip() else template_id

    body_text = html_to_text(template['body'])
    preview   = body_text[:350].strip()
    if len(body_text) > 350:
        preview += "…"

    back_cb = context.user_data.get('template_list_back_cb', 'mailer_templates')

    text = (
        f"{JM}"
        f"📧 *{md_safe(name)}*\n\n"
        f"{md_safe(preview)}"
    )

    kb = [
        [InlineKeyboardButton("✉️  Send Email",
                              callback_data=f"send_template_{template_id}")],
        [InlineKeyboardButton("⭐ Remove Fav" if is_fav else "☆ Add to Favourites",
                              callback_data=f"toggle_favorite_{template_id}")],
        [InlineKeyboardButton("⬅️  Back",
                              callback_data=f"back_to_item_{back_cb}")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')


async def toggle_favorite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query       = update.callback_query
    uinfo       = get_user_data(update.effective_user.id)
    template_id = query.data.replace("toggle_favorite_", "")

    if template_id in uinfo['favorites']:
        uinfo['favorites'].remove(template_id)
        await query.answer("Removed from favourites")
    else:
        uinfo['favorites'].append(template_id)
        await query.answer("⭐ Added to favourites")

    await view_template(update, context, template_id=template_id)


async def show_favorites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uinfo = get_user_data(update.effective_user.id)

    if not uinfo['favorites']:
        await query.edit_message_text(
            f"{JM}"
            "⭐ *Favourites*\n\n"
            "Nothing saved yet.\n"
            "Tap ☆ on any template to add it here.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️  Back", callback_data="mailer")]
            ]),
            parse_mode='Markdown')
        return

    kb = []
    for fav_id in uinfo['favorites']:
        t = find_template(fav_id)
        if t:
            kb.append([InlineKeyboardButton(f"⭐ {t['name']}", callback_data=f"view_template_{fav_id}")])
    kb.append([InlineKeyboardButton("⬅️  Back", callback_data="mailer")])

    await query.edit_message_text(
        f"{JM}"
        f"⭐ *Favourites*  ·  {len(uinfo['favorites'])} saved",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode='Markdown')


# ═══════════════════════════════════════════
# EMAIL FLOW — START
# ═══════════════════════════════════════════

async def send_email_from_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query       = update.callback_query
    template_id = query.data.replace("send_template_", "")
    template    = find_template(template_id)

    context.user_data['selected_template_id'] = template_id
    context.user_data['email_step']           = 'sender_name'
    context.user_data['awaiting_email']       = True

    name = template['name'] if template and template['name'] else template_id

    await query.edit_message_text(
        f"{JM}"
        f"✉️ *Send — {md_safe(name)}*\n\n"
        f"{JM_DIV}\n"
        "*Step 1 of 5 · Sender name*\n"
        "e.g. `ANZ Security Team`",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌  Cancel", callback_data=f"cancel_email_{template_id}")]
        ]),
        parse_mode='Markdown')


# ═══════════════════════════════════════════
# CUSTOM EMAIL FLOW — START
# ═══════════════════════════════════════════

async def show_custom_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the custom email flow — user writes their own body."""
    query = update.callback_query
    cancel_email_flow(context)  # clear any previous state

    context.user_data['custom_email_mode'] = True
    context.user_data['email_step']        = 'sender_name'
    context.user_data['awaiting_email']    = True

    await query.edit_message_text(
        f"{JM}"
        "✍️ *Custom Email*\n\n"
        f"{JM_DIV}\n"
        "*Step 1 of 6 · Sender name*\n"
        "e.g. `ANZ Security Team`",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌  Cancel", callback_data="cancel_custom_email")]
        ]),
        parse_mode='Markdown')


# ═══════════════════════════════════════════
# BUTTON CALLBACK ROUTER
# ═══════════════════════════════════════════

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    user_id = update.effective_user.id

    register_user(update.effective_user)

    try:
        await query.answer()
    except Exception as e:
        logger.warning(f"query.answer() failed: {e}")

    if not is_allowed(user_id):
        await deny(update)
        return

    d = query.data

    # ── ADMIN ──
    if d == "admin_panel":
        await show_admin_panel(update, context)
    elif d == "admin_add":
        await show_admin_add(update, context)
    elif d == "admin_remove":
        await show_admin_remove(update, context)
    elif d.startswith("admin_del_"):
        await admin_delete_user(update, context)

    # ── MAILER ──
    elif d == "mailer":
        await show_mailer_hub(update, context)
    elif d == "mailer_templates":
        await show_categories(update, context)
    elif d == "mailer_favorites":
        await show_favorites(update, context)
    elif d == "custom_email":
        await show_custom_email(update, context)

    elif d == "cat_BANKS":
        await show_bank_countries(update, context)
    elif d == "cat_CRYPTO":
        await show_crypto_types(update, context)
    elif d.startswith("cat_"):
        await show_items(update, context)
    elif d.startswith("country_"):
        await show_items(update, context)
    elif d.startswith("type_"):
        await show_items(update, context)
    elif d.startswith("item_"):
        await show_templates(update, context)

    elif d.startswith("view_template_"):
        await view_template(update, context)
    elif d.startswith("toggle_favorite_"):
        await toggle_favorite(update, context)
    elif d.startswith("send_template_"):
        await send_email_from_template(update, context)

    # ── CONFIRM: send ──
    elif d == "confirm_send":
        await do_send_email(update, context)

    # ── CONFIRM: edit (restart from step 1) ──
    elif d == "confirm_edit_custom":
        # Snapshot current confirmed data before restarting
        context.user_data['edit_snapshot'] = {
            k: context.user_data.get(k)
            for k in ['sender_name','sender_email','reply_to_email',
                      'email_recipient','email_subject','email_body',
                      'filled_vars','raw_body','custom_email_mode','selected_template_id']
        }
        cancel_email_flow(context)
        context.user_data['custom_email_mode'] = True
        context.user_data['email_step']        = 'sender_name'
        context.user_data['awaiting_email']    = True
        await query.edit_message_text(
            f"{JM}"
            "✍️ *Custom Email — Edit*\n\n"
            f"{JM_DIV}\n"
            "*Step 1 of 6 · Sender name*\n"
            "e.g. `ANZ Security Team`",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌  Cancel", callback_data="cancel_edit")]
            ]),
            parse_mode='Markdown')

    elif d.startswith("confirm_edit_"):
        template_id = d.replace("confirm_edit_", "")
        # Snapshot current confirmed data before restarting
        context.user_data['edit_snapshot'] = {
            k: context.user_data.get(k)
            for k in ['sender_name','sender_email','reply_to_email',
                      'email_recipient','email_subject','email_body',
                      'filled_vars','raw_body','custom_email_mode','selected_template_id']
        }
        cancel_email_flow(context)
        context.user_data['selected_template_id'] = template_id
        context.user_data['email_step']           = 'sender_name'
        context.user_data['awaiting_email']       = True
        template = find_template(template_id)
        name     = template['name'] if template and template['name'] else template_id
        await query.edit_message_text(
            f"{JM}"
            f"✏️ *Edit — {md_safe(name)}*\n\n"
            f"{JM_DIV}\n"
            "*Step 1 of 5 · Sender name*\n"
            "e.g. `ANZ Security Team`",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌  Cancel", callback_data="cancel_edit")]
            ]),
            parse_mode='Markdown')

    # ── CANCEL EDIT — restore snapshot and show confirm again ──
    elif d == "cancel_edit":
        snapshot = context.user_data.pop('edit_snapshot', None)
        cancel_email_flow(context)
        if snapshot:
            context.user_data.update({k: v for k, v in snapshot.items() if v is not None})
            context.user_data['awaiting_email'] = True
            await show_confirm_screen(update, context)
        else:
            # No snapshot (shouldn't happen) — fall back to menu
            await start(update, context)

    # ── CANCEL CUSTOM EMAIL (back to mailer hub) ──
    elif d == "cancel_custom_email":
        context.user_data.pop('edit_snapshot', None)
        cancel_email_flow(context)
        await show_mailer_hub(update, context)

    # ── CANCEL TEMPLATE EMAIL (back to template detail) ──
    elif d.startswith("cancel_email_"):
        template_id = d.replace("cancel_email_", "")
        context.user_data.pop('edit_snapshot', None)
        cancel_email_flow(context)
        await view_template(update, context, template_id=template_id)

    # ── BACK TO ITEM LIST (from template detail) ──
    elif d.startswith("back_to_item_"):
        back_cb = d.replace("back_to_item_", "")
        if back_cb.startswith("item_"):
            await show_templates(update, context, cb=back_cb)
        elif back_cb.startswith("country_") or back_cb.startswith("type_") or back_cb.startswith("cat_"):
            await show_items(update, context, cb=back_cb)
        elif back_cb == "mailer_templates":
            await show_categories(update, context)
        elif back_cb == "mailer":
            await show_mailer_hub(update, context)
        else:
            await show_categories(update, context)

    # ── BACK TO MENU ──
    elif d == "back":
        await start(update, context)


# ═══════════════════════════════════════════
# EMAIL SENDING
# ═══════════════════════════════════════════

def _try_send_via(srv: dict, msg) -> None:
    """Attempt to send a pre-built MIMEMultipart via one SMTP server.
    Tries STARTTLS/587 first, then SSL/465. Raises on both failures."""
    last_err = None
    for port, use_ssl in [(587, False), (465, True)]:
        conn = None
        try:
            if use_ssl:
                ctx  = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                conn = smtplib.SMTP_SSL(srv["host"], port, timeout=30, context=ctx)
            else:
                conn = smtplib.SMTP(srv["host"], port, timeout=30)
                conn.ehlo(); conn.starttls()
            conn.ehlo()
            conn.login(srv["user"], srv["pass"])
            conn.send_message(msg)
            conn.quit()
            srv["online"] = True
            return
        except Exception as e:
            last_err = e
            try: conn.close()
            except Exception: pass
    srv["online"] = False
    srv["last_err"] = str(last_err)
    raise Exception(str(last_err))


def send_email(recipient, subject, body,
               sender_name=None, sender_email=None, reply_to_email=None):
    """Try each SMTP server in order (online ones first). Raises if all fail."""
    if not SMTP_SERVERS:
        raise Exception("No SMTP servers configured. Add SMTP_1_HOST/USER/PASS to .env")

    msg = MIMEMultipart('alternative')
    # Use the first server's user as the technical From if needed
    fallback_from = SMTP_SERVERS[0]["user"] if SMTP_SERVERS else ""
    msg['From']    = (f"{sender_name} <{sender_email}>"
                      if sender_name and sender_email
                      else (sender_email or fallback_from))
    msg['To']      = recipient
    msg['Subject'] = subject
    if reply_to_email:
        msg['Reply-To'] = reply_to_email
    msg.attach(MIMEText(body, 'html'))

    # Sort: online servers first, then offline (give them one more chance)
    ordered = sorted(SMTP_SERVERS, key=lambda s: (0 if s["online"] else 1))
    errors  = []
    for srv in ordered:
        try:
            _try_send_via(srv, msg)
            logger.info(f"Email sent → {recipient} via [{srv['name']}]")
            return
        except Exception as e:
            logger.warning(f"SMTP [{srv['name']}] failed: {e}")
            errors.append(f"{srv['name']}: {e}")

    raise Exception("All SMTP servers failed:\n" + "\n".join(errors))


async def do_send_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Called from the ✅ Send Now button on the confirm screen."""
    query = update.callback_query
    cd    = context.user_data

    # Build body if not already set (no-placeholder path)
    if 'email_body' not in cd:
        template = find_template(cd.get('selected_template_id', ''))
        cd['email_body'] = template['body'] if template else ''

    try:
        send_email(
            recipient      = cd['email_recipient'],
            subject        = cd['email_subject'],
            body           = cd['email_body'],
            sender_name    = cd.get('sender_name'),
            sender_email   = cd.get('sender_email'),
            reply_to_email = cd.get('reply_to_email'),
        )
        await query.edit_message_text(
            f"{JM}"
            "✅ *Email Sent!*\n\n"
            f"📤 *From:*    `{md_safe(cd.get('sender_email',''))}`\n"
            f"📬 *To:*      `{md_safe(cd.get('email_recipient',''))}`\n"
            f"📌 *Subject:* `{md_safe(cd.get('email_subject',''))}`\n\n"
            f"{JM_DIV}\n"
            "🃏 _Joker Mailer — delivered._",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️  Back to Menu", callback_data="back")]
            ]),
            parse_mode='Markdown')

    except Exception as e:
        smtp_info = smtp_status_block().replace("\\n", "\n")
        await query.edit_message_text(
            f"{JM}"
            f"❌ *Send Failed*\n\n"
            f"{md_safe(str(e))}\n\n"
            f"{JM_DIV}\n"
            f"*SMTP Status:*\n{smtp_info}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄  Retry", callback_data="confirm_send")],
                [InlineKeyboardButton("⬅️  Back to Menu", callback_data="back")],
            ]),
            parse_mode='Markdown')
        logger.error(f"Email error: {e}")

    finally:
        context.user_data.pop('edit_snapshot', None)
        cancel_email_flow(context)


# ═══════════════════════════════════════════
# TEXT INPUT HANDLER
# ═══════════════════════════════════════════

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text    = update.message.text.strip()

    if not is_allowed(user_id):
        await deny(update)
        return

    register_user(update.effective_user)

    try:
        await _handle_text_inner(update, context, user_id, text)
    except Exception as e:
        logger.error(f"handle_text exception for user {user_id}: {e}", exc_info=True)
        cancel_email_flow(context)
        try:
            await update.message.reply_text(
                f"{JM}"
                "❌ *Something went wrong.*\n\n"
                "Your flow has been reset. Tap Menu to start again.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🏠  Menu", callback_data="back")]
                ]),
                parse_mode='Markdown')
        except Exception:
            pass


async def _handle_text_inner(update: Update, context: ContextTypes.DEFAULT_TYPE,
                             user_id: int, text: str):

    # ── ADMIN: add user ──
    if context.user_data.get('admin_step') == 'add_user':
        if not is_admin(user_id):
            context.user_data.pop('admin_step', None)
            return
        try:
            new_uid = int(text)
            if new_uid in allowed_users:
                await update.message.reply_text(
                    f"{JM}"
                    f"ℹ️ `{new_uid}` is already allowed.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⬅️  Admin Panel", callback_data="admin_panel")]
                    ]),
                    parse_mode='Markdown')
            else:
                allowed_users.add(new_uid)
                save_allowed_users()
                await update.message.reply_text(
                    f"{JM}"
                    f"✅ User `{new_uid}` added successfully.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⬅️  Admin Panel", callback_data="admin_panel")]
                    ]),
                    parse_mode='Markdown')
        except ValueError:
            await update.message.reply_text(
                f"{JM}"
                "❌ Invalid ID — send a number e.g. `123456789`\n\n"
                "Or tap Cancel to go back.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌  Cancel", callback_data="admin_panel")]
                ]),
                parse_mode='Markdown')
            return   # keep admin_step so they can retry
        context.user_data.pop('admin_step', None)
        return

    # ── Idle ──
    if not context.user_data.get('awaiting_email'):
        await update.message.reply_text(
            f"{JM}"
            "👋 Use the menu to get started.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠  Menu", callback_data="back")]
            ]),
            parse_mode='Markdown')
        return

    step        = context.user_data.get('email_step')
    template_id = context.user_data.get('selected_template_id', '')
    is_custom   = context.user_data.get('custom_email_mode', False)
    cancel_cb   = "cancel_custom_email" if is_custom else f"cancel_email_{template_id}"
    cancel_kb   = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌  Cancel", callback_data=cancel_cb)]
    ])

    # ── EMAIL STEPS ──

    if step == 'sender_name':
        context.user_data['sender_name'] = text
        context.user_data['email_step']  = 'sender_email'
        await update.message.reply_text(
            f"{JM}"
            "*Step 2 of 5 · Sender email*\n"
            "e.g. `security@anz`",
            reply_markup=cancel_kb, parse_mode='Markdown')

    elif step == 'sender_email':
        ok, msg = validate_sender_email(text)
        if not ok:
            await update.message.reply_text(
                f"{JM}❌ {msg}",
                reply_markup=cancel_kb, parse_mode='Markdown')
            return
        context.user_data['sender_email'] = text
        context.user_data['email_step']   = 'reply_to'
        await update.message.reply_text(
            f"{JM}"
            "*Step 3 of 5 · Reply-to email*\n"
            "Where should replies go?",
            reply_markup=cancel_kb, parse_mode='Markdown')

    elif step == 'reply_to':
        context.user_data['reply_to_email'] = text
        context.user_data['email_step']     = 'recipient'
        await update.message.reply_text(
            f"{JM}"
            "*Step 4 of 5 · Recipient*\n"
            "Who is this email going to?",
            reply_markup=cancel_kb, parse_mode='Markdown')

    elif step == 'recipient':
        context.user_data['email_recipient'] = text
        context.user_data['email_step']      = 'subject'
        is_custom = context.user_data.get('custom_email_mode')
        step_label = "Step 5 of 6" if is_custom else "Step 5 of 5"
        await update.message.reply_text(
            f"{JM}"
            f"*{step_label} · Subject line*\n"
            "What's the email subject?",
            reply_markup=cancel_kb, parse_mode='Markdown')

    elif step == 'subject':
        context.user_data['email_subject'] = text

        # Custom email — ask for body next
        if context.user_data.get('custom_email_mode'):
            context.user_data['email_step'] = 'custom_body'
            await update.message.reply_text(
                f"{JM}"
                "*Step 6 of 6 · Email body*\n\n"
                "Either:\n"
                "• Type your body below _(plain text or HTML)_\n"
                "• Upload an *.html* file and it will be read automatically",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌  Cancel", callback_data="cancel_custom_email")]
                ]),
                parse_mode='Markdown')
            return

        template = find_template(template_id)

        if not template:
            await update.message.reply_text(
                f"{JM}❌ Template not found.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️  Menu", callback_data="back")]
                ]),
                parse_mode='Markdown')
            cancel_email_flow(context)
            return

        raw_body = template['body']
        context.user_data['raw_body'] = raw_body

        vars_found  = []
        vars_found += re.findall(r'\[([A-Za-z_][A-Za-z0-9_ ]*)\]',             raw_body)
        vars_found += re.findall(r'\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}',          raw_body)
        vars_found += re.findall(r'(?<!\{)\{([A-Za-z_][A-Za-z0-9_]*)\}(?!\})', raw_body)
        vars_found += re.findall(r'\$([A-Za-z_][A-Za-z0-9_]*)',                 raw_body)

        seen, unique = set(), []
        for v in vars_found:
            if v.lower() not in seen:
                seen.add(v.lower())
                unique.append(v)

        if unique:
            context.user_data['pending_vars'] = unique.copy()
            context.user_data['filled_vars']  = {}
            context.user_data['email_step']   = 'fill_vars'
            await update.message.reply_text(
                f"{JM}"
                f"🔍 *{len(unique)} placeholder(s) found*\n\n"
                f"Enter value for `{md_safe(unique[0])}`:",
                reply_markup=cancel_kb, parse_mode='Markdown')
        else:
            # No placeholders — body is ready, go straight to confirm
            context.user_data['email_body'] = raw_body
            context.user_data['filled_vars'] = {}
            await show_confirm_screen(update, context)

    elif step == 'custom_body':
        # Body typed — store it and go straight to confirm
        context.user_data['email_body']   = text
        context.user_data['filled_vars']  = {}
        await show_confirm_screen(update, context)
        return

    elif step == 'fill_vars':
        pending = context.user_data['pending_vars']

        if not pending:
            await _build_body_and_confirm(update, context)
            return

        current = pending.pop(0)
        context.user_data['filled_vars'][current] = text

        if pending:
            await update.message.reply_text(
                f"{JM}"
                f"✅ `{md_safe(current)}` = `{md_safe(text)}`\n\n"
                f"Enter value for `{md_safe(pending[0])}` ({len(pending)} left):",
                reply_markup=cancel_kb, parse_mode='Markdown')
        else:
            await _build_body_and_confirm(update, context)

    else:
        # Unknown / missing step — never leave the user hanging
        logger.warning(f"handle_text: unrecognised step={step!r} for user {user_id}")
        await update.message.reply_text(
            f"{JM}"
            "⚠️ Something went wrong with the flow.\n\n"
            "Tap *Cancel* to go back to the menu and start again.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌  Cancel & Menu", callback_data="back")]
            ]),
            parse_mode='Markdown')
        cancel_email_flow(context)


# ═══════════════════════════════════════════
# DOCUMENT / FILE UPLOAD HANDLER
# ═══════════════════════════════════════════

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Accept an uploaded HTML file as the custom email body (custom_body step only)."""
    user_id = update.effective_user.id

    if not is_allowed(user_id):
        await deny(update)
        return

    register_user(update.effective_user)

    # Grab the document — Telegram always populates this for file messages
    doc = update.message.document if update.message else None
    if not doc:
        return  # not a document message, ignore silently

    # If we're not in the custom_body step, tell the user and bail
    if (not context.user_data.get('awaiting_email')
            or context.user_data.get('email_step') != 'custom_body'):
        await update.message.reply_text(
            f"{JM}"
            "⚠️ *Not expecting a file right now.*\n\n"
            "Start a *Custom Email* flow first, then upload your HTML "
            "when asked for the body.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠  Menu", callback_data="back")]
            ]),
            parse_mode='Markdown')
        return

    fname = doc.file_name or ""
    mime  = doc.mime_type or ""

    # Accept .html extension OR text/html mime type
    is_html = fname.lower().endswith('.html') or 'html' in mime.lower()
    if not is_html:
        await update.message.reply_text(
            f"{JM}"
            f"❌ *Wrong file type:* `{md_safe(fname or mime)}`

"
            "Please upload a `.html` file, or type your body as text.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌  Cancel", callback_data="cancel_custom_email")]
            ]),
            parse_mode='Markdown')
        return

    # File size guard (1 MB)
    if doc.file_size and doc.file_size > 1_048_576:
        await update.message.reply_text(
            f"{JM}❌ File too large (max 1 MB). Please upload a smaller HTML file.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌  Cancel", callback_data="cancel_custom_email")]
            ]),
            parse_mode='Markdown')
        return

    # Acknowledge immediately so the user knows something is happening
    await update.message.reply_text(
        f"{JM}⏳ Reading your HTML file…",
        parse_mode='Markdown')

    try:
        import io
        tg_file = await doc.get_file()
        buf = io.BytesIO()
        await tg_file.download_to_memory(buf)
        buf.seek(0)
        html_body = buf.read().decode('utf-8')
    except Exception as e:
        logger.error(f"handle_document: download failed for user {user_id}: {e}", exc_info=True)
        await update.message.reply_text(
            f"{JM}❌ Could not read the file: `{md_safe(str(e))}`

Please try again.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌  Cancel", callback_data="cancel_custom_email")]
            ]),
            parse_mode='Markdown')
        return

    if not html_body.strip():
        await update.message.reply_text(
            f"{JM}❌ The file appears to be empty. Please upload a valid HTML file.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌  Cancel", callback_data="cancel_custom_email")]
            ]),
            parse_mode='Markdown')
        return

    context.user_data['email_body']  = html_body
    context.user_data['filled_vars'] = {}

    plain    = html_to_text(html_body)
    preview  = plain[:200].strip()
    if len(plain) > 200:
        preview += "…"

    display_name = fname if fname else "uploaded file"
    await update.message.reply_text(
        f"{JM}"
        f"✅ *Loaded:* `{md_safe(display_name)}`
"
        f"📏 `{len(html_body):,}` bytes

"
        f"📝 *Preview:*
`{md_safe(preview)}`",
        parse_mode='Markdown')

    await show_confirm_screen(update, context)


async def _build_body_and_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Substitute all filled vars into the raw body then show the confirm screen."""
    body = context.user_data['raw_body']
    for var, val in context.user_data['filled_vars'].items():
        body = body.replace(f"[{var}]",      val)
        body = body.replace(f"{{{{{var}}}}}",  val)
        body = body.replace(f"{{{var}}}",      val)
        body = body.replace(f"${var}",         val)
        body = re.sub(re.escape(f"[{var}]"),       val, body, flags=re.IGNORECASE)
        body = re.sub(re.escape(f"{{{{{var}}}}}"),  val, body, flags=re.IGNORECASE)
        body = re.sub(r'(?<!\{)\{' + re.escape(var) + r'\}(?!\})',
                      val, body, flags=re.IGNORECASE)
        body = re.sub(re.escape(f"${var}"),         val, body, flags=re.IGNORECASE)
    context.user_data['email_body'] = body
    await show_confirm_screen(update, context)


# ═══════════════════════════════════════════
# ERROR HANDLER
# ═══════════════════════════════════════════

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, NetworkError):
        logger.warning(f"Network blip (auto-retry): {context.error}")
        return
    logger.error("Unhandled exception:", exc_info=context.error)


# ═══════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_callback))
    # Document handler MUST come before the text handler.
    # python-telegram-bot dispatches handlers in registration order within
    # the same group, and a document message will never match TEXT anyway,
    # but being explicit avoids any edge-case ordering surprises.
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)

    total = 0
    for cat_key, cat in TEMPLATES.items():
        if cat_key == "BANKS":
            total += sum(len(i.get("templates", []))
                         for c in cat["countries"].values()
                         for i in c["items"].values())
        elif cat_key == "CRYPTO":
            total += sum(len(i.get("templates", []))
                         for td in cat["types"].values()
                         for i in td["items"].values())
        elif cat_key == "AUTHORITY":
            total += sum(len(i.get("templates", [])) for i in cat["items"].values())

    logger.info(f"✅ Loaded {total} templates — invite-only mode active")
    logger.info(f"👑 Admin: {ADMIN_ID}  |  Allowed users: {len(allowed_users)}")
    logger.info(f"🔌 Checking {len(SMTP_SERVERS)} SMTP server(s)…")
    check_all_smtp()
    online = sum(1 for s in SMTP_SERVERS if s["online"])
    logger.info(f"📡 {online}/{len(SMTP_SERVERS)} SMTP servers online")
    logger.info("🚀 Joker Mailer starting…")

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        bootstrap_retries=-1,
    )


if __name__ == '__main__':
    main()
