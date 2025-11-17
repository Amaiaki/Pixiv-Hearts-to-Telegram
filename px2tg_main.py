# 把 Pixiv 收藏同步到 TG 频道
#
# Usage: nohup python -u px2tg_main.py > /dev/null 2>&1 &

import tomlkit

from telebot.types import Message
from telebot import TeleBot

from Pixar2Tele import Tasks, P2TLogging, autoRetry



# 读取配置信息
with open('config.toml', 'r+t') as f:
    config: dict = tomlkit.load(f)
    # 时区
    timezone = config['timezone']
    # 创建 Bot 对象
    bot = TeleBot(config['telegram']['botToken'])
    # 有权限使用机器人的用户
    ALLOWED_TELEGRAM_USERS = config['telegram']['allowedUsers']
    # 日志配置
    p2t_logging = P2TLogging(
        log_file_path = config['logFile'],
        timezone = timezone,
    )
    # 设置任务，并初始化
    tasks = Tasks(
        bot = bot,
        local_api_server_url = config['telegram']['localApiServerURL'],
        allowed_telegram_users = config['telegram']['allowedUsers'],
        pixiv_user_id = config['pixiv']['userID'],
        channel_id = config['telegram']['archiveChatIDs']['channel'],
        group_id = config['telegram']['archiveChatIDs']['group'],
        dustbin_id = config['telegram']['archiveChatIDs']['dustbin'],
        channel_catalog_msg_id = config['telegram'].get('channelCatalogMsgID', None),
        metadata_file_path = config['paths']['metadataFile'],
        records_file_path = config['paths']['recordsFile'],
        err404_cover_file_path = config['paths']['err404Picture'],
        save_path = config['paths']['artworkSave'],
        temp_path = './temp',
        headers = config['pixiv']['headers'],
        proxies = None,
        timezone = timezone,
    )
    channel_catalog_msg_id = tasks.getChannelCatalogID()
    # 如果没有频道目录，则已经自动创建，需要保存到配置中
    if 'channel_catalog_msg_id' not in config['telegram']:
        config['telegram']['channelCatalogMsgID'] = channel_catalog_msg_id
        f.seek(0)
        f.write(config.as_string())
        f.truncate()


logger = p2t_logging.getLogger()

p2t_logging.filterKeywords(exclude_keywords=[
    'TimeoutError',
    'urllib3.exceptions.ReadTimeoutError',
    'requests.exceptions.ReadTimeout',
    'requests.exceptions.ConnectionError',
    'telebot.apihelper.ApiTelegramException',
])


@bot.message_handler(commands=['start'], 
    func=lambda msg: int(msg.from_user.id) in ALLOWED_TELEGRAM_USERS)
def showHelpInfo(message:Message):
    logger.info("【用法提示】请求来自：tg://user?id=%d", message.chat.id)
    autoRetry(bot.send_message)(message.chat.id, parse_mode='HTML',
        text="<code>/start</code>\n<blockquote>开启对话，查看命令用法。</blockquote>\n" +\
            "<code>/sync</code>\n<blockquote>命令式（触发式）同步 Pixiv 收藏夹。</blockquote>" +\
            "<code>/input</code>\n<blockquote>手动输入作品。</blockquote>" +\
            "<code>/modify</code>\n<blockquote>手动修改作品。</blockquote>" +\
            "<code>/cancel</code>\n<blockquote>取消所有当前任务。</blockquote>",
    )


@bot.message_handler(commands=['sync'], 
    func=lambda msg: int(msg.from_user.id) in ALLOWED_TELEGRAM_USERS)
def syncByTriggered(message: Message):
    '''触发式/命令式同步Pixiv收藏夹，无参数。'''
    logger.info("【触发式同步】请求来自：tg://user?id=%d", message.chat.id)
    tasks.startTriggeredSync(feedback_chat_ids=[message.chat.id])


@bot.message_handler(commands=['input'], 
    func=lambda msg: int(msg.from_user.id) in ALLOWED_TELEGRAM_USERS)
def manuallyInputArtwork(message: Message):
    logger.info("【手动输入作品】请求来自：tg://user?id=%d", message.chat.id)
    tasks.manuallyInputArtwork(message)


@bot.message_handler(commands=['modify'], 
    func=lambda msg: int(msg.from_user.id) in ALLOWED_TELEGRAM_USERS)
def manuallyModifyArtwork(message: Message):
    logger.info("【手动修改作品】请求来自：tg://user?id=%d", message.chat.id)
    tasks.manuallyModifyArtwork(message)


@bot.message_handler(commands=['cancel'], 
    func=lambda msg: int(msg.from_user.id) in ALLOWED_TELEGRAM_USERS)
def cancelAllTasks(message: Message):
    logger.info("【取消当前所有任务】请求来自：tg://user?id=%d", message.chat.id)
    tasks.stopAllTasks()
    tasks.startScheduledTasks()
    autoRetry(bot.send_message)(message.chat.id, "✅ 已取消当前所有任务。")


@bot.message_handler(commands=['start', 'sync', 'input', 'modify'], 
    func=lambda msg: int(msg.from_user.id) not in ALLOWED_TELEGRAM_USERS)
def handleRestrictedMessage(message:Message):
    autoRetry(bot.send_message)(message.chat.id, "你没有权限使用这个机器人。")
    logger.warning("【禁止访客】已禁止无权限访问者: tg://user?id=%d", message.chat.id)


logger.info("启动 Bot: Pixiv Hearts to Telegram")
bot.infinity_polling()


