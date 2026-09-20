from datetime import datetime
import cnlunar
from plugins_func.register import register_function, ToolType, ActionResponse, Action

get_lunar_function_desc = {
    "type": "function",
    "function": {
        "name": "get_lunar",
        "description": (
            "Get Chinese lunar-calendar and almanac details for a specific date. "
            "The user may request the lunar date, heavenly stems and earthly "
            "branches, solar terms, zodiac, horoscope, eight characters, or "
            "auspicious activities. Use context instead for today's basic lunar date."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Date in YYYY-MM-DD format; defaults to today.",
                },
                "query": {
                    "type": "string",
                    "description": "Requested lunar-calendar or almanac details.",
                },
            },
            "required": [],
        },
    },
}


@register_function("get_lunar", get_lunar_function_desc, ToolType.WAIT)
def get_lunar(date=None, query=None):
    """Return Chinese lunar-calendar and almanac data for a date."""
    from core.utils.cache.manager import cache_manager, CacheType

    # Use the requested date, or today when omitted.
    if date:
        try:
            now = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            return ActionResponse(
                Action.REQLLM,
                "Invalid date format; use YYYY-MM-DD, for example 2024-01-01",
                None,
            )
    else:
        now = datetime.now()

    current_date = now.strftime("%Y-%m-%d")

    # Default to the year cycle and lunar date.
    if query is None:
        query = "the year cycle and lunar date"

    # 尝试从缓存获取农历信息
    lunar_cache_key = f"lunar_info_{current_date}"
    cached_lunar_info = cache_manager.get(CacheType.LUNAR, lunar_cache_key)
    if cached_lunar_info:
        return ActionResponse(Action.REQLLM, cached_lunar_info, None)

    response_text = (
        f"Use the following almanac data to answer the user's request about {query}:\n"
    )

    lunar = cnlunar.Lunar(now, godType="8char")
    response_text += (
        "农历信息：\n"
        "%s年%s%s\n" % (lunar.lunarYearCn, lunar.lunarMonthCn[:-1], lunar.lunarDayCn)
        + "干支: %s年 %s月 %s日\n" % (lunar.year8Char, lunar.month8Char, lunar.day8Char)
        + "生肖: 属%s\n" % (lunar.chineseYearZodiac)
        + "八字: %s\n"
        % (
            " ".join(
                [lunar.year8Char, lunar.month8Char, lunar.day8Char, lunar.twohour8Char]
            )
        )
        + "今日节日: %s\n"
        % (
            ",".join(
                filter(
                    None,
                    (
                        lunar.get_legalHolidays(),
                        lunar.get_otherHolidays(),
                        lunar.get_otherLunarHolidays(),
                    ),
                )
            )
        )
        + "今日节气: %s\n" % (lunar.todaySolarTerms)
        + "下一节气: %s %s年%s月%s日\n"
        % (
            lunar.nextSolarTerm,
            lunar.nextSolarTermYear,
            lunar.nextSolarTermDate[0],
            lunar.nextSolarTermDate[1],
        )
        + "今年节气表: %s\n"
        % (
            ", ".join(
                [
                    f"{term}({date[0]}月{date[1]}日)"
                    for term, date in lunar.thisYearSolarTermsDic.items()
                ]
            )
        )
        + "生肖冲煞: %s\n" % (lunar.chineseZodiacClash)
        + "星座: %s\n" % (lunar.starZodiac)
        + "纳音: %s\n" % lunar.get_nayin()
        + "彭祖百忌: %s\n" % (lunar.get_pengTaboo(delimit=", "))
        + "值日: %s执位\n" % lunar.get_today12DayOfficer()[0]
        + "值神: %s(%s)\n"
        % (lunar.get_today12DayOfficer()[1], lunar.get_today12DayOfficer()[2])
        + "廿八宿: %s\n" % lunar.get_the28Stars()
        + "吉神方位: %s\n" % " ".join(lunar.get_luckyGodsDirection())
        + "今日胎神: %s\n" % lunar.get_fetalGod()
        + "宜: %s\n" % "、".join(lunar.goodThing[:10])
        + "忌: %s\n" % "、".join(lunar.badThing[:10])
        + "(默认返回干支年和农历日期；仅在要求查询宜忌信息时才返回本日宜忌)"
    )

    # 缓存农历信息
    cache_manager.set(CacheType.LUNAR, lunar_cache_key, response_text)

    return ActionResponse(Action.REQLLM, response_text, None)
