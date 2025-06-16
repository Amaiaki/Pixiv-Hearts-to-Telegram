# 把 Pixiv 收藏同步到 TG 频道
#
# Usage: nohup python -u pxh2tg_main.py >> pxh2tg.log 2>&1 &

import os
import tomlkit

from telebot import TeleBot
from telebot.types import Message

from PixivHearts2Telegram import Tasks, auto_retry, P2TLogger, NowTimer

os.environ["TZ"] = "Asia/Shanghai"
now_timer = NowTimer(dt_format='%Y-%m-%d %H:%M:%S %z', tz="Asia/Shanghai")
print(f'【{now_timer.now()}】启动 Bot: Pixiv Hearts to Telegram')

# 读取配置信息
with open('config.toml', 'r+t') as f:
    config: dict = tomlkit.load(f)
    # 创建 Bot 对象
    bot = TeleBot(config['telegram']['botToken'])
    # 有权限使用机器人的用户
    ALLOWED_TELEGRAM_USERS = config['telegram']['allowedUsers']
    # 日志文件
    LOG_FILE_PATH = config['paths']['logFile']
    # 使用说明
    HELP_INFO_FILE_PATH = config['paths']['helpInfoFile']
    # 设置任务，并初始化
    tasks = Tasks(
        bot = bot,
        allowed_telegram_users = config['telegram']['allowedUsers'],
        pixiv_user_id = config['pixiv']['userID'],
        archive_chat_ids = config['telegram']['archiveChatIDs'],
        channel_catalog_msg_id = config['telegram'].get('channelCatalogMsgID', None),
        artwork_save_path = config['paths']['artworkSave'],
        metadata_filepath = config['paths']['metadataFile'],
        headers = config['pixiv']['headers'],
        proxies = None,
        timezone='Asia/Shanghai',
        dt_format='%Y-%m-%d %H:%M:%S %z',
    )
    channel_catalog_msg_id = tasks.setup()
    # 如果没有频道目录，则已经自动创建，需要保存到配置中
    if 'channel_catalog_msg_id' not in config['telegram']:
        config['telegram']['channelCatalogMsgID'] = channel_catalog_msg_id
        f.seek(0)
        f.write(config.as_string())
        f.truncate()

p2t_logger = P2TLogger()
p2t_logger.filter_keywords(exclude_keywords = [
    'TimeoutError',
    'urllib3.exceptions.ReadTimeoutError',
    'requests.exceptions.ReadTimeout',
    'requests.exceptions.ConnectionError',
    'telebot.apihelper.ApiTelegramException',
])


@bot.message_handler(commands=['help'], func=lambda msg: int(msg.from_user.id) in ALLOWED_TELEGRAM_USERS)
def show_help_info(message:Message):
    with open(HELP_INFO_FILE_PATH, 'r') as f:
        text = f.read()
        bot.send_message(message.chat.id, text, parse_mode='HTML')


@bot.message_handler(commands=['sync'], func=lambda msg: int(msg.from_user.id) in ALLOWED_TELEGRAM_USERS)
def synchronize(message:Message):
    '''同步Pixiv收藏夹，无参数。'''
    # 同步时终止元数据更新
    tasks.stop_all_metadata_updating()
    # 开始同步
    msg = auto_retry(bot.send_message)(message.chat.id, "正在同步Pixiv收藏夹……")
    try:
        updating_feedback = tasks.sync_pixiv_collections(msg)
    except Exception as e:
        updating_feedback_msg = tasks.TelegramUploader.get_message_content(msg.chat.id, msg.id)
        auto_retry(bot.edit_message_text)(updating_feedback_msg.text+"\n同步失败！", msg.chat.id, msg.id)
        raise e
    else:
        auto_retry(bot.edit_message_text)(updating_feedback, msg.chat.id, msg.id)
    # 恢复定时元数据更新
    tasks.start_scheduled_metadata_updating()


@bot.message_handler(commands=['meta'], func=lambda msg: int(msg.from_user.id) in ALLOWED_TELEGRAM_USERS)
def update_metadata(message:Message):
    '''
    更新元数据，可以用参数指定更新范围，offset指的是Pixiv收藏夹中指定作品相对于最新收藏的作品的偏移。
    - `/meta [offset_1] [offset_2]`：其中 offset_1 < offset_2
    - `/meta [offset_2]`：指从最新收藏（即offset_1==0）更新到 offset_2
    - `/meta`：无参数，更新最新100个收藏
    '''
    # 格式：[start, end] (start < end) 从新到旧
    msg_args = message.text.split(' ')[1:]
    if len(msg_args) == 2:
        offset_range = []
        offset_range.append(None if msg_args[0] == 'n' else int(msg_args[0]))
        offset_range.append(None if msg_args[1] == 'n' else int(msg_args[1]))
        offset_range = tuple(offset_range)
    elif len(msg_args) == 1:
        offset_range = (0, None if msg_args[0] == 'n' else int(msg_args[0]))
    else:
        offset_range = (0, 100)
    # 启动任务
    tasks.start_triggered_metadata_updating(
        offset_range = offset_range,
        feedback_chat_ids = [message.chat.id],
    )


@bot.message_handler(
    commands=['help','sync','meta'], 
    func=lambda msg: int(msg.from_user.id) not in ALLOWED_TELEGRAM_USERS
)
def handle_restricted_message(message:Message):
    bot.send_message(message.chat.id, "你没有权限使用这个机器人。")
    print(f'【{now_timer.now()}】【禁止用户】已禁止无权限访问者: tg://user?id={message.chat.id}')


bot.infinity_polling()

