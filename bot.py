import os
import json
import asyncio
from time import time
import html
from pathlib import Path
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.helpers import mention_html

# ---------- 配置（必填环境变量） ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_ID = int(os.getenv("GROUP_ID", "0"))

# 持久化文件路径
PERSIST_FILE = Path("/data/topic_mapping.json")

# 获取原始环境变量（不设默认值）
_raw_verify_question = os.getenv("VERIFY_QUESTION")
_raw_verify_answer = os.getenv("VERIFY_ANSWER")
_raw_use_math = os.getenv("USE_MATH_CAPTCHA")

# 判断是否启用了相应功能
USE_MATH_CAPTCHA = _raw_use_math is not None and _raw_use_math.lower() == "true"
USE_FIXED_CAPTCHA = _raw_verify_answer is not None

# 设置默认值
VERIFY_QUESTION = _raw_verify_question or "请输入访问密码："
VERIFY_ANSWER = _raw_verify_answer

if not BOT_TOKEN:
    raise RuntimeError("请设置 BOT_TOKEN 环境变量")
if GROUP_ID == 0:
    raise RuntimeError("请设置 GROUP_ID 环境变量")

# ---------- 内存数据 ----------
# user_id -> message_thread_id
user_to_thread = {}
# message_thread_id -> user_id
thread_to_user = {}
# user_id -> bool
user_verified = {}
# user_id -> bool (黑名单)
banned_users = set()

# 【新增】消息映射表 (用于编辑同步)
# Key: (source_chat_id, source_message_id)
# Value: (target_chat_id, target_message_id)
# 仅存在内存中，重启后失效（为了性能不建议持久化所有消息ID）
message_map = {}

# 数学验证码存储 (用户ID -> 正确答案)
math_answers = {}

# 启动时加载数据
if PERSIST_FILE.exists():
    try:
        content = PERSIST_FILE.read_text(encoding="utf-8")
        if content.strip():
            data = json.loads(content)
            user_to_thread = {int(k): int(v) for k, v in data.get("user_to_thread", {}).items()}
            thread_to_user = {int(k): int(v) for k, v in data.get("thread_to_user", {}).items()}
            user_verified = {int(k): v for k, v in data.get("user_verified", {}).items()}
            banned_users = set(data.get("banned_users", []))
    except Exception as e:
        print(f"读取数据文件失败: {e}")
        user_to_thread = {}
        thread_to_user = {}
        user_verified = {}
        banned_users = set()

def persist_mapping():
    """保存数据到文件"""
    data = {
        "user_to_thread": {str(k): v for k, v in user_to_thread.items()},
        "thread_to_user": {str(k): v for k, v in thread_to_user.items()},
        "user_verified": {str(k): v for k, v in user_verified.items()},
        "banned_users": list(banned_users),
    }
    try:
        if not PERSIST_FILE.parent.exists():
            PERSIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        PERSIST_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"保存数据失败: {e}")


async def cleanup_message_map(context: ContextTypes.DEFAULT_TYPE):
    """清理超过24小时的消息映射记录"""
    now = time()
    expired_keys = []
    for key, value in message_map.items():
        # value = (dst_chat, dst_msg, timestamp)
        if now - value[2] > 86400:  # 24小时 = 86400秒
            expired_keys.append(key)
    
    for key in expired_keys:
        del message_map[key]
    
    if expired_keys:
        print(f"🧹 清理了 {len(expired_keys)} 条过期消息映射")
# ---------- 辅助函数 ----------
async def _create_topic_for_user(bot, user_id: int, title: str) -> int:
    safe_title = title[:40]
    resp = await bot.create_forum_topic(chat_id=GROUP_ID, name=safe_title)
    thread_id = getattr(resp, "message_thread_id", None)
    if thread_id is None:
        thread_id = resp.get("message_thread_id") if isinstance(resp, dict) else None
    if thread_id is None:
        raise RuntimeError("创建 topic 未返回 message_thread_id")
    return int(thread_id)

async def _ensure_thread_for_user(context: ContextTypes.DEFAULT_TYPE, user_id: int, display: str):
    if user_id in user_to_thread:
        return user_to_thread[user_id], False 
    
    try:
        thread_id = await _create_topic_for_user(context.bot, user_id, f"user_{user_id}_{display}")
    except Exception as e:
        raise e

    user_to_thread[user_id] = thread_id
    thread_to_user[thread_id] = user_id
    persist_mapping()
    return thread_id, True

def _display_name_from_update(update: Update) -> str:
    u = update.effective_user
    if not u:
        return "匿名"
    name = u.full_name or u.username or str(u.id)
    return name.replace("\n", " ")

# ---------- 数学验证码辅助函数 ----------

def _generate_math_question() -> tuple[str, int]:
    """生成随机数学题及答案"""
    import random
    op = random.choice(['+', '-', '*', '/'])
    
    if op == '+':
        a, b = random.randint(1, 10), random.randint(1, 10)
        return f"{a} + {b} = ?", a + b
    
    elif op == '-':
        a, b = random.randint(1, 10), random.randint(1, 10)
        if a < b:
            a, b = b, a
        return f"{a} - {b} = ?", a - b
    
    elif op == '*':
        a, b = random.randint(1, 10), random.randint(1, 10)
        return f"{a} × {b} = ?", a * b
    
    else:  # op == '/'
        divisor = random.randint(1, 10)
        quotient = random.randint(1, 10)
        dividend = divisor * quotient
        return f"{dividend} ÷ {divisor} = ?", quotient

async def _expire_math_answer(uid: int, delay: int = 300):
    """异步延迟清理数学验证码，delay为延迟时间（秒）"""
    await asyncio.sleep(delay)
    # 使用 pop 方法安全地移除，如果不存在也不会报错
    math_answers.pop(uid, None)

# ---------- 命令处理器 ----------

async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    msg_lines = [f"👤 你的 ID: <code>{user.id}</code>"]
    if chat.type != "private":
        msg_lines.insert(0, f"📢 群组 ID: <code>{chat.id}</code>")
        if update.effective_message.message_thread_id:
             msg_lines.append(f"💬 话题 ID: <code>{update.effective_message.message_thread_id}</code>")
    await update.message.reply_text("\n".join(msg_lines), parse_mode=ParseMode.HTML)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if update.effective_chat.type != "private":
        return
    if uid in banned_users:
        return 
    if user_verified.get(uid):
        await update.message.reply_text("你已经验证过了，可以直接发送消息（支持文本、图片、视频等）。")
        return
    
    if USE_MATH_CAPTCHA:
        question, answer = _generate_math_question()
        math_answers[uid] = answer
        await update.message.reply_text(f"请回答数学题完成验证：\n{question}")
        
        # 创建过期任务，5分钟后清理数学答案
        asyncio.create_task(_expire_math_answer(uid))
    elif USE_FIXED_CAPTCHA:
        await update.message.reply_text(VERIFY_QUESTION)
    else:
        # 两者都未启用：自动验证通过
        user_verified[uid] = True
        persist_mapping()
        await update.message.reply_text("你可以直接发送消息，我会帮你转达。")

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ID:
        return
    target_uid = None
    if context.args and context.args[0].isdigit():
        target_uid = int(context.args[0])
    elif update.effective_message.message_thread_id:
        thread_id = update.effective_message.message_thread_id
        target_uid = thread_to_user.get(thread_id)
    
    if not target_uid:
        await update.message.reply_text("❌ 无法识别目标。请在用户话题内使用或指定ID。")
        return
    if target_uid in banned_users:
        await update.message.reply_text(f"用户 {target_uid} 已经在黑名单中了。")
        return
    banned_users.add(target_uid)
    persist_mapping()
    await update.message.reply_text(f"🚫 用户 {target_uid} 已被封禁。")

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ID:
        return
    target_uid = None
    if context.args and context.args[0].isdigit():
        target_uid = int(context.args[0])
    elif update.effective_message.message_thread_id:
        thread_id = update.effective_message.message_thread_id
        target_uid = thread_to_user.get(thread_id)
    
    if not target_uid:
        await update.message.reply_text("❌ 无法识别目标。请在用户话题内使用或指定ID。")
        return
    if target_uid not in banned_users:
        await update.message.reply_text(f"用户 {target_uid} 不在黑名单中。")
        return
    banned_users.remove(target_uid)
    persist_mapping()
    await update.message.reply_text(f"✅ 用户 {target_uid} 已解封。")

# ---------- 消息处理器 (核心功能) ----------

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """私聊处理：支持媒体 + 验证 + 自动恢复失效话题"""
    if update.effective_chat.type != "private":
        return

    uid = update.effective_user.id
    msg = update.message
    # 获取文本或图片的附言，用于验证密码
    text_content = msg.text or msg.caption or ""
    
    if uid in banned_users:
        await msg.reply_text("🚫 你已被管理员禁止发送消息。")
        return

    user = update.effective_user
    display = _display_name_from_update(update)

    # 1. 验证流程
    if not user_verified.get(uid):
        if USE_MATH_CAPTCHA:
            # 使用数学验证码验证
            try:
                user_answer = int(text_content.strip())
                correct_answer = math_answers.get(uid)
                
                if user_answer == correct_answer:
                    # 验证成功，清除记录
                    user_verified[uid] = True
                    math_answers.pop(uid, None)  # 清除该用户的数学题答案
                    persist_mapping()
                    await msg.reply_text("验证成功！你现在可以发送消息了。")
                else:
                    # 重新生成数学题并发送
                    question, answer = _generate_math_question()
                    math_answers[uid] = answer
                    await msg.reply_text(f"答案错误，请重新回答：\n{question}")
            except ValueError:
                # 输入不是有效数字，重新生成题目
                question, answer = _generate_math_question()
                math_answers[uid] = answer
                await msg.reply_text(f"请输入有效数字：\n{question}")
        elif USE_FIXED_CAPTCHA:
            # 使用固定验证问题
            if text_content.strip() == VERIFY_ANSWER:
                user_verified[uid] = True
                persist_mapping()
                await msg.reply_text("验证成功！你现在可以发送消息了。")
            else:
                await msg.reply_text("请先通过验证：" + VERIFY_QUESTION)
        else:
            # 无验证模式：自动放行
            user_verified[uid] = True
            persist_mapping()
        return

    # 2. 确保话题存在
    try:
        thread_id, is_new_topic = await _ensure_thread_for_user(context, uid, display)
    except Exception as e:
        await msg.reply_text(f"系统错误：{e}")
        return

    # 3. 新用户发名片
    if is_new_topic:
        safe_name = html.escape(user.full_name or "无名氏")
        username_text = f"@{user.username}" if user.username else "未设置" # 获取用户名
        mention_link = mention_html(uid, safe_name) # 原有的跳转链接
        
        info_text = (
            f"<b>新用户接入</b>\n"
            f"ID: <code>{uid}</code>\n"
            f"名字: {mention_link}\n"
            f"用户名: {username_text}\n" # 新增用户名展示
            f"#id{uid}"
        )
        try:
            await context.bot.send_message(
                chat_id=GROUP_ID,
                message_thread_id=thread_id,
                text=info_text,
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

    # 4. 转发用户消息，并验证是否真的进入了正确话题
    try:
        sent_msg = await context.bot.copy_message(
            chat_id=GROUP_ID,
            message_thread_id=thread_id,
            from_chat_id=uid,
            message_id=msg.message_id
        )

        # 检查实际 thread_id 是否与预期一致
        actual_thread_id = getattr(sent_msg, 'message_thread_id', None)
        if actual_thread_id != thread_id:
            print(f"⚠️ 话题失效检测：用户 {uid} 的消息未进入预期话题 "
                  f"(期望 {thread_id}, 实际 {actual_thread_id})，正在重建...")

            # 清理旧映射
            old_tid = user_to_thread.pop(uid, None)
            if old_tid:
                thread_to_user.pop(old_tid, None)
            persist_mapping()

            # 重新创建话题
            thread_id, is_new_topic = await _ensure_thread_for_user(context, uid, display)

            # 重新转发消息
            sent_msg = await context.bot.copy_message(
                chat_id=GROUP_ID,
                message_thread_id=thread_id,
                from_chat_id=uid,
                message_id=msg.message_id
            )

            # 如果是新话题，补发用户名片
            if is_new_topic:
                safe_name = html.escape(user.full_name or "无名氏")
                username_text = f"@{user.username}" if user.username else "未设置"
                mention_link = mention_html(uid, safe_name)
                info_text = (
                    f"<b>会话已恢复</b>\n"
                    f"ID: <code>{uid}</code>\n"
                    f"名字: {mention_link}\n"
                    f"用户名: {username_text}\n"
                    f"#id{uid}"
                )
                try:
                    await context.bot.send_message(
                        chat_id=GROUP_ID,
                        message_thread_id=thread_id,
                        text=info_text,
                        parse_mode=ParseMode.HTML
                    )
                except Exception:
                    pass

        #【记录ID】用于编辑同步：(用户ID, 用户消息ID) -> (群组ID, 群组消息ID)（使用最终有效的消息）
        message_map[(uid, msg.message_id)] = (GROUP_ID, sent_msg.message_id, time())
        
    except Exception as e:
        await msg.reply_text(f"消息发送失败：{e}")

async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """群组处理：支持媒体转发"""
    msg = update.message
    if not msg or update.effective_chat.id != GROUP_ID:
        return

    thread_id = getattr(msg, "message_thread_id", None)
    if thread_id is None: return
    if msg.from_user and msg.from_user.is_bot: return
    if msg.text and msg.text.startswith("/"): return

    target_user_id = thread_to_user.get(int(thread_id))
    if not target_user_id: return

    # 【修改】管理员回复（使用 copy_message）
    try:
        sent_msg = await context.bot.copy_message(
            chat_id=target_user_id,
            from_chat_id=GROUP_ID,
            message_id=msg.message_id
        )
        # 【记录ID】用于编辑同步：(群组ID, 群组消息ID) -> (用户ID, 用户消息ID)
        message_map[(GROUP_ID, msg.message_id)] = (target_user_id, sent_msg.message_id, time())
        
    except Exception:
        pass # 如果用户屏蔽了机器人，这里会报错，忽略即可

async def handle_edit_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """【新增】处理消息编辑同步"""
    edited_msg = update.edited_message
    if not edited_msg:
        return
    
    source_chat_id = edited_msg.chat_id
    source_msg_id = edited_msg.message_id
    
    # 查找对应的目标消息
    target = message_map.get((source_chat_id, source_msg_id))
    if not target:
        return # 找不到记录（可能是重启前发的，或者没记录上的）
    
    target_chat_id, target_msg_id = target
    
    # 尝试同步编辑内容
    # 注意：copy_message 生成的是新消息，copy 不支持“再编辑”关联
    # 我们只能用 edit_message_text/caption 来修改已发送的消息
    try:
        if edited_msg.text:
            # 纯文本编辑
            await context.bot.edit_message_text(
                chat_id=target_chat_id,
                message_id=target_msg_id,
                text=edited_msg.text,
                entities=edited_msg.entities
            )
        elif edited_msg.caption:
            # 媒体说明编辑
            await context.bot.edit_message_caption(
                chat_id=target_chat_id,
                message_id=target_msg_id,
                caption=edited_msg.caption,
                caption_entities=edited_msg.caption_entities
            )
        else:
            # 如果是纯图片/文件修改（Telegram 较少见），或者其他类型，目前 API 处理比较复杂，暂略过
            pass
    except Exception as e:
        print(f"编辑同步失败: {e}")

# ---------- 启动 ----------
def main():
    print("Bot is starting...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CommandHandler("id", id_command))

    # 【新增】编辑消息处理器
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, handle_edit_message))

    # 私聊消息：允许所有类型 (去掉 filters.TEXT)，排除命令和状态更新(比如xxx加入群组)
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & ~filters.COMMAND & ~filters.StatusUpdate.ALL, 
        handle_private_message
    ))

    # 群组消息：同上
    app.add_handler(MessageHandler(
        filters.Chat(chat_id=GROUP_ID) & ~filters.COMMAND & ~filters.StatusUpdate.ALL, 
        handle_group_message
    ))

    # 注册每小时清理一次过期消息映射
    app.job_queue.run_repeating(
        callback=cleanup_message_map,
        interval=3600,   # 每3600秒（1小时）执行一次
        first=3600       # 启动后1小时首次执行
    )

    print("Polling started.")
    app.run_polling()

if __name__ == "__main__":
    main()
