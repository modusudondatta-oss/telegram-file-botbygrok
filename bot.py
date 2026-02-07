import os
import uuid
import asyncio
import aiosqlite
from datetime import timedelta
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ConversationHandler, filters
from telegram.request import HTTPXRequest
from telegram.error import TelegramError

# Constants
BOT_USERNAME = "OnlyHubServerBot"
ALLOWED_UPLOADERS = [8295342154, 7025490921]
FORCE_CHANNEL_USERNAME = "only_hub69"
FORCE_CHANNEL_URL = "https://t.me/only_hub69"
STORAGE_CHANNEL_ID = -1003893001355
AUTO_DELETE_SECONDS = 600
DB_FILE = "files.db"

# Conversation states
UPLOAD_FILES, ADD_CAPTION = range(2)

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

async def start(update: Update, context):
    user_id = update.effective_user.id
    args = context.args
    if args:
        batch_id = args[0]
        # Check if batch exists
        exists = await check_batch_exists(batch_id)
        if not exists:
            await update.message.reply_text("Invalid link.")
            return
        # Check channel membership
        joined = await check_membership(context, user_id)
        if not joined:
            keyboard = [
                [InlineKeyboardButton("Join Channel", url=FORCE_CHANNEL_URL)],
                [InlineKeyboardButton("I already joined", callback_data=f"check_join_{batch_id}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text("You must join the channel to access the files.", reply_markup=reply_markup)
            return
        # Proceed to send files
        await send_files(update, context, batch_id)
    else:
        await update.message.reply_text("Welcome! Use /newbatch to start uploading files if you are an admin.")

async def check_membership(context, user_id):
    try:
        member = await context.bot.get_chat_member(f"@{FORCE_CHANNEL_USERNAME}", user_id)
        return member.status in ['member', 'administrator', 'creator']
    except TelegramError:
        return False

async def send_files(update: Update, context, batch_id):
    chat_id = update.effective_chat.id
    msg_ids = []
    # Send warning
    warning_msg = await context.bot.send_message(chat_id, "Please save or forward the files. They will auto-delete after 10 minutes.")
    msg_ids.append(warning_msg.message_id)
    # Get caption and msg_ids
    caption, channel_msg_ids = await get_batch_data(batch_id)
    if caption:
        caption_msg = await context.bot.send_message(chat_id, caption)
        msg_ids.append(caption_msg.message_id)
    # Send files
    for msg_id in channel_msg_ids:
        sent_msg = await context.bot.copy_message(
            chat_id=chat_id,
            from_chat_id=STORAGE_CHANNEL_ID,
            message_id=msg_id
        )
        msg_ids.append(sent_msg.message_id)
    # Increment downloads
    await increment_downloads(batch_id)
    # Schedule deletion
    asyncio.create_task(delete_after_delay(context, chat_id, msg_ids, AUTO_DELETE_SECONDS))

async def delete_after_delay(context, chat_id, msg_ids, delay):
    await asyncio.sleep(delay)
    for msg_id in msg_ids:
        try:
            await context.bot.delete_message(chat_id, msg_id)
        except TelegramError:
            pass

async def check_batch_exists(batch_id):
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT 1 FROM batches WHERE batch_id = ?", (batch_id,))
        row = await cursor.fetchone()
        return row is not None

async def get_batch_data(batch_id):
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT caption FROM batches WHERE batch_id = ?", (batch_id,))
        row = await cursor.fetchone()
        caption = row[0] if row else None
        cursor = await db.execute("SELECT channel_msg_id FROM files WHERE batch_id = ?", (batch_id,))
        rows = await cursor.fetchall()
        channel_msg_ids = [r[0] for r in rows]
        return caption, channel_msg_ids

async def increment_downloads(batch_id):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("UPDATE stats SET downloads = downloads + 1 WHERE batch_id = ?", (batch_id,))
        await db.commit()

async def newbatch(update: Update, context):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_UPLOADERS:
        await update.message.reply_text("You are not allowed to upload files.")
        return ConversationHandler.END
    context.user_data['current_batch_msg_ids'] = []
    keyboard = [[InlineKeyboardButton("Done uploading", callback_data="done_upload")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Send files now. Press 'Done uploading' when finished.", reply_markup=reply_markup)
    return UPLOAD_FILES

async def upload_file(update: Update, context):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_UPLOADERS:
        return
    message = update.message
    if message.document:
        file_id = message.document.file_id
    elif message.video:
        file_id = message.video.file_id
    elif message.audio:
        file_id = message.audio.file_id
    elif message.photo:
        file_id = message.photo[-1].file_id
    else:
        return
    # Copy to storage channel
    copied_msg = await context.bot.copy_message(
        chat_id=STORAGE_CHANNEL_ID,
        from_chat_id=message.chat_id,
        message_id=message.message_id
    )
    msg_id = copied_msg.message_id
    context.user_data['current_batch_msg_ids'].append(msg_id)
    await update.message.reply_text("File added to batch.")

async def handle_done_upload(update: Update, context):
    query = update.callback_query
    await query.answer()
    msg_ids = context.user_data.get('current_batch_msg_ids', [])
    if not msg_ids:
        await query.edit_message_text("No files uploaded. Cancelled.")
        return ConversationHandler.END
    await query.edit_message_text("Enter caption for the batch or use /skip.")
    return ADD_CAPTION

async def set_caption(update: Update, context):
    context.user_data['caption'] = update.message.text
    await save_batch(update, context)
    return ConversationHandler.END

async def skip_caption(update: Update, context):
    context.user_data['caption'] = None
    await save_batch(update, context)
    return ConversationHandler.END

async def save_batch(update: Update, context):
    batch_id = uuid.uuid4().hex[:12]  # Unique 12 char hex
    msg_ids = context.user_data['current_batch_msg_ids']
    caption = context.user_data.get('caption')
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT INTO batches (batch_id, caption) VALUES (?, ?)", (batch_id, caption))
        for msg_id in msg_ids:
            await db.execute("INSERT INTO files (batch_id, channel_msg_id) VALUES (?, ?)", (batch_id, msg_id))
        await db.execute("INSERT INTO stats (batch_id, downloads) VALUES (?, 0)", (batch_id,))
        await db.commit()
    link = f"https://t.me/{BOT_USERNAME}?start={batch_id}"
    await update.message.reply_text(f"Batch saved. Link: {link}")
    del context.user_data['current_batch_msg_ids']
    if 'caption' in context.user_data:
        del context.user_data['caption']

async def stats(update: Update, context):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_UPLOADERS:
        await update.message.reply_text("You are not allowed to view stats.")
        return
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM batches")
        total_batches = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT COUNT(*) FROM files")
        total_files = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT SUM(downloads) FROM stats")
        total_downloads = (await cursor.fetchone())[0] or 0
        msg = f"Total links (batches): {total_batches}\nTotal stored files: {total_files}\nTotal downloads: {total_downloads}\n\nPer link stats:\n"
        cursor = await db.execute("""
            SELECT b.batch_id, s.downloads, COUNT(f.channel_msg_id) as file_count
            FROM batches b
            JOIN stats s ON b.batch_id = s.batch_id
            LEFT JOIN files f ON b.batch_id = f.batch_id
            GROUP BY b.batch_id
        """)
        rows = await cursor.fetchall()
        for row in rows:
            batch_id, downloads, file_count = row
            msg += f"Batch {batch_id}: {file_count} files, {downloads} downloads\n"
        await update.message.reply_text(msg)

async def check_join(update: Update, context):
    query = update.callback_query
    await query.answer()
    batch_id = query.data.split('_')[-1]
    user_id = query.from_user.id
    joined = await check_membership(context, user_id)
    if joined:
        await query.edit_message_text("Verified. Sending files...")
        await send_files(update, context, batch_id)
    else:
        await query.answer("You haven't joined yet. Please join the channel.", show_alert=True)

async def error_handler(update: Update, context):
    print(f"Error: {context.error}")

def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise ValueError("BOT_TOKEN not set")
    request = HTTPXRequest(connect_timeout=10.0, read_timeout=10.0)
    application = Application.builder().token(token).request(request).build()
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('newbatch', newbatch)],
        states={
            UPLOAD_FILES: [
                MessageHandler(filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.DOCUMENT, upload_file),
                CallbackQueryHandler(handle_done_upload, pattern='^done_upload$')
            ],
            ADD_CAPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_caption),
                CommandHandler('skip', skip_caption)
            ]
        },
        fallbacks=[]
    )
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('stats', stats))
    application.add_handler(CallbackQueryHandler(check_join, pattern='^check_join_'))
    application.add_error_handler(error_handler)
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    asyncio.run(init_db())
    main()
