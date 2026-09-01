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

# ── Multi-SMTP ──
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
    if not servers:
        h = os.getenv("MAILGUN_SMTP")
        u = os.getenv("MAILGUN_USER")
        p = os.getenv("MAILGUN_PASS")
        if h:
            servers.append({"name": "Mailgun", "host": h, "user": u, "pass": p,
                            "online": False, "last_err": ""})
    return servers

SMTP_SERVERS: list = _load_smtp_servers()
ADMIN_ID = 6197474466

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

user_data = {}


# ═══════════════════════════════════════════
# BRAND
# ═══════════════════════════════════════════

JM     = "🃏 *JOKER MAILER*\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
JM_DIV = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"


# ═══════════════════════════════════════════
# SENDER CONFIG — folder name → sender
# ═══════════════════════════════════════════

SENDER_CONFIGS = {
    "AFP": {
        "display": "🇦🇺 Australian Federal Police",
        "sender_name": "Australian Federal Police",
        "sender_email": "noreply@afp",
        "reply_to_email": "noreply@afp.gov.au"
    },
    "ANZ": {
        "display": "🏦 ANZ Bank",
        "sender_name": "ANZ",
        "sender_email": "noreply@anz",
        "reply_to_email": "noreply@anz.co.nz"
    },
    "NZ POLICE": {
        "display": "🇳🇿 NZ Police",
        "sender_name": "NZ Police",
        "sender_email": "noreply@police",
        "reply_to_email": "noreply@police.govt.nz"
    },
    "UK POLICE": {
        "display": "🇬🇧 UK Police (NPCC)",
        "sender_name": "UK Police",
        "sender_email": "noreply@npcc.police",
        "reply_to_email": "info@npcc.police.uk"
    },
    "WESTPAC": {
        "display": "🏦 Westpac NZ",
        "sender_name": "Westpac",
        "sender_email": "noreply@westpac",
        "reply_to_email": "noreply@westpac.co.nz"
    },
    "BNZ": {
        "display": "🏦 BNZ (Bank of New Zealand)",
        "sender_name": "BNZ",
        "sender_email": "noreply@bnz",
        "reply_to_email": "noreply@bnz.co.nz"
    },
    "ASB": {
        "display": "🏦 ASB Bank",
        "sender_name": "ASB",
        "sender_email": "noreply@asb",
        "reply_to_email": "noreply@asb.co.nz"
    },
    "KIWIBANK": {
        "display": "🏦 Kiwibank",
        "sender_name": "Kiwibank",
        "sender_email": "noreply@kiwibank",
        "reply_to_email": "noreply@kiwibank.co.nz"
    },
    "GOOGLE": {
        "display": "🔵 Google",
        "sender_name": "Google",
        "sender_email": "noreply@google",
        "reply_to_email": "support-noreply@google.com"
    },
    "LEDGER": {
        "display": "🔷 Ledger Support",
        "sender_name": "Ledger Support",
        "sender_email": "support@ledger",
        "reply_to_email": "support@ledger.com"
    },
}

# Fuzzy folder-name → sender key mapping
FOLDER_TO_SENDER = {
    "anz":        "ANZ",
    "anz bank":   "ANZ",
    "bnz":        "BNZ",
    "asb":        "ASB",
    "asb bank":   "ASB",
    "westpac":    "WESTPAC",
    "kiwibank":   "KIWIBANK",
    "afp":        "AFP",
    "australian federal police": "AFP",
    "nz police":  "NZ POLICE",
    "nzpolice":   "NZ POLICE",
    "uk police":  "UK POLICE",
    "npcc":       "UK POLICE",
    "google":     "GOOGLE",
    "ledger":     "LEDGER",
    "ledger support": "LEDGER",
}

def folder_to_sender_config(folder_name: str):
    """Return sender config dict for a folder name, or None if no match."""
    key = FOLDER_TO_SENDER.get(folder_name.strip().lower())
    if key:
        return SENDER_CONFIGS.get(key)
    return None


# ═══════════════════════════════════════════
# SMTP HEALTH CHECK
# ═══════════════════════════════════════════

def test_smtp(server: dict) -> bool:
    for port, use_ssl in [(587, False), (465, True)]:
        conn = None
        try:
            if use_ssl:
                ctx = ssl.create_default_context()
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
    for srv in SMTP_SERVERS:
        srv["online"] = test_smtp(srv)
        status = "✅ online" if srv["online"] else f"❌ offline ({srv['last_err'][:60]})"
        logger.info(f"  SMTP [{srv['name']}] {status}")

def smtp_status_line() -> str:
    online = sum(1 for s in SMTP_SERVERS if s["online"])
    return f"{online}/{len(SMTP_SERVERS)} SMTP online"

def smtp_status_block() -> str:
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
# SIDECAR FIELDS LOADER
# ═══════════════════════════════════════════

def load_sidecar_fields(template_path: Path) -> list:
    """
    Look for a .fields.json file next to the template.
    e.g. auth_alert.html → auth_alert.fields.json
    Returns list of {"label": ..., "example": ...} or empty list.
    """
    sidecar = template_path.with_suffix('').with_suffix('.fields.json')
    # handle double extension like auth_alert.html.fields.json too
    alt_sidecar = template_path.parent / (template_path.name + '.fields.json')
    
    target = None
    if sidecar.exists():
        target = sidecar
    elif alt_sidecar.exists():
        target = alt_sidecar
    
    if not target:
        return []
    
    try:
        data = json.loads(target.read_text(encoding='utf-8'))
        fields = data.get('fields', [])
        if isinstance(fields, list) and all(
            isinstance(f, dict) and 'label' in f for f in fields
        ):
            return fields
    except Exception as e:
        logger.warning(f"Could not load sidecar {target}: {e}")
    return []


# ═══════════════════════════════════════════
# TEMPLATE LOADING
# ═══════════════════════════════════════════

# New structure:
# TEMPLATES = {
#   "countries": {
#     "New Zealand": {
#       "emoji": "🇳🇿",
#       "items": {
#         "ANZ": {
#           "sender_key": "ANZ",   # resolved at load time
#           "templates": [...]
#         }
#       }
#     }
#   },
#   "crypto": {
#     "Ledger": { "templates": [...] }
#   },
#   "email": {
#     "Google": { "templates": [...] }
#   }
# }

COUNTRY_EMOJIS = {
    "new zealand": "🇳🇿",
    "australia":   "🇦🇺",
    "uk":          "🇬🇧",
    "united kingdom": "🇬🇧",
    "usa":         "🇺🇸",
    "united states": "🇺🇸",
}

def _country_emoji(name: str) -> str:
    return COUNTRY_EMOJIS.get(name.strip().lower(), "🌐")


def load_templates_from_files():
    data = {
        "countries": {},   # country → { emoji, items: { folder → { sender_cfg, templates } } }
        "crypto":    {},   # folder → { sender_cfg, templates }
        "email":     {},   # folder → { sender_cfg, templates }
    }

    def load_file(f: Path) -> dict | None:
        try:
            if f.suffix == '.json':
                raw  = json.loads(f.read_text(encoding='utf-8'))
                body = raw.get('body', '')
                return {
                    "id":     raw.get('id', f.stem),
                    "name":   raw.get('name', format_template_name(f.name)),
                    "body":   body,
                    "path":   f,
                    "fields": raw.get('fields', []),   # inline fields (JSON templates)
                }
            else:
                return {
                    "id":     f.stem,
                    "name":   format_template_name(f.name),
                    "body":   f.read_text(encoding='utf-8'),
                    "path":   f,
                    "fields": load_sidecar_fields(f),  # sidecar for HTML/txt/eml
                }
        except Exception as e:
            logger.error(f"Failed to load {f}: {e}")
            return None

    # ── BANKS (country sub-folders) ──
    banks_path = TEMPLATES_DIR / "banks"
    if banks_path.exists():
        for country_folder in sorted(banks_path.iterdir()):
            if not country_folder.is_dir():
                continue
            country_name = country_folder.name
            emoji = _country_emoji(country_name)
            data["countries"].setdefault(country_name, {"emoji": emoji, "items": {}})

            for item_folder in sorted(country_folder.iterdir()):
                if not item_folder.is_dir():
                    continue
                item_name  = item_folder.name
                sender_cfg = folder_to_sender_config(item_name)
                tmpls = []
                for tf in sorted(item_folder.glob("*")):
                    if tf.is_file() and tf.suffix in ('.json', '.html', '.txt', '.eml') \
                            and '.fields' not in tf.name:
                        t = load_file(tf)
                        if t and t.get('body'):
                            tmpls.append(t)

                if tmpls:
                    data["countries"][country_name]["items"][item_name] = {
                        "sender_cfg": sender_cfg,
                        "templates":  tmpls,
                    }
                    logger.info(f"  Banks/{country_name}/{item_name}: "
                                f"{len(tmpls)} templates, "
                                f"sender={'auto' if sender_cfg else 'manual'}")

    # ── AUTHORITY (flat, treated as a country group called "Authority") ──
    auth_path = TEMPLATES_DIR / "authority"
    if auth_path.exists():
        country_name = "Authority"
        data["countries"].setdefault(country_name, {"emoji": "🏛️", "items": {}})
        for item_folder in sorted(auth_path.iterdir()):
            if not item_folder.is_dir():
                continue
            item_name  = item_folder.name
            sender_cfg = folder_to_sender_config(item_name)
            tmpls = []
            for tf in sorted(item_folder.glob("*")):
                if tf.is_file() and tf.suffix in ('.json', '.html', '.txt', '.eml') \
                        and '.fields' not in tf.name:
                    t = load_file(tf)
                    if t and t.get('body'):
                        tmpls.append(t)
            if tmpls:
                data["countries"][country_name]["items"][item_name] = {
                    "sender_cfg": sender_cfg,
                    "templates":  tmpls,
                }

    # ── CRYPTO ──
    crypto_path = TEMPLATES_DIR / "crypto"
    if crypto_path.exists():
        for item_folder in sorted(crypto_path.iterdir()):
            if not item_folder.is_dir():
                continue
            item_name  = item_folder.name
            sender_cfg = folder_to_sender_config(item_name)
            tmpls = []
            # support one level of sub-folder (type) or flat
            candidates = list(item_folder.glob("*"))
            for tf in sorted(candidates):
                if tf.is_file() and tf.suffix in ('.json', '.html', '.txt', '.eml') \
                        and '.fields' not in tf.name:
                    t = load_file(tf)
                    if t and t.get('body'):
                        tmpls.append(t)
                elif tf.is_dir():
                    for sub in sorted(tf.glob("*")):
                        if sub.is_file() and sub.suffix in ('.json', '.html', '.txt', '.eml') \
                                and '.fields' not in sub.name:
                            t = load_file(sub)
                            if t and t.get('body'):
                                tmpls.append(t)
            if tmpls:
                data["crypto"][item_name] = {
                    "sender_cfg": sender_cfg,
                    "templates":  tmpls,
                }

    # ── EMAIL ──
    email_path = TEMPLATES_DIR / "email"
    if email_path.exists():
        for item_folder in sorted(email_path.iterdir()):
            if not item_folder.is_dir():
                continue
            item_name  = item_folder.name
            sender_cfg = folder_to_sender_config(item_name)
            tmpls = []
            for tf in sorted(item_folder.glob("*")):
                if tf.is_file() and tf.suffix in ('.json', '.html', '.txt', '.eml') \
                        and '.fields' not in tf.name:
                    t = load_file(tf)
                    if t and t.get('body'):
                        tmpls.append(t)
            if tmpls:
                data["email"][item_name] = {
                    "sender_cfg": sender_cfg,
                    "templates":  tmpls,
                }

    return data


TEMPLATES = load_templates_from_files()


def find_template(template_id: str) -> dict | None:
    """Search all sections for a template by id. Returns template dict or None."""
    for cdata in TEMPLATES["countries"].values():
        for idata in cdata["items"].values():
            for t in idata["templates"]:
                if t['id'] == template_id:
                    return t
    for idata in TEMPLATES["crypto"].values():
        for t in idata["templates"]:
            if t['id'] == template_id:
                return t
    for idata in TEMPLATES["email"].values():
        for t in idata["templates"]:
            if t['id'] == template_id:
                return t
    return None


def find_template_sender(template_id: str) -> dict | None:
    """Return the auto-resolved sender config for a template, or None."""
    for cdata in TEMPLATES["countries"].values():
        for idata in cdata["items"].values():
            for t in idata["templates"]:
                if t['id'] == template_id:
                    return idata.get("sender_cfg")
    for idata in TEMPLATES["crypto"].values():
        for t in idata["templates"]:
            if t['id'] == template_id:
                return idata.get("sender_cfg")
    for idata in TEMPLATES["email"].values():
        for t in idata["templates"]:
            if t['id'] == template_id:
                return idata.get("sender_cfg")
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
              'template_list_back_cb', 'custom_email_mode',
              'fields_def', 'fields_error', 'selected_smtp']:  # ← ADDED selected_smtp
        context.user_data.pop(k, None)


# ═══════════════════════════════════════════
# SENDER PICKER — fallback for unmatched folders / custom email
# ═══════════════════════════════════════════

def _sender_kb(cancel_cb: str, include_custom: bool = False) -> InlineKeyboardMarkup:
    kb = []
    for key, cfg in SENDER_CONFIGS.items():
        kb.append([InlineKeyboardButton(cfg['display'], callback_data=f"sender_{key}")])
    if include_custom:
        kb.append([InlineKeyboardButton("✏️  Custom Sender Details", callback_data="custom_sender")])
    kb.append([InlineKeyboardButton("❌  Cancel", callback_data=cancel_cb)])
    return InlineKeyboardMarkup(kb)


# ═══════════════════════════════════════════
# CONFIRM SCREEN
# ═══════════════════════════════════════════

async def show_confirm_screen(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        raw_body = cd.get('email_body', '')
        preview  = raw_body[:200].strip()
        if len(raw_body) > 200:
            preview += "…"
        extra_block = f"\n\n📝 *Body preview:*\n`{md_safe(preview)}`"
    elif filled:
        vars_lines  = "\n".join(f"  `{md_safe(k)}` → `{md_safe(v)}`" for k, v in filled.items())
        extra_block = f"\n\n📝 *Placeholders filled:*\n{vars_lines}"

    cancel_cb = "cancel_custom_email" if cd.get('custom_email_mode') else f"cancel_email_{template_id}"
    edit_cb   = "confirm_edit_custom" if cd.get('custom_email_mode') else f"confirm_edit_{template_id}"

    # ── NEW: Show selected SMTP if chosen ──
    selected_smtp = cd.get('selected_smtp')
    smtp_line = ""
    if selected_smtp:
        for srv in SMTP_SERVERS:
            if srv['name'] == selected_smtp:
                smtp_line = f"\n🔌 *SMTP:*         `{md_safe(srv['name'])}` ✓"
                break

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
        f"{smtp_line}"  # ← NEW: Show selected SMTP
        f"{extra_block}\n\n"
        f"{JM_DIV}\n"
        "Does everything look correct?\n\n"
        "✅ Tap *Send* to fire it off\n"
        "🔌 Tap *SMTP* to choose a server _(optional)_\n"
        "✏️  Tap *Edit* to start over\n"
        "❌ Tap *Cancel* to abort"
    )

    kb = [
        [InlineKeyboardButton("✅  Send Now",  callback_data="confirm_send")],
        [InlineKeyboardButton("🔌  Choose SMTP", callback_data="select_smtp")],  # ← NEW
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
# SMTP SELECTION SCREEN
# ═══════════════════════════════════════════

async def show_smtp_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show list of available SMTP servers to choose from."""
    query = update.callback_query
    
    # Filter to only online servers
    online_servers = [s for s in SMTP_SERVERS if s['online']]
    
    if not online_servers:
        await query.answer(
            "No SMTP servers online right now. Will auto-select on send.",
            show_alert=True)
        return
    
    kb = []
    for srv in online_servers:
        kb.append([InlineKeyboardButton(
            f"✅ {srv['name']} ({srv['user']})",
            callback_data=f"smtp_pick_{srv['name']}"
        )])
    
    kb.append([InlineKeyboardButton("❌  Cancel (Auto-select)", callback_data="confirm_send")])
    
    await query.edit_message_text(
        f"{JM}"
        "🔌 *Choose SMTP Server*\n\n"
        "Pick which server to send through:\n\n"
        "_Tap Cancel to send automatically._",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode='Markdown')


# ═══════════════════════════════════════════
# MAIN MENU  (no Mailer Hub — straight to actions)
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
        f"📡 *{smtp_status_line()}*\n"
        "🔒 *Invite-only* — keep your access private\n"
        f"{JM_DIV}\n\n"
        "👇 *Select an option to get started:*"
    )

    kb = [
        [InlineKeyboardButton("📋  Templates",   callback_data="show_templates")],
        [InlineKeyboardButton("⭐  Favourites",  callback_data="mailer_favorites")],
        [InlineKeyboardButton("✍️  Custom Email", callback_data="custom_email")],
    ]
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
        [InlineKeyboardButton("⬅️  Back",         callback_data="back")],
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
    query   = update.callback_query
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
# TEMPLATE BROWSER  — new flat structure
# ═══════════════════════════════════════════

async def show_top_level(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Top level: list every country group + Crypto + Email buckets.
    Countries come from templates/banks + templates/authority.
    """
    if not is_allowed(update.effective_user.id):
        await deny(update)
        return

    kb = []

    # Country groups (banks + authority merged by country)
    for country_name, cdata in TEMPLATES["countries"].items():
        total = sum(len(i["templates"]) for i in cdata["items"].values())
        items = len(cdata["items"])
        if total:
            label = f"{cdata['emoji']} {country_name}  ·  {items} senders  ·  {total} templates"
            kb.append([InlineKeyboardButton(label,
                        callback_data=f"grp_country_{country_name}")])

    # Crypto
    if TEMPLATES["crypto"]:
        total = sum(len(i["templates"]) for i in TEMPLATES["crypto"].values())
        items = len(TEMPLATES["crypto"])
        kb.append([InlineKeyboardButton(
            f"🪙 Crypto  ·  {items} platforms  ·  {total} templates",
            callback_data="grp_crypto")])

    # Email
    if TEMPLATES["email"]:
        total = sum(len(i["templates"]) for i in TEMPLATES["email"].values())
        items = len(TEMPLATES["email"])
        kb.append([InlineKeyboardButton(
            f"📧 Email  ·  {items} providers  ·  {total} templates",
            callback_data="grp_email")])

    kb.append([InlineKeyboardButton("⬅️  Back", callback_data="back")])

    await update.callback_query.edit_message_text(
        f"{JM}"
        "📋 *Templates* — pick a category:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode='Markdown')


async def show_country_items(update: Update, context: ContextTypes.DEFAULT_TYPE,
                              country_name: str):
    """List all items (banks, police etc.) under a country."""
    cdata = TEMPLATES["countries"].get(country_name)
    if not cdata:
        await update.callback_query.answer("Not found", show_alert=True)
        return

    kb = []
    for item_name, idata in cdata["items"].items():
        n = len(idata["templates"])
        sender_tag = f" · {idata['sender_cfg']['sender_name']}" if idata.get("sender_cfg") else ""
        kb.append([InlineKeyboardButton(
            f"{item_name}  ·  {n} templates{sender_tag}",
            callback_data=f"grp_item_country_{country_name}_{item_name}")])

    kb.append([InlineKeyboardButton("⬅️  Back", callback_data="show_templates")])
    await update.callback_query.edit_message_text(
        f"{JM}"
        f"{cdata['emoji']} *{md_safe(country_name)}* — pick a sender:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode='Markdown')


async def show_group_items(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            group: str):
    """List items in crypto or email group."""
    items = TEMPLATES.get(group, {})
    kb    = []
    for item_name, idata in items.items():
        n          = len(idata["templates"])
        sender_tag = f" · {idata['sender_cfg']['sender_name']}" if idata.get("sender_cfg") else ""
        kb.append([InlineKeyboardButton(
            f"{item_name}  ·  {n} templates{sender_tag}",
            callback_data=f"grp_item_{group}_{item_name}")])
    kb.append([InlineKeyboardButton("⬅️  Back", callback_data="show_templates")])

    emoji = "🪙" if group == "crypto" else "📧"
    label = "Crypto" if group == "crypto" else "Email"
    await update.callback_query.edit_message_text(
        f"{JM}"
        f"{emoji} *{label}* — pick a platform:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode='Markdown')


async def show_item_templates(update: Update, context: ContextTypes.DEFAULT_TYPE,
                               group: str, item_name: str,
                               country_name: str | None = None):
    """List templates for a specific item."""
    if group == "country" and country_name:
        idata   = TEMPLATES["countries"][country_name]["items"].get(item_name, {})
        back_cb = f"grp_country_{country_name}"
    else:
        idata   = TEMPLATES.get(group, {}).get(item_name, {})
        back_cb = f"grp_{group}"

    templates = idata.get("templates", [])
    uinfo     = get_user_data(update.effective_user.id)

    # Store back context for the template detail view
    context.user_data['template_list_back_cb'] = back_cb
    context.user_data['template_item_group']   = group
    context.user_data['template_item_name']    = item_name
    context.user_data['template_item_country'] = country_name

    kb = []
    for t in templates:
        star = "⭐" if t["id"] in uinfo['favorites'] else "☆"
        name = t['name'] if t['name'] and t['name'].strip() else t['id']
        kb.append([InlineKeyboardButton(f"{star} {name}",
                    callback_data=f"view_template_{t['id']}")])
    kb.append([InlineKeyboardButton("⬅️  Back", callback_data=back_cb)])

    sender_cfg = idata.get("sender_cfg")
    sender_tag = f"\n_Sending as: {md_safe(sender_cfg['sender_name'])}_" if sender_cfg else ""

    await update.callback_query.edit_message_text(
        f"{JM}"
        f"*{md_safe(item_name)}*{sender_tag}\n\n"
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

    back_cb = context.user_data.get('template_list_back_cb', 'show_templates')

    # Show field labels if sidecar/inline fields exist
    fields = template.get('fields', [])
    fields_tag = ""
    if fields:
        labels = ", ".join(f['label'] for f in fields)
        fields_tag = f"\n\n📝 _Fields: {md_safe(labels)}_"

    text = (
        f"{JM}"
        f"📧 *{md_safe(name)}*"
        f"{fields_tag}\n\n"
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
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb),
                                  parse_mode='Markdown')


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
                [InlineKeyboardButton("⬅️  Back", callback_data="back")]
            ]),
            parse_mode='Markdown')
        return

    kb = []
    for fav_id in uinfo['favorites']:
        t = find_template(fav_id)
        if t:
            kb.append([InlineKeyboardButton(f"⭐ {t['name']}",
                        callback_data=f"view_template_{fav_id}")])
    kb.append([InlineKeyboardButton("⬅️  Back", callback_data="back")])

    await query.edit_message_text(
        f"{JM}"
        f"⭐ *Favourites*  ·  {len(uinfo['favorites'])} saved",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode='Markdown')


# ═══════════════════════════════════════════
# EMAIL FLOW — START (template)
# ═══════════════════════════════════════════

async def send_email_from_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tap Send Email → auto-resolve sender, skip picker if matched."""
    query       = update.callback_query
    template_id = query.data.replace("send_template_", "")
    template    = find_template(template_id)

    context.user_data['selected_template_id'] = template_id
    context.user_data['awaiting_email']       = True

    name = template['name'] if template and template['name'] else template_id

    # Try auto-resolve sender
    sender_cfg = find_template_sender(template_id)

    if sender_cfg:
        # Auto-fill sender, skip picker
        context.user_data['sender_name']    = sender_cfg['sender_name']
        context.user_data['sender_email']   = sender_cfg['sender_email']
        context.user_data['reply_to_email'] = sender_cfg['reply_to_email']
        context.user_data['email_step']     = 'recipient'

        await query.edit_message_text(
            f"{JM}"
            f"✉️ *Send — {md_safe(name)}*\n\n"
            f"{JM_DIV}\n"
            f"_Sender auto-set: {md_safe(sender_cfg['sender_name'])}_\n\n"
            "*Step 1 of 2 · Recipient*\n"
            "Who is this email going to?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌  Cancel",
                    callback_data=f"cancel_email_{template_id}")]
            ]),
            parse_mode='Markdown')
    else:
        # No auto-match — fall back to manual picker
        context.user_data['email_step'] = 'select_sender'
        await query.edit_message_text(
            f"{JM}"
            f"✉️ *Send — {md_safe(name)}*\n\n"
            f"{JM_DIV}\n"
            "*Step 1 of 3 · Select Sender*\n"
            "No sender auto-matched — choose manually:",
            reply_markup=_sender_kb(f"cancel_email_{template_id}"),
            parse_mode='Markdown')


# ═══════════════════════════════════════════
# CUSTOM EMAIL FLOW
# ═══════════════════════════════════════════

async def show_custom_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    cancel_email_flow(context)

    context.user_data['custom_email_mode'] = True
    context.user_data['email_step']        = 'select_sender'
    context.user_data['awaiting_email']    = True

    await query.edit_message_text(
        f"{JM}"
        "✍️ *Custom Email*\n\n"
        f"{JM_DIV}\n"
        "*Step 1 of 5 · Select Sender*\n"
        "Choose an organisation or enter custom sender details:",
        reply_markup=_sender_kb("cancel_custom_email", include_custom=True),
        parse_mode='Markdown')


# ═══════════════════════════════════════════
# SENDER SELECTION HANDLER (fallback / custom)
# ═══════════════════════════════════════════

async def handle_sender_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query      = update.callback_query
    sender_key = query.data.replace("sender_", "")

    if sender_key not in SENDER_CONFIGS:
        await query.answer("Invalid sender", show_alert=True)
        return

    cfg = SENDER_CONFIGS[sender_key]
    context.user_data['sender_name']    = cfg['sender_name']
    context.user_data['sender_email']   = cfg['sender_email']
    context.user_data['reply_to_email'] = cfg['reply_to_email']
    context.user_data['email_step']     = 'recipient'

    is_custom   = context.user_data.get('custom_email_mode', False)
    template_id = context.user_data.get('selected_template_id', '')
    cancel_cb   = "cancel_custom_email" if is_custom else f"cancel_email_{template_id}"
    step_label  = "Step 2 of 5" if is_custom else "Step 2 of 3"

    await query.edit_message_text(
        f"{JM}"
        f"*{step_label} · Recipient*\n"
        "Who is this email going to?\n\n"
        f"_Sending as:_ `{md_safe(cfg['sender_name'])}`",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌  Cancel", callback_data=cancel_cb)]
        ]),
        parse_mode='Markdown')


# ═══════════════════════════════════════════
# FIELDS PROMPT — one-shot comma entry
# ═══════════════════════════════════════════

async def prompt_fields(update, context: ContextTypes.DEFAULT_TYPE,
                        fields: list, error: str = None):
    """Show the one-shot comma-separated fill prompt."""
    template_id = context.user_data.get('selected_template_id', '')
    cancel_cb   = f"cancel_email_{template_id}"

    labels   = ", ".join(f['label'] for f in fields)
    examples = ", ".join(f.get('example', '...') for f in fields)

    error_line = f"⚠️ *{md_safe(error)}*\n\n" if error else ""

    text = (
        f"{JM}"
        f"{error_line}"
        "📝 *Fill in the details*\n\n"
        f"Enter: `{md_safe(labels)}`\n\n"
        f"_Example:_ `{md_safe(examples)}`"
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌  Cancel", callback_data=cancel_cb)]
    ])

    if update.message:
        await update.message.reply_text(text, reply_markup=kb, parse_mode='Markdown')
    else:
        await update.callback_query.edit_message_text(text, reply_markup=kb,
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

    # ── TOP LEVEL ──
    elif d == "show_templates":
        await show_top_level(update, context)
    elif d == "mailer_favorites":
        await show_favorites(update, context)
    elif d == "custom_email":
        await show_custom_email(update, context)

    # ── GROUP NAVIGATION ──
    elif d.startswith("grp_country_"):
        country_name = d.replace("grp_country_", "")
        await show_country_items(update, context, country_name)

    elif d == "grp_crypto":
        await show_group_items(update, context, "crypto")

    elif d == "grp_email":
        await show_group_items(update, context, "email")

    elif d.startswith("grp_item_country_"):
        # grp_item_country_<country>_<item>
        rest         = d.replace("grp_item_country_", "")
        # country name may contain spaces/underscores — split on last underscore segment
        # We stored country name without modification so we need to find the split point
        # Strategy: try all split points and find the one where country exists
        parts        = rest.split("_")
        found        = False
        for i in range(1, len(parts)):
            country_name = "_".join(parts[:i])
            item_name    = "_".join(parts[i:])
            if country_name in TEMPLATES["countries"] and \
               item_name in TEMPLATES["countries"][country_name]["items"]:
                await show_item_templates(update, context, "country",
                                          item_name, country_name)
                found = True
                break
        if not found:
            await query.answer("Navigation error — please go back.", show_alert=True)

    elif d.startswith("grp_item_crypto_"):
        item_name = d.replace("grp_item_crypto_", "")
        await show_item_templates(update, context, "crypto", item_name)

    elif d.startswith("grp_item_email_"):
        item_name = d.replace("grp_item_email_", "")
        await show_item_templates(update, context, "email", item_name)

    # ── TEMPLATE ACTIONS ──
    elif d.startswith("view_template_"):
        await view_template(update, context)
    elif d.startswith("toggle_favorite_"):
        await toggle_favorite(update, context)
    elif d.startswith("send_template_"):
        await send_email_from_template(update, context)

    # ── SENDER SELECTION (fallback / custom) ──
    elif d.startswith("sender_"):
        await handle_sender_selection(update, context)

    # ── CUSTOM SENDER manual entry ──
    elif d == "custom_sender":
        context.user_data['email_step']     = 'custom_sender_name'
        context.user_data['awaiting_email'] = True
        await query.edit_message_text(
            f"{JM}"
            "✍️ *Custom Email — Custom Sender*\n\n"
            f"{JM_DIV}\n"
            "*Sender Name*\n"
            "e.g. `ANZ Security Team`",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌  Cancel", callback_data="cancel_custom_email")]
            ]),
            parse_mode='Markdown')

    # ── SMTP SELECTION ──
    elif d.startswith("smtp_pick_"):
        smtp_name = d.replace("smtp_pick_", "")
        context.user_data['selected_smtp'] = smtp_name
        await query.answer(f"✅ Selected: {smtp_name}")
        await show_confirm_screen(update, context)
    
    elif d == "select_smtp":
        await show_smtp_selection(update, context)

    # ── CONFIRM ──
    elif d == "confirm_send":
        await do_send_email(update, context)

    elif d == "confirm_edit_custom":
        context.user_data['edit_snapshot'] = {
            k: context.user_data.get(k)
            for k in ['sender_name', 'sender_email', 'reply_to_email',
                      'email_recipient', 'email_subject', 'email_body',
                      'filled_vars', 'raw_body', 'custom_email_mode',
                      'selected_template_id', 'fields_def', 'selected_smtp']
        }
        cancel_email_flow(context)
        context.user_data['custom_email_mode'] = True
        context.user_data['email_step']        = 'select_sender'
        context.user_data['awaiting_email']    = True
        await query.edit_message_text(
            f"{JM}"
            "✍️ *Custom Email — Edit*\n\n"
            f"{JM_DIV}\n"
            "*Step 1 of 5 · Select Sender*\n"
            "Choose an organisation or enter custom sender details:",
            reply_markup=_sender_kb("cancel_edit", include_custom=True),
            parse_mode='Markdown')

    elif d.startswith("confirm_edit_"):
        template_id = d.replace("confirm_edit_", "")
        context.user_data['edit_snapshot'] = {
            k: context.user_data.get(k)
            for k in ['sender_name', 'sender_email', 'reply_to_email',
                      'email_recipient', 'email_subject', 'email_body',
                      'filled_vars', 'raw_body', 'custom_email_mode',
                      'selected_template_id', 'fields_def', 'selected_smtp']
        }
        cancel_email_flow(context)
        context.user_data['selected_template_id'] = template_id
        context.user_data['email_step']           = 'select_sender'
        context.user_data['awaiting_email']       = True
        template = find_template(template_id)
        name     = template['name'] if template and template['name'] else template_id
        await query.edit_message_text(
            f"{JM}"
            f"✏️ *Edit — {md_safe(name)}*\n\n"
            f"{JM_DIV}\n"
            "*Step 1 of 3 · Select Sender*\n"
            "Choose the organisation sending this email:",
            reply_markup=_sender_kb("cancel_edit"),
            parse_mode='Markdown')

    elif d == "cancel_edit":
        snapshot = context.user_data.pop('edit_snapshot', None)
        cancel_email_flow(context)
        if snapshot:
            context.user_data.update({k: v for k, v in snapshot.items() if v is not None})
            context.user_data['awaiting_email'] = True
            await show_confirm_screen(update, context)
        else:
            await start(update, context)

    elif d == "cancel_custom_email":
        context.user_data.pop('edit_snapshot', None)
        cancel_email_flow(context)
        await start(update, context)

    elif d.startswith("cancel_email_"):
        template_id = d.replace("cancel_email_", "")
        context.user_data.pop('edit_snapshot', None)
        cancel_email_flow(context)
        await view_template(update, context, template_id=template_id)

    # ── BACK NAVIGATION ──
    elif d.startswith("back_to_item_"):
        back_cb = d.replace("back_to_item_", "")
        if back_cb.startswith("grp_country_"):
            country_name = back_cb.replace("grp_country_", "")
            await show_country_items(update, context, country_name)
        elif back_cb == "grp_crypto":
            await show_group_items(update, context, "crypto")
        elif back_cb == "grp_email":
            await show_group_items(update, context, "email")
        elif back_cb == "show_templates":
            await show_top_level(update, context)
        else:
            await show_top_level(update, context)

    elif d == "back":
        await start(update, context)


# ═══════════════════════════════════════════
# EMAIL SENDING
# ═══════════════════════════════════════════

def _try_send_via(srv: dict, msg) -> None:
    last_err = None
    for port, use_ssl in [(587, False), (465, True)]:
        conn = None
        try:
            if use_ssl:
                ctx = ssl.create_default_context()
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
               sender_name=None, sender_email=None, reply_to_email=None,
               force_smtp_name=None):  # ← NEW parameter
    if not SMTP_SERVERS:
        raise Exception("No SMTP servers configured. Add SMTP_1_HOST/USER/PASS to .env")

    msg = MIMEMultipart('alternative')
    fallback_from = SMTP_SERVERS[0]["user"] if SMTP_SERVERS else ""
    msg['From']    = (f"{sender_name} <{sender_email}>"
                      if sender_name and sender_email
                      else (sender_email or fallback_from))
    msg['To']      = recipient
    msg['Subject'] = subject
    if reply_to_email:
        msg['Reply-To'] = reply_to_email
    msg.attach(MIMEText(body, 'html'))

    # ── NEW: If user selected a specific SMTP, try only that one ──
    if force_smtp_name:
        for srv in SMTP_SERVERS:
            if srv['name'] == force_smtp_name:
                try:
                    _try_send_via(srv, msg)
                    logger.info(f"Email sent → {recipient} via [{srv['name']}] (forced)")
                    return
                except Exception as e:
                    raise Exception(f"Failed to send via {force_smtp_name}: {e}")
        raise Exception(f"SMTP server '{force_smtp_name}' not found")
    
    # ── FALLBACK: Auto-select from available servers ──
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
    query = update.callback_query
    cd    = context.user_data

    if 'email_body' not in cd:
        template = find_template(cd.get('selected_template_id', ''))
        cd['email_body'] = template['body'] if template else ''

    try:
        send_email(
            recipient       = cd['email_recipient'],
            subject         = cd['email_subject'],
            body            = cd['email_body'],
            sender_name     = cd.get('sender_name'),
            sender_email    = cd.get('sender_email'),
            reply_to_email  = cd.get('reply_to_email'),
            force_smtp_name = cd.get('selected_smtp'),  # ← NEW: Pass selected SMTP
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
            return
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

    # ── CUSTOM SENDER steps ──

    if step == 'custom_sender_name':
        context.user_data['sender_name'] = text
        context.user_data['email_step']  = 'custom_sender_email'
        await update.message.reply_text(
            f"{JM}"
            "*Sender Email*\n"
            "e.g. `noreply@google` or `security@bank.com`",
            reply_markup=cancel_kb, parse_mode='Markdown')

    elif step == 'custom_sender_email':
        ok, msg = validate_sender_email(text)
        if not ok:
            await update.message.reply_text(f"{JM}❌ {msg}",
                                            reply_markup=cancel_kb, parse_mode='Markdown')
            return
        context.user_data['sender_email'] = text
        context.user_data['email_step']   = 'custom_sender_replyto'
        await update.message.reply_text(
            f"{JM}"
            "*Reply-To Email*\n"
            "e.g. `noreply@google.com`",
            reply_markup=cancel_kb, parse_mode='Markdown')

    elif step == 'custom_sender_replyto':
        context.user_data['reply_to_email'] = text
        context.user_data['email_step']     = 'recipient'
        await update.message.reply_text(
            f"{JM}"
            "*Recipient*\n"
            "Who is this email going to?",
            reply_markup=cancel_kb, parse_mode='Markdown')

    # ── SHARED STEPS ──

    elif step == 'recipient':
        context.user_data['email_recipient'] = text
        context.user_data['email_step']      = 'subject'
        step_label = "Step 3 of 5" if is_custom else "Step 2 of 2"
        await update.message.reply_text(
            f"{JM}"
            f"*{step_label} · Subject line*\n"
            "What's the email subject?",
            reply_markup=cancel_kb, parse_mode='Markdown')

    elif step == 'subject':
        context.user_data['email_subject'] = text

        # Custom email → ask for body
        if is_custom:
            context.user_data['email_step'] = 'custom_body'
            await update.message.reply_text(
                f"{JM}"
                "*Step 4 of 5 · Email body*\n\n"
                "Either:\n"
                "• Type your body below _(plain text or HTML)_\n"
                "• Upload an *.html* file and it will be read automatically",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌  Cancel", callback_data="cancel_custom_email")]
                ]),
                parse_mode='Markdown')
            return

        # Template flow — check for sidecar fields first
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
        fields   = template.get('fields', [])

        if fields:
            # ── NEW: one-shot comma entry ──
            context.user_data['fields_def']  = fields
            context.user_data['email_step']  = 'fill_fields'
            await prompt_fields(update, context, fields)

        else:
            # ── FALLBACK: old placeholder detection ──
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
                # Convert detected placeholders into fields_def format
                # and use the same one-shot comma flow as sidecar fields
                fields = [{"label": v, "example": f"value{i+1}"}
                          for i, v in enumerate(unique)]
                context.user_data['fields_def']  = fields
                context.user_data['filled_vars'] = {}
                context.user_data['email_step']  = 'fill_fields'
                await prompt_fields(update, context, fields)
            else:
                context.user_data['email_body']  = raw_body
                context.user_data['filled_vars'] = {}
                await show_confirm_screen(update, context)

    elif step == 'custom_body':
        context.user_data['email_body']  = text
        context.user_data['filled_vars'] = {}
        await show_confirm_screen(update, context)

    # ── NEW: one-shot fields entry ──
    elif step == 'fill_fields':
        fields = context.user_data.get('fields_def', [])
        parts  = [p.strip() for p in text.split(',')]

        if len(parts) != len(fields):
            error = f"Expected {len(fields)} values, got {len(parts)} — please try again"
            await prompt_fields(update, context, fields, error=error)
            return

        filled = {f['label']: parts[i] for i, f in enumerate(fields)}
        context.user_data['filled_vars'] = filled

        # Substitute into body — try all known placeholder patterns
        body = context.user_data['raw_body']
        for label, val in filled.items():
            slug = label.replace(' ', '_')
            body = body.replace(f"[{label}]",        val)
            body = body.replace(f"[{slug}]",          val)
            body = body.replace(f"{{{{{label}}}}}",   val)
            body = body.replace(f"{{{{{slug}}}}}",    val)
            body = body.replace(f"{{{label}}}",        val)
            body = body.replace(f"{{{slug}}}",         val)
            body = body.replace(f"${label}",           val)
            body = body.replace(f"${slug}",            val)
            # Case-insensitive sweep
            body = re.sub(re.escape(f"[{label}]"),      val, body, flags=re.IGNORECASE)
            body = re.sub(re.escape(f"[{slug}]"),       val, body, flags=re.IGNORECASE)

        context.user_data['email_body'] = body
        await show_confirm_screen(update, context)

    # ── OLD: one-by-one placeholder fill (fallback) ──
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
    user_id = update.effective_user.id

    if not is_allowed(user_id):
        await deny(update)
        return

    register_user(update.effective_user)

    doc = update.message.document if update.message else None
    if not doc:
        return

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

    fname   = doc.file_name or ""
    mime    = doc.mime_type or ""
    is_html = fname.lower().endswith('.html') or 'html' in mime.lower()

    if not is_html:
        await update.message.reply_text(
            f"{JM}"
            f"❌ *Wrong file type:* `{md_safe(fname or mime)}`\n\n"
            "Please upload a `.html` file, or type your body as text.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌  Cancel", callback_data="cancel_custom_email")]
            ]),
            parse_mode='Markdown')
        return

    if doc.file_size and doc.file_size > 1_048_576:
        await update.message.reply_text(
            f"{JM}❌ File too large (max 1 MB). Please upload a smaller HTML file.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌  Cancel", callback_data="cancel_custom_email")]
            ]),
            parse_mode='Markdown')
        return

    await update.message.reply_text(f"{JM}⏳ Reading your HTML file…", parse_mode='Markdown')

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
            f"{JM}❌ Could not read the file: `{md_safe(str(e))}`\n\nPlease try again.",
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

    plain   = html_to_text(html_body)
    preview = plain[:200].strip()
    if len(plain) > 200:
        preview += "…"

    display_name = fname if fname else "uploaded file"
    await update.message.reply_text(
        f"{JM}"
        f"✅ *Loaded:* `{md_safe(display_name)}`\n"
        f"📏 `{len(html_body):,}` bytes\n\n"
        f"📝 *Preview:*\n`{md_safe(preview)}`",
        parse_mode='Markdown')

    await show_confirm_screen(update, context)


async def _build_body_and_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Substitute filled vars (old one-by-one flow) into raw body."""
    body = context.user_data['raw_body']
    for var, val in context.user_data['filled_vars'].items():
        body = body.replace(f"[{var}]",        val)
        body = body.replace(f"{{{{{var}}}}}",   val)
        body = body.replace(f"{{{var}}}",        val)
        body = body.replace(f"${var}",           val)
        body = re.sub(re.escape(f"[{var}]"),        val, body, flags=re.IGNORECASE)
        body = re.sub(re.escape(f"{{{{{var}}}}}"),   val, body, flags=re.IGNORECASE)
        body = re.sub(r'(?<!\{)\{' + re.escape(var) + r'\}(?!\})',
                      val, body, flags=re.IGNORECASE)
        body = re.sub(re.escape(f"${var}"),          val, body, flags=re.IGNORECASE)
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
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)

    # Count templates
    total = 0
    for cdata in TEMPLATES["countries"].values():
        total += sum(len(i["templates"]) for i in cdata["items"].values())
    for idata in TEMPLATES["crypto"].values():
        total += len(idata["templates"])
    for idata in TEMPLATES["email"].values():
        total += len(idata["templates"])

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
