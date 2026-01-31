import os
import json
import asyncio
import html
from dataclasses import dataclass, field
from pathlib import Path
from time import time
from collections import defaultdict
from typing import Any, Dict, Optional, Tuple

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

# ---------- 全局锁：避免同一用户并发处理导致状态错乱 ----------
user_locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

# ---------- 配置（必填环境变量） ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_ID = int(os.getenv("GROUP_ID", "0"))

# 持久化文件路径
PERSIST_FILE = Path("/data/topic_mapping.json")

# 获取原始环境变量（不设默认值）
_RAW_VERIFY_QUESTION = os.getenv("VERIFY_QUESTION")
_RAW_VERIFY_ANSWER = os.getenv("VERIFY_ANSWER")
_RAW_USE_MATH = os.getenv("USE_MATH_CAPTCHA")

# 判断是否启用了相应功能
USE_MATH_CAPTCHA = _RAW_USE_MATH is not None and _RAW_USE_MATH.lower() == "true"
USE_FIXED_CAPTCHA = _RAW_VERIFY_ANSWER is not None

# 设置默认值
VERIFY_QUESTION = _RAW_VERIFY_QUESTION or "请输入访问密码："
VERIFY_ANSWER = _RAW_VERIFY_ANSWER

if not BOT_TOKEN:
    raise RuntimeError("请设置 BOT_TOKEN 环境变量")
if GROUP_ID == 0:
    raise RuntimeError("请设置 GROUP_ID 环境变量")

# ---------- 常量 ----------
THREAD_HEALTH_CACHE_SECONDS = 60
MATH_CAPTCHA_EXPIRE_SECONDS = 300
MESSAGE_MAP_TTL_SECONDS = 86400  # 24小时
CLEANUP_INTERVAL_SECONDS = 3600  # 1小时
TOPIC_CREATE_RETRIES = 3


# ---------- 用户会话管理 ----------
@dataclass
class UserSession:
    user_id: int
    verified: bool = False
    thread_id: Optional[int] = None
    banned: bool = False
    verify_time: Optional[float] = None
    last_activity: float = field(default_factory=time)


# 存储所有用户会话
user_sessions: Dict[int, UserSession] = {}

# 话题到用户的映射 (用于通过话题ID查找用户)
thread_to_user: Dict[int, int] = {}

# 消息映射表 (用于编辑同步)
# Key: (source_chat_id, source_message_id)
# Value: (target_chat_id, target_message_id, created_ts)
# 仅存在内存中，重启后失效
message_map: Dict[Tuple[int, int], Tuple[int, int, float]] = {}

# 数学验证码存储 (用户ID -> 正确答案)
math_answers: Dict[int, int] = {}

# 话题健康检查缓存，减少频繁探测请求
thread_health_cache: Dict[int, Dict[str, Any]] = {}


def get_session(user_id: int) -> UserSession:
    """获取或创建用户会话。"""
    session = user_sessions.get(user_id)
    if session is None:
        session = UserSession(user_id=user_id)
        user_sessions[user_id] = session
    return session


def load_persisted_mapping() -> None:
    """启动时加载持久化数据，兼容旧数据格式。"""
    global user_sessions, thread_to_user

    if not PERSIST_FILE.exists():
        return

    try:
        content = PERSIST_FILE.read_text(encoding="utf-8")
        if not content.strip():
            return

        data = json.loads(content)

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
            session = UserSession(user_id=user_id)
            session.thread_id = thread_id
            session.verified = bool(user_verified_old.get(user_id, False))
            session.banned = user_id in banned_users_old
            user_sessions[user_id] = session

        # 重建 thread_to_user 映射（优先使用重建结果；thread_to_user_old仅用于兼容）
        thread_to_user = {}
        for user_id, session in user_sessions.items():
            if session.thread_id:
                thread_to_user[session.thread_id] = user_id

        # 兼容：若旧映射中存在但 session 中缺失（理论上不该发生），补一层
        for tid, uid in thread_to_user_old.items():
            if tid not in thread_to_user and uid in user_sessions:
                thread_to_user[tid] = uid

    except Exception as exc:
        print(f"读取数据文件失败: {exc}")
        user_sessions = {}
        thread_to_user = {}


def persist_mapping() -> None:
    """保存数据到文件（保持旧格式兼容）。"""
    data = {
        "user_to_thread": {},
        "thread_to_user": {},
        "user_verified": {},
        "banned_users": [],
    }

    for user_id, session in user_sessions.items():
        if session.thread_id:
            data["user_to_thread"][str(user_id)] = session.thread_id
            data["thread_to_user"][str(session.thread_id)] = user_id
        data["user_verified"][str(user_id)] = session.verified
        if session.banned:
            data["banned_users"].append(user_id)

    try:
        PERSIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        PERSIST_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"保存数据失败: {exc}")


# ---------- 辅助函数 ----------
async def _create_topic_for_user(bot: Any, user_id: int, title: str) -> int:
    safe_title = title[:40]
    resp = await bot.create_forum_topic(chat_id=GROUP_ID, name=safe_title)

    thread_id = getattr(resp, "message_thread_id", None)
    if thread_id is None and isinstance(resp, dict):
        thread_id = resp.get("message_thread_id")

    if thread_id is None:
        raise RuntimeError("创建 topic 未返回 message_thread_id")
    return int(thread_id)


async def _probe_forum_thread(
    bot: Any,
    expected_thread_id: int,
    user_id: int,
    reason: str = "health_check",
) -> Dict[str, Any]:
    """探测话题是否仍然存在且有效。"""
    _ = (user_id, reason)  # 保留参数以保持调用签名与行为一致（便于扩展/排查）

    try:
        result = await bot.send_message(
            chat_id=GROUP_ID,
            message_thread_id=expected_thread_id,
            text="🔍",
            disable_notification=True,
        )

        actual_thread_id = getattr(result, "message_thread_id", None)
        probe_message_id = getattr(result, "message_id", None)

        if probe_message_id:
            try:
                await bot.delete_message(chat_id=GROUP_ID, message_id=probe_message_id)
            except Exception:
                pass

        if actual_thread_id is None:
            return {"status": "missing_thread_id"}

        if int(actual_thread_id) != int(expected_thread_id):
            return {"status": "redirected", "actual_thread_id": actual_thread_id}

        return {"status": "ok"}

    except Exception as exc:
        error_desc = str(exc).lower()

        if any(
            phrase in error_desc
            for phrase in (
                "thread not found",
                "topic not found",
                "message thread not found",
                "topic deleted",
                "thread deleted",
                "forum topic not found",
                "topic closed permanently",
            )
        ):
            return {"status": "missing", "description": str(exc)}

        if any(
            phrase in error_desc
            for phrase in (
                "message text is empty",
                "bad request: message text is empty",
            )
        ):
            return {"status": "probe_invalid", "description": str(exc)}

        return {"status": "unknown_error", "description": str(exc)}


async def _verify_topic_health(
    bot: Any,
    thread_id: int,
    user_id: int,
    reason: str = "health_check",
) -> bool:
    """验证话题健康状态，带缓存机制。"""
    now = time()
    cached = thread_health_cache.get(thread_id)

    if cached and now - cached.get("timestamp", 0) < THREAD_HEALTH_CACHE_SECONDS:
        return bool(cached.get("healthy"))

    probe_result = await _probe_forum_thread(bot, thread_id, user_id, reason)
    is_healthy = probe_result.get("status") == "ok"

    thread_health_cache[thread_id] = {
        "healthy": is_healthy,
        "timestamp": now,
        "probe_result": probe_result,
    }
    return is_healthy


def _cleanup_dead_thread(session: UserSession) -> None:
    """清理已失效话题的映射与缓存。"""
    if session.thread_id is None:
        return

    old_tid = session.thread_id
    print(f"⚠️ 用户 {session.user_id} 的话题 {old_tid} 已失效，正在清理...")

    thread_to_user.pop(old_tid, None)
    thread_health_cache.pop(old_tid, None)
    session.thread_id = None


async def _ensure_thread_for_user(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    display: str,
) -> Tuple[int, bool]:
    """确保用户拥有一个有效的话题。返回 (thread_id, is_new_topic)。"""
    session = get_session(user_id)

    if session.thread_id is not None:
        is_healthy = await _verify_topic_health(
            context.bot,
            session.thread_id,
            user_id,
            reason="ensure_thread",
        )
        if is_healthy:
            return session.thread_id, False

        _cleanup_dead_thread(session)

    for attempt in range(TOPIC_CREATE_RETRIES):
        try:
            thread_id = await _create_topic_for_user(
                context.bot,
                user_id,
                f"user_{user_id}_{display}",
            )

            await asyncio.sleep(0.5)

            # 立即测试新创建的话题是否可用
            try:
                test_msg = await context.bot.send_message(
                    chat_id=GROUP_ID,
                    message_thread_id=thread_id,
                    text="🔍 Test message to verify topic availability",
                    disable_notification=True,
                )

                actual_thread_id = getattr(test_msg, "message_thread_id", None)
                if actual_thread_id is None or int(actual_thread_id) != int(thread_id):
                    raise Exception(
                        f"Topic test failed: expected {thread_id}, got {actual_thread_id}"
                    )

                await context.bot.delete_message(
                    chat_id=GROUP_ID,
                    message_id=test_msg.message_id,
                )
                print(f"✅ 话题 {thread_id} 创建并验证成功")

            except Exception as exc:
                print(
                    f"❌ 新创建的话题 {thread_id} 无法使用 "
                    f"(尝试 {attempt + 1}/{TOPIC_CREATE_RETRIES}): {exc}"
                )
                if attempt < TOPIC_CREATE_RETRIES - 1:
                    await asyncio.sleep(1)
                    continue
                raise

            session.thread_id = thread_id
            thread_to_user[thread_id] = user_id
            persist_mapping()

            thread_health_cache[thread_id] = {
                "healthy": True,
                "timestamp": time(),
                "probe_result": {"status": "ok"},
            }
            return thread_id, True

        except Exception as exc:
            if attempt == TOPIC_CREATE_RETRIES - 1:
                print(f"❌ 创建话题失败，已达到最大重试次数: {exc}")
                raise

    # 理论上不会走到这里（上面要么 return 要么 raise）
    raise RuntimeError("创建话题失败：未知原因")


def _display_name_from_update(update: Update) -> str:
    user = update.effective_user
    if not user:
        return "匿名"
    name = user.full_name or user.username or str(user.id)
    return name.replace("\n", " ")


# ---------- 数学验证码辅助函数 ----------
def _generate_math_question() -> Tuple[str, int]:
    """生成随机数学题及答案。"""
    import random

    op = random.choice(["+", "-", "*", "/"])

    if op == "+":
        a, b = random.randint(1, 10), random.randint(1, 10)
        return f"{a} + {b} = ?", a + b

    if op == "-":
        a, b = random.randint(1, 10), random.randint(1, 10)
        if a < b:
            a, b = b, a
        return f"{a} - {b} = ?", a - b

    if op == "*":
        a, b = random.randint(1, 10), random.randint(1, 10)
        return f"{a} × {b} = ?", a * b

    divisor = random.randint(1, 10)
    quotient = random.randint(1, 10)
    dividend = divisor * quotient
    return f"{dividend} ÷ {divisor} = ?", quotient


async def _expire_math_answer(
    uid: int, delay: int = MATH_CAPTCHA_EXPIRE_SECONDS
) -> None:
    """异步延迟清理数学验证码，delay为延迟时间（秒）。"""
    await asyncio.sleep(delay)
    math_answers.pop(uid, None)


# ---------- 命令处理器 ----------
async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    msg_parts = [f"👤 你的 ID: <code>{user.id}</code>"]

    if chat.type != "private":
        msg_parts.insert(0, f"📢 群组 ID: <code>{chat.id}</code>")
        thread_id = getattr(update.effective_message, "message_thread_id", None)
        if thread_id:
            msg_parts.append(f"💬 话题 ID: <code>{thread_id}</code>")

    await update.message.reply_text("\n".join(msg_parts), parse_mode=ParseMode.HTML)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if update.effective_chat.type != "private":
        return

    session = get_session(uid)
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
        asyncio.create_task(_expire_math_answer(uid))
        return

    if USE_FIXED_CAPTCHA:
        await update.message.reply_text(VERIFY_QUESTION)
        return

    # 两者都未启用：自动验证通过
    session.verified = True
    session.verify_time = time()
    persist_mapping()
    await update.message.reply_text("你可以直接发送消息，我会帮你转达。")


def _resolve_target_uid(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> Optional[int]:
    """从 /ban /unban 参数或当前话题解析目标用户ID。"""
    if context.args and context.args[0].isdigit():
        return int(context.args[0])

    thread_id = getattr(update.effective_message, "message_thread_id", None)
    if thread_id:
        return thread_to_user.get(int(thread_id))

    return None


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != GROUP_ID:
        return

    target_uid = _resolve_target_uid(update, context)
    if not target_uid:
        await update.message.reply_text("❌ 无法识别目标。请在用户话题内使用或指定ID。")
        return

    session = get_session(target_uid)
    if session.banned:
        await update.message.reply_text(f"用户 {target_uid} 已经在黑名单中了。")
        return

    session.banned = True
    persist_mapping()
    await update.message.reply_text(f"🚫 用户 {target_uid} 已被封禁。")


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != GROUP_ID:
        return

    target_uid = _resolve_target_uid(update, context)
    if not target_uid:
        await update.message.reply_text("❌ 无法识别目标。请在用户话题内使用或指定ID。")
        return

    session = get_session(target_uid)
    if not session.banned:
        await update.message.reply_text(f"用户 {target_uid} 不在黑名单中。")
        return

    session.banned = False
    persist_mapping()
    await update.message.reply_text(f"✅ 用户 {target_uid} 已解封。")


# ---------- 消息处理器 (核心功能) ----------
async def handle_private_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """私聊处理：支持媒体 + 验证 + 自动恢复失效话题。"""
    if update.effective_chat.type != "private":
        return

    uid = update.effective_user.id
    msg = update.message

    text_content = msg.text or msg.caption or ""

    debug_info = f"User {uid}, message_id: {msg.message_id}"
    print(f"DEBUG: Processing message from {debug_info}")

    async with user_locks[uid]:
        print(f"DEBUG: Acquired lock for {debug_info}")

        session = get_session(uid)
        session.last_activity = time()

        if session.banned:
            print(f"DEBUG: {debug_info} is banned")
            await msg.reply_text("🚫 你已被管理员禁止发送消息。")
            return

        user = update.effective_user
        display = _display_name_from_update(update)

        # 1. 验证流程
        if not session.verified:
            print(f"DEBUG: {debug_info} needs verification")

            if USE_MATH_CAPTCHA:
                try:
                    user_answer = int(text_content.strip())
                    correct_answer = math_answers.get(uid)

                    print(
                        "DEBUG: Math verification - user input: "
                        f"{user_answer}, expected: {correct_answer}"
                    )

                    if user_answer == correct_answer:
                        session.verified = True
                        session.verify_time = time()
                        math_answers.pop(uid, None)
                        persist_mapping()
                        await msg.reply_text("验证成功！你现在可以发送消息了。")
                        print(f"DEBUG: {debug_info} verification successful")
                    else:
                        question, answer = _generate_math_question()
                        math_answers[uid] = answer
                        await msg.reply_text(f"答案错误，请重新回答：\n{question}")
                        print(f"DEBUG: {debug_info} gave wrong answer, asking again")

                except ValueError:
                    question, answer = _generate_math_question()
                    math_answers[uid] = answer
                    await msg.reply_text(f"请输入有效数字：\n{question}")
                    print(f"DEBUG: {debug_info} input invalid, asking again")

            elif USE_FIXED_CAPTCHA:
                if text_content.strip() == VERIFY_ANSWER:
                    session.verified = True
                    session.verify_time = time()
                    persist_mapping()
                    await msg.reply_text("验证成功！你现在可以发送消息了。")
                    print(f"DEBUG: {debug_info} fixed verification successful")
                else:
                    await msg.reply_text("请先通过验证：" + VERIFY_QUESTION)
                    print(f"DEBUG: {debug_info} needs to answer fixed question")

            else:
                session.verified = True
                session.verify_time = time()
                persist_mapping()
                print(f"DEBUG: {debug_info} auto-verified (no captcha)")

            return

        print(f"DEBUG: {debug_info} already verified, proceeding to send message")

        # 检查用户名
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
                f"DEBUG: Got thread_id {thread_id} for {debug_info}, "
                f"is_new_topic: {is_new_topic}"
            )
        except Exception as exc:
            print(f"ERROR: Failed to ensure thread for {debug_info}: {exc}")
            await msg.reply_text(f"系统错误：{exc}")
            return

        # 3. 新用户发名片
        if is_new_topic:
            print(f"DEBUG: Sending welcome card for {debug_info} in thread {thread_id}")
            safe_name = html.escape(user.full_name or "无名氏")
            username_text = f"@{user.username}" if user.username else "未设置"
            mention_link = mention_html(uid, safe_name)

            info_text = (
                "<b>新用户接入</b>\n"
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
                print(
                    f"DEBUG: Sent welcome card for {debug_info} in thread {thread_id}"
                )
            except Exception as exc:
                print(f"ERROR: Failed to send welcome card for {debug_info}: {exc}")

        # 4. 转发用户消息
        print(
            f"DEBUG: About to forward message from {debug_info} to thread {thread_id}"
        )

        try:
            sent_msg = await context.bot.copy_message(
                chat_id=GROUP_ID,
                message_thread_id=thread_id,
                from_chat_id=uid,
                message_id=msg.message_id,
            )

            actual_thread_id = getattr(sent_msg, "message_thread_id", None)
            print(
                f"DEBUG: Expected thread_id: {thread_id}, "
                f"Actual thread_id: {actual_thread_id}"
            )

            # 关键逻辑：sent_msg 成功即认为发送成功；仅当 actual_thread_id 明确且不同才重建
            if actual_thread_id is not None and int(actual_thread_id) != int(thread_id):
                print(
                    f"⚠️ {debug_info} 的消息被重定向到话题 {actual_thread_id}"
                    f"（预期话题 {thread_id}），正在重建..."
                )

                session.thread_id = None
                thread_to_user.pop(thread_id, None)
                if thread_id in thread_health_cache:
                    thread_health_cache[thread_id]["healthy"] = False
                persist_mapping()
                print(
                    f"DEBUG: Cleaned up mappings for {debug_info}, old_tid: {thread_id}"
                )

                thread_id, is_new_topic = await _ensure_thread_for_user(
                    context, uid, display
                )
                print(
                    f"DEBUG: Re-created thread_id {thread_id} for {debug_info}, "
                    f"is_new_topic: {is_new_topic}"
                )

                print(f"DEBUG: Re-forwarding message to new thread {thread_id}")
                sent_msg = await context.bot.copy_message(
                    chat_id=GROUP_ID,
                    message_thread_id=thread_id,
                    from_chat_id=uid,
                    message_id=msg.message_id,
                )
                print("DEBUG: Message re-forwarded successfully")

            message_map[(uid, msg.message_id)] = (GROUP_ID, sent_msg.message_id, time())
            print(
                f"DEBUG: Recorded message mapping for {debug_info}, msg_id: {msg.message_id}"
            )

        except Exception as exc:
            print(f"ERROR: Failed to forward message from {debug_info}: {exc}")

            if session.thread_id:
                if session.thread_id in thread_health_cache:
                    thread_health_cache[session.thread_id]["healthy"] = False
                session.thread_id = None

            try:
                await msg.reply_text(f"消息发送失败：{exc}")
            except Exception:
                print(f"ERROR: Could not notify {debug_info} of error: {exc}")

    print(f"DEBUG: Finished processing message from {debug_info}")


async def handle_group_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """群组处理：支持媒体转发。"""
    msg = update.message
    if not msg:
        return

    thread_id = getattr(msg, "message_thread_id", None)
    if (
        msg.chat_id != GROUP_ID
        or not thread_id
        or (msg.from_user and msg.from_user.is_bot)
        or (msg.text and msg.text.startswith("/"))
    ):
        return

    target_user_id = thread_to_user.get(int(thread_id))
    if not target_user_id:
        return

    try:
        sent_msg = await context.bot.copy_message(
            chat_id=target_user_id,
            from_chat_id=GROUP_ID,
            message_id=msg.message_id,
        )
        message_map[(GROUP_ID, msg.message_id)] = (
            target_user_id,
            sent_msg.message_id,
            time(),
        )
    except Exception as exc:
        print(f"ERROR: Could not send message to user {target_user_id}: {exc}")


async def handle_edit_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """处理消息编辑同步。"""
    edited_msg = update.edited_message
    if not edited_msg:
        return

    source_chat_id = edited_msg.chat_id
    source_msg_id = edited_msg.message_id

    target = message_map.get((source_chat_id, source_msg_id))
    if not target:
        return

    target_chat_id, target_msg_id, _ = target

    try:
        if edited_msg.text:
            await context.bot.edit_message_text(
                chat_id=target_chat_id,
                message_id=target_msg_id,
                text=edited_msg.text,
                entities=edited_msg.entities,
            )
        elif edited_msg.caption:
            await context.bot.edit_message_caption(
                chat_id=target_chat_id,
                message_id=target_msg_id,
                caption=edited_msg.caption,
                caption_entities=edited_msg.caption_entities,
            )
    except Exception as exc:
        print(f"编辑同步失败: {exc}")


async def cleanup_message_map(context: ContextTypes.DEFAULT_TYPE) -> None:
    """清理超过24小时的消息映射记录。"""
    now = time()
    preserved = {
        key: value
        for key, value in message_map.items()
        if now - value[2] <= MESSAGE_MAP_TTL_SECONDS
    }

    removed_count = len(message_map) - len(preserved)
    message_map.clear()
    message_map.update(preserved)

    if removed_count > 0:
        print(f"🧹 清理了 {removed_count} 条过期消息映射")


def main() -> None:
    load_persisted_mapping()

    print("Bot is starting...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # 注册命令处理器
    for cmd_name, handler_func in (
        ("start", start),
        ("ban", ban_command),
        ("unban", unban_command),
        ("id", id_command),
    ):
        app.add_handler(CommandHandler(cmd_name, handler_func))

    # 编辑消息处理器
    app.add_handler(
        MessageHandler(filters.UpdateType.EDITED_MESSAGE, handle_edit_message)
    )

    # 私聊消息：允许所有类型，排除命令和状态更新
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

    # 每小时清理一次过期消息映射
    app.job_queue.run_repeating(
        callback=cleanup_message_map,
        interval=CLEANUP_INTERVAL_SECONDS,
        first=CLEANUP_INTERVAL_SECONDS,
    )

    print("Polling started.")
    app.run_polling()


if __name__ == "__main__":
    main()
