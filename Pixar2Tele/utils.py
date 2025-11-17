import os
import time
import pytz
import logging
import telebot
import traceback

from datetime import datetime
from typing import Callable
from logging.handlers import RotatingFileHandler



# 消息报错
class MessageNotFound(Exception):
    '''找不到指定消息'''
class MessageSendingFailed(Exception):
    '''消息发送失败'''



def autoRetry(
    func: Callable,
    max_tries: int = 5,
    base_delay: float | int = 1,
    backoff_factor: float = 2.0,
):
    '''自动重试装饰器，支持指数退避。'''
    def decorator(*args, **kwargs):
        err = Exception()
        delay = base_delay
        for attempt in range(max_tries):
            try:
                feedback = func(*args, **kwargs)
                return feedback
            except Exception as e:
                err = e
                if attempt < max_tries - 1:
                    time.sleep(delay)
                    delay *= backoff_factor
        raise err
    return decorator



class P2TLogging:
    def __init__(
            self,
            formatter: logging.Formatter = None,
            log_file_path: str = None,
            timezone = 'Asia/Shanghai',
            max_bytes = 1 * 1024 * 1024,
            backup_count = 5,
        ):
        self.logger_name = 'Pixar2Tele'
        self.logger = logging.getLogger(self.logger_name)

        # 首先关闭并移除所有 handlers，包括 telebot 的，确保设置统一
        for handler in self.logger.handlers[:]:  # 使用切片复制，避免迭代时修改原列表
            handler.close()
            self.logger.removeHandler(handler)
        telebot.logger.handlers.clear()
        
        # 设置日志级别
        self.logger.setLevel(logging.INFO)
        
        # 设置日志格式
        if formatter is None:
            formatter = self.CustomTZFormatter(
                timezone = timezone,
                fmt = '[%(asctime)s][%(levelname)s][%(name)s] %(message)s',
                datefmt = "%Y-%m-%d %H:%M:%S %z",
            )
        
        # 控制台输出
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        self.logger.addHandler(console_handler)
        telebot.logger.addHandler(console_handler)

        # 文件输出
        if log_file_path:
            log_file_path = os.path.abspath(log_file_path)
            log_dir = os.path.dirname(log_file_path)
            os.makedirs(log_dir, exist_ok=True)
            
            file_handler = RotatingFileHandler(
                log_file_path,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding='utf-8',
            )
            file_handler.setFormatter(formatter)

            self.logger.addHandler(file_handler)
            telebot.logger.addHandler(file_handler)
    

    class CustomTZFormatter(logging.Formatter):
        def __init__(self, timezone = 'Asia/Shanghai', *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.timezone = timezone
        def formatTime(self, record, datefmt=None):
            tz = pytz.timezone(self.timezone)
            dt = datetime.fromtimestamp(record.created, tz)
            if datefmt: return dt.strftime(datefmt)
            else: return dt.isoformat()
    

    class KeywordFilter(logging.Filter):
        def __init__(self, exclude_keywords: list[str], name = "",):
            super().__init__(name)
            self.EXCLUDE_KEYWORDS = exclude_keywords
        def filter(self, record):
            return not any(kw in record.getMessage() for kw in self.EXCLUDE_KEYWORDS)


    def filterKeywords(self, exclude_keywords: list[str]):
        log_filter_instance = self.KeywordFilter(exclude_keywords)
        # 处理 root logger
        root_logger = logging.getLogger()
        root_logger.addFilter(log_filter_instance)
        # 处理所有已创建的 logger
        for logger_name in logging.root.manager.loggerDict:
            logger = logging.getLogger(logger_name)
            logger.addFilter(log_filter_instance)
    

    def getLogger(self):
        return logging.getLogger(self.logger_name)



def logIfError(logger: logging.Logger, func: Callable):
    '''将func的报错输出到日志'''
    def decorator(*args, **kwargs):
        try:
            feedback = func(*args, **kwargs)
            return feedback
        except Exception as e:
            logger.error(str(e))
            logger.error(f'\n{traceback.format_exc()}')
            time.sleep(3)
    return decorator


