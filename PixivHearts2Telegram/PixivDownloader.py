'''
使用 Pixiv Ajax API 获取元数据: https://github.com/daydreamer-json/pixiv-ajax-api-docs
'''
import os
import time
import zipfile
import requests
import pandas as pd
import imageio.v2 as imageio

from telebot import TeleBot
from telebot.types import Message

from .utils import auto_retry



class PixivDownloader:
    def __init__(
            self,
            pixiv_user_id: int | str,
            save_path: str,
            metadata_filepath: str,
            headers: dict,
            proxies: dict,
        ):
        self.USER_ID = pixiv_user_id
        self.SAVE_PATH = save_path
        self.METADATA_FILEPATH = metadata_filepath
        self.HEADERS = headers
        self.PROXIES = proxies


    def download_collections(
            self,
            bot: TeleBot | None,
            feed_back_msg: Message | None,
            pace = 5,
            timeout = 30,
            gap_time = 3,
        ):
        '''下载收藏的作品，只下载更新的部分。'''
        feedback_text = feed_back_msg.text if feed_back_msg is not None else ''
        count = self.count_collection()
        all_image_info = []
        
        # 获取旧元数据中最后一个作品的 likeOrder 和 id
        if os.path.exists(self.METADATA_FILEPATH):
            old_info_df = pd.read_csv(self.METADATA_FILEPATH)
            columns = old_info_df.columns.to_list()
            max_previous_likeorder = old_info_df.iat[-1, columns.index("likeOrder")]
            last_previous_pid = old_info_df.iat[-1, columns.index("id")]
        else:
            max_previous_likeorder = None
            last_previous_pid = None

        for offset in range(0, count, pace):
            collection_url = f"https://www.pixiv.net/ajax/user/{self.USER_ID}/illusts/" + \
                            f"bookmarks?tag=&offset={offset}&limit={pace}&rest=show"
            resp = auto_retry(requests.get)(
                collection_url, headers=self.HEADERS, timeout=timeout, proxies=self.PROXIES
            ).json()
            datas = resp["body"]["works"]
            bookmark_tags: dict = resp["body"].get("bookmarkTags", dict())

            for idx in range(len(datas)):
                data = datas[idx]
                
                image_bookmark_data = data["bookmarkData"] if data["bookmarkData"] is not None else dict()
                image_bookmark_tags = bookmark_tags.get(image_bookmark_data.get("id", "NotFound"), [])
                image = {
                    # 现在Pixiv删除作品后会同时删除收藏记录，所以这种办法得到的不是真正的likeOrder，需要在后续修正
                    "likeOrder": count - offset - idx,
                    "id": data["id"],
                    "illustType": data["illustType"],
                    "pageCount": data["pageCount"],
                    "title": data["title"],
                    "tags": data["tags"],
                    "createDate": data["createDate"],
                    "referer": f"https://www.pixiv.net/artworks/{data['id']}",
                    "userName": data["userName"],
                    "userId": data["userId"],
                    "bookmarkTags": image_bookmark_tags,
                }

                if str(data['id']) == str(last_previous_pid):
                    if bot and feed_back_msg:
                        feedback_text += f"\n下载完成位置：{image['likeOrder']}"
                        auto_retry(bot.edit_message_text)(feedback_text, feed_back_msg.chat.id, feed_back_msg.id)
                    break

                if bot and feed_back_msg:
                    if offset + idx <= 0:
                        feedback_text += f"\n下载起始位置：{image['likeOrder']}"
                    auto_retry(bot.edit_message_text)(
                        feedback_text + f"\n当前下载位置：{image['likeOrder']}", 
                        feed_back_msg.chat.id, feed_back_msg.id,
                    )

                image['pages'], _ = self.download_artwork(
                    illust_id = image['id'],
                    illust_type = image['illustType'],
                    referer = image['referer'],
                    timeout = timeout,
                    gap_time = gap_time / 10,
                )

                all_image_info.append(image)
                time.sleep(gap_time)

            else:
                continue
            
            # 同步完成
            break

        # 将新的元数据转换为DataFrame
        if all_image_info:
            new_info_df = pd.DataFrame(all_image_info)
            new_info_df.sort_values("likeOrder", ascending=True, inplace=True)
            if last_previous_pid is None or max_previous_likeorder is None:
                # 如果之前没有存过元数据，将会生成新的元数据文件
                new_info_df.to_csv(self.METADATA_FILEPATH, index=False, mode="x")
            else:
                # 否则将新增元数据合并到元数据文件中
                # 修正likeOrder
                new_info_df.reset_index(drop=True, inplace=True)
                new_info_df.index += max_previous_likeorder + 1
                new_info_df.loc[:,"likeOrder"] = new_info_df.index
                new_info_df.to_csv(self.METADATA_FILEPATH, index=False, mode="a", header=False)

        return count, feedback_text
    

    def download_artwork(
            self,
            illust_id: str | int,
            illust_type: int,
            referer: str,
            timeout=30,
            gap_time=1,
        ):
        '''
        将作品保存在指定文件夹中。

        :return:
        - 同时返回更新过的图片信息列表。
        - 如果所有文件都已经被下载过，则返回True，否则返回False。
        '''
        # 检查文件夹是否存在
        if not os.path.exists(self.SAVE_PATH):
            # 如果不存在，则创建文件夹
            os.makedirs(self.SAVE_PATH)
        # headers需要带上referer，pixiv才允许下载
        download_headers = self.HEADERS
        download_headers["referer"] = referer
        # 插画、漫画
        if illust_type == 0 or illust_type == 1:
            pages, if_already_downloaded = self.download_pictures(
                illust_id=illust_id,
                download_headers=download_headers,
                timeout=timeout,
                gap_time=gap_time,
            )
        # 动图
        elif illust_type == 2:
            file_name, if_already_downloaded = self.download_ugoira(
                illust_id=illust_id,
                download_headers=download_headers,
                timeout=timeout,
            )
            pages = [file_name]
        else:
            raise ValueError(f"只支持同步插画（0）、漫画（1）、动图（2），但当前作品类型为 {illust_type}")
        return pages, if_already_downloaded


    def download_pictures(
            self,
            illust_id: str | int,
            download_headers: dict,
            timeout=30,
            gap_time=1,
        ):
        '''
        下载插画、漫画。

        :return:
        - 返回文件名列表。
        - 返回文件是否早已存在。
        '''
        if_already_downloaded = True

        # 请求图片详情
        image_data = auto_retry(requests.get)(
            f"https://www.pixiv.net/ajax/illust/{illust_id}/pages?lang=zh",
            headers=download_headers, timeout=timeout, proxies=self.PROXIES,
        ).json()["body"]

        pages = []
        for page in image_data:
            # 获取下载链接和文件名
            download_url = page["urls"]["original"]
            file_name = download_url.split("/")[-1]
            file_path = os.path.join(self.SAVE_PATH, file_name)
            pages.append(file_name)

            # 检查图片是否已经下载过，没有下载过就下载
            if not os.path.exists(file_path):
                if_already_downloaded = False
                resp = auto_retry(requests.get)(
                    download_url, headers=download_headers, timeout=timeout, proxies=self.PROXIES)
                with open(file_path, "wb") as file:
                    file.write(resp.content)
                time.sleep(gap_time)
    
        return pages, if_already_downloaded

    
    def download_ugoira(
            self,
            illust_id: str | int,
            download_headers: dict,
            timeout=30,
        ):
        '''
        下载动图。

        :return:
        - 返回文件名。
        - 返回文件是否早已存在。
        '''
        gif_path = os.path.join(self.SAVE_PATH, f"{illust_id}.gif")
        # 如果文件已经存在，结束下载
        if os.path.exists(gif_path):
            return f"{illust_id}.gif", True
        
        ugoira_meta = auto_retry(requests.get)(
            f"https://www.pixiv.net/ajax/illust/{illust_id}/ugoira_meta", 
            headers=download_headers, timeout=timeout, proxies=self.PROXIES,
        ).json()
        ugoira_zip = auto_retry(requests.get)(
            ugoira_meta['body']['originalSrc'], 
            headers=download_headers, timeout=timeout, proxies=self.PROXIES,
        )
        
        zip_path = os.path.join(self.SAVE_PATH, f"{illust_id}.zip")
        with open(zip_path, 'wb') as f:
            f.write(ugoira_zip.content)
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(os.path.join(self.SAVE_PATH, f"{illust_id}"))
        os.remove(zip_path)
        
        frames = ugoira_meta['body']['frames']
        frame_files = [os.path.join(self.SAVE_PATH, f"{illust_id}", f"{frame['file']}") for frame in frames]
        durations = [frame['delay'] / 1000 for frame in frames]
        
        images = [imageio.imread(frame_file) for frame_file in frame_files]
        imageio.mimsave(gif_path, images, duration=durations)
        
        for frame_file in frame_files:
            os.remove(frame_file)
        os.rmdir(os.path.join(self.SAVE_PATH, f"{illust_id}"))

        return f"{illust_id}.gif", False


    def count_collection(self) -> int:
        '''获取收藏总数。'''
        resp = auto_retry(requests.get)(
            f"https://www.pixiv.net/ajax/user/{self.USER_ID}/illusts/bookmarks?tag=&offset=0&limit=1&rest=show",
            headers=self.HEADERS,
        )
        count = resp.json()["body"]["total"]
        return count

