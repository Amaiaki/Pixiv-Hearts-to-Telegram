import re
import os
import time
import pytz
import logging
import tomlkit
import schedule
import threading

from math import ceil
from threading import Event
from datetime import datetime
from telebot import TeleBot, types
from telebot.types import Message, CallbackQuery

from .utils import autoRetry, logIfError
from .syncher import Syncher



class Tasks:
    '''
    - 元数据的格式：
    ```
    {
        "<pixiv_artwork_id>": {
            "id": <pixiv_artwork_id: int>,
            "illustType": <: int>,
            "pageCount": <: int>,
            "title": <: str>,
            "tags": <: list[str]>,
            "createDate": <: str>,
            "updateDate": <: str>,
            "authorScreenName": <author_screen_name: str>,
            "authorUserId": <author_user_id: int>,
            "bookmarkTags": <: list[str]>,
            "referer": "https://www.pixiv.net/artworks/<pixiv_artwork_id>",
            "pages": <["<page_0_file_name>", "<page_1_file_name>", ...]>,
            "existence": <: bool>,
            "version": <: int>,
            "channelMessageId": <: int>,
            "groupMessageId": <: int>,
            "groupDocumentMessageIds": <: list[int]>,
        },
        ...
    }
    ```
    - 同步记录表的列：`'syncNo', 'id', 'existence'`
    '''
    def __init__(
            self,
            bot: TeleBot,
            local_api_server_url: str,
            allowed_telegram_users: list[str | int],
            pixiv_user_id: int,
            channel_id: int,
            group_id: int,
            dustbin_id: int,
            channel_catalog_msg_id: int,
            metadata_file_path: str,
            records_file_path: str,
            err404_cover_file_path: str,
            save_path: str,
            temp_path: str,
            headers: dict,
            proxies: dict = None,
            timezone: str = "Asia/Shanghai",
        ):
        self.bot = bot

        self.DUSTBIN_ID = dustbin_id
        self.CHANNEL_ID = channel_id
        self.GROUP_ID = group_id
        self.SAVE_PATH = save_path

        self.Syncher = Syncher(
            bot = bot,
            local_api_server_url=local_api_server_url,
            pixiv_user_id = pixiv_user_id,
            channel_id = channel_id,
            group_id = group_id,
            dustbin_id = dustbin_id,
            channel_catalog_msg_id = channel_catalog_msg_id,
            metadata_file_path = metadata_file_path,
            records_file_path = records_file_path,
            err404_cover_file_path = err404_cover_file_path,
            save_path = save_path,
            temp_path = temp_path,
            headers = headers,
            proxies = proxies,
        )
        self.Pixiv = self.Syncher.Pixiv
        self.Teleg = self.Syncher.Teleg
        self.getChannelCatalogID = self.Syncher.getChannelCatalogID
        
        # 任务事件终止标志
        self.event_stop_triggered_synchronizing = threading.Event()
        self.event_stop_scheduled_tasks = threading.Event()
        self.event_stop_manual_tasks = threading.Event()

        # 设置并启动定时任务：包括定时同步任务和定时清理任务
        schedule.every().monday.at("09:30", pytz.timezone(timezone)).do(
            self.syncOnSchedule, allowed_telegram_users)
        schedule.every().day.at("09:00", pytz.timezone(timezone)).do(
            self.removeOutDatedFiles, self.SAVE_PATH, 86400)
        self.thread_scheduled_tasks = threading.Thread(
            target=self.runSchedule, args=(self.event_stop_scheduled_tasks,))
        self.thread_scheduled_tasks.start()
        
        # 设置触发同步任务
        self.thread_triggered_synchronizing = threading.Thread(
            target=self.syncByTriggered, args=([],))
        # 防止其他同步任务和触发同步任务同时进行
        self.is_synchronizing_by_triggered = False

        # 防止两个手动任务同时进行
        self.manual_artwork_info = None
        
        # 日志
        self.logger = logging.getLogger('Pixar2Tele')
    

    def startScheduledTasks(self):
        # 停止定时任务
        self.event_stop_scheduled_tasks.set()
        if self.thread_scheduled_tasks.is_alive():
            self.thread_scheduled_tasks.join()
        self.event_stop_scheduled_tasks.clear()
        # 启动定时任务
        self.thread_scheduled_tasks = threading.Thread(
            target=self.runSchedule, 
            args=(self.event_stop_scheduled_tasks,),
        )
        self.thread_scheduled_tasks.start()
    

    def startTriggeredSync(self, feedback_chat_ids: list[int|str]):
        # 停止所有任务
        self.stopAllTasks()
        # 启动触发同步任务
        self.thread_triggered_synchronizing = threading.Thread(
            target=self.syncByTriggered, args=(feedback_chat_ids,))
        self.thread_triggered_synchronizing.start()
        # 恢复定时任务
        self.startScheduledTasks()
    
    
    def runSchedule(self, stop_event: threading.Event):
        while not stop_event.is_set():
            schedule.run_pending() # 检查是否到了任务的预定开始执行时期
            time.sleep(1)
    

    def syncOnSchedule(self, feedback_chat_ids: list[int|str]):
        # 防止与触发同步任务冲突
        if self.is_synchronizing_by_triggered:
            for chat_id in feedback_chat_ids:
                autoRetry(self.bot.send_message)(
                    chat_id, '已取消本次同步任务，因为当前有触发同步任务。')
            return False
        # 开始同步
        self.logger.info("【定时同步】启动定时同步任务。")
        logIfError(self.logger, self.syncTask)(
            self.event_stop_scheduled_tasks, feedback_chat_ids)
        self.logger.info("【定时同步】定时同步任务完成。")
    

    def removeOutDatedFiles(self, dir: str, time2live: float, stop_event: Event = None):
        self.logger.info(f"【清理过期文件】正在清理目录 \"{dir}\" 下创建时间大于 {time2live}s 的文件。")
        now = time.time()
        out_dated_time = now - time2live
        # 遍历文件夹中的所有文件
        for filename in os.listdir(dir):
            file_path = os.path.join(dir, filename)
            if stop_event and stop_event.is_set(): return
            # 判断是否是文件（排除子文件夹）
            if os.path.isfile(file_path):
                # 获取文件的最后修改时间
                file_ctime = os.path.getctime(file_path)
                # 如果文件修改时间早于过期时间，则删除
                if file_ctime < out_dated_time: os.remove(file_path)
        self.logger.info(f"【清理过期文件】目录 \"{dir}\" 清理完成。")
    

    def syncByTriggered(self, feedback_chat_ids: list[int|str]):
        self.is_synchronizing_by_triggered = True
        self.logger.info("【触发式同步】启动触发式同步任务。")
        logIfError(self.logger, self.syncTask)(
            self.event_stop_triggered_synchronizing, feedback_chat_ids)
        self.logger.info("【触发式同步】触发式同步任务完成。")
        self.is_synchronizing_by_triggered = False
    

    def syncTask(self, stop_event: Event, feedback_chat_ids: list[int|str]):
        # 确定步长，通过步长控制步数为50步左右，如果50步不能完成，则最大步长为50
        num_collections = self.Pixiv.countCollection()
        pace = max(min(ceil(num_collections / 50), 50), 1)
        # 开始同步
        feedback_text, feedback_messages = self.Syncher.autoSync(
            feedback_chat_ids = feedback_chat_ids, stop_event = stop_event,
            start_offset = 0, end_offset = num_collections, pace = pace,
        )
        # 完成同步
        for msg in feedback_messages:
            autoRetry(self.bot.edit_message_text)(
                text = f'{feedback_text}\n同步结束。', parse_mode='HTML',
                chat_id = msg.chat.id, message_id = msg.id,
            )
        return
    

    def stopAllTasks(self):
        self.event_stop_scheduled_tasks.set()
        self.event_stop_triggered_synchronizing.set()
        self.event_stop_manual_tasks.set()
        if self.thread_scheduled_tasks.is_alive():
            self.thread_scheduled_tasks.join()
        if self.thread_triggered_synchronizing.is_alive():
            self.thread_triggered_synchronizing.join()
        self.event_stop_scheduled_tasks.clear()
        self.event_stop_triggered_synchronizing.clear()
        self.event_stop_manual_tasks.clear()
        

    def manuallyInputArtwork(self, message: Message):
        '''手动输入作品。
        #TODO: 增加 `/cancel` 命令取消任务的功能。
        ```
        {
            "id": <pixiv_artwork_id: str>,
            "illustType": <: int>,
            "title": <: str>,
            "tags": <: list[str]>,
            "createDate": <: str>,
            "updateDate": <: str>,
            "authorScreenName": <author_screen_name: str>,
            "authorUserId": <author_user_id: str>,
            "bookmarkTags": <: list[str]>,
            "version": <: int>,
            "pages": <["<page_0_file_name>", "<page_1_file_name>", ...]>,
            "existence": <: bool>
        }
        ```'''
        def finishProcessingPagesMarkup():
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("✅ 完成", callback_data="completeInput"))
            markup.add(types.InlineKeyboardButton("❌ 取消", callback_data="cancelInput"))
            markup.add(types.InlineKeyboardButton("➡️ 继续", callback_data="goOnInput"))
            return markup
        
        @self.bot.callback_query_handler(func=lambda call: call.data == "completeInput")
        def complete(call: CallbackQuery):
            status = self.Syncher.manuallyInputArtwork(self.manual_artwork_info)
            self.manual_artwork_info = None
            autoRetry(self.bot.edit_message_text)(
                "✅ 已成功手动输入作品。", call.message.chat.id, call.message.id)
        
        @self.bot.callback_query_handler(func=lambda call: call.data == "cancelInput")
        def cancel(call: CallbackQuery):
            self.manual_artwork_info = None
            autoRetry(self.bot.edit_message_text)(
                "❌ 已取消此次作品输入。", call.message.chat.id, call.message.id)
        
        @self.bot.callback_query_handler(func=lambda call: call.data == "goOnInput")
        def goOn(call: CallbackQuery):
            part_no_str = re.search(r'p(\d+)', call.message.text)
            part_no = part_no_str.group(1)
            autoRetry(self.bot.edit_message_text)(f"➡️ 继续上传作品原图p{part_no}：", 
                call.message.chat.id, call.message.id)
            self.bot.register_next_step_handler(message, processPages)
        
        def processMeta(message: Message):
            try: info = dict(tomlkit.loads(message.text))
            except Exception as e:
                self.logger.warning(f"【手动输入作品】元数据无法解析为 Toml 格式：\n{message.text}\n报错：{e}")
                autoRetry(self.bot.send_message)(chat_id=message.chat.id, parse_mode='HTML',
                    text=f"❗元数据不是 Toml 格式，作品输入取消。")
                self.manual_artwork_info = None
                return

            error_key = None
            for _ in range(1):
                if 'id' in info and isinstance(info['id'], str):
                    if self.Syncher.isArtworkRecorded(info['id']):
                        autoRetry(self.bot.send_message)(
                            message.chat.id, "❌ 已有相同 id 的作品存在。")
                        error_key = 'id'
                        continue
                    else: self.manual_artwork_info['id'] = info['id']
                else:
                    error_key = 'id'
                    continue

                if ('illustType' in info and isinstance(info['illustType'], int) 
                and 0 <= info['illustType'] <= 2):
                    self.manual_artwork_info['illustType'] = info['illustType']
                else:
                    error_key = 'illustType'
                    continue

                if 'title' in info and isinstance(info['title'], str):
                    self.manual_artwork_info['title'] = info['title']
                else:
                    error_key = 'title'
                    continue

                if ('tags' in info and isinstance(info['tags'], list) 
                and all(isinstance(tag, str) for tag in info['tags'])):
                    self.manual_artwork_info['tags'] = info['tags']
                else:
                    error_key = 'tags'
                    continue

                if 'createDate' in info:
                    try:
                        datetime.strptime(info['createDate'], "%Y-%m-%dT%H:%M:%S+09:00")
                        self.manual_artwork_info['createDate'] = info['createDate']
                    except ValueError:
                        error_key = 'createDate'
                        continue
                else:
                    error_key = 'createDate'
                    continue

                if 'updateDate' in info:
                    try:
                        datetime.strptime(info['updateDate'], "%Y-%m-%dT%H:%M:%S+09:00")
                        self.manual_artwork_info['updateDate'] = info['updateDate']
                    except ValueError:
                        error_key = 'updateDate'
                        continue
                else:
                    error_key = 'updateDate'
                    continue

                if 'authorScreenName' in info and isinstance(info['authorScreenName'], str):
                    self.manual_artwork_info['authorScreenName'] = info['authorScreenName']
                else:
                    error_key = 'authorScreenName'
                    continue

                if 'authorUserId' in info and isinstance(info['authorUserId'], str):
                    self.manual_artwork_info['authorUserId'] = info['authorUserId']
                else:
                    error_key = 'authorUserId'
                    continue

                if ('bookmarkTags' in info and isinstance(info['bookmarkTags'], list) 
                and all(isinstance(tag, str) for tag in info['bookmarkTags'])):
                    self.manual_artwork_info['bookmarkTags'] = info['bookmarkTags']
                else:
                    error_key = 'bookmarkTags'
                    continue

                if 'version' in info and isinstance(info['version'], int) and info['version'] >= 0:
                    self.manual_artwork_info['version'] = info['version']
                else:
                    error_key = 'version'
                    continue

                if 'existence' in info and isinstance(info['existence'], bool):
                    self.manual_artwork_info['existence'] = info['existence']
                else:
                    error_key = 'existence'
                    continue

                break

            else:
                autoRetry(self.bot.send_message)(chat_id=message.chat.id, parse_mode='HTML',
                    text=f"❗<code>{error_key}</code> 有误或缺失，作品输入失败。")
                self.manual_artwork_info = None
                return
            
            self.manual_artwork_info['pages'] = []
            autoRetry(self.bot.send_message)(chat_id = message.chat.id, 
                reply_markup = finishProcessingPagesMarkup(), parse_mode = 'HTML',
                text = f"<code>pages</code> 点击「➡️ 继续」后上传作品原图p0：")
        
        def processPages(message: Message):
            artwork_id = self.manual_artwork_info['id']
            part_no = len(self.manual_artwork_info['pages'])
            version = self.manual_artwork_info['version']

            try:
                if self.manual_artwork_info['illustType'] == 2:
                    file_stem = f"{artwork_id}_v{version}"
                else: file_stem = f"{artwork_id}_p{part_no}_v{version}"
                self.manual_artwork_info['pages'].append(
                    self.Teleg.downloadFile(message, self.SAVE_PATH, file_stem)
                )
            except Exception as e:
                self.logger.error(f"【手动输入作品】原图下载失败：{artwork_id}_p{part_no}")
                autoRetry(self.bot.send_message)(message.chat.id, "❗原图下载失败，此次输入取消。")
                self.manual_artwork_info = None
                raise e
            
            next_part_no = len(self.manual_artwork_info['pages'])
            autoRetry(self.bot.send_message)(chat_id = message.chat.id, 
                reply_markup = finishProcessingPagesMarkup(), parse_mode = 'HTML',
                text = f"<code>pages</code> 点击「➡️ 继续」后上传作品原图p{next_part_no}：")
        
        if self.manual_artwork_info is not None:
            autoRetry(self.bot.send_message)(message.chat.id, "当前有其他手动任务，请稍后再试。")
            return
        else: self.manual_artwork_info = dict()
        autoRetry(self.bot.send_message)(
            chat_id=message.chat.id, parse_mode='Markdown',
            text="请按 Toml 格式输入下面各项元数据（任何一项都不能省略）：" +\
            "\n```\nid: str = $pixiv_artwork_id\nillustType: int = 0:插画 | 1:漫画 | 2:动图" +\
            "\ntitle: str\ntags: list[str]\ncreateDate: str = %Y-%m-%dT%H:%M:%S+09:00" +\
            "\nupdateDate: str = %Y-%m-%dT%H:%M:%S+09:00\nauthorScreenName: str\nauthorUserId: str" +\
            "\nbookmarkTags: list[str]\nversion: int = $自定义的整数\nexistence: bool```" +\
            "\n输入任意非 Toml 文本可取消本次输入。",
        )
        self.bot.register_next_step_handler(message, processMeta)
    

    def manuallyModifyArtwork(self, message: Message):
        '''手动修改作品。
        #TODO: 增加 `/cancel` 命令取消任务的功能。
        ```
        {
            "id": <pixiv_artwork_id: str>, #NOTE: Required
            "illustType": <: int>,
            "title": <: str>,
            "tags": <: list[str]>,
            "createDate": <: str>,
            "updateDate": <: str>,
            "authorScreenName": <author_screen_name: str>,
            "authorUserId": <author_user_id: str>,
            "bookmarkTags": <: list[str]>,
            "version": <: int>,
            "pages": <["<page_0_file_name>", "<page_1_file_name>", ...]>,
            "existence": <: bool>
        }
        ```'''
        def finishProcessingPagesMarkup():
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("✅ 完成", callback_data="completeModification"))
            markup.add(types.InlineKeyboardButton("❌ 取消", callback_data="cancelModification"))
            markup.add(types.InlineKeyboardButton("➡️ 继续", callback_data="goOnModification"))
            return markup
        
        @self.bot.callback_query_handler(func=lambda call: call.data == "completeModification")
        def complete(call: CallbackQuery):
            status = self.Syncher.manuallyModifyArtwork(self.manual_artwork_info)
            autoRetry(self.bot.edit_message_text)(
                "✅ 已成功手动修改作品。", call.message.chat.id, call.message.id)
            self.manual_artwork_info = None
        
        @self.bot.callback_query_handler(func=lambda call: call.data == "cancelModification")
        def cancel(call: CallbackQuery):
            self.manual_artwork_info = None
            autoRetry(self.bot.edit_message_text)(
                "❌ 已取消此次元数据修改。", call.message.chat.id, call.message.id)
        
        @self.bot.callback_query_handler(func=lambda call: call.data == "goOnModification")
        def goOn(call: CallbackQuery):
            part_no_str = re.search(r'p(\d+)', call.message.text)
            part_no = part_no_str.group(1)
            autoRetry(self.bot.edit_message_text)(f"➡️ 继续上传作品原图p{part_no}：", 
                call.message.chat.id, call.message.id)
            self.bot.register_next_step_handler(message, processPages)
        
        def processMeta(message: Message):
            try: info = dict(tomlkit.loads(message.text))
            except Exception as e:
                self.logger.warning(f"【手动修改作品】元数据无法解析为 Toml 格式：\n{message.text}\n报错：{e}")
                autoRetry(self.bot.send_message)(chat_id=message.chat.id, parse_mode='HTML',
                    text=f"❗元数据不是 Toml 格式，作品修改取消。")
                self.manual_artwork_info = None
                return

            if 'id' not in info:
                autoRetry(self.bot.send_message)(chat_id=message.chat.id, parse_mode='HTML',
                    text=f"❗<code>id</code> 缺失，作品修改失败。")
                self.manual_artwork_info = None
                return

            error_key = None
            for key in info.keys():
                match key:
                    case 'id':
                        if isinstance(info['id'], str):
                            if self.Syncher.isArtworkRecorded(info['id']):
                                self.manual_artwork_info['id'] = info['id']
                            else:
                                autoRetry(self.bot.send_message)(
                                    message.chat.id, "❌ 该作品未记录，无法修改。")
                                error_key = 'id'
                        else: error_key = 'id'
                    case 'illustType':
                        if isinstance(info['illustType'],int) and 0 <= info['illustType'] <= 2:
                            self.manual_artwork_info['illustType'] = info['illustType']
                        else: error_key = 'illustType'
                    case 'title':
                        if isinstance(info['title'], str):
                            self.manual_artwork_info['title'] = info['title']
                        else: error_key = 'title'
                    case 'tags':
                        if (isinstance(info['tags'],list) 
                        and all(isinstance(tag,str) for tag in info['tags'])):
                            self.manual_artwork_info['tags'] = info['tags']
                        else: error_key = 'tags'
                    case 'createDate':
                        try:
                            datetime.strptime(info['createDate'], "%Y-%m-%dT%H:%M:%S+09:00")
                            self.manual_artwork_info['createDate'] = info['createDate']
                        except ValueError: error_key = 'createDate'
                    case 'updateDate':
                        try:
                            datetime.strptime(info['updateDate'], "%Y-%m-%dT%H:%M:%S+09:00")
                            self.manual_artwork_info['updateDate'] = info['updateDate']
                        except ValueError: error_key = 'updateDate'
                    case 'authorScreenName':
                        if isinstance(info['authorScreenName'], str):
                            self.manual_artwork_info['authorScreenName'] = info['authorScreenName']
                        else: error_key = 'authorScreenName'
                    case 'authorUserId':
                        if isinstance(info['authorUserId'], str):
                            self.manual_artwork_info['authorUserId'] = info['authorUserId']
                        else: error_key = 'authorUserId'
                    case 'bookmarkTags':
                        if (isinstance(info['bookmarkTags'], list) 
                        and all(isinstance(tag, str) for tag in info['bookmarkTags'])):
                            self.manual_artwork_info['bookmarkTags'] = info['bookmarkTags']
                        else: error_key = 'bookmarkTags'
                    case 'version':
                        if isinstance(info['version'], int) and info['version'] >= 0:
                            self.manual_artwork_info['version'] = info['version']
                        else: error_key = 'version'
                    case 'existence':
                        if isinstance(info['existence'], bool):
                            self.manual_artwork_info['existence'] = info['existence']
                        else: error_key = 'existence'
                    case _: pass
                
                if error_key is not None:
                    autoRetry(self.bot.send_message)(chat_id=message.chat.id, parse_mode='HTML',
                        text=f"❗<code>{error_key}</code> 有误，作品修改失败。")
                    self.manual_artwork_info = None
                    return
            
            autoRetry(self.bot.send_message)(chat_id = message.chat.id, 
                reply_markup = finishProcessingPagesMarkup(), parse_mode = 'HTML',
                text = f"<code>pages</code> 点击「➡️ 继续」后上传作品原图p0：")
        
        def processPages(message: Message):
            artwork_id = self.manual_artwork_info['id']
            if 'pages' not in self.manual_artwork_info: self.manual_artwork_info['pages'] = []
            part_no = len(self.manual_artwork_info['pages'])
            if 'version' not in self.manual_artwork_info:
                autoRetry(self.bot.send_message)(chat_id = message.chat.id, parse_mode = 'HTML',
                    text = "❗<code>version</code> 缺失，无法上传原图，作品修改失败。")
                self.manual_artwork_info = None
                return
            else: version = self.manual_artwork_info['version']
            if 'illustType' not in self.manual_artwork_info:
                autoRetry(self.bot.send_message)(chat_id = message.chat.id, parse_mode = 'HTML',
                    text = "❗<code>illustType</code> 缺失，无法上传原图，作品修改失败。")
                self.manual_artwork_info = None
                return
            else: is_gif = (self.manual_artwork_info['illustType'] == 2)

            try:
                self.manual_artwork_info['pages'].append(
                    self.Teleg.downloadFile(message, self.SAVE_PATH,
                    f"{artwork_id}_v{version}" if is_gif else f"{artwork_id}_p{part_no}_v{version}")
                )
            except Exception as e:
                self.logger.error(f"【手动修改作品】原图下载失败：{artwork_id}_p{part_no}")
                autoRetry(self.bot.send_message)(message.chat.id, "❗原图下载失败，此次修改取消。")
                self.manual_artwork_info = None
                raise e
            
            next_part_no = len(self.manual_artwork_info['pages'])
            autoRetry(self.bot.send_message)(chat_id = message.chat.id, 
                reply_markup = finishProcessingPagesMarkup(), parse_mode = 'HTML',
                text = f"<code>pages</code> 点击「➡️ 继续」后上传作品原图p{next_part_no}：")
        
        if self.manual_artwork_info is not None:
            autoRetry(self.bot.send_message)(message.chat.id, "当前有其他手动任务，请稍后再试。")
            return
        else: self.manual_artwork_info = dict()
        autoRetry(self.bot.send_message)(
            chat_id=message.chat.id, parse_mode='Markdown',
            text="请按 Toml 格式输入下面各项元数据" +\
            "（除 `id` 不能省略外，其他都可以省略；如果要上传新的原图，`illustType` 和 `version` 也不能省略）：" +\
            "\n```\nid: int = $pixiv_artwork_id\nillustType: int = 0:插画 | 1:漫画 | 2:动图" +\
            "\ntitle: str\ntags: list[str]\ncreateDate: str = %Y-%m-%dT%H:%M:%S+09:00" +\
            "\nupdateDate: str = %Y-%m-%dT%H:%M:%S+09:00\nauthorScreenName: str\nauthorUserId: int" +\
            "\nbookmarkTags: list[str]\nversion: int = $自定义的整数\nexistence: bool```" +\
            "\n输入任意非 Toml 文本可取消本次修改。",
        )
        self.bot.register_next_step_handler(message, processMeta)







