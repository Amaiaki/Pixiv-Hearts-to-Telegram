import os
import re
import time
import json
import logging
import pandas as pd

from html import escape
from threading import Event
from datetime import datetime
from telebot import TeleBot
from telebot.types import Message

from .utils import autoRetry, MessageSendingFailed
from .pixiv import PixivTools
from .telegram import TelegramTools



class Syncher:
    '''
    - 元数据的格式：
    ```
    {
        "<pixiv_artwork_id>": {
            "id": <pixiv_artwork_id: str>,
            "illustType": <: int>,
            "pageCount": <: int>,
            "title": <: str>,
            "tags": <: list[str]>,
            "createDate": <: str>,
            "updateDate": <: str>,
            "authorScreenName": <author_screen_name: str>,
            "authorUserId": <author_user_id: str>,
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
            proxies: dict,
        ):
        self.bot = bot

        self.DUSTBIN_ID = dustbin_id
        self.CHANNEL_ID = channel_id
        self.GROUP_ID = group_id
        self.CHANNEL_CATALOG_MSG_ID = channel_catalog_msg_id
        self.METADATA_FILE_PATH = metadata_file_path
        self.RECORDS_FILE_PATH = records_file_path
        self.ERR404_PHOTO_FILE_PATH = err404_cover_file_path
        self.SAVE_PATH = save_path
        self.TEMP_PATH = temp_path

        self.ILLUST_TYPE_DICT = {0:'插画', 1:'漫画', 2:'动图', 3:'小说'}

        self.Pixiv = PixivTools(
            pixiv_user_id=pixiv_user_id, 
            save_path=save_path,
            headers=headers, 
            proxies=proxies,
        )
        self.Teleg = TelegramTools(
            bot=bot, 
            dustbin_id=dustbin_id, 
            temp_path=self.TEMP_PATH, 
            local_api_server_url=local_api_server_url,
        )

        self.getChannelCatalogID()

        # 日志
        self.logger = logging.getLogger('Pixar2Tele')


    def autoSync(
            self,
            feedback_chat_ids: list[int|str], 
            stop_event: Event,
            start_offset: int, 
            end_offset: int, 
            pace: int,
            gap_time: float = 2.8, 
            max_tries: int = 5, 
            timeout: float = 30,
        ):
        #TODO: 增加收藏被主动移除的标记

        meta_dict, records_df = self.getMetaAndRecords()
        num_sync = end_offset - start_offset

        # bot反馈
        feedback_messages: list[Message] = []
        curr_feedback_text = feedback_text = f'正在同步收藏夹……\n本次同步作品数量：{num_sync}'
        for chat_id in feedback_chat_ids:
            feedback_messages.append(autoRetry(self.bot.send_message)(chat_id, feedback_text))
        
        progress = 0
        existence_dict = dict()
        
        for offset in range(end_offset-pace, start_offset-pace, -pace):
            artwork_infos = self.Pixiv.getCollectionInfos(
                tag='', offset=max(0,offset), limit=min(pace,pace+offset), 
                rest='show', timeout=timeout)
            artwork_infos.reverse()
            # 获取起始作品的序号，用在起始反馈信息中
            if offset == start_offset:
                if artwork_infos[0]['id'] not in records_df.index:
                    if len(records_df) <= 0: first_artwork_syncno = 1
                    else: first_artwork_syncno = records_df.iloc[-1]['syncNo'] + 1
                else: first_artwork_syncno = records_df['syncNo'][artwork_infos[0]['id']]
                feedback_text += f'\n起始序号：{first_artwork_syncno}'
            
            try:
                for artwork in artwork_infos:
                    # 中止信号处理：保存元数据和同步记录
                    if stop_event.is_set():
                        self.saveMetaAndRecords(meta_dict, records_df)
                        return curr_feedback_text, feedback_messages

                    # 如果作品没有被同步过，需要下载和上传，会记录当前作品存活状态
                    if artwork['id'] not in records_df.index:
                        # 确定此作品的同步序号
                        syncno = records_df.iloc[-1]['syncNo'] + 1 if len(records_df) > 0 else 1
                        # 下载新作品，如果作品404，version=0，否则version=1
                        (   artwork['pages'], artwork['existence'], artwork['version'], 
                        ) = self.downloadNewArtwork(artwork, timeout)
                        # 上传，无论作品是否404，都发送消息，404的消息封面即为pixiv的404页面图片
                        (   artwork['channelMessageId'], artwork['groupMessageId'], 
                            artwork['groupDocumentMessageIds'],
                        ) = self.uploadNewArtwork(syncno=syncno, artwork_info=artwork, 
                            gap_time=gap_time, max_tries=max_tries,
                        )
                        # 记录作品元数据和同步记录
                        meta_dict[str(artwork['id'])] = artwork
                        records_df.loc[artwork['id']] = pd.Series({
                            'syncNo': int(syncno), 'id': str(artwork['id']),
                            'existence': bool(artwork['existence']),
                        })
                        # 更新频道置顶目录
                        if (syncno - 1) % 20 == 0:
                            self.updateChannelCatalog(syncno, artwork['channelMessageId'])
                    
                    # 如果作品被同步过，检查更新，不会更新存活状态
                    # BUG: 更新失败不能保存元数据
                    else:
                        update_status, old_artwork = self.checkUpdateStatus(artwork, meta_dict)
                        syncno = records_df.at[artwork['id'], 'syncNo']

                        match update_status:
                            case 'UpdateMeta' | 'Reupload':
                                # 更新元数据
                                updated_artwork = old_artwork
                                for key, val in artwork.items(): updated_artwork[key] = val
                                # 更新作品文件（如果需要）
                                if update_status == 'Reupload':
                                    (   updated_artwork['pages'], updated_artwork['version'],
                                    ) = self.downloadUpdatedArtwork(updated_artwork, timeout)
                                # 修改封面描述，并上传新文件（如果需要）
                                time.sleep(gap_time)
                                updated_artwork['groupDocumentMessageIds'] = self.updateArtworkMSG(
                                    syncno=syncno, artwork_info=updated_artwork, 
                                    need_reupload=(update_status=='Reupload'), 
                                    doc_uploading_gap_time=gap_time,
                                )
                                # 记录更新的作品元数据，不更新同步记录（即不更新存活状态）
                                meta_dict[str(updated_artwork['id'])] = updated_artwork
                            case 'NoUpdates': pass
                            case _: raise NotImplementedError(f'没有实现 {update_status} 的功能。')
                    
                    # 记录当前作品存活状态
                    existence_dict[artwork['id']] = (int(artwork['authorUserId']) > 0)
                    progress += 1
            
                time.sleep(gap_time)
            
            except Exception as e:
                self.saveMetaAndRecords(meta_dict, records_df)
                raise e
            
            # bot反馈
            for msg in feedback_messages:
                curr_feedback_text = feedback_text +\
                    f"\n当前序号：{records_df['syncNo'][artwork_infos[-1]['id']]}" +\
                    f"\n进度：{100 * progress / num_sync :.2f}%"
                autoRetry(self.bot.edit_message_text)(
                    curr_feedback_text, msg.chat.id, msg.id, parse_mode='HTML')
            # 保存元数据和同步记录
            self.saveMetaAndRecords(meta_dict, records_df)
        
        # 更新作品存活状态
        meta_dict, records_df, curr_feedback_text = self.updateExistences(
            feedback_text=curr_feedback_text, feedback_messages=feedback_messages, 
            checked_existence_dict=existence_dict, 
            meta_dict=meta_dict, records_df=records_df, gap_time=gap_time,
        )
        # 保存元数据和同步记录
        self.saveMetaAndRecords(meta_dict, records_df)
        # 返回反馈消息
        return curr_feedback_text, feedback_messages
    

    def manuallyInputArtwork(
            self,
            artwork_info: dict,
            gap_time: float = 2.8,
            max_tries: int = 5,
            **kwargs,
        ):
        '''
        手动输入作品。

        :param artwork_info: `dict[str, Any]` 除了标记为`Not Needed`的项，其他都不能省略:
        ```
        {
            "id": <pixiv_artwork_id: int>,
            "illustType": <: int>,
            "title": <: str>,
            "tags": <: list[str]>,
            "createDate": <: str>,
            "updateDate": <: str>,
            "authorScreenName": <author_screen_name: str>,
            "authorUserId": <author_user_id: int>,
            "bookmarkTags": <: list[str]>,
            "referer": "https://www.pixiv.net/artworks/<pixiv_artwork_id>", #NOTE: Not Needed
            "version": <: int>,
            "pages": <["<page_0_file_name>", "<page_1_file_name>", ...]>,
            "pageCount": <: int>, #NOTE: Not Needed
            "existence": <: bool>,
            "channelMessageId": <: int>, #NOTE: Not Needed
            "groupMessageId": <: int>, #NOTE: Not Needed
            "groupDocumentMessageIds": <: list[int]>, #NOTE: Not Needed
        }
        ```
        '''
        meta_dict, records_df = self.getMetaAndRecords()
        if artwork_info['id'] in records_df['id']: return False
        # 获取作品同步序号
        syncno = records_df.iloc[-1]['syncNo'] + 1 if len(records_df) > 0 else 1
        # 补充referer和pageCount
        artwork_info['referer'] = f"https://www.pixiv.net/artworks/{artwork_info['id']}"
        artwork_info['pageCount'] = len(artwork_info['pages'])
        # 上传，无论作品是否404，都发送消息，404的消息封面即为pixiv的404页面图片
        (   artwork_info['channelMessageId'], artwork_info['groupMessageId'], 
            artwork_info['groupDocumentMessageIds'],
        ) = self.uploadNewArtwork(syncno=syncno, artwork_info=artwork_info, 
            gap_time=gap_time, max_tries=max_tries,
        )
        # 记录作品元数据和同步记录
        meta_dict[str(artwork_info['id'])] = artwork_info
        records_df.loc[artwork_info['id']] = pd.Series({
            'syncNo': int(syncno), 'id': str(artwork_info['id']),
            'existence': bool(artwork_info['existence']),
        })
        # 更新频道置顶目录
        if (syncno - 1) % 20 == 0:
            self.updateChannelCatalog(syncno, artwork_info['channelMessageId'])
        # 保存元数据和同步记录
        self.saveMetaAndRecords(meta_dict, records_df)
        # 成功
        return True
    

    def manuallyModifyArtwork(
            self,
            new_artwork_info: dict,
            gap_time: float = 1,
            **kwargs,
        ):
        '''
        手动修改作品。

        :param artwork_info: `dict[str, Any]` 除了标记为`Required`的项，其他项都可以省略:
        ```
        {
            "id": <pixiv_artwork_id: int>, #NOTE: Required
            "illustType": <: int>,
            "title": <: str>,
            "tags": <: list[str]>,
            "createDate": <: str>,
            "updateDate": <: str>,
            "authorScreenName": <author_screen_name: str>,
            "authorUserId": <author_user_id: int>,
            "bookmarkTags": <: list[str]>,
            "referer": "https://www.pixiv.net/artworks/<pixiv_artwork_id>", #NOTE: Not Needed
            "version": <: int>,
            "pages": <["<page_0_file_name>", "<page_1_file_name>", ...]>,
            "pageCount": <: int>, #NOTE: Not Needed
            "existence": <: bool>,
            "channelMessageId": <: int>, #NOTE: Not Needed
            "groupMessageId": <: int>, #NOTE: Not Needed
            "groupDocumentMessageIds": <: list[int]>, #NOTE: Not Needed
        }
        ```
        '''
        meta_dict, records_df = self.getMetaAndRecords()
        if new_artwork_info['id'] not in records_df['id']: return False
        # 获取旧的元数据信息
        syncno = records_df.at[new_artwork_info['id'], 'syncNo']
        old_artwork_info = meta_dict[str(new_artwork_info['id'])]
        # 用新的元数据进行更新
        updated_artwork_info = dict()
        for key in old_artwork_info.keys():
            if (key not in new_artwork_info 
            or old_artwork_info[key] == new_artwork_info[key]):
                updated_artwork_info[key] = old_artwork_info[key]
            else: updated_artwork_info[key] = new_artwork_info[key]
        updated_artwork_info['pageCount'] = len(updated_artwork_info['pages'])
        # 更新作品频道消息，如有新文件则上传
        updated_artwork_info['groupDocumentMessageIds'] = self.updateArtworkMSG(
            syncno=syncno, artwork_info=updated_artwork_info, 
            need_reupload=('pages' in new_artwork_info), 
            doc_uploading_gap_time=gap_time,
        )
        # 记录作品元数据和同步记录
        meta_dict[str(updated_artwork_info['id'])] = updated_artwork_info
        records_df.at[updated_artwork_info['id'], 'existence'] = updated_artwork_info['existence']
        # 保存元数据和同步记录
        self.saveMetaAndRecords(meta_dict, records_df)
        # 成功
        return True
        
    

    def updateExistences(
            self,
            feedback_text: str,
            feedback_messages: Message,
            checked_existence_dict: dict[int, bool],
            meta_dict: dict,
            records_df: pd.DataFrame,
            gap_time: float
        ):
        '''
        更新作品存活状态。
        '''
        # 检查有哪些作品存活状态发生变化
        ids_to_update = []
        for illust_id, row in records_df.iterrows():
            if illust_id not in checked_existence_dict:
                if self.Pixiv.exists(illust_id) != row['existence']:
                    ids_to_update.append(illust_id)
                    time.sleep(gap_time)
            elif checked_existence_dict[illust_id] != row['existence']:
                ids_to_update.append(illust_id)
        
        # 反馈消息
        for msg in feedback_messages:
            feedback_text += f'\n{len(ids_to_update)} 个作品存活状态改变'
            autoRetry(self.bot.edit_message_text)(
                feedback_text, msg.chat.id, msg.id, parse_mode='HTML')
        
        # 更新频道消息
        for illust_id in ids_to_update:
            new_existence = not records_df['existence'][illust_id]
            records_df.at[illust_id, 'existence'] = new_existence
            meta_dict[str(illust_id)]['existence'] = new_existence
            self.updateArtworkMSG(
                records_df.at[illust_id, 'syncNo'], meta_dict[str(illust_id)], 
                need_reupload=False, doc_uploading_gap_time=0,
            )
            # 反馈消息
            for msg in feedback_messages:
                feedback_text += f"\n<code>{illust_id}</code> " +\
                    f"{'存活' if new_existence else '被删除'}"
                autoRetry(self.bot.edit_message_text)(
                    feedback_text, msg.chat.id, msg.id, parse_mode='HTML')
            time.sleep(gap_time)
        
        return meta_dict, records_df, feedback_text


    def checkUpdateStatus(
            self,
            new_artwork_info: dict,
            meta_dict: dict[str, dict],
        ):
        old_artwork = meta_dict[str(new_artwork_info['id'])]
        
        # 作品存活，检查需要更新什么
        if int(new_artwork_info['authorUserId']) > 0:
            # 如果修改时间变动，则需要重新下载和上传图片文件，同时更新元数据
            if old_artwork['updateDate'] != new_artwork_info['updateDate']:
                return 'Reupload', old_artwork
            # 否则检查其他值是否更新，只需更新元数据
            else:
                for key, val in new_artwork_info.items():
                    if val != old_artwork[key]: return 'UpdateMeta', old_artwork
                else: return 'NoUpdates', old_artwork

        # 作品404
        else: return 'NoUpdates', old_artwork
    

    def downloadUpdatedArtwork(
            self,
            artwork_info: dict,
            timeout: float,
        ):
        version = 1 + artwork_info['version']
        pages = self.Pixiv.downloadArtwork(
            illust_id=artwork_info['id'], version=version, 
            illust_type=artwork_info['illustType'],
            referer=artwork_info['referer'], timeout=timeout,
        )
        return pages, version
    

    def updateArtworkMSG(
            self,
            syncno: int,
            artwork_info: dict,
            need_reupload: bool,
            doc_uploading_gap_time: float = 2.8,
        ) -> list[int]:
        '''
        修改封面描述，根据情况决定是否重新上传。
        
        :return: 如果需要重新上传，则返回新文件的群组消息ID列表，否则返回旧列表。
        :rtype: `list[int]`
        '''
        # 更新封面
        try:
            if need_reupload:
                cover_path = os.path.join(self.SAVE_PATH, artwork_info['pages'][0])
            else: cover_path = None
            self.Teleg.updatePhoto(
                chat_id=self.CHANNEL_ID, message_id=artwork_info['channelMessageId'],
                caption=self.genCaption(syncno, **artwork_info), parse_mode='HTML',
                photo_path=cover_path,
            )
        except: raise MessageSendingFailed(
            f"频道消息更新出错，消息id ({artwork_info['channelMessageId']})。")

        # 重新上传图片文件
        if need_reupload:
            group_document_msg_ids = []
            for page in artwork_info['pages']:
                group_document_msg_ids += self.Teleg.sendFile(
                    file_path=os.path.join(self.SAVE_PATH, page), 
                    chat_id=self.GROUP_ID, reply_to_msg_id=artwork_info['groupMessageId'],
                    gap_time_for_sending_zip_volumes=doc_uploading_gap_time,
                )
                time.sleep(doc_uploading_gap_time)
            return group_document_msg_ids
        else: return artwork_info['groupDocumentMessageIds']


    def downloadNewArtwork(
            self,
            artwork_info: dict,
            timeout: float,
        ):
        # 如果作品404
        if int(artwork_info['authorUserId']) <= 0:
            pages = []
            existence = False
            version = 0
        # 如果作品存活
        else:
            pages = self.Pixiv.downloadArtwork(
                illust_id=artwork_info['id'], version=1, 
                illust_type=artwork_info['illustType'],
                referer=artwork_info['referer'], timeout=timeout,
            )
            existence = True
            version = 1
        # 返回值
        return pages, existence, version


    def uploadNewArtwork(
            self,
            syncno: int,
            artwork_info: dict,
            gap_time: float = 2.8,
            max_tries: int = 5,
        ) -> tuple[int, int, list[int]]:
        '''
        将下载好的作品上传到收藏频道和群组。

        :return: 封面的频道消息ID
        :rtype: `int`
        :return: 封面的群组消息ID
        :rtype: `int`
        :return: 文件的群组消息ID列表
        :rtype: `list[int]`
        '''
        # 发送封面，如果404，发送self.ERR404_PHOTO_FILE_PATH做为封面图
        pages = artwork_info['pages']
        if pages: cover_path = os.path.join(self.SAVE_PATH, pages[0])
        else: cover_path = self.ERR404_PHOTO_FILE_PATH
        (   channel_cover_msg_id, group_cover_msg_id,
        ) = self.Teleg.sendPhoto2Channel(
            photo_path=cover_path, caption=self.genCaption(syncno, **artwork_info),
            channel_id=self.CHANNEL_ID, chat_group_id=self.GROUP_ID, parse_mode='HTML',
            retry_gap_time=gap_time, max_tries=max_tries,
        )
        # 在群组中取消所有置顶
        self.bot.unpin_all_chat_messages(self.GROUP_ID)
        # 发送作品文件
        try:
            group_document_msg_ids = []
            for page in pages:
                group_document_msg_ids += self.Teleg.sendFile(
                    file_path=os.path.join(self.SAVE_PATH, page),
                    chat_id=self.GROUP_ID, reply_to_msg_id=group_cover_msg_id,
                    gap_time_for_sending_zip_volumes=gap_time,
                )
                time.sleep(gap_time)
        except Exception as e:
            # 删除封面
            autoRetry(self.bot.delete_message)(self.CHANNEL_ID, channel_cover_msg_id)
            raise e
        
        return channel_cover_msg_id, group_cover_msg_id, group_document_msg_ids
    

    def getChannelCatalogID(self) -> int:
        '''
        获取频道目录的消息ID，如果不存在，则创建一个目录消息。
        '''
        if self.CHANNEL_CATALOG_MSG_ID is None:
            msg = autoRetry(self.bot.send_message)(
                self.CHANNEL_ID, '<b>收藏序号目录</b>', parse_mode='HTML')
            autoRetry(self.bot.pin_chat_message)(self.CHANNEL_ID, msg.id),
            self.CHANNEL_CATALOG_MSG_ID = msg.id
        return self.CHANNEL_CATALOG_MSG_ID
    

    def updateChannelCatalog(self, syncno, channel_msg_id):
        if self.CHANNEL_CATALOG_MSG_ID is None: raise ValueError(f'频道目录消息不存在。')
        # 获取频道目录内容
        msg = self.Teleg.getMessageContent(self.CHANNEL_ID, self.CHANNEL_CATALOG_MSG_ID)
        # 分割内容为：标题 + 链接1 + 链接2 + ……
        catalog_components = msg.html_text.splitlines()
        title = catalog_components[0]
        # 创建新链接，加入目录中
        new_link = f'[<a href="https://t.me/c/{str(self.CHANNEL_ID)[4:]}/' +\
            f'{channel_msg_id}">{syncno:06d}</a>]'
        links = catalog_components[1:] + [new_link]
        links.sort(key = lambda link : re.search(r'<a href="[^"]+">(\d+)</a>', link).group(1))
        # 更新目录
        autoRetry(self.bot.edit_message_text)(
            f"{title}\n{'\n'.join(links)}",
            self.CHANNEL_ID, self.CHANNEL_CATALOG_MSG_ID, parse_mode='HTML',
        )


    def genCaption(
            self,
            syncno: int, id: int, title: str, illustType: int,
            authorScreenName: str, authorUserId: int,
            bookmarkTags: list[str], tags: list[str],
            pageCount: int, referer: str, existence: bool,
            createDate: str, updateDate: str,
            *args, **kwargs,
        ):
        createDate = datetime.fromisoformat(createDate)
        updateDate = datetime.fromisoformat(updateDate)
        caption = \
            f"序号：{syncno}\n" +\
            f"收藏标签：{escape('#'+' #'.join(bookmarkTags))}\n\n" +\
            f"标题：{escape(str(title))}\n" +\
            f"作品ID：<code>{id}</code>{' (#ERR404)' if not existence else ''}\n" +\
            f"作者：{escape(str(authorScreenName))}\n" +\
            f"作者ID：<code>{authorUserId}</code>\n\n" +\
            f"页数：{pageCount}    链接：<a href=\"{referer}\">PIXIV</a>    " +\
            f"类型：{self.ILLUST_TYPE_DICT[illustType]}\n" +\
            f"发布于：{createDate :%y/%m/%d %H:%M:%S %z}\n" +\
            f"修改于：{updateDate :%y/%m/%d %H:%M:%S %z}\n\n" +\
            f"标签：{escape('#'+' #'.join(tags))}"
        return caption


    def getMetaAndRecords(self):
        # 元数据
        if os.path.exists(self.METADATA_FILE_PATH):
            with open(self.METADATA_FILE_PATH, 'rt') as f:
                meta_dict: dict[str, dict] = json.load(f)
        else: meta_dict: dict[str, dict] = dict()
        # 同步记录
        if os.path.exists(self.RECORDS_FILE_PATH):
            records_df = pd.read_csv(self.RECORDS_FILE_PATH,
                dtype={'syncNo': int, 'id': str, 'existence': bool})
            records_df.index = records_df['id']
        else: records_df = pd.DataFrame(columns=['syncNo', 'id', 'existence'])
        # 返回
        return meta_dict, records_df


    def saveMetaAndRecords(self, meta_dict: dict, records_df: pd.DataFrame):
        with open(self.METADATA_FILE_PATH, 'w') as f:
            json.dump(meta_dict, f, ensure_ascii=False, indent=4, separators=(',', ': '))
        records_df.to_csv(self.RECORDS_FILE_PATH, index=False)
    

    def isArtworkRecorded(self, artwork_id):
        _, records_df = self.getMetaAndRecords()
        return artwork_id in records_df['id']



