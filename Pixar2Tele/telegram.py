import os
import re
import time
import shutil
import logging

from PIL import Image, ImageFile, ImageSequence
from telebot import TeleBot, apihelper
from telebot.types import Message, InputMediaPhoto

from .utils import autoRetry, MessageNotFound



# 允许打开损坏的图像
ImageFile.LOAD_TRUNCATED_IMAGES = True



class TelegramTools:
    def __init__(
            self,
            bot: TeleBot,
            dustbin_id: int,
            temp_path: str,
            local_api_server_url: str = None,
        ):
        self.bot = bot

        self.DUSTBIN_ID = dustbin_id
        self.TEMP_PATH = temp_path
        self.LOCAL_API_SERVER_URL = local_api_server_url
        self.MAX_PHOTO_DIM = 2160
        self.MAX_PHOTO_FILE_SIZE = 8 * 1000 * 1000

        if not os.path.exists(temp_path): os.mkdir(temp_path)

        # 使用本地 API 服务器
        if isinstance(local_api_server_url, str) and local_api_server_url:
            apihelper.API_URL = os.path.join(local_api_server_url, "bot{0}/{1}")
            apihelper.FILE_URL = os.path.join(local_api_server_url, "file/bot{0}/{1}")
            self.MAX_DOCUMENT_SIZE = 2000 * 1000 * 1000
        # 使用官方 API 服务器
        else:
            self.MAX_DOCUMENT_SIZE = 50 * 1000 * 1000
        
        # 日志
        self.logger = logging.getLogger('Pixar2Tele')
    

    def updatePhoto(
            self,
            chat_id: int,
            message_id: int,
            caption: str,
            parse_mode: str = None,
            photo_path: str = None,
        ):
        '''
        如果`photo_path`为空，则保留原图，只更新`caption`。
        '''
        def editMessagePhoto(bot: TeleBot, chat_id, message_id, photo_path, caption, parse_mode):
            with open(photo_path, 'rb') as photo:
                media = InputMediaPhoto(photo, caption, parse_mode)
                bot.edit_message_media(media, chat_id, message_id)

        if photo_path is None:
            try: autoRetry(self.bot.edit_message_caption)(
                caption, chat_id, message_id, parse_mode=parse_mode)
            except Exception as e:
                self.logger.error(f"图片描述更新失败，图片描述：\n{caption}\n报错：{e}")
        else:
            file_ext = os.path.splitext(photo_path)[1]
            resized_path = os.path.join(self.TEMP_PATH, f'temp{file_ext}')
            photo_path = self.resizePicture(
                input_path=photo_path, resized_path=resized_path,
                to_file_size=self.MAX_PHOTO_FILE_SIZE, to_photo_dim=self.MAX_PHOTO_DIM,
            )
            try: autoRetry(editMessagePhoto, base_delay=2.8)(
                self.bot, chat_id, message_id, photo_path, caption, parse_mode)
            except Exception as e:
                self.logger.error(f"带图消息更新失败，图片描述：\n{caption}\n报错：{e}")


    def sendPhoto2Channel(
            self,
            photo_path: str,
            caption: str,
            channel_id: int,
            chat_group_id: int = None,
            parse_mode: str = None,
            retry_gap_time: float = 2.8,
            max_tries: int = 5,
        ) -> tuple[int, int] | int:
        '''
        返回频道消息ID，和对应的群组消息ID（如果有）。
        '''
        def sendPhoto(bot: TeleBot, chat_id, photo_path, caption, parse_mode):
            with open(photo_path, 'rb') as photo:
                return bot.send_photo(chat_id, photo, caption, parse_mode)
        
        file_ext = os.path.splitext(photo_path)[1]
        resized_path = os.path.join(self.TEMP_PATH, f'temp{file_ext}')
        photo_path = self.resizePicture(
            input_path=photo_path, resized_path=resized_path,
            to_file_size=self.MAX_PHOTO_FILE_SIZE, to_photo_dim=self.MAX_PHOTO_DIM,
        )
        
        if chat_group_id is not None:
            # 发送测试消息，获取讨论群在发送封面前的最新message_id
            group_msg_before_photo = autoRetry(self.bot.send_message)(chat_group_id, '.')
            autoRetry(self.bot.delete_message)(chat_group_id, group_msg_before_photo.id)
        
        # 发送封面，此后报错将需要立刻删除频道消息
        channel_msg = autoRetry(sendPhoto, base_delay=retry_gap_time)(
            self.bot, channel_id, photo_path, caption, parse_mode)

        # 如果有讨论群组
        if chat_group_id is None: return channel_msg.id

        # 找出与频道消息对应的讨论组消息，最多尝试max_tries次寻找消息
        else:
            try:
                for _ in range(max_tries):
                    time.sleep(retry_gap_time)
                    for id in range(group_msg_before_photo.id + 1, group_msg_before_photo.id + 5):
                        try:
                            msg = self.getMessageContent(chat_group_id, id, max_tries=2)
                            if (str(msg.forward_from_chat.id) == str(channel_id) 
                                and msg.forward_from_message_id == channel_msg.id):
                                group_cover_msg_id = id
                                break
                        except: pass
                    else: continue
                    break
                else: raise MessageNotFound(f"频道消息id为 {channel_msg.id}，无法找到群组中的对应消息。")
            
            except Exception as e:
                # 删除频道消息
                autoRetry(self.bot.delete_message)(channel_id, channel_msg.id)
                # 重新报错
                raise e

            return channel_msg.id, group_cover_msg_id
    

    def sendFile(
            self,
            file_path: str,
            chat_id: int,
            reply_to_msg_id: int = None,
            gap_time_for_sending_zip_volumes: float = 2.8,
        ) -> list[int]:
        '''
        返回消息ID列表，文件过大时会发送压缩分卷，所以可能不止一条消息。
        '''
        def sendDocument(bot: TeleBot, chat_id, file_path, reply_to_msg_id):
            with open(file_path, 'rb') as file:
                return bot.send_document(chat_id, file, reply_to_msg_id)
        
        # 如果文件过大，需要分卷压缩再上传
        if os.stat(file_path).st_size >= self.MAX_DOCUMENT_SIZE:
            msg_ids = []

            zip_path = os.path.join(self.TEMP_PATH, 'zip')
            zip_file_path = os.path.join(zip_path, f'{os.path.basename(file_path)}.zip')
            os.system(f'zip -r -s {self.MAX_DOCUMENT_SIZE//(1024**2)}M {zip_file_path} {file_path}')

            # 上传分卷
            for filename in os.listdir(zip_path):
                volume_path = os.path.join(zip_path, filename)
                msg: Message = autoRetry(sendDocument, base_delay=gap_time_for_sending_zip_volumes)(
                    self.bot, chat_id, volume_path, reply_to_msg_id)
                msg_ids.append(msg.id)
                time.sleep(gap_time_for_sending_zip_volumes)
            
            # 移除本地压缩包
            shutil.rmtree(zip_path)

            return msg_ids
        
        # 小文件则直接上传
        else:
            msg: Message = autoRetry(sendDocument)(self.bot, chat_id, file_path, reply_to_msg_id)
            return [msg.id]
    

    def downloadFile(
            self,
            message: Message,
            save_path: str,
            file_stem: str,
        ):
        file_info = self.bot.get_file(message.document.file_id)
        file_name = f"{file_stem}{os.path.splitext(message.document.file_name)[-1]}"
        # 使用本地API
        if self.USE_LOCAL_API_SERVER:
            file_path = self.replacePrefix(file_info.file_path, 
                '/var/lib/telegram-bot-api', '/cih/.docker/telegram-bot-api/data')
            os.rename(file_path, os.path.join(save_path, file_name))
        # 使用官方API
        else:
            downloaded_file = self.bot.download_file(file_info.file_path)
            with open(os.path.join(save_path, file_name), 'wb') as new_file:
                new_file.write(downloaded_file)
        return file_name


    def replacePrefix(self, s: str, old_prefix_pattern: str, new_prefix: str) -> str:
        # 使用 ^ 限定只匹配开头
        pattern = re.compile(r'^' + old_prefix_pattern)
        return pattern.sub(new_prefix, s, count=1)


    def resizePicture(
            self,
            input_path: str, 
            resized_path: str, 
            to_file_size: int, 
            to_photo_dim: int,
        ):
        '''
        调整图片大小和图片文件大小。支持静态图和 GIF（仅处理第一帧作为封面）。

        :param input_path: 原始图片路径
        :param resized_path: 输出图片路径
        :param to_file_size: 封面图目标文件大小（字节）
        :param to_photo_dim: 最大边长限制（像素）
        :return: 返回封面图路径（如果无需压缩则返回原路径）
        '''
        img = Image.open(input_path)

        # 如果是 GIF，提取第一帧作为封面图
        if img.format == 'GIF':
            img = next(ImageSequence.Iterator(img)).convert('RGB')  # 转为 RGB 以便保存为静态图像
            if not resized_path.lower().endswith(('.jpg', '.jpeg', '.png')):
                resized_path += '.jpg'  # 默认保存为 JPEG
            input_path = resized_path
            img.save(input_path)
        
        origin_filesize = os.stat(input_path).st_size

        # 计算压缩比例
        rate = 1
        if origin_filesize and origin_filesize > to_file_size:
            rate = (to_file_size / origin_filesize) ** 0.5

        max_dim = max(img.size)
        if max_dim > to_photo_dim:
            rate = min(rate, to_photo_dim / max_dim)

        # 开始压缩
        if rate != 1:
            new_size = (int(img.size[0] * rate), int(img.size[1] * rate))
            new_img = img.resize(new_size, Image.LANCZOS)
            new_img.save(resized_path)
            return resized_path
        else: return input_path
    
    
    def appendText2Message(
            self, 
            text: str, 
            chat_id: str | int, 
            message_id: str | int, 
            parse_mode='HTML',
        ):
        '''编辑消息：将文本补充到消息末尾。'''
        msg = autoRetry(self.bot.forward_message)(self.DUSTBIN_ID, chat_id, message_id)
        autoRetry(self.bot.edit_message_text)(
            msg.html_text + text, chat_id, message_id, parse_mode=parse_mode)
        autoRetry(self.bot.delete_message)(self.DUSTBIN_ID, msg.id)
    
    
    def getMessageContent(
            self, 
            chat_id: str | int, 
            message_id: str | int,
            max_tries = 5,
        ) -> Message:
        msg = autoRetry(self.bot.forward_message, max_tries=max_tries)(self.DUSTBIN_ID, chat_id, message_id)
        autoRetry(self.bot.delete_message, max_tries=max_tries)(self.DUSTBIN_ID, msg.id)
        return msg


