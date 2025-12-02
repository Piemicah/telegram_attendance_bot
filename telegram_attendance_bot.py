"""
Telegram Attendance Bot (single-file)
Features implemented:
- Group-aware: stores groups by chat_id
- Member registration and admin assignment
- Create attendance sessions (one-off)
- Post attendance message with inline buttons for Present/Absent/Late
- Users mark attendance via callback buttons
- Export CSV report for a session
- Schedule recurring sessions using APScheduler

Dependencies:
- python-telegram-bot>=20.0
- APScheduler

Run:
$ pip install python-telegram-bot==20.6 apscheduler
$ export TELEGRAM_TOKEN="<your token>"
$ python telegram_attendance_bot.py

Note: This code is a production-ready starting point. You can extend it with a web dashboard, Excel export, and authentication.
"""

import os
import sqlite3
import csv
import io
import logging
from datetime import datetime, date
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    Application,
    MessageHandler,
    filters,
)

# -------- Configuration & Logging --------
TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TOKEN:
    raise RuntimeError("Set TELEGRAM_TOKEN environment variable with your bot token")

DB_PATH = os.environ.get("ATTENDANCE_DB", "attendance_bot.db")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -------- Database helpers --------


def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = conn.cursor()
    # groups table (one row per chat/group)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER UNIQUE,
            group_name TEXT,
            created_at TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER,
            telegram_id INTEGER,
            full_name TEXT,
            role TEXT DEFAULT 'member',
            active INTEGER DEFAULT 1,
            UNIQUE(group_id, telegram_id),
            FOREIGN KEY (group_id) REFERENCES groups(id)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS attendance_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER,
            session_date TEXT,
            session_title TEXT,
            message_id INTEGER,
            created_by INTEGER,
            created_at TEXT,
            FOREIGN KEY (group_id) REFERENCES groups(id)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS attendance_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            member_id INTEGER,
            status TEXT,
            timestamp TEXT,
            UNIQUE(session_id, member_id),
            FOREIGN KEY (session_id) REFERENCES attendance_sessions(id),
            FOREIGN KEY (member_id) REFERENCES members(id)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS scheduled_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER,
            cron_expr TEXT,
            job_name TEXT,
            created_at TEXT
        )
        """
    )

    conn.commit()
    return conn


conn = init_db()


def get_group_by_chat(chat_id):
    cur = conn.cursor()
    cur.execute("SELECT id, group_name FROM groups WHERE chat_id = ?", (chat_id,))
    return cur.fetchone()  # (id, group_name) or None


def ensure_group(chat_id, group_name=None):
    g = get_group_by_chat(chat_id)
    cur = conn.cursor()
    if g:
        return g[0]  # group_id
    created_at = datetime.utcnow().isoformat()
    cur.execute(
        "INSERT OR IGNORE INTO groups (chat_id, group_name, created_at) VALUES (?, ?, ?)",
        (chat_id, group_name or str(chat_id), created_at),
    )
    conn.commit()
    return get_group_by_chat(chat_id)[0]


def add_member_db(group_id, telegram_id, full_name, role="member"):
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO members (group_id, telegram_id, full_name, role) VALUES (?, ?, ?, ?)",
            (group_id, telegram_id, full_name, role),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # already exists - update name/role
        cur.execute(
            "UPDATE members SET full_name = ?, role = ?, active = 1 WHERE group_id = ? AND telegram_id = ?",
            (full_name, role, group_id, telegram_id),
        )
        conn.commit()


def get_member_by_telegram(group_id, telegram_id):
    cur = conn.cursor()
    cur.execute(
        "SELECT id, full_name, role FROM members WHERE group_id = ? AND telegram_id = ? AND active = 1",
        (group_id, telegram_id),
    )
    return cur.fetchone()  # (id, full_name, role)


def get_all_members(group_id):
    cur = conn.cursor()
    cur.execute(
        "SELECT id, telegram_id, full_name, role FROM members WHERE group_id = ? AND active = 1 ORDER BY full_name",
        (group_id,),
    )
    return cur.fetchall()


def create_session_db(group_id, session_title, created_by):
    cur = conn.cursor()
    session_date = date.today().isoformat()
    created_at = datetime.utcnow().isoformat()
    cur.execute(
        "INSERT INTO attendance_sessions (group_id, session_date, session_title, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
        (group_id, session_date, session_title, created_by, created_at),
    )
    conn.commit()
    return cur.lastrowid


def set_session_message_id(session_id, message_id):
    cur = conn.cursor()
    cur.execute(
        "UPDATE attendance_sessions SET message_id = ? WHERE id = ?",
        (message_id, session_id),
    )
    conn.commit()


def record_attendance(session_id, member_id, status):
    cur = conn.cursor()
    timestamp = datetime.utcnow().isoformat()
    try:
        cur.execute(
            "INSERT INTO attendance_records (session_id, member_id, status, timestamp) VALUES (?, ?, ?, ?)",
            (session_id, member_id, status, timestamp),
        )
    except sqlite3.IntegrityError:
        # update
        cur.execute(
            "UPDATE attendance_records SET status = ?, timestamp = ? WHERE session_id = ? AND member_id = ?",
            (status, timestamp, session_id, member_id),
        )
    conn.commit()


def get_session_records(session_id):
    cur = conn.cursor()
    cur.execute(
        "SELECT m.full_name, r.status, r.timestamp FROM attendance_records r JOIN members m ON m.id = r.member_id WHERE r.session_id = ? ORDER BY m.full_name",
        (session_id,),
    )
    return cur.fetchall()


# -------- Scheduler --------
scheduler = BackgroundScheduler()
scheduler.start()


def schedule_attendance_job(group_chat_id, cron_expr, job_name, application):
    # cron_expr is a dict suitable for CronTrigger e.g. {'day_of_week':'sun','hour':9,'minute':0}
    trigger = CronTrigger(**cron_expr)

    def job_func():
        # runs in background thread; need to use application.create_task to call async function
        logger.info(f"Scheduler firing job {job_name} for chat {group_chat_id}")
        application.create_task(post_scheduled_attendance(group_chat_id))

    sched_job = scheduler.add_job(
        job_func, trigger, id=f"job-{group_chat_id}-{job_name}"
    )
    # persist job info
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO scheduled_jobs (group_id, cron_expr, job_name, created_at) VALUES ((SELECT id FROM groups WHERE chat_id = ?), ?, ?, ?)",
        (group_chat_id, str(cron_expr), job_name, datetime.utcnow().isoformat()),
    )
    conn.commit()
    return sched_job


async def post_scheduled_attendance(chat_id: int):
    # Create session and post attendance message to chat
    group = get_group_by_chat(chat_id)
    if not group:
        logger.warning("Scheduled job: group not found for chat %s", chat_id)
        return
    group_id = group[0]
    # Use bot from application
    # We can get a new application instance via ApplicationBuilder? Instead we will store the app globally in on_startup
    global APP_INSTANCE
    if not APP_INSTANCE:
        logger.error("Application instance not set; cannot post scheduled attendance")
        return
    # default title
    session_id = create_session_db(
        group_id, f"Scheduled attendance {date.today().isoformat()}", None
    )
    members = get_all_members(group_id)
    if not members:
        await APP_INSTANCE.bot.send_message(
            chat_id=chat_id, text="No members registered yet for attendance."
        )
        return
    keyboard = []
    # Make a keyboard with a button per member (or grouped) -- for simplicity we provide Present/Absent/Late buttons once user clicks their row
    # We'll post a message with instructions and list members so they can click their name (we'll create buttons with callback data including member_id and session_id)
    for m in members:
        m_id, tg_id, full_name, role = m
        keyboard.append(
            [InlineKeyboardButton(full_name, callback_data=f"mark:{session_id}:{m_id}")]
        )
    reply_markup = InlineKeyboardMarkup(keyboard)
    msg = await APP_INSTANCE.bot.send_message(
        chat_id=chat_id,
        text=f"Attendance time! Click your name to mark attendance for {date.today().isoformat()}\n(After clicking your name choose Present/Absent/Late)",
        reply_markup=reply_markup,
    )
    set_session_message_id(session_id, msg.message_id)


# APP_INSTANCE will be set in main()
APP_INSTANCE = None

# -------- Command handlers (async) --------


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "Commands:\n"
        "/register - To register for the meeting(every member)\n"
        "/add_member @username - To add member(Admin only)\n "
        "/promote ID - To promote member to admin role,where ID is the member's Telegram numeric ID(Admin only)\n"
        "/attendance - bot posts a list of names with buttons(every member)\n"
        "/report latest - see who is present/absent(every member)\n"
        "/export latest - downloads a perfect CSV file(every member)"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type in ("group", "supergroup"):
        # ensure group exists
        group_id = ensure_group(chat.id, chat.title)
        admins = await context.bot.get_chat_administrators(chat.id)
        if user.id in [admin.user.id for admin in admins]:
            add_member_db(group_id, user.id, user.full_name, role="admin")
        # add the user as admin by default if they are group creator? We keep simple: user must /register to be member
        await update.message.reply_text(
            "Hello! I'm AttendanceBot. Members should register using /register <Full name>. Admins can add members with /add_member @username Full Name."
        )
    else:
        await update.message.reply_text(
            "Use me inside a group where you manage attendance."
        )


async def register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text(
            "Please register from within a group chat where attendance is taken."
        )
        return
    group_id = ensure_group(chat.id, chat.title)
    # parse name
    if context.args:
        full_name = " ".join(context.args)
    else:
        # use Telegram name fallback
        full_name = user.full_name
    add_member_db(group_id, user.id, full_name)
    await update.message.reply_text(f"Registered {full_name} for attendance.")


async def add_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Admin-only command: /add_member @username Full Name
    chat = update.effective_chat
    user = update.effective_user
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("This command must be run in a group chat.")
        return
    group = get_group_by_chat(chat.id)
    if not group:
        await update.message.reply_text(
            "Group not registered. Ask someone to /start the bot in this group first."
        )
        return
    group_id = group[0]
    # simple admin check: check if the invoking user is in members table with role admin
    invoker = get_member_by_telegram(group_id, user.id)
    if not invoker or invoker[2] != "admin":
        await update.message.reply_text(
            "Only group admins (set via database) can add members. Register yourself and ask an admin to promote you."
        )
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: /add_member <telegram_id or @username> Full Name"
        )
        return
    # For simplicity: accept numeric telegram id or mention
    first = context.args[0]
    full_name = " ".join(context.args[1:]) if len(context.args) > 1 else first
    # try to extract id
    tg_id = None
    if first.startswith("@"):
        # can't resolve username to id without extra API call; instead require numeric id optionally
        await update.message.reply_text(
            "Please provide the user's numeric Telegram id instead of @username, or ask them to /register."
        )
        return
    else:
        try:
            tg_id = int(first)
        except ValueError:
            tg_id = None
    if not tg_id:
        await update.message.reply_text(
            "Couldn't parse telegram id. Example: /add_member 123456789 "
        )
        return
    add_member_db(group_id, tg_id, full_name)
    await update.message.reply_text(f"Added {full_name} to the group.")


async def promote_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /promote <telegram_id> - promote to admin
    chat = update.effective_chat
    user = update.effective_user
    group = get_group_by_chat(chat.id)
    if not group:
        await update.message.reply_text("Group not registered.")
        return
    group_id = group[0]
    invoker = get_member_by_telegram(group_id, user.id)
    if not invoker or invoker[2] != "admin":
        await update.message.reply_text("Only group admins can promote members.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /promote <telegram_id>")
        return
    try:
        tg_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid id.")
        return
    cur = conn.cursor()
    cur.execute(
        "UPDATE members SET role = 'admin' WHERE group_id = ? AND telegram_id = ?",
        (group_id, tg_id),
    )
    conn.commit()
    await update.message.reply_text("Promoted user to admin.")


async def attendance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /attendance [Optional session title]
    chat = update.effective_chat
    user = update.effective_user
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Run this command in a group chat.")
        return
    group = get_group_by_chat(chat.id)
    if not group:
        await update.message.reply_text(
            "Group not registered. Use /start in the group first."
        )
        return
    group_id = group[0]
    title = (
        " ".join(context.args)
        if context.args
        else f"Attendance {date.today().isoformat()}"
    )
    session_id = create_session_db(group_id, title, user.id)
    members = get_all_members(group_id)
    if not members:
        await update.message.reply_text(
            "No members registered yet. Members should run /register or admins can add them."
        )
        return
    keyboard = []
    for m in members:
        m_id, tg_id, full_name, role = m
        # clicking a name will ask the user to choose status
        keyboard.append(
            [
                InlineKeyboardButton(
                    full_name, callback_data=f"choose:{session_id}:{m_id}"
                )
            ]
        )
    reply_markup = InlineKeyboardMarkup(keyboard)
    msg = await update.message.reply_text(
        f"Attendance started: {title}\nClick your name to mark attendance.",
        reply_markup=reply_markup,
    )
    set_session_message_id(session_id, msg.message_id)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = update.effective_user
    # data formats: choose:session:member , mark:session:member:status
    parts = data.split(":")
    if parts[0] == "choose":
        _, session_id, member_id = parts
        # Only allow the real Telegram user who matches member's telegram_id to mark themselves, or allow admins
        cur = conn.cursor()
        cur.execute(
            "SELECT telegram_id, full_name FROM members WHERE id = ?", (member_id,)
        )
        row = cur.fetchone()
        if not row:
            await query.edit_message_text("Member not found (maybe removed).")
            return
        member_tg_id, full_name = row
        # if the clicking user is not the member and not an admin, deny
        group_id = (
            conn.cursor()
            .execute(
                "SELECT group_id FROM attendance_sessions WHERE id = ?", (session_id,)
            )
            .fetchone()[0]
        )
        invoker = get_member_by_telegram(group_id, user.id)
        if user.id != member_tg_id and (not invoker or invoker[2] != "admin"):
            await query.answer("You cannot mark for another member.", show_alert=True)
            return
        keyboard = [
            [
                InlineKeyboardButton(
                    "Present", callback_data=f"mark:{session_id}:{member_id}:present"
                )
            ],
            [
                InlineKeyboardButton(
                    "Late", callback_data=f"mark:{session_id}:{member_id}:late"
                )
            ],
            [
                InlineKeyboardButton(
                    "Absent", callback_data=f"mark:{session_id}:{member_id}:absent"
                )
            ],
        ]
        await query.message.reply_text(
            f"{full_name} — choose your attendance status:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif parts[0] == "mark":
        # mark:session:member:status
        _, session_id, member_id, status = parts
        # verify
        cur = conn.cursor()
        cur.execute(
            "SELECT telegram_id, full_name FROM members WHERE id = ?", (member_id,)
        )
        row = cur.fetchone()
        if not row:
            await query.answer("Member not found", show_alert=True)
            return
        member_tg_id, full_name = row
        group_id = (
            conn.cursor()
            .execute(
                "SELECT group_id FROM attendance_sessions WHERE id = ?", (session_id,)
            )
            .fetchone()[0]
        )
        invoker = get_member_by_telegram(group_id, user.id)
        if user.id != member_tg_id and (not invoker or invoker[2] != "admin"):
            await query.answer("You cannot mark for another member.", show_alert=True)
            return
        record_attendance(session_id, member_id, status)
        await query.answer(f"Marked {full_name} as {status}")
        # Optionally edit message or reply
        await query.message.reply_text(
            f"{full_name} marked as {status} by {user.full_name}"
        )


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /report <session_id> or /report latest
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("This command must be used in the group chat.")
        return
    group = get_group_by_chat(chat.id)
    if not group:
        await update.message.reply_text("Group not registered.")
        return
    group_id = group[0]
    cur = conn.cursor()
    if context.args and context.args[0].lower() == "latest":
        cur.execute(
            "SELECT id, session_title, session_date FROM attendance_sessions WHERE group_id = ? ORDER BY id DESC LIMIT 1",
            (group_id,),
        )
    elif context.args:
        try:
            sid = int(context.args[0])
            cur.execute(
                "SELECT id, session_title, session_date FROM attendance_sessions WHERE id = ? AND group_id = ?",
                (sid, group_id),
            )
        except ValueError:
            await update.message.reply_text("Invalid session id")
            return
    else:
        await update.message.reply_text("Usage: /report latest OR /report <session_id>")
        return
    row = cur.fetchone()
    if not row:
        await update.message.reply_text("Session not found.")
        return
    session_id, title, session_date = row
    records = get_session_records(session_id)
    summary = {}
    for name, status, ts in records:
        summary.setdefault(status, 0)
        summary[status] += 1
    text = f"Report for {title} (id={session_id}, date={session_date})\n"
    text += "\n".join([f"{k}: {v}" for k, v in summary.items()]) or "No records yet"
    await update.message.reply_text(text)


async def export_csv_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /export <session_id|latest>
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("This command must be used in the group chat.")
        return
    group = get_group_by_chat(chat.id)
    if not group:
        await update.message.reply_text("Group not registered.")
        return
    group_id = group[0]
    cur = conn.cursor()
    if context.args and context.args[0].lower() == "latest":
        cur.execute(
            "SELECT id, session_title, session_date FROM attendance_sessions WHERE group_id = ? ORDER BY id DESC LIMIT 1",
            (group_id,),
        )
    elif context.args:
        try:
            sid = int(context.args[0])
            cur.execute(
                "SELECT id, session_title, session_date FROM attendance_sessions WHERE id = ? AND group_id = ?",
                (sid, group_id),
            )
        except ValueError:
            await update.message.reply_text("Invalid session id")
            return
    else:
        await update.message.reply_text("Usage: /export latest OR /export <session_id>")
        return
    row = cur.fetchone()
    if not row:
        await update.message.reply_text("Session not found.")
        return
    session_id, title, session_date = row
    records = (
        conn.cursor()
        .execute(
            "SELECT m.full_name, r.status, r.timestamp FROM attendance_records r JOIN members m ON m.id = r.member_id WHERE r.session_id = ? ORDER BY m.full_name",
            (session_id,),
        )
        .fetchall()
    )
    # create CSV in memory
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Full Name", "Status", "Timestamp"])
    for r in records:
        writer.writerow(r)
    output.seek(0)
    bio = io.BytesIO(output.read().encode("utf-8"))
    bio.name = f"attendance_session_{session_id}.csv"
    bio.seek(0)
    await update.message.reply_document(document=InputFile(bio, filename=bio.name))


async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /schedule day_of_week hour minute job_name e.g. /schedule sun 9 0 sunday_service
    chat = update.effective_chat
    user = update.effective_user
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("This command must be used in the group chat.")
        return
    group = get_group_by_chat(chat.id)
    if not group:
        await update.message.reply_text("Group not registered.")
        return
    group_id = group[0]
    invoker = get_member_by_telegram(group_id, user.id)
    if not invoker or invoker[2] != "admin":
        await update.message.reply_text("Only group admins can schedule sessions.")
        return
    if len(context.args) < 4:
        await update.message.reply_text(
            "Usage: /schedule <day_of_week> <hour> <minute> <job_name>\nExample: /schedule sun 9 0 sunday_service"
        )
        return
    day_of_week = context.args[0]
    try:
        hour = int(context.args[1])
        minute = int(context.args[2])
    except ValueError:
        await update.message.reply_text("Hour and minute must be integers.")
        return
    job_name = context.args[3]
    cron_expr = {"day_of_week": day_of_week, "hour": hour, "minute": minute}
    schedule_attendance_job(chat.id, cron_expr, job_name, APP_INSTANCE)
    await update.message.reply_text(
        f"Scheduled job {job_name} on {day_of_week} at {hour}:{minute:02d}"
    )


# -------- Startup and main --------


def register_handlers(app):
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("register", register))
    app.add_handler(CommandHandler("add_member", add_member))
    app.add_handler(CommandHandler("promote", promote_member))
    app.add_handler(CommandHandler("attendance", attendance_command))
    app.add_handler(CommandHandler("report", report_command))
    app.add_handler(CommandHandler("export", export_csv_command))
    app.add_handler(CommandHandler("schedule", schedule_command))
    app.add_handler(CallbackQueryHandler(callback_handler))


async def post_init(application):
    # Called after app starts
    global APP_INSTANCE
    APP_INSTANCE = application
    logger.info("Attendance bot started and ready")


def main():
    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)  # This is now correct
        .build()
    )
    register_handlers(application)
    application.run_polling()


if __name__ == "__main__":
    main()
