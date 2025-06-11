import time
import pytz
import schedule
import threading

from telebot import TeleBot

from .utils import auto_retry, NowTimer
from .PixivDownloader import PixivDownloader
from .TelegramUploader import TelegramUploader



class Tasks:
    def __init__(
            self,
            bot: TeleBot,
            allowed_telegram_users: list[str | int],
            pixiv_user_id: str | int,
            archive_chat_ids: dict,
            channel_catalog_msg_id: str | int | None,
            artwork_save_path: str,
            metadata_filepath: str,
            headers: dict,
            proxies: dict,
            timezone = "Asia/Shanghai",
            dt_format = None,
        ):
        self.bot = bot
        self.now_timer = NowTimer(dt_format, timezone)

        self.ALLOWED_TELEGRAM_USERS = allowed_telegram_users
        self.PIXIV_USER_ID = pixiv_user_id
        self.DUSTBIN_ID = archive_chat_ids['dustbin']
        self.CHANNEL_ID = archive_chat_ids['channel']
        self.GROUP_ID = archive_chat_ids['group']
        self.CHANNEL_CATALOG_MSG_ID = channel_catalog_msg_id
        self.ARTWORK_SAVE_PATH = artwork_save_path
        self.METADATA_FILEPATH = metadata_filepath
        self.HEADERS = headers
        self.PROXIES = proxies
        self.TIMEZONE = timezone

        self.PixivDownloader = PixivDownloader(
            pixiv_user_id=pixiv_user_id,
            save_path=artwork_save_path,
            metadata_filepath=metadata_filepath,
            headers=headers,
            proxies=proxies,
        )
        self.TelegramUploader = TelegramUploader(
            bot=bot,
            pixiv_user_id=pixiv_user_id,
            channel_id=self.CHANNEL_ID,
            group_id=self.GROUP_ID,
            dustbin_id=self.DUSTBIN_ID,
            channel_catalog_msg_id=channel_catalog_msg_id,
            metadata_filepath=metadata_filepath,
            save_path=artwork_save_path,
            headers=headers,
            proxies=proxies,
        )
        
        # 防止两个同步任务同时进行、防止更新任务和同步任务同时进行
        self.is_synchronizing = False
        # 防止两个更新任务同时进行
        self.is_updating_metadata = False
        # 任务事件终止标志
        self.event_stop_triggered_metadata_updating = threading.Event()
        self.event_stop_scheduled_metadata_updating = threading.Event()
    

    def setup(self):
        '''
        初始化，必须运行。

        :return: 频道目录的消息ID
        :rtype: `int | str`
        '''
        # 定时任务
        schedule.every().monday.at("13:30", pytz.timezone(self.TIMEZONE)).do(
            self.update_metadata_on_schedule, self.ALLOWED_TELEGRAM_USERS)
        self.thread_of_update_metadata_on_schedule = threading.Thread(
            target=self.run_schedule,
            args=(self.event_stop_scheduled_metadata_updating,),
        )
        self.thread_of_update_metadata_on_schedule.start()
        # 触发任务
        self.thread_of_update_metadata_by_triggered = threading.Thread(
            target=self.update_metadata_by_triggered,
            args=(None, []),
        )
        # 设置频道目录，不存在则自动创建
        self.CHANNEL_CATALOG_MSG_ID = self.TelegramUploader.set_channel_catalog_message()
        # 返回频道目录
        return self.CHANNEL_CATALOG_MSG_ID


    def sync_pixiv_collections(
            self, 
            feed_back_msg,
        ):
        '''
        任务：同步pixiv收藏夹。
        '''
        if self.is_synchronizing:
            return '已取消，当前有其他同步任务。'
        self.is_synchronizing = True
        # 下载
        print(f'【{self.now_timer.now()}】【同步收藏夹】开始同步收藏夹，正在下载……')
        count, updating_feedback = self.PixivDownloader.download_collections(
            self.bot, 
            feed_back_msg=feed_back_msg,
        )
        print(f'【{self.now_timer.now()}】【同步收藏夹】下载完成，正在上传……')
        likeorder_last_uploaded = self.TelegramUploader.get_last_uploaded_artwork()
        if likeorder_last_uploaded is None:
            likeorder_last_uploaded = 0
        # 上传
        updating_feedback += "\n正在上传……"
        auto_retry(
            self.bot.edit_message_text, 
            (updating_feedback, feed_back_msg.chat.id, feed_back_msg.id),
        )
        self.TelegramUploader.upload_artworks(
            start_artwork = likeorder_last_uploaded + 1,
            end_artwork = None,
        )
        print(f'【{self.now_timer.now()}】【同步收藏夹】上传完毕，同步完成。')
        # 结束
        self.is_synchronizing = False
        return f'{updating_feedback}\n同步完成。'
    
    
    def update_metadata_by_triggered(
            self,
            offset_range: tuple,
            feedback_chat_ids: list[str | int],
        ):
        '''
        任务：通过消息触发启动的元数据更新。
        '''
        if self.is_synchronizing:
            feedback_text = '已取消，当前有同步任务。'
            print(f'【{self.now_timer.now()}】【手动更新元数据】{feedback_text}')
            for chat_id in feedback_chat_ids:
                auto_retry(self.bot.send_message, (chat_id, feedback_text))
            return
        
        else:
            self.is_updating_metadata = True
            print(f'【{self.now_timer.now()}】【手动更新元数据】开始更新元数据……')

            try:
                if_completed = self.TelegramUploader.update_metadata_and_related_message(
                    stop_event=self.event_stop_triggered_metadata_updating,
                    num_collections=self.PixivDownloader.count_collection(),
                    feedback_chat_ids=feedback_chat_ids,
                    offset_range=offset_range,
                    pace=5,
                )
            except Exception as e:
                print(f'【{self.now_timer.now()}】【手动更新元数据】元数据更新失败！')
                self.is_updating_metadata = False
                raise e
            else:
                if if_completed:
                    print(f'【{self.now_timer.now()}】【手动更新元数据】元数据更新完成。')
                else:
                    print(f'【{self.now_timer.now()}】【手动更新元数据】元数据更新中止。')
            
            self.is_updating_metadata = False
    
    
    def update_metadata_on_schedule(
            self,
            feedback_chat_ids: list[str | int],
        ):
        '''
        任务：通过定期任务启动的元数据更新任务。
        '''
        if self.is_updating_metadata or self.is_synchronizing:
            feedback_text = ''
            if self.is_updating_metadata:
                feedback_text = '已取消，当前有其他元数据更新任务。'
            if self.is_synchronizing:
                feedback_text = '已取消，当前有同步任务。'
            print(f'【{self.now_timer.now()}】【手动更新元数据】{feedback_text}')
            for chat_id in feedback_chat_ids:
                auto_retry(self.bot.send_message, (chat_id, feedback_text))
            return
        
        else:
            self.is_updating_metadata = True
            print(f'【{self.now_timer.now()}】【定时更新元数据】开始更新元数据……')

            try:
                if_completed = self.TelegramUploader.update_metadata_and_related_message(
                    stop_event=self.event_stop_scheduled_metadata_updating,
                    num_collections=self.PixivDownloader.count_collection(),
                    feedback_chat_ids=feedback_chat_ids,
                    pace=50,
                )
            except Exception as e:
                print(f'【{self.now_timer.now()}】【定时更新元数据】元数据更新失败！')
                self.is_updating_metadata = False
                raise e
            else:
                if if_completed:
                    print(f'【{self.now_timer.now()}】【定时更新元数据】元数据更新完成。')
                else:
                    print(f'【{self.now_timer.now()}】【定时更新元数据】元数据更新中止。')
            
            self.is_updating_metadata = False


    def run_schedule(
            self,
            stop_event: threading.Event,
        ):
        while not stop_event.is_set():
            schedule.run_pending() # 检查是否到了任务的预定开始执行时期
            time.sleep(1)
    

    def stop_all_metadata_updating(self):
        '''停止所有元数据更新任务。'''
        # 设置停止事件
        self.event_stop_scheduled_metadata_updating.set()
        self.event_stop_triggered_metadata_updating.set()
        # 等待正在进行的更新任务结束
        if self.thread_of_update_metadata_on_schedule.is_alive():
            self.thread_of_update_metadata_on_schedule.join()
        if self.thread_of_update_metadata_by_triggered.is_alive():
            self.thread_of_update_metadata_by_triggered.join()
        # 清除停止事件
        self.event_stop_scheduled_metadata_updating.clear()
        self.event_stop_triggered_metadata_updating.clear()

    
    def start_triggered_metadata_updating(
            self,
            offset_range: tuple,
            feedback_chat_ids: list[str | int],
        ):
        '''启动手动元数据更新任务。'''
        # 停止所有元数据更新
        self.stop_all_metadata_updating()
        # 开始手动元数据更新
        self.thread_of_update_metadata_by_triggered = threading.Thread(
            target=self.update_metadata_by_triggered, 
            args=(offset_range, feedback_chat_ids),
        )
        self.thread_of_update_metadata_by_triggered.start()
        # 恢复定时元数据更新
        self.start_scheduled_metadata_updating()
    

    def start_scheduled_metadata_updating(self):
        '''启动定时元数据更新任务。'''
        self.thread_of_update_metadata_on_schedule = threading.Thread(
            target=self.run_schedule,
            args=(self.event_stop_scheduled_metadata_updating,),
        )
        self.thread_of_update_metadata_on_schedule.start()

