import os
import uuid
import asyncio
import aiosqlite
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)
from telegram.request import HTTPXRequest
from telegram.error import TelegramError

# ────────────────────────────────────────────────
# CONFIG
# ────────────────────────────────────────────────

BOT_USERNAME          = "OnlyHubServerBot"
ALLOWED_UPLOADERS     = [8295342154, 7025490921]
FORCE_CHANNEL_USERNAME = "only_hub69"
FORCE_CHANNEL_URL     = "https://t.me/only_hub69"
STORAGE_CHANNEL_ID    = -1003893001355
AUTO_DELETE_SECONDS   = 600
DB_FILE               = "files.db"

# Conversation states
UPLOAD_FILES, ADD_CAPTION = range(2)

# ────────────────────────────────────────────────
# DATABASE
# ────────────────────────────────────────────────

async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS batches (
                batch_id TEXT PRIMARY KEY,
                caption TEXT
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS files (
                batch_id TEXT,
                channel_msg_id INTEGER,
                FOREIGN KEY (batch_id) REFERENCES batches (batch_id)
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS stats (
                batch_id TEXT PRIMARY KEY,
                downloads INTEGER DEFAULT 0,
                FOREIGN KEY (batch_id) REFERENCES batches (batch_id)
            )
        ''')
        await db.commit()

# ────────────────────────────────────────────────
# HANDLERS
# ────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Welcome! Use /newbatch to start uploading (admins only).")
        return

    batch_id = args[0]

    if not await check_batch_exists(batch_id):
        await update.message.reply_text("Invalid or expired link.")
        return

    user_id = update.effective_user.id
    if not await check_membership(context, user_id):
        keyboard = [
            [InlineKeyboardButton("Join Channel", url=FORCE_CHANNEL_URL)],
            [InlineKeyboardButton("I've joined → Check", callback_data=f"check_join_{batch_id}")]
        ]
        await update.message.reply_text(
            "You must join the channel first to view the files.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    await send_files(update, context, batch_id)


async def check_membership(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id=f"@{FORCE_CHANNEL_USERNAME}", user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except:
        return False


async def send_files(update: Update, context: ContextTypes.DEFAULT_TYPE, batch_id: str):
    chat_id = update.effective_chat.id
    to_delete = []

    warning = await context.bot.send_message(
        chat_id,
        "⚠️ Save or forward these files now!\nThey will be automatically deleted from this chat in 10 minutes."
    )
    to_delete.append(warning.message_id)

    caption, msg_ids = await get_batch_data(batch_id)

    if caption:
        cap_msg = await context.bot.send_message(chat_id, caption)
        to_delete.append(cap_msg.message_id)

    for mid in msg_ids:
        sent = await context.bot.copy_message(
            chat_id=chat_id,
            from_chat_id=STORAGE_CHANNEL_ID,
            message_id=mid
        )
        to_delete.append(sent.message_id)

    await increment_downloads(batch_id)

    # Schedule auto-delete
    context.job_queue.run_once(
        callback=delete_messages,
        when=AUTO_DELETE_SECONDS,
        data={"chat_id": chat_id, "message_ids": to_delete},
        name=f"autodel_{chat_id}_{uuid.uuid4().hex[:8]}"
    )


async def delete_messages(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id = job.data["chat_id"]
    msg_ids = job.data["message_ids"]

    for mid in msg_ids:
        try:
            await context.bot.delete_message(chat_id, mid)
        except:
            pass


async def check_batch_exists(batch_id: str) -> bool:
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT 1 FROM batches WHERE batch_id = ?", (batch_id,))
        return bool(await cur.fetchone())


async def get_batch_data(batch_id: str):
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT caption FROM batches WHERE batch_id = ?", (batch_id,))
        row = await cur.fetchone()
        caption = row[0] if row else None

        cur = await db.execute("SELECT channel_msg_id FROM files WHERE batch_id = ?", (batch_id,))
        rows = await cur.fetchall()
        msg_ids = [r[0] for r in rows]

        return caption, msg_ids


async def increment_downloads(batch_id: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT OR IGNORE INTO stats (batch_id, downloads) VALUES (?, 0)",
            (batch_id,)
        )
        await db.execute(
            "UPDATE stats SET downloads = downloads + 1 WHERE batch_id = ?",
            (batch_id,)
        )
        await db.commit()


async def newbatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_UPLOADERS:
        await update.message.reply_text("Access denied.")
        return ConversationHandler.END

    context.user_data["batch_files"] = []
    keyboard = [[InlineKeyboardButton("Done", callback_data="done_upload")]]
    await update.message.reply_text(
        "Send photos/videos/audio/documents.\nClick Done when finished.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return UPLOAD_FILES


async def upload_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_UPLOADERS:
        return

    msg = update.message
    if not any([msg.photo, msg.video, msg.audio, msg.document]):
        return

    copied = await msg.copy(chat_id=STORAGE_CHANNEL_ID)
    context.user_data["batch_files"].append(copied.message_id)

    await msg.reply_text("File added to batch.")


async def handle_done_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    files = context.user_data.get("batch_files", [])
    if not files:
        await query.message.edit_text("No files were uploaded. Operation cancelled.")
        return ConversationHandler.END

    await query.message.edit_text("Send caption for this batch (or /skip):")
    return ADD_CAPTION


async def set_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["batch_caption"] = update.message.text
    await finalize_batch(update, context)
    return ConversationHandler.END


async def skip_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["batch_caption"] = None
    await finalize_batch(update, context)
    return ConversationHandler.END


async def finalize_batch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    batch_id = uuid.uuid4().hex[:12]
    files = context.user_data.get("batch_files", [])
    caption = context.user_data.get("batch_caption")

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT INTO batches (batch_id, caption) VALUES (?, ?)", (batch_id, caption))
        for fid in files:
            await db.execute("INSERT INTO files (batch_id, channel_msg_id) VALUES (?, ?)", (batch_id, fid))
        await db.execute("INSERT OR IGNORE INTO stats (batch_id) VALUES (?)", (batch_id,))
        await db.commit()

    link = f"https://t.me/{BOT_USERNAME}?start={batch_id}"
    await update.message.reply_text(f"Batch created!\nPermanent link:\n{link}")

    # Cleanup
    for k in ["batch_files", "batch_caption"]:
        context.user_data.pop(k, None)


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_UPLOADERS:
        await update.message.reply_text("Access denied.")
        return

    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT COUNT(*) FROM batches")
        total_batches = (await cur.fetchone())[0]

        cur = await db.execute("SELECT COUNT(*) FROM files")
        total_files = (await cur.fetchone())[0]

        cur = await db.execute("SELECT COALESCE(SUM(downloads), 0) FROM stats")
        total_downloads = (await cur.fetchone())[0]

        text = (
            f"📊 Statistics\n\n"
            f"Total batches: {total_batches}\n"
            f"Total files stored: {total_files}\n"
            f"Total downloads: {total_downloads}\n\n"
            f"Per-batch breakdown:\n"
        )

        cur = await db.execute("""
            SELECT b.batch_id, s.downloads, COUNT(f.channel_msg_id)
            FROM batches b
            LEFT JOIN stats s ON b.batch_id = s.batch_id
            LEFT JOIN files f ON b.batch_id = f.batch_id
            GROUP BY b.batch_id
            ORDER BY s.downloads DESC
        """)
        for row in await cur.fetchall():
            bid, dl, fc = row
            text += f"• {bid} → {fc} files • {dl or 0} downloads\n"

        await update.message.reply_text(text)


async def check_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, batch_id = query.data.split("_", 2)[1:]

    if await check_membership(context, query.from_user.id):
        await query.message.edit_text("Access granted. Sending files...")
        await send_files(update, context, batch_id)
    else:
        await query.answer("You still haven't joined the channel.", show_alert=True)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print(f"Exception: {context.error}")


# ────────────────────────────────────────────────
# MAIN
# ────────────────────────────────────────────────

def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN environment variable is not set")

    request = HTTPXRequest(connect_timeout=15, read_timeout=30)

    application = (
        Application.builder()
        .token(token)
        .request(request)
        .get_updates_read_timeout(30)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[CommandHandler("newbatch", newbatch)],
        states={
            UPLOAD_FILES: [
                MessageHandler(
                    filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.Document.ALL,
                    upload_file
                ),
                CallbackQueryHandler(handle_done_upload, pattern="^done_upload$"),
            ],
            ADD_CAPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_caption),
                CommandHandler("skip", skip_caption),
            ],
        },
        fallbacks=[],
        # Recommended setting for CallbackQueryHandler inside ConversationHandler
        per_message=False,
    )

    application.add_handler(conv)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CallbackQueryHandler(check_join_callback, pattern="^check_join_"))
    application.add_error_handler(error_handler)

    print("Bot starting (polling mode)...")
    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
        poll_interval=0.5,
    )


if __name__ == "__main__":
    # Run init_db synchronously once at startup
    asyncio.run(init_db())
    main()
