import os
import re
import ast
import time
import shutil
import requests
import pandas as pd

from telebot import TeleBot
from telebot.types import Message
from threading import Event
from PIL import Image, ImageFile
from html import escape

from .utils import auto_retry

# 允许打开损坏的图像
ImageFile.LOAD_TRUNCATED_IMAGES = True

# 消息报错
class MessageNotFoundError(Exception):
    '''找不到指定消息'''
class MessageSendingError(Exception):
    '''消息发送失败'''
class RetryError(Exception):
    '''超过最大重试次数'''



class TelegramUploader:
    def __init__(
            self,
            bot: TeleBot,
            pixiv_user_id: str | int,
            channel_id: str | int,
            group_id: str | int,
            dustbin_id: str | int,
            channel_catalog_msg_id: str | int,
            metadata_filepath: str,
            save_path: str,
            headers: dict,
            proxies: dict,
        ):
        self.bot = bot

        self.PIXIV_USER_ID = pixiv_user_id
        self.DUSTBIN_ID = dustbin_id
        self.CHANNEL_ID = str(channel_id)
        self.GROUP_ID = group_id
        self.CHANNEL_CATALOG_MSG_ID = channel_catalog_msg_id
        self.METADATA_FILEPATH = metadata_filepath
        self.SAVE_PATH = save_path
        self.HEADERS = headers
        self.PROXIES = proxies

        self.ILLUST_TYPE_DICT = {0:'插画', 1:'漫画', 2:'动图', 3:'小说'}
        self.VALUES_TO_UPDATE = ['title','tags','userName','bookmarkTags']
        self.MAX_COVER_PHOTO_SHAPE = 2000            # 2000×2000px
        self.MAX_COVER_FILE_SIZE = 5 * 1024 * 1024  # 5MB
        self.MAX_DOCUMENT_SIZE = 50 * 1024 * 1024    # 50MB

    
    def upload_artworks(
            self,
            start_artwork: int | None,
            end_artwork: int | None,
            gap_time = 5,
            max_tries = 5,
        ):
        # 元数据
        meta_df = pd.read_csv(self.METADATA_FILEPATH)
        meta_df.index = meta_df['likeOrder']
        # 初始化与TG有关的列
        tg_columns = ['channelMessageId', 'groupMessageId', 'documentGroupMessageIdList', 'existence']
        for col in tg_columns:
            if col not in meta_df.columns:
                meta_df[col] = None
        # 填充空值
        meta_df[['channelMessageId', 'groupMessageId']] = \
            meta_df[['channelMessageId', 'groupMessageId']].fillna(-1).astype(int)
        meta_df['documentGroupMessageIdList'] = meta_df['documentGroupMessageIdList'].fillna('[]')
        meta_df['existence'] = meta_df['existence'].fillna(True)
        
        if start_artwork is None:
            start_artwork = 1

        # 获取频道目录消息的message_id，如果不存在则创建一个
        if self.CHANNEL_CATALOG_MSG_ID is None:
            raise MessageNotFoundError(
                f'找不到频道目录，可能目录消息未创建或被删除，消息ID为 {self.CHANNEL_CATALOG_MSG_ID}。')

        for like_order, row in meta_df.iterrows():
            if like_order < start_artwork:
                continue
            if end_artwork is not None and like_order > end_artwork:
                break
            pages = ast.literal_eval(row['pages'])
            if not pages:
                continue

            # 先在频道发一张封面
            file_path = os.path.join(self.SAVE_PATH, pages[0])
            # 压缩封面图
            file_path = self.resize_picture(
                input_path=file_path,
                output_path=None,
                to_filesize=self.MAX_COVER_FILE_SIZE,
                to_photoshape=self.MAX_COVER_PHOTO_SHAPE,
            )
            # 发送测试消息，获取讨论群在发送封面前的最新message_id
            group_msg_before_cover = auto_retry(self.bot.send_message, (self.GROUP_ID, '.'))
            auto_retry(self.bot.delete_message, (self.GROUP_ID, group_msg_before_cover.id))
            # 发送封面和元数据
            with open(file_path, 'rb') as pic:
                caption = self.gen_artwork_caption(**row.to_dict())
                # 发送封面，此后报错将需要立刻删除封面
                chan_msg = auto_retry(
                    self.bot.send_photo,
                    (self.CHANNEL_ID, pic, caption),
                    dict(parse_mode='HTML'),
                )
                meta_df.at[like_order, 'channelMessageId'] = chan_msg.id
            
            try:
                # 在讨论区发该作品的所有图片
                # 先找出与频道消息对应的讨论组消息，最多尝试max_tries次寻找消息
                for _ in range(max_tries):
                    time.sleep(gap_time)
                    for id in range(group_msg_before_cover.id+1, group_msg_before_cover.id+5):
                        try:
                            test_msg = self.get_message_content(self.GROUP_ID, id)
                            if (str(test_msg.forward_from_chat.id) == str(self.CHANNEL_ID) 
                                and test_msg.forward_from_message_id == chan_msg.id):
                                group_msg_id = id
                                break
                        except:
                            pass
                    else:
                        continue
                    break
                else:
                    raise MessageNotFoundError(f"频道消息id为 {chan_msg.id}，无法找到群组中的对应消息。")
                meta_df.at[like_order, 'groupMessageId'] = group_msg_id
                
                # 发送该作品的图片文件
                meta_df.at[like_order, 'documentGroupMessageIdList'] = []
                for page in pages:
                    file_path = os.path.join(self.SAVE_PATH, page)
                    # 如果文件过大，需要分卷压缩再上传
                    if os.stat(file_path).st_size >= self.MAX_DOCUMENT_SIZE:
                        zip_folder = f'./{page}.zip'
                        os.mkdir(zip_folder)
                        os.system(f'zip -r -s 32M {zip_folder}/{page}.zip {file_path}')
                        for filename in os.listdir(zip_folder):
                            file_path = os.path.join(zip_folder, filename)
                            with open(file_path, 'rb') as file:
                                page_msg = auto_retry(
                                    self.bot.send_document, 
                                    (self.GROUP_ID, file, group_msg_id),
                                )
                                time.sleep(gap_time / 2)
                        shutil.rmtree(zip_folder)
                    else:
                        with open(file_path, 'rb') as pic:
                            page_msg = auto_retry(self.bot.send_document, (self.GROUP_ID, pic, group_msg_id))
                            time.sleep(gap_time / 2)
                    # 记录图片文件在讨论群中的message_id
                    meta_df['documentGroupMessageIdList'][like_order].append(page_msg.id)
                
                # 更新频道置顶目录
                if (like_order - 1) % 20 == 0:
                    self.append_text_to_message(
                        f'\n「<a href="https://t.me/c/{self.CHANNEL_ID[4:]}/{chan_msg.id}">{like_order:06d}</a>」',
                        self.CHANNEL_ID, 
                        self.CHANNEL_CATALOG_MSG_ID, 
                        parse_mode='HTML',
                    )
                    time.sleep(gap_time / 2)
            
            except Exception as e:
                # 删除封面
                auto_retry(self.bot.delete_message, (self.CHANNEL_ID, chan_msg.id))
                # 清空此作品消息相关的元数据
                meta_df.loc[like_order, ['channelMessageId', 'groupMessageId']] = -1
                meta_df.at[like_order, 'documentGroupMessageIdList'] = '[]'
                meta_df.at[like_order, 'existence'] = True
                # 保存现有元数据
                meta_df.to_csv(self.METADATA_FILEPATH, index=False)
                # 重新报错
                raise e
        
        # 保存元数据、取消群组的所有置顶
        meta_df.to_csv(self.METADATA_FILEPATH, index=False)
        auto_retry(self.bot.unpin_all_chat_messages, (self.GROUP_ID,))
    

    def update_metadata_and_related_message(
            self,
            stop_event: Event,
            num_collections: int,
            feedback_chat_ids: list[int|str] = [],
            offset_range: tuple[int, int] = None,
            pace = 50,
            gap_time = 1,
            timeout = 30,
        ):
        '''
        更新收藏元数据，不支持更新收藏序号。被删除的作品将打上“#被删除”标签。
        
        :param offset_range: 在原Pixiv收藏夹中，最新收藏的作品的offset为0。
        :return: True: 已完成; False: 被中止。
        :rtype: `bool`
        '''
        meta_df = pd.read_csv(self.METADATA_FILEPATH)
        meta_df.index = meta_df['id'].astype(int)

        start_offset = 0
        end_offset = num_collections
        if offset_range is not None:
            if offset_range[0] is not None and offset_range[0] > 0:
                start_offset = offset_range[0]
            if offset_range[1] is not None and offset_range[1] < num_collections:
                end_offset = offset_range[1]

        feedback_msg_list = []
        feedback_text = f'正在更新元数据……\n本次更新作品数量：{end_offset - start_offset}'
        for chat_id in feedback_chat_ids:
            feedback_msg_list.append(auto_retry(
                self.bot.send_message,
                (chat_id, feedback_text + f'\n起始更新收藏序号：NaN\n当前更新收藏序号：NaN\n进度：0%'),
            ))
        
        # 更新作品信息
        if_completed, cover_range, exist_statuses, meta_df, feedback_text = self.update_artwork_infos(
            meta_df=meta_df, num_collections=num_collections,
            start_offset=start_offset, end_offset=end_offset, pace=pace,
            feedback_msg_list=feedback_msg_list, feedback_text=feedback_text,
            stop_event=stop_event, timeout=timeout, gap_time=gap_time,
        )
        if not if_completed:
            return False
        
        # 更新存活状态
        if_completed = self.update_artwork_existence_status(
            meta_df=meta_df, cover_range=cover_range, exist_statuses=exist_statuses,
            feedback_msg_list=feedback_msg_list, feedback_text=feedback_text,
            stop_event=stop_event, gap_time=gap_time,
        )
        return if_completed
    

    def update_artwork_existence_status(
            self,
            meta_df: pd.DataFrame,
            cover_range: tuple[int, int],
            exist_statuses: list[bool],
            feedback_msg_list: list[Message],
            feedback_text: str,
            stop_event: Event,
            gap_time: float,
        ):
        '''
        更新作品的存活状态。
        '''
        # 统计需要更新存活状态的作品
        if 'existence' in meta_df.columns:
            old_exist_statuses = list(meta_df['existence'])
            different_indices = [i for i in range(*cover_range) if exist_statuses[i] != old_exist_statuses[i]]
        else:
            different_indices = [i for i in range(*cover_range) if not exist_statuses[i]]
            meta_df['existence'] = True
        
        for msg in feedback_msg_list:
            auto_retry(
                self.bot.edit_message_text,
                (feedback_text + f"\n{len(different_indices)} 个作品存活状态改变。", msg.chat.id, msg.id),
                dict(parse_mode='HTML'),
            )
        if not different_indices:
            return True
        
        status_feedback = ''
        feedback_text += f"\n{len(different_indices)} 个作品存活状态改变……"
        for i in different_indices:
            # 中止信号处理
            if stop_event and stop_event.is_set():
                meta_df.to_csv(self.METADATA_FILEPATH, index=False)
                return False
            
            # 更新元数据
            meta_df.at[meta_df.iloc[i]['id'], 'existence'] = exist_statuses[i]
            row = meta_df.iloc[i]
            
            # 反馈消息
            status_feedback += f"\n<code>{row['id']}</code> {'存活' if exist_statuses[i] else '被删除'}"
            for msg in feedback_msg_list:
                auto_retry(
                    self.bot.edit_message_text,
                    (feedback_text + f"\n正在更新：{status_feedback}", 
                    msg.chat.id, msg.id),
                    dict(parse_mode='HTML'),
                )
            
            # 跳过未记录的被删除的作品
            if row['channelMessageId'] < 0:
                continue

            # 更新频道消息中的存活状态
            caption = self.gen_artwork_caption(**row.to_dict())
            try:
                auto_retry(
                    self.bot.edit_message_caption, 
                    (caption, self.CHANNEL_ID, row['channelMessageId']),
                    dict(parse_mode='HTML'),
                )
            except Exception as e:
                raise MessageSendingError(
                    f"更新作品频道消息时出现错误，消息id为 ({row['channelMessageId']}): \n{e}\n")
            time.sleep(gap_time)
        
        for msg in feedback_msg_list:
            auto_retry(
                self.bot.edit_message_text,
                (feedback_text + f"\n更新完成：{status_feedback}", 
                msg.chat.id, msg.id),
                dict(parse_mode='HTML'),
            )
        meta_df.to_csv(self.METADATA_FILEPATH, index=False)
        return True
    

    def update_artwork_infos(
            self,
            meta_df: pd.DataFrame,
            num_collections: int,
            start_offset: int,
            end_offset: int,
            pace: int,
            feedback_msg_list: list[Message],
            feedback_text: str,
            stop_event: Event,
            timeout: int,
            gap_time: float,
        ):
        '''
        更新作品信息以及关联的频道消息。

        :return: 是否顺利完成。
        :rtype: `bool`
        :return: 更新范围。
        :rtype: `cover_range: tuple[int, int]`
        :return: 存活状态。
        :rtype: `exist_statuses: list[bool]`
        :return: 元数据
        :rtype: `meta_df: DataFrame`
        :return: 反馈消息
        :rtype: `feedback_text: str`
        '''
        exist_statuses = [False for _ in range(len(meta_df))]  # 用来确认作品是否被删除
        num_updates = end_offset - start_offset
        progress = 0
        first_checked_artwork_id = None
        last_checked_artwork_id = None
        id = None
        like_order = 'NaN'

        for offset in range(start_offset, end_offset, pace):
            resp = auto_retry(
                requests.get,
                (f"https://www.pixiv.net/ajax/user/{self.PIXIV_USER_ID}/illusts/" + \
                    f"bookmarks?tag=&offset={offset}&limit={pace}&rest=show",), 
                dict(headers=self.HEADERS, timeout=timeout, proxies=self.PROXIES),
            ).json()
            datas = resp["body"]["works"]
            bookmark_tags: dict = resp["body"].get("bookmarkTags", dict())

            for data in datas:
                # 中止信号处理
                if stop_event and stop_event.is_set():
                    meta_df.to_csv(self.METADATA_FILEPATH, index=False)
                    return False, None, None, None, None

                id = int(data['id'])

                # 跳过被删除的作品，如果 userId == 0，则为被删除的作品
                if id in meta_df.index:
                    if like_order == 'NaN':
                        like_order = meta_df['likeOrder'][id]
                        feedback_text += f"\n起始更新收藏序号：{like_order}"
                    else:
                        like_order = meta_df['likeOrder'][id]
                    
                    if int(data['userId']) > 0:
                        # 得知该作品未被删除
                        exist_statuses[list(meta_df.index).index(id)] = True
                        if first_checked_artwork_id is None:
                            first_checked_artwork_id = id
                        last_checked_artwork_id = id
                        # 整理作品信息
                        item_bookmark_data = data["bookmarkData"] if data["bookmarkData"] is not None else dict()
                        item_bookmark_tags = bookmark_tags.get(item_bookmark_data.get("id", 'NotFound'), [])
                        info = {
                            "illustType": data["illustType"],
                            "pageCount": data["pageCount"],
                            "title": data["title"],
                            "tags": data["tags"],
                            "userName": data["userName"],
                            "bookmarkTags": item_bookmark_tags,
                        }
                        # 对比更新元数据
                        is_different = False
                        for col in self.VALUES_TO_UPDATE:
                            if str(meta_df[col][id]) != str(info[col]):
                                is_different = True
                                meta_df.at[id, col] = info[col]
                        # 更新频道消息
                        if is_different:
                            row = meta_df.loc[id]
                            caption = self.gen_artwork_caption(**row.to_dict())
                            try:
                                auto_retry(
                                    self.bot.edit_message_caption,
                                    (caption, self.CHANNEL_ID, row['channelMessageId']),
                                    dict(parse_mode='HTML'),
                                )
                            except Exception as e:
                                raise MessageSendingError(
                                    f"更新作品频道消息时出现错误，消息id为 ({row['channelMessageId']}): \n{e}\n")
                            time.sleep(gap_time)
                
                progress += 1
                if progress >= num_updates:
                    feedback_text += f"\n完成更新收藏序号：{like_order}"
                    break
            
            else:
                time.sleep(gap_time)
            
            for msg in feedback_msg_list:
                progress_info = f"\n起始更新收藏序号：NaN" if like_order == 'NaN' else ''
                progress_info += f"\n当前更新收藏序号：{like_order}" +\
                    f"\n进度：{100*progress/num_updates:.2f}%"
                auto_retry(
                    self.bot.edit_message_text,
                    (feedback_text + progress_info if progress < num_updates else feedback_text,
                        msg.chat.id, msg.id,),
                    dict(parse_mode='HTML'),
                )
            meta_df.to_csv(self.METADATA_FILEPATH, index=False)

        # 计算此次更新覆盖的本地收藏范围
        if first_checked_artwork_id is not None and last_checked_artwork_id is not None:
            start = list(meta_df.index).index(last_checked_artwork_id) if end_offset != num_collections else 0
            end = list(meta_df.index).index(first_checked_artwork_id) if start_offset != 0 else len(meta_df)
            cover_range = (start, end)
        else:
            cover_range = (int(-1), int(-1))
        
        return True, cover_range, exist_statuses, meta_df, feedback_text
    

    def set_channel_catalog_message(self):
        if self.CHANNEL_CATALOG_MSG_ID is None:
            msg = auto_retry(
                self.bot.send_message,
                (self.CHANNEL_ID, '<b>收藏序号目录</b>'),
                dict(parse_mode='HTML'),
            )
            auto_retry(
                self.bot.pin_chat_message,
                (self.CHANNEL_ID, msg.id),
            )
            self.CHANNEL_CATALOG_MSG_ID = msg.id
            return msg.id
        else:
            return self.CHANNEL_CATALOG_MSG_ID
    

    def gen_artwork_caption(
            self,
            id: int, likeOrder: int, title: str, illustType: int,
            userName: str, userId: int,
            bookmarkTags: list[str], tags: list[str],
            pageCount: int, referer: str, createDate: str,
            existence: bool,
            *args, **kwargs,
        ):
        if isinstance(bookmarkTags, str):
            bookmarkTags = ast.literal_eval(bookmarkTags)
        if isinstance(tags, str):
            tags = ast.literal_eval(tags)
        caption = \
            f"收藏标签：{escape('#'+' #'.join(bookmarkTags))}\n" +\
            f"收藏序号：{likeOrder}{' (#被删除)' if not existence else ''}\n\n" +\
            f"标题：{escape(str(title))}\n" +\
            f"作者：{escape(str(userName))}    作者ID：<code>{userId}</code>\n\n" +\
            f"作品ID：<code>{id}</code>    页数：{pageCount}\n" +\
            f"图片链接：<a href=\"{referer}\">pixiv</a>    " +\
            f"作品类型：{self.ILLUST_TYPE_DICT[illustType]}\n" +\
            f"发布时间：{createDate}\n\n" +\
            f"标签：{escape('#'+' #'.join(tags))}"
        return caption

    
    def resize_picture(
            self, 
            input_path: str, 
            output_path: str, 
            to_filesize: int, 
            to_photoshape: int,
        ):
        '''调整图片大小和图片文件大小。'''
        # 确定压缩的比率
        rate = 1
        origin_filesize = os.stat(input_path).st_size
        if origin_filesize > to_filesize:
            rate = (to_filesize / origin_filesize) ** 0.5
        img = Image.open(input_path)
        max_dim = max(img.size[0], img.size[1])
        if max_dim > to_photoshape:
            rate = min(rate, to_photoshape / max_dim)
        
        # 开始压缩
        default_output_folder = './temp'
        if rate != 1:
            new_img = img.resize((int(img.size[0]*rate), int(img.size[1]*rate)))
            file_type = input_path.split('.')[-1]
            if not output_path:
                if not os.path.exists(default_output_folder):
                    os.mkdir(default_output_folder)
                output_path = f'{default_output_folder}/temp.{file_type}'
            new_img.save(output_path)
            return output_path
        else:
            return input_path
    
    
    def append_text_to_message(
            self, 
            text: str, 
            chat_id: str | int, 
            message_id: str | int, 
            parse_mode='HTML',
        ):
        '''编辑消息：将文本补充到消息末尾。'''
        msg = auto_retry(self.bot.forward_message, (self.DUSTBIN_ID, chat_id, message_id))
        auto_retry(
            self.bot.edit_message_text, 
            (msg.html_text + text, chat_id, message_id), 
            dict(parse_mode=parse_mode),
        )
        auto_retry(self.bot.delete_message, (self.DUSTBIN_ID, msg.id))
    
    
    def get_message_content(
            self, 
            chat_id: str | int, 
            message_id: str | int,
            max_tries = 5,
        ):
        msg = auto_retry(
            self.bot.forward_message, 
            (self.DUSTBIN_ID, chat_id, message_id),
            max_tries=max_tries,
        )
        auto_retry(
            self.bot.delete_message, 
            (self.DUSTBIN_ID, msg.id),
            max_tries=max_tries,
        )
        return msg

    
    def get_last_uploaded_artwork(self):
        '''寻找并返回最后上传的图片的收藏序号。'''
        # 发送一个测试消息，测试当前最大的message_id是多少
        test_msg = auto_retry(self.bot.send_message, (self.CHANNEL_ID, '.'))
        auto_retry(self.bot.delete_message, (self.CHANNEL_ID, test_msg.id))
        # 开始从后往前顺序查找最后上传的图片
        for id in range(test_msg.id-1, 0, -1):
            try:
                msg = self.get_message_content(self.CHANNEL_ID, id)
                mtch = re.findall(r'收藏序号：(\d+)', msg.caption)
                if mtch:
                    return int(mtch[0])
            except:
                pass
        return None

