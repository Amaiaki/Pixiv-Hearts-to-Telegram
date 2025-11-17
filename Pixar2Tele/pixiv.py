'''
使用 Pixiv Ajax API 获取元数据: https://github.com/daydreamer-json/pixiv-ajax-api-docs
'''

import os
import time
import logging
import zipfile
import requests
import imageio.v2 as imageio

from .utils import autoRetry



class PixivTools:
    def __init__(
            self,
            pixiv_user_id: int | str,
            save_path: str,
            headers: dict,
            proxies: dict,
        ):
        self.USER_ID = pixiv_user_id
        self.SAVE_PATH = save_path
        self.HEADERS = headers
        self.PROXIES = proxies
        
        self.ILLUST_TYPE_DICT = {0:'插画', 1:'漫画', 2:'动图', 3:'小说'}

        # 日志
        self.logger = logging.getLogger('Pixar2Tele')
    

    def getCollectionInfos(
            self,
            tag: str = '',
            offset: int = 0,
            limit: int = 1,
            rest: str = 'show',
            timeout: float = 30,
        ) -> list[dict]:
        '''
        获取收藏作品的信息。

        :param tag:
        :param offset:
        :param limit:
        :param rest: `show`或`hide`，指公开的收藏和私密的收藏。

        :return: 收藏顺序从新到旧的作品信息。
        :rtype: `list[dict]`
        '''
        artwork_infos = []

        resp = autoRetry(requests.get)(
            f"https://www.pixiv.net/ajax/user/{self.USER_ID}/illusts/" + \
                f"bookmarks?tag={tag}&offset={offset}&limit={limit}&rest={rest}",
            headers=self.HEADERS, timeout=timeout, proxies=self.PROXIES,
        ).json()
        datas = resp["body"]["works"]
        bookmark_tags: dict = resp["body"].get("bookmarkTags", dict())
        
        for idx in range(len(datas)):
            data = datas[idx]
            if data["bookmarkData"]:
                artwork_bookmark_data = data["bookmarkData"]
            else: artwork_bookmark_data = dict()
            if bookmark_tags:
                artwork_bookmark_tags = bookmark_tags.get(
                    artwork_bookmark_data.get("id", "NotFound"), [])
            else: artwork_bookmark_tags = []
            info = {
                "id": str(data["id"]),
                "illustType": int(data["illustType"]),
                "pageCount": int(data["pageCount"]),
                "title": data["title"],
                "tags": data["tags"],
                "createDate": data["createDate"],
                "updateDate": data["updateDate"],
                "authorScreenName": data["userName"],
                "authorUserId": str(data["userId"]),
                "bookmarkTags": artwork_bookmark_tags,
                "referer": f"https://www.pixiv.net/artworks/{data['id']}",
            }
            artwork_infos.append(info)
        
        return artwork_infos
    

    def downloadArtwork(
            self,
            illust_id: str | int,
            version: int,
            illust_type: int,
            referer: str,
            timeout=30,
            gap_time=1,
        ):
        '''
        将作品保存在指定文件夹中。

        :return: 返回更新过的图片信息列表。
        :rtype: `list[str]`
        '''
        # 检查文件夹是否存在，如果不存在，则创建文件夹
        if not os.path.exists(self.SAVE_PATH): os.makedirs(self.SAVE_PATH)
        # headers需要带上referer，pixiv才允许下载
        download_headers = self.HEADERS
        download_headers["referer"] = referer

        # 插画、漫画
        if illust_type == 0 or illust_type == 1:
            pages = self.downloadPictures(
                illust_id=illust_id, version=version, 
                download_headers=download_headers,
                timeout=timeout, gap_time=gap_time,
            )
        # 动图
        elif illust_type == 2:
            pages = self.downloadUgoira(
                illust_id=illust_id, version=version,
                download_headers=download_headers, timeout=timeout,
            )
        else: raise ValueError(f"仅支持插画(0)、漫画(1)、动图(2)，不支持当前类型 {illust_type}。")
        
        return pages


    def downloadPictures(
            self,
            illust_id: str | int,
            version: int,
            download_headers: dict,
            timeout=30,
            gap_time=1,
        ) -> list[str]:
        '''
        下载插画、漫画。

        :return: 返回文件名列表。
        :rtype: `list[str]`
        '''
        # 请求图片详情
        image_data = autoRetry(requests.get)(
            f"https://www.pixiv.net/ajax/illust/{illust_id}/pages?lang=zh",
            headers=download_headers, timeout=timeout, proxies=self.PROXIES,
        ).json()["body"]

        pages = []
        for page in image_data:
            # 获取下载链接和文件名
            download_url:str = page["urls"]["original"]
            file_stem, file_suffix = os.path.splitext(download_url.split('/')[-1])
            file_name = f'{file_stem}_v{version}{file_suffix}'
            file_path = os.path.join(self.SAVE_PATH, file_name)
            pages.append(file_name)

            # 当图片没被下载时，下载图片
            if not os.path.exists(file_path):
                resp = autoRetry(requests.get)(download_url, 
                    headers=download_headers, timeout=timeout, proxies=self.PROXIES)
                with open(file_path, "wb") as file: file.write(resp.content)
                time.sleep(gap_time)
    
        return pages

    
    def downloadUgoira(
            self,
            illust_id: str | int,
            version: int,
            download_headers: dict,
            timeout=30,
        ) -> list[str]:
        '''
        下载动图。

        :return: 返回文件名列表，只有一个动图文件。
        :rtype: `list[str]`
        '''
        # 动图的保存路径
        file_stem = f"{illust_id}_v{version}"
        file_name = f"{file_stem}.gif"
        file_path = os.path.join(self.SAVE_PATH, file_name)
        
        # 当动图还未下载时，下载动图帧
        if not os.path.exists(file_path):
            ugoira_meta = autoRetry(requests.get)(
                f"https://www.pixiv.net/ajax/illust/{illust_id}/ugoira_meta", 
                headers=download_headers, timeout=timeout, proxies=self.PROXIES,
            ).json()
            ugoira_zip = autoRetry(requests.get)(
                ugoira_meta['body']['originalSrc'], 
                headers=download_headers, timeout=timeout, proxies=self.PROXIES,
            )
            
            zip_path = os.path.join(self.SAVE_PATH, f"{illust_id}.zip")
            with open(zip_path, 'wb') as f: f.write(ugoira_zip.content)
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(os.path.join(self.SAVE_PATH, f"{illust_id}"))
            os.remove(zip_path)
            
            # 将动图帧组合为动图，并保存
            frames = ugoira_meta['body']['frames']
            frame_files = [os.path.join(self.SAVE_PATH, f"{illust_id}", f"{frame['file']}") 
                for frame in frames]
            durations = [frame['delay'] / 1000 for frame in frames]
            
            images = [imageio.imread(frame_file) for frame_file in frame_files]
            imageio.mimsave(file_path, images, duration=durations)
            
            # 删除动图帧
            for frame_file in frame_files: os.remove(frame_file)
            os.rmdir(os.path.join(self.SAVE_PATH, f"{illust_id}"))

        return [file_name]


    def countCollection(self) -> int:
        '''获取收藏总数。'''
        resp = autoRetry(requests.get)(
            f"https://www.pixiv.net/ajax/user/{self.USER_ID}" +\
                "/illusts/bookmarks?tag=&offset=0&limit=1&rest=show",
            headers=self.HEADERS,
        )
        count = resp.json()["body"]["total"]
        return count
    

    def exists(self, illust_id, timeout: float = 20) -> bool:
        resp = autoRetry(requests.get)(
            f"https://www.pixiv.net/ajax/illust/{illust_id}",
            headers=self.HEADERS, timeout=timeout, proxies=self.PROXIES,
        )
        return not resp.json()['error']


