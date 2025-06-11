import time
import logging
import pytz
from datetime import datetime


def auto_retry(
        func, 
        args: tuple = tuple(), 
        kwargs: dict = dict(), 
        max_tries = 5,
        gap_time = 1,
    ):
    err = Exception()
    for _ in range(max_tries):
        try:
            feedback = func(*args, **kwargs)
            return feedback
        except Exception as e:
            err = e
            time.sleep(gap_time)
    raise err


class P2TLogger:
    def __init__(self):
        pass

    def filter_keywords(self, exclude_keywords: list[str]):
        log_filter_instance = self.LogFilter(exclude_keywords)
        # 处理 root logger
        root_logger = logging.getLogger()
        root_logger.addFilter(log_filter_instance)
        # 处理所有已创建的 logger
        for logger_name in logging.root.manager.loggerDict:
            logger = logging.getLogger(logger_name)
            logger.addFilter(log_filter_instance)
    
    class LogFilter(logging.Filter):
        def __init__(
                self,
                exclude_keywords: list[str],
                name = "",
            ):
            super().__init__(name)
            self.EXCLUDE_KEYWORDS = exclude_keywords
        def filter(self, record):
            return not any(kw in record.getMessage() for kw in self.EXCLUDE_KEYWORDS)


class NowTimer:
    def __init__(self, dt_format=None, tz="Asia/Shanghai"):
        self.dt_format = dt_format
        self.timezone = pytz.timezone(tz)
    
    def now(self):
        dt = datetime.now().astimezone(self.timezone)
        return dt.strftime(self.dt_format or "%Y-%m-%d %H:%M:%S %z")

