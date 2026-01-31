# Telegram 私聊转发机器人（PM Forwarder Bot）

一个基于 Python 的 Telegram 机器人：将用户私聊发给机器人的消息**自动转发**到你指定的管理群（支持话题 / Forum Topics），并支持你在管理群中**直接回复用户**，体验接近“客服工单”。

<img width="1015" height="941" alt="image" src="https://github.com/user-attachments/assets/cacc4b94-42bb-4cf0-939e-885cbbb31eee" />

---

## ✨ 功能特点

- **私聊 → 管理群自动转发**：文本、图片、视频、贴纸、文件等媒体都支持（使用 `copy_message` 保留原消息形态）。
- **一人一话题（Topic）隔离**：每个用户对应管理群里的一个独立话题，消息不混线，跟进更清晰。
- **管理群 → 用户自动回传**：你在对应话题里发消息，机器人会自动转发给该用户。
- **无需引用/回复也能发**：在该用户的话题里直接发消息即可（无需 reply 才能回传）。
- **新用户欢迎卡片**：首次建立话题时自动发送用户信息卡（ID、名字、用户名等）。
- **验证防骚扰（可选）**：
  - 固定口令问答（自定义问题/答案）
  - 随机四则运算验证码（优先生效）
  - 若都不启用：直接放行
- **封禁/解封**：管理员可对指定用户封禁，阻止其继续私聊。
- **数据持久化**：重启后仍保留用户 ↔ 话题映射（需要挂载数据卷）。
- **编辑同步（可选增益）**：用户或你编辑消息后，会尝试同步到对端（仅在机器人运行期间且映射未过期时有效）。

---

## 🧭 工作原理（建议先看）

1. 用户私聊机器人  
2. 机器人在管理群创建一个专属话题（Topic），并把消息转发到该话题  
3. 你在该话题里发消息，机器人自动转发回给用户  
4. 机器人会做**话题健康检查**：若话题被删除/失效，会自动重建并修复映射  

> ✅ 依赖 Telegram 的“话题模式（Forum Topics）”，请确保管理群开启了话题模式。

---

## ✅ 事先准备

### 1）创建机器人并获取 Token（BOT_TOKEN）

在 Telegram 找到 @BotFather：
- 输入 `/newbot`
- 按提示设置机器人名称与用户名
- 获得 `BOT_TOKEN`

<img width="437" height="286" alt="image" src="https://github.com/user-attachments/assets/5a16772f-7232-4d5b-b328-7d65fde94b13" />

---

### 2）创建管理群并获取 GROUP_ID

1. 创建一个**私有群组**（建议只有你和机器人）
2. 把机器人拉进群，并给它**管理员权限**
3. 在群设置中开启 **话题模式**（建议选择「列表」展示）

<img width="270" height="340" alt="image" src="https://github.com/user-attachments/assets/9b42820a-b48c-4237-ab85-de1c865bffad" />
<img width="396" height="254" alt="image" src="https://github.com/user-attachments/assets/a4564c74-6cdf-4f94-9897-786a18d2ac04" />
<img width="812" height="825" alt="image" src="https://github.com/user-attachments/assets/cef0df0c-422e-4db2-a76d-43939aaafb03" />

#### 获取群组 ID 的几种方式

**方式 A（Windows 客户端）**  
Windows 客户端点击群组信息可以看到群组 ID（需要自行加 `-100` 前缀），例如：`-100113320xxxxx`

<img width="1018" height="923" alt="image" src="https://github.com/user-attachments/assets/123d4305-aee0-4814-bd5f-2eca527e049e" />

**方式 B（用 /id 命令）**  
你也可以邀请一个临时机器人进群获取 ID，然后移除它即可：  
在群里发送 `/id` 会显示群组 ID

> 离歌的机器人：`https://t.me/lige0407_bot`（无需给任何权限）

<img width="389" height="606" alt="image" src="https://github.com/user-attachments/assets/6ca50743-2e36-4372-8bbb-3545d3f69163" />
<img width="804" height="169" alt="image" src="https://github.com/user-attachments/assets/9bfc02a7-a3b1-4b4d-b431-842119eaa93c" />

## 🚀 Zeabur 一键部署指南

### 1）Fork 本项目
先 Fork 到你的 GitHub 账号。

### 2）在 Zeabur 创建项目
登录 Zeabur → **Create New Project** → 选择共享集群（免费试用随便选一个）→ 创建项目

<img width="1374" height="639" alt="image" src="https://github.com/user-attachments/assets/0f55f084-73ad-45ea-b39e-792059d31d6f" />
<img width="914" height="1143" alt="image" src="https://github.com/user-attachments/assets/c2ff5601-415b-43f6-8241-441b59484603" />

### 3）部署服务
点击【部署新服务】→【从 GitHub 部署】→ 选择你刚才 Fork 的仓库

<img width="430" height="442" alt="image" src="https://github.com/user-attachments/assets/bfeb4f88-b92d-4293-8222-422f566abb42" />
<img width="603" height="599" alt="image" src="https://github.com/user-attachments/assets/a88a5848-927d-4af6-83a3-4593fef852db" />
<img width="633" height="193" alt="image" src="https://github.com/user-attachments/assets/01683fde-1ce2-4dfb-ab0e-01d70f049eef" />

### 4）配置环境变量
进入服务的【配置】页，添加上面表格里的变量。

<img width="719" height="373" alt="image" src="https://github.com/user-attachments/assets/649a44ce-6dd7-4fda-b10f-973739174df9" />

---

## 🔧 环境变量配置说明

| 变量名 | 必填 | 说明 | 示例 |
| :--- | :---: | :--- | :--- |
| `BOT_TOKEN` | ✅ | 机器人 Token（@BotFather 获取） | `123456:ABC-Def...` |
| `GROUP_ID` | ✅ | 管理群 ID（必须带 `-100` 前缀） | `-10012345678` |
| `VERIFY_QUESTION` | ❌ | 固定口令模式下的提示问题 | `请输入访问密码：` |
| `VERIFY_ANSWER` | ❌ | 固定口令的答案（设置后即启用固定口令验证） | `4399` |
| `USE_MATH_CAPTCHA` | ❌ | 是否启用数学验证码（`true` 启用） | `true` |

### 验证规则（重要）
- 「数学验证码」和「固定口令」**二选一**即可
- 如果二者都设置了：**优先生效数学验证码**
- 如果都未设置：不需要验证即可直接聊天

---

### 5）部署
点击【下一步】→【部署】  
看到【运行中】表示部署完成。

<img width="997" height="593" alt="image" src="https://github.com/user-attachments/assets/718ab9e6-a858-4ceb-ae4f-9bda5c1cb210" />

### 6）修改简介
> [!IMPORTANT]
> 将机器人的名字放在你的 tg 账号简介中，比如 `@My-bot`
> 
> 其他人即可使用该机器人与你聊天
> 
> 记得在 `@` 前面加一个空格

---

## 💾 ⚠️ 强烈建议：挂载数据卷（防止重启丢数据）

如果不挂载，机器人重启后会丢失「用户 ↔ 话题」映射，历史对话会重新开新话题。

在 Zeabur 服务页面：
1. 点击 **硬盘**
2. 点击 **挂载硬盘**
3. **硬盘名**：`data`
4. **挂载目录**：`/data`

> 机器人会把映射写到：`/data/topic_mapping.json`

---

## 🧪 快速测试

在管理群中发送：
- `/id`：查看群组 ID / 当前话题 ID（如果在话题里）
- 私聊机器人 `/start`：查看验证是否生效
- 私聊发送一条消息：应自动出现在管理群对应话题
- 在话题里发消息：应自动转发回用户私聊

---

## 🛠️ 管理命令

- `/id`  
  - 私聊：显示用户 ID  
  - 群组：显示群组 ID、话题 ID（若在话题内）

- `/ban <user_id>`  
  - 封禁指定用户  
  - 也可以在该用户话题内直接执行 `/ban`（无需参数）

- `/unban <user_id>`  
  - 解封指定用户  
  - 也可以在该用户话题内直接执行 `/unban`

