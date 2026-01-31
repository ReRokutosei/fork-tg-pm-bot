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
from collections import defaultdict
import logging

user_locks = defaultdict(asyncio.Lock)

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


# ---------- 用户会话管理 ----------
class UserSession:
    def __init__(
        self, user_id, verified=False, thread_id=None, banned=False, verify_time=None
    ):
        self.user_id = user_id
        self.verified = verified
        self.thread_id = thread_id
        self.banned = banned
        self.verify_time = verify_time  # 记录验证时间
        self.last_activity = time()  # 记录最后活动时间


# 存储所有用户会话
user_sessions = {}

# 话题到用户的映射 (用于通过话题ID查找用户)
thread_to_user = {}

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
            # 重构加载逻辑，兼容旧数据格式
            user_to_thread_old = {
                int(k): int(v) for k, v in data.get("user_to_thread", {}).items()
            }
            thread_to_user_old = {
                int(k): int(v) for k, v in data.get("thread_to_user", {}).items()
            }
            user_verified_old = {
                int(k): v for k, v in data.get("user_verified", {}).items()
            }
            banned_users_old = set(data.get("banned_users", []))

            # 将旧数据转换为新格式
            for user_id, thread_id in user_to_thread_old.items():
                session = UserSession(user_id)
                session.thread_id = thread_id
                session.verified = user_verified_old.get(user_id, False)
                session.banned = user_id in banned_users_old
                user_sessions[user_id] = session

            # 重建 thread_to_user 映射
            for user_id, session in user_sessions.items():
                if session.thread_id:
                    thread_to_user[session.thread_id] = user_id

    except Exception as e:
        print(f"读取数据文件失败: {e}")
        user_sessions = {}


def persist_mapping():
    """保存数据到文件"""
    # 转换回旧格式以保持兼容性
    user_to_thread = {}
    thread_to_user = {}
    user_verified = {}
    banned_users = []

    for user_id, session in user_sessions.items():
        if session.thread_id:
            user_to_thread[user_id] = session.thread_id
            thread_to_user[session.thread_id] = user_id
        user_verified[user_id] = session.verified
        if session.banned:
            banned_users.append(user_id)

    data = {
        "user_to_thread": {str(k): v for k, v in user_to_thread.items()},
        "thread_to_user": {str(k): v for k, v in thread_to_user.items()},
        "user_verified": {str(k): v for k, v in user_verified.items()},
        "banned_users": banned_users,
    }
    try:
        if not PERSIST_FILE.parent.exists():
            PERSIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        PERSIST_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        print(f"保存数据失败: {e}")


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


# 话题健康检查缓存，减少频繁探测请求
thread_health_cache = {}

async def _probe_forum_thread(bot, expected_thread_id, user_id, reason="health_check"):
    """
    探测话题是否仍然存在且有效
    """
    try:
        # 向话题发送探测消息
        result = await bot.send_message(
            chat_id=GROUP_ID,
            message_thread_id=expected_thread_id,
            text="🔍",  # 探测消息
            disable_notification=True
        )

        actual_thread_id = getattr(result, 'message_thread_id', None)
        probe_message_id = getattr(result, 'message_id', None)

        # 尽可能清理探测消息（无论落到哪个话题/General）
        if probe_message_id:
            try:
                await bot.delete_message(
                    chat_id=GROUP_ID,
                    message_id=probe_message_id
                )
            except Exception:
                # 删除失败不影响主流程
                pass

        if actual_thread_id is None:
            # 话题可能已失效，消息被重定向到General
            return {"status": "missing_thread_id"}
        
        if int(actual_thread_id) != int(expected_thread_id):
            # 消息被重定向到其他话题
            return {"status": "redirected", "actual_thread_id": actual_thread_id}
        
        # 话题健康状态良好
        return {"status": "ok"}
    
    except Exception as e:
        error_desc = str(e).lower()
        
        # 检查是否是话题不存在的错误
        if ("thread not found" in error_desc or 
            "topic not found" in error_desc or
            "message thread not found" in error_desc or
            "topic deleted" in error_desc or
            "thread deleted" in error_desc or
            "forum topic not found" in error_desc or
            "topic closed permanently" in error_desc):
            return {"status": "missing", "description": str(e)}
        
        # 检查是否是消息内容为空的错误
        if ("message text is empty" in error_desc or
            "bad request: message text is empty" in error_desc):
            return {"status": "probe_invalid", "description": str(e)}
        
        # 其他未知错误
        return {"status": "unknown_error", "description": str(e)}


async def _verify_topic_health(bot, thread_id, user_id, reason="health_check"):
    """
    验证话题健康状态，带缓存机制
    """
    cache_key = thread_id
    now = time()
    
    # 检查缓存
    if cache_key in thread_health_cache:
        cached = thread_health_cache[cache_key]
        # 如果缓存时间小于60秒，直接使用缓存
        if now - cached['timestamp'] < 60:  # 60秒缓存
            return cached['healthy']
    
    # 执行探测
    probe_result = await _probe_forum_thread(bot, thread_id, user_id, reason)
    
    is_healthy = probe_result['status'] == 'ok'
    
    # 更新缓存
    thread_health_cache[cache_key] = {
        'healthy': is_healthy,
        'timestamp': now,
        'probe_result': probe_result
    }
    
    return is_healthy


async def _ensure_thread_for_user(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, display: str
):
    """确保用户拥有一个有效的话题"""
    # 获取或创建用户会话
    if user_id not in user_sessions:
        user_sessions[user_id] = UserSession(user_id)

    session = user_sessions[user_id]

    # 如果已有话题ID，验证其有效性
    if session.thread_id is not None:
        # 验证话题健康状态
        is_healthy = await _verify_topic_health(context.bot, session.thread_id, user_id, "ensure_thread")
        
        if is_healthy:
            return session.thread_id, False  # 话题有效，返回现有话题
        else:
            # 话题无效，清理旧映射
            print(f"⚠️ 用户 {user_id} 的话题 {session.thread_id} 已失效，正在清理...")
            if session.thread_id in thread_to_user:
                del thread_to_user[session.thread_id]
            # 清除健康缓存
            if session.thread_id in thread_health_cache:
                del thread_health_cache[session.thread_id]
            session.thread_id = None

    # 最多重试3次
    max_retries = 3
    for attempt in range(max_retries):
        try:
            # 创建新话题
            thread_id = await _create_topic_for_user(
                context.bot, user_id, f"user_{user_id}_{display}"
            )

            # 等待片刻，确保话题完全创建
            await asyncio.sleep(0.5)
            
            # 立即测试新创建的话题是否可用
            try:
                test_msg = await context.bot.send_message(
                    chat_id=GROUP_ID,
                    message_thread_id=thread_id,
                    text="🔍 Test message to verify topic availability",
                    disable_notification=True
                )
                
                # 检查返回的消息是否在正确的线程中
                actual_thread_id = getattr(test_msg, 'message_thread_id', None)
                
                if actual_thread_id is None or int(actual_thread_id) != int(thread_id):
                    # 话题可能存在问题，抛出异常让外层处理
                    raise Exception(f"Topic test failed: expected {thread_id}, got {actual_thread_id}")
                
                # 删除测试消息
                await context.bot.delete_message(
                    chat_id=GROUP_ID,
                    message_id=test_msg.message_id
                )
                
                print(f"✅ 话题 {thread_id} 创建并验证成功")
            except Exception as e:
                print(f"❌ 新创建的话题 {thread_id} 无法使用 (尝试 {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    # 如果不是最后一次尝试，等待后重试
                    await asyncio.sleep(1)
                    continue
                else:
                    # 最后一次尝试也失败了，抛出异常
                    raise e

            # 更新会话和映射
            session.thread_id = thread_id
            thread_to_user[thread_id] = user_id
            persist_mapping()

            # 更新健康缓存
            thread_health_cache[thread_id] = {
                'healthy': True,
                'timestamp': time(),
                'probe_result': {'status': 'ok'}
            }

            return thread_id, True
            
        except Exception as e:
            if attempt == max_retries - 1:  # 最后一次尝试
                print(f"❌ 创建话题失败，已达到最大重试次数: {e}")
                raise e
            # 否则继续下一次循环重试


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

    op = random.choice(["+", "-", "*", "/"])

    if op == "+":
        a, b = random.randint(1, 10), random.randint(1, 10)
        return f"{a} + {b} = ?", a + b

    elif op == "-":
        a, b = random.randint(1, 10), random.randint(1, 10)
        if a < b:
            a, b = b, a
        return f"{a} - {b} = ?", a - b

    elif op == "*":
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
            msg_lines.append(
                f"💬 话题 ID: <code>{update.effective_message.message_thread_id}</code>"
            )
    await update.message.reply_text("\n".join(msg_lines), parse_mode=ParseMode.HTML)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if update.effective_chat.type != "private":
        return

    # 获取或创建用户会话
    if uid not in user_sessions:
        user_sessions[uid] = UserSession(uid)

    session = user_sessions[uid]

    if session.banned:
        return

    if session.verified:
        await update.message.reply_text(
            "你已经验证过了，可以直接发送消息（支持文本、图片、视频等）。"
        )
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
        session.verified = True
        session.verify_time = time()
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

    # 获取或创建用户会话
    if target_uid not in user_sessions:
        user_sessions[target_uid] = UserSession(target_uid)

    session = user_sessions[target_uid]

    if session.banned:
        await update.message.reply_text(f"用户 {target_uid} 已经在黑名单中了。")
        return

    session.banned = True
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

    # 获取或创建用户会话
    if target_uid not in user_sessions:
        user_sessions[target_uid] = UserSession(target_uid)

    session = user_sessions[target_uid]

    if not session.banned:
        await update.message.reply_text(f"用户 {target_uid} 不在黑名单中。")
        return

    session.banned = False
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

    print(f"DEBUG: Processing message from user {uid}, message_id: {msg.message_id}")

    async with user_locks[uid]:
        print(f"DEBUG: Acquired lock for user {uid}")

        # 获取或创建用户会话
        if uid not in user_sessions:
            user_sessions[uid] = UserSession(uid)

        session = user_sessions[uid]

        # 更新最后活动时间
        session.last_activity = time()

        if session.banned:
            print(f"DEBUG: User {uid} is banned")
            await msg.reply_text("🚫 你已被管理员禁止发送消息。")
            return

        user = update.effective_user
        display = _display_name_from_update(update)

        print(
            f"DEBUG: User {uid} is verified: {session.verified}, use_math: {USE_MATH_CAPTCHA}, use_fixed: {USE_FIXED_CAPTCHA}"
        )

        # 1. 验证流程
        if not session.verified:
            print(f"DEBUG: User {uid} needs verification")
            if USE_MATH_CAPTCHA:
                # 使用数学验证码验证
                try:
                    user_answer = int(text_content.strip())
                    correct_answer = math_answers.get(uid)

                    print(
                        f"DEBUG: Math verification - user input: {user_answer}, expected: {correct_answer}"
                    )
                    if user_answer == correct_answer:
                        # 验证成功，清除记录
                        session.verified = True
                        session.verify_time = time()
                        math_answers.pop(uid, None)  # 清除该用户的数学题答案
                        persist_mapping()
                        await msg.reply_text("验证成功！你现在可以发送消息了。")
                        print(f"DEBUG: User {uid} verification successful")
                    else:
                        # 重新生成数学题并发送
                        question, answer = _generate_math_question()
                        math_answers[uid] = answer
                        await msg.reply_text(f"答案错误，请重新回答：\n{question}")
                        print(f"DEBUG: User {uid} gave wrong answer, asking again")
                except ValueError:
                    # 输入不是有效数字，重新生成题目
                    question, answer = _generate_math_question()
                    math_answers[uid] = answer
                    await msg.reply_text(f"请输入有效数字：\n{question}")
                    print(f"DEBUG: User {uid} input invalid, asking again")
            elif USE_FIXED_CAPTCHA:
                # 使用固定验证问题
                if text_content.strip() == VERIFY_ANSWER:
                    session.verified = True
                    session.verify_time = time()
                    persist_mapping()
                    await msg.reply_text("验证成功！你现在可以发送消息了。")
                    print(f"DEBUG: User {uid} fixed verification successful")
                else:
                    await msg.reply_text("请先通过验证：" + VERIFY_QUESTION)
                    print(f"DEBUG: User {uid} needs to answer fixed question")
            else:
                # 无验证模式：自动放行
                session.verified = True
                session.verify_time = time()
                persist_mapping()
                print(f"DEBUG: User {uid} auto-verified (no captcha)")
            return

        print(f"DEBUG: User {uid} already verified, proceeding to send message")
        
        # 检查用户名：如果用户没有设置 username，则要求其设置
        if not user.username:
            await msg.reply_text(
                "⚠️ 验证通过，但你的 Telegram 用户名为空。\n"
                "请先在 Telegram 设置中设置一个 @用户名，否则无法继续使用此服务。"
            )
            return

        # 2. 确保话题存在且有效
        try:
            thread_id, is_new_topic = await _ensure_thread_for_user(
                context, uid, display
            )
            print(
                f"DEBUG: Got thread_id {thread_id} for user {uid}, is_new_topic: {is_new_topic}"
            )
        except Exception as e:
            print(f"ERROR: Failed to ensure thread for user {uid}: {e}")
            await msg.reply_text(f"系统错误：{e}")
            return

        # 3. 新用户发名片
        if is_new_topic:
            print(f"DEBUG: Sending welcome card for user {uid} in thread {thread_id}")
            safe_name = html.escape(user.full_name or "无名氏")
            username_text = (
                f"@{user.username}" if user.username else "未设置"
            )  # 获取用户名
            mention_link = mention_html(uid, safe_name)  # 原有的跳转链接

            info_text = (
                f"<b>新用户接入</b>\n"
                f"ID: <code>{uid}</code>\n"
                f"名字: {mention_link}\n"
                f"用户名: {username_text}\n"  # 新增用户名展示
                f"#id{uid}"
            )
            try:
                await context.bot.send_message(
                    chat_id=GROUP_ID,
                    message_thread_id=thread_id,
                    text=info_text,
                    parse_mode=ParseMode.HTML,
                )
                print(f"DEBUG: Sent welcome card for user {uid} in thread {thread_id}")
            except Exception as e:
                print(f"ERROR: Failed to send welcome card for user {uid}: {e}")

        # 4. 转发用户消息，并验证是否真的进入了正确话题
        print(f"DEBUG: About to forward message from user {uid} to thread {thread_id}")
        try:
            # 首先尝试复制消息
            sent_msg = await context.bot.copy_message(
                chat_id=GROUP_ID,
                message_thread_id=thread_id,
                from_chat_id=uid,
                message_id=msg.message_id,
            )

            print(f"DEBUG: Successfully copied message, checking thread_id...")
            # 检查实际 thread_id 是否与预期一致
            actual_thread_id = getattr(sent_msg, "message_thread_id", None)
            print(
                f"DEBUG: Expected thread_id: {thread_id}, Actual thread_id: {actual_thread_id}"
            )

            # 检查是否落入 General（说明原话题已失效）
            expected_non_general = thread_id != 1
            actually_in_general = actual_thread_id is None or actual_thread_id == 1

            # 静默重定向检测：消息被发送到不同于预期话题的其他话题
            redirected_to_other_topic = (
                actual_thread_id is not None 
                and int(actual_thread_id) != int(thread_id) 
                and int(actual_thread_id) != 1
            )

            # 如果消息被重定向或发送到了General频道，需要重建话题
            if expected_non_general and (actually_in_general or redirected_to_other_topic):
                redirect_info = "General" if actually_in_general else f"话题 {actual_thread_id}"
                print(
                    f"⚠️ 用户 {uid} 的消息被重定向到 {redirect_info}（预期话题 {thread_id} 已失效），正在重建..."
                )

                # 清理旧映射
                session.thread_id = None
                if thread_id in thread_to_user:
                    del thread_to_user[thread_id]
                # 清除健康缓存
                if thread_id in thread_health_cache:
                    del thread_health_cache[thread_id]
                persist_mapping()
                print(
                    f"DEBUG: Cleaned up mappings for user {uid}, old_tid: {thread_id}"
                )

                # 重新创建话题
                thread_id, is_new_topic = await _ensure_thread_for_user(
                    context, uid, display
                )
                print(
                    f"DEBUG: Re-created thread_id {thread_id} for user {uid}, is_new_topic: {is_new_topic}"
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
                            parse_mode=ParseMode.HTML,
                        )
                        print(f"DEBUG: Sent session restored message for user {uid}")
                    except Exception as e:
                        print(
                            f"ERROR: Failed to send session restored message for user {uid}: {e}"
                        )

                # 重新发送当前消息到新的话题
                print(f"DEBUG: Re-forwarding message to new thread {thread_id}")
                sent_msg = await context.bot.copy_message(
                    chat_id=GROUP_ID,
                    message_thread_id=thread_id,
                    from_chat_id=uid,
                    message_id=msg.message_id,
                )
                print(f"DEBUG: Message re-forwarded successfully")

            # 【记录ID】用于编辑同步：(用户ID, 用户消息ID) -> (群组ID, 群组消息ID)（使用最终有效的消息）
            message_map[(uid, msg.message_id)] = (GROUP_ID, sent_msg.message_id, time())
            print(
                f"DEBUG: Recorded message mapping for user {uid}, msg_id: {msg.message_id}"
            )

        except Exception as e:
            print(f"ERROR: Failed to forward message from user {uid}: {e}")
            
            # 如果copy_message失败，需要标记当前话题为不健康并清理session中的thread_id
            if session.thread_id:
                if session.thread_id in thread_health_cache:
                    thread_health_cache[session.thread_id]['healthy'] = False
                # 清理session中的thread_id，以便下次重新创建
                session.thread_id = None
            
            # 如果copy_message失败，尝试发送错误信息给用户
            try:
                await msg.reply_text(f"消息发送失败：{e}")
            except Exception:
                # 如果连回复都无法发送，至少在日志中记录
                print(f"ERROR: Could not notify user {uid} of error: {e}")

    print(f"DEBUG: Finished processing message from user {uid}")


async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """群组处理：支持媒体转发"""
    msg = update.message
    if not msg or update.effective_chat.id != GROUP_ID:
        return

    thread_id = getattr(msg, "message_thread_id", None)
    if thread_id is None:
        return
    if msg.from_user and msg.from_user.is_bot:
        return
    if msg.text and msg.text.startswith("/"):
        return

    target_user_id = thread_to_user.get(int(thread_id))
    if not target_user_id:
        return

    # 【修改】管理员回复（使用 copy_message）
    try:
        sent_msg = await context.bot.copy_message(
            chat_id=target_user_id, from_chat_id=GROUP_ID, message_id=msg.message_id
        )
        # 【记录ID】用于编辑同步：(群组ID, 群组消息ID) -> (用户ID, 用户消息ID)
        message_map[(GROUP_ID, msg.message_id)] = (
            target_user_id,
            sent_msg.message_id,
            time(),
        )

    except Exception as e:
        print(f"ERROR: Could not send message to user {target_user_id}: {e}")
        pass  # 如果用户屏蔽了机器人，这里会报错，忽略即可


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
        return  # 找不到记录（可能是重启前发的，或者没记录上的）

    target_chat_id, target_msg_id = target[:2]  # 提取前两个元素

    # 尝试同步编辑内容
    # 注意：copy_message 生成的是新消息，copy 不支持"再编辑"关联
    # 我们只能用 edit_message_text/caption 来修改已发送的消息
    try:
        if edited_msg.text:
            # 纯文本编辑
            await context.bot.edit_message_text(
                chat_id=target_chat_id,
                message_id=target_msg_id,
                text=edited_msg.text,
                entities=edited_msg.entities,
            )
        elif edited_msg.caption:
            # 媒体说明编辑
            await context.bot.edit_message_caption(
                chat_id=target_chat_id,
                message_id=target_msg_id,
                caption=edited_msg.caption,
                caption_entities=edited_msg.caption_entities,
            )
        else:
            # 如果是纯图片/文件修改（Telegram 较少见），或者其他类型，目前 API 处理比较复杂，暂略过
            pass
    except Exception as e:
        print(f"编辑同步失败: {e}")


# 定义清理函数
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


def main():
    print("Bot is starting...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CommandHandler("id", id_command))

    # 【新增】编辑消息处理器
    app.add_handler(
        MessageHandler(filters.UpdateType.EDITED_MESSAGE, handle_edit_message)
    )

    # 私聊消息：允许所有类型 (去掉 filters.TEXT)，排除命令和状态更新(比如xxx加入群组)
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & ~filters.COMMAND & ~filters.StatusUpdate.ALL,
            handle_private_message,
        )
    )

    # 群组消息：同上
    app.add_handler(
        MessageHandler(
            filters.Chat(chat_id=GROUP_ID)
            & ~filters.COMMAND
            & ~filters.StatusUpdate.ALL,
            handle_group_message,
        )
    )

    # 注册每小时清理一次过期消息映射
    app.job_queue.run_repeating(
        callback=cleanup_message_map,
        interval=3600,  # 每3600秒（1小时）执行一次
        first=3600,  # 启动后1小时首次执行
    )

    print("Polling started.")
    app.run_polling()


if __name__ == "__main__":
    main()