# Telegram 私聊转发机器人 (PM Forwarder Bot)

这是一个基于 Python 的 Telegram 机器人，可以将用户发送给机器人的私聊消息转发到你指定的群组，并支持在群组中回复用户。

## ✨ 功能特点
- 收到私聊消息自动转发到管理群。
- 在管理群回复（引用消息），自动转发给原用户。
- 支持文字、图片、视频、贴纸等多种媒体格式。
- 新用户自动发送欢迎/验证卡片。
- **数据持久化**：重启不会丢失用户数据。

## 🚀 Zeabur 一键部署指南

1. **Fork 本项目** 到你的 GitHub 账号。
2. 登录 [Zeabur](https://zeabur.com)，点击 **Create New Project**。
3. 选择 **Deploy New Service** -> **GitHub** -> 选择你刚才 Fork 的仓库。
4. 部署后，进入该服务的 **Settings (设置)** -> **Variables (环境变量)**，添加以下变量：

| 变量名 | 必填 | 说明 | 示例 |
| :--- | :--- | :--- | :--- |
| `BOT_TOKEN` | ✅ | 你的机器人 Token (找 @BotFather 获取) | `123456:ABC-Def...` |
| `GROUP_ID` | ✅ | 消息转发的目标群组 ID (必须带 -100) | `-10012345678` |
| `VERIFY_QUESTION` | ❌ | (可选) 自定义验证问题 | `请输入暗号：` |
| `VERIFY_ANSWER` | ❌ | (可选) 自定义验证答案 | `666` |

5. **⚠️ 重要：挂载数据卷 (防止重启后数据丢失)**
   - 在 Zeabur 服务页面，点击 **Volumes (挂载卷)**。
   - 点击 **Add Volume**。
   - **Mount Path (挂载路径)** 填写：`/data`
   - 点击确认。

6. 等待部署完成，给你的机器人发个消息测试吧！

---
Powered by Python Telegram Bot
