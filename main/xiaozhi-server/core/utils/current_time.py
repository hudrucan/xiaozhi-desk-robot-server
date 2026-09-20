"""
时间工具模块
提供统一的时间获取功能
"""

from datetime import datetime

WEEKDAY_MAP = {
    "Monday": "星期一",
    "Tuesday": "星期二", 
    "Wednesday": "星期三",
    "Thursday": "星期四",
    "Friday": "星期五",
    "Saturday": "星期六",
    "Sunday": "星期日",
}


def get_current_time() -> str:
    """
    获取当前时间字符串 (格式: HH:MM)
    """
    return datetime.now().strftime("%H:%M")


def get_current_date() -> str:
    """
    获取今天日期字符串 (格式: YYYY-MM-DD)
    """
    return datetime.now().strftime("%Y-%m-%d")


def get_current_weekday() -> str:
    """
    获取今天星期几
    """
    now = datetime.now()
    return WEEKDAY_MAP[now.strftime("%A")]


def get_current_time_info() -> tuple:
    """
    获取当前时间信息
    返回: (当前时间字符串, 今天日期, 今天星期)
    """
    current_time = get_current_time()
    today_date = get_current_date()
    today_weekday = get_current_weekday()

    return current_time, today_date, today_weekday
