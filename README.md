# Pixiv-Hearts-to-Telegram

## *将你的 Pixiv 收藏夹同步到 Telegram 频道*

一个自动化工具，将您的 Pixiv 喜欢（收藏）的作品同步到 Telegram 频道，支持定时更新、元数据管理和消息同步。

## 功能特性

- 🎨 **自动同步**：定时从 Pixiv 账户拉取新的收藏作品
- 📤 **多渠道支持**：支持向 Telegram 频道、群组和垃圾箱发送内容
- 📝 **元数据管理**：完整保存作品信息（标题、标签、作者等）
- 📊 **同步记录**：维护详细的同步历史记录
- 🤖 **机器人命令**：支持 Telegram 机器人指令进行手动控制
- 🔐 **权限控制**：仅允许指定的用户使用机器人
- 🌍 **时区支持**：灵活的时区配置
- 🔄 **本地 API 服务器**：支持官方 TG API 或本地 MTProto 服务器

## 项目结构

```
PixivHearts2Telegram/
├── px2tg_main.py              # 主程序入口
├── config_template.toml        # 配置文件模板
├── config.toml                # 实际配置文件（需自行创建）
├── metadata.json              # 作品元数据存储
├── records.csv                # 同步记录日志
├── env.yml                    # Conda 环境配置
├── README.md                  # 本文件
├── Pixar2Tele/               # 核心模块
│   ├── __init__.py
│   ├── pixiv.py              # Pixiv API 相关功能
│   ├── telegram.py           # Telegram 机器人相关功能
│   ├── syncher.py            # 同步引擎
│   ├── tasks.py              # 定时任务处理
│   └── utils.py              # 工具函数（日志、重试等）
├── log/                       # 日志文件目录
└── 我的Pixiv公开收藏夹/        # 作品保存目录

```

## 安装与配置

### 环境要求

- Python 3.7+
- Conda（推荐）或 pip

### 1. 克隆项目

```bash
git clone <repository-url>
cd PixivHearts2Telegram
```

### 2. 创建 Conda 环境

```bash
conda env create -f env.yml
conda activate px2tg
```

### 3. 配置项目

复制配置模板并修改：

```bash
cp config_template.toml config.toml
```

在 `config.toml` 中设置以下内容：

#### Pixiv 配置

```toml
[pixiv]
userID = 100000000              # 你的 Pixiv 用户 ID
headers.User-Agent = '...'      # 浏览器 User-Agent
headers.Cookie = '...'          # Pixiv 登录 Cookie（包含 PHPSESSID）
```

**获取 Pixiv Cookie：**
1. 在浏览器中登录 Pixiv
2. 打开开发者工具（F12）
3. 查看 Network 标签中的请求头
4. 复制 `Cookie` 字段的值

#### Telegram 配置

```toml
[telegram]
botToken = 'YOUR_BOT_TOKEN'                    # BotFather 获取的 Token
localApiServerURL = 'http://localhost:8081/'  # 本地 MTProto API 服务器 URL（可选）
allowedUsers = [123456789, 987654321]         # 允许使用机器人的用户 ID 列表

[telegram.archiveChatIDs]
channel = -1001234567890                      # Telegram 频道 ID
group = -1009876543210                        # Telegram 群组 ID
dustbin = -1001122334455                      # 垃圾箱聊天 ID
```

**获取 Telegram IDs：**
- **频道/群组 ID**：添加 @getidsbot 到频道/群组，它会显示 ID
- **用户 ID**：与 @get_id_bot 私聊可获取你的用户 ID
- **Bot Token**：与 @BotFather 私聊创建机器人并获取 Token

#### 其他配置

```toml
timezone = 'Asia/Shanghai'      # 时区设置
logFile = './log/px2tg.log'     # 日志文件路径

[paths]
artworkSave = './我的Pixiv公开收藏夹'  # 作品保存目录
metadataFile = './metadata.json'      # 元数据文件
recordsFile = './records.csv'         # 同步记录文件
err404Picture = './pixiv404.png'      # 404 错误图片
```

### 4. 运行程序

```bash
# 前台运行（调试时使用）
python px2tg_main.py

# 后台运行（生产环境）
nohup python -u px2tg_main.py > /dev/null 2>&1 &
```

## 使用指南

### Telegram 机器人命令

程序运行后，可在 Telegram 中向机器人发送以下命令（仅允许列表中的用户可用）：

- `/sync` - 立即执行一次同步
- `/status` - 查看当前状态
- 等等（具体命令请参考代码中的定义）

### 数据文件说明

- **metadata.json**：存储所有作品的详细信息，包括 ID、标题、标签、作者、页数等
- **records.csv**：同步历史记录，含有 `syncNo`、`id` 和 `existence` 列
- **我的Pixiv公开收藏夹/**：保存下载的作品文件

## 核心模块说明

- **pixiv.py**：处理 Pixiv API 调用，获取收藏作品信息
- **telegram.py**：处理 Telegram 机器人通信和消息发送
- **syncher.py**：核心同步引擎，协调 Pixiv 和 Telegram 的数据同步
- **tasks.py**：定时任务调度（使用 schedule 库），处理周期性同步
- **utils.py**：日志、异常处理、装饰器等工具函数

## 注意事项

⚠️ **隐私与安全：**
- 不要将 `config.toml` 提交到版本控制系统
- 妥善保管 Pixiv Cookie 和 Telegram Bot Token
- 确保频道/群组设置正确，避免内容泄露

⚠️ **API 限制：**
- 注意 Pixiv 的访问频率限制
- 合理设置同步周期，避免被限流或封号

⚠️ **网络：**
- 若需要访问 Pixiv，可能需要配置代理
- 本地 MTProto 服务器可加速 Telegram API 调用

## 故障排除

- 检查日志文件：`./log/px2tg.log`
- 确保网络连接和 API 密钥配置正确
- 验证 Telegram 账号权限和频道权限设置

## 许可证

MIT

## 贡献

欢迎提交 Issue 和 Pull Request！
