# Pixiv Hearts to Telegram (px2tg)

将 Pixiv 收藏同步到 Telegram 频道/群组的自动化工具。

## 功能

- **自动同步** — 定时从 Pixiv 拉取新的收藏作品，下载原图并发送到 Telegram
- **多渠道分发** — 频道发封面（含元数据描述），群组分发原图文件
- **增量更新** — 仅同步新增/更新的作品，附带版本号管理
- **存活检测** — 自动标记已被作者删除（404）的作品
- **手动管理** — 通过 Bot 命令手动输入/修改作品元数据
- **定时清理** — 自动清理过期缓存文件
- **Docker 部署** — 支持搭配本地 MTProto API 服务器提升消息发送速度

## 用法

### 1. 配置

复制 `config_template.toml` 为 `config.toml`，填入以下信息：

- **Pixiv** — 用户 ID（个人主页 URL 中的数字）、浏览器 Cookie（含 `PHPSESSID`）
- **Telegram** — Bot Token（@BotFather 获取）、允许使用的用户 ID、频道/群组 ID
- **时区、文件路径** 等

### 2. 运行

```bash
# 直接运行（Python 3.7+）
pip install -r requirements.txt
python px2tg_main.py

# Docker Compose（含本地 MTProto API 服务器）
docker compose up -d
```

### 3. Bot 命令

| 命令 | 说明 |
|------|------|
| `/start` | 查看用法 |
| `/sync` | 触发一次完整同步 |
| `/input` | 手动输入作品（Toml 格式元数据 + 上传原图） |
| `/modify` | 手动修改已同步作品 |
| `/cancel` | 取消当前所有任务 |

仅 `config.toml` 中 `allowedUsers` 列表内的用户可执行命令。

## 项目结构

```
px2tg_main.py          # 入口
Pixar2Tele/
├── pixiv.py           # Pixiv API（获取收藏、下载原图）
├── telegram.py        # Telegram 消息发送/编辑/文件管理
├── syncher.py         # 同步引擎（下载→上传→记录）
├── tasks.py           # 定时/触发式任务调度
└── utils.py           # 日志、重试、异常处理
config_template.toml   # 配置模板
Dockerfile             # Docker 构建
docker-compose.yml     # 含本地 MTProto API 的部署方案
```

## 数据文件

- `metadata.json` — 所有作品的元数据（标题、标签、作者、同步状态等）
- `records.csv` — 同步记录（序号、ID、存活状态）
- `我的Pixiv公开收藏夹/` — 下载的原图文件

## 注意事项

- `config.toml` 包含敏感凭据，**不要**提交到版本控制
- 注意 Pixiv 访问频率限制，建议同步间隔不小于 2.8 秒
- 若网络受限，可配置代理

## 许可证

MIT
