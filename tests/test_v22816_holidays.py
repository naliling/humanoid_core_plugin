"""v2.28.16：节日查询加农历节日（2025–2030）与二十四节气。

优先级写死为 **自定义 > 农历 > 节气 > 公历内置**。这一组测试锁三件事：

1. 照抄过来的硬表本身是完整的、日期是真实存在的（表写错一个数字，她一整年都在错的日子过节）；
2. 查询真的走到新表上（2026 春节/中秋、清明/立春、2031 年只剩节气）；
3. 优先级真的按上面那条链走（自定义盖农历、农历盖节气、同一天只回一个名字）。

数据源：koishi-plugin-chatluna-spark 的 `lib/data/festivals.js`（天文台计算）。
"""

from __future__ import annotations

import unittest
from datetime import datetime

from humanoid.data.holidays import (
    BUILTIN_HOLIDAYS,
    LUNAR_FESTIVAL_DATES,
    LUNAR_FESTIVAL_NAMES,
    SOLAR_TERMS,
    lunar_holiday,
    resolve_holiday,
    solar_term,
)

# 二十四节气全名单（立春…大寒）。单独抄一份在这里，改表漏一项就会失败。
ALL_SOLAR_TERMS = {
    "立春", "雨水", "惊蛰", "春分", "清明", "谷雨",
    "立夏", "小满", "芒种", "夏至", "小暑", "大暑",
    "立秋", "处暑", "白露", "秋分", "寒露", "霜降",
    "立冬", "小雪", "大雪", "冬至", "小寒", "大寒",
}


def day(year: int, month: int, date: int) -> datetime:
    """不带时区的日期：`resolve_holiday` 只看年月日，时区不该影响结果。"""
    return datetime(year, month, date, 12, 0)


class LunarFestivalTableTest(unittest.TestCase):
    """表结构：年份覆盖 2025–2030、每年 8 个、日期真实且不重复。"""

    def test_covers_exactly_2025_to_2030(self):
        self.assertEqual(set(LUNAR_FESTIVAL_DATES), set(range(2025, 2031)))
        self.assertEqual(
            set(LUNAR_FESTIVAL_NAMES),
            {"春节", "元宵节", "端午节", "七夕节", "中秋节", "重阳节", "腊八节", "除夕"},
        )

    def test_each_year_has_all_eight_festivals(self):
        for year, table in LUNAR_FESTIVAL_DATES.items():
            with self.subTest(year=year):
                self.assertEqual(set(table), set(LUNAR_FESTIVAL_NAMES))
                self.assertEqual(
                    len(set(table.values())), 8,
                    f"{year} 年有两个节日落在同一天：{table}",
                )

    def test_every_date_is_a_real_calendar_day(self):
        """MM-DD 必须是真实存在的公历日期——02-30、13-01 这种手滑要当场炸出来。"""
        for year, table in LUNAR_FESTIVAL_DATES.items():
            for name, month_day in table.items():
                with self.subTest(year=year, name=name):
                    self.assertRegex(month_day, r"^\d{2}-\d{2}$")
                    # strptime 对非法日期抛 ValueError
                    datetime.strptime(f"{year}-{month_day}", "%Y-%m-%d")


class SolarTermTableTest(unittest.TestCase):
    """二十四节气：24 个、名字齐全、日期真实且互不相同。"""

    def test_has_all_24_terms(self):
        self.assertEqual(len(SOLAR_TERMS), 24)
        self.assertEqual(set(SOLAR_TERMS.values()), ALL_SOLAR_TERMS)
        # 每个节气只出现在一个日子上（同一个名字不该有两行）
        self.assertEqual(len(set(SOLAR_TERMS.values())), 24)

    def test_term_dates_are_real_days(self):
        for month_day, name in SOLAR_TERMS.items():
            with self.subTest(name=name):
                self.assertRegex(month_day, r"^\d{2}-\d{2}$")
                datetime.strptime(f"2026-{month_day}", "%Y-%m-%d")


class ResolveHolidayTest(unittest.TestCase):
    """查询本身：2026 的几个硬例子 + 优先级 + 未知年份降级。"""

    def test_2026_spring_festival(self):
        self.assertEqual(resolve_holiday(day(2026, 2, 17)), "春节")

    def test_2026_mid_autumn(self):
        self.assertEqual(resolve_holiday(day(2026, 9, 25)), "中秋节")

    def test_2026_lichun_is_a_solar_term(self):
        self.assertEqual(resolve_holiday(day(2026, 2, 4)), "立春")

    def test_2026_qingming_is_a_solar_term(self):
        self.assertEqual(resolve_holiday(day(2026, 4, 5)), "清明")

    def test_plain_builtin_holiday_still_works(self):
        self.assertEqual(resolve_holiday(day(2026, 1, 1)), "元旦")
        self.assertEqual(resolve_holiday(day(2026, 10, 1)), "国庆节")

    def test_ordinary_day_is_empty(self):
        self.assertEqual(resolve_holiday(day(2026, 3, 15)), "")
        self.assertEqual(resolve_holiday(day(2026, 6, 15)), "")

    def test_unknown_year_has_no_lunar_but_solar_terms_still_work(self):
        """2031 以后农历表没收录：返回空，但节气/公历不受影响。"""
        self.assertEqual(lunar_holiday(day(2031, 2, 17)), "")
        self.assertEqual(resolve_holiday(day(2031, 2, 17)), "")
        self.assertEqual(resolve_holiday(day(2031, 2, 4)), "立春")
        self.assertEqual(resolve_holiday(day(2031, 4, 5)), "清明")
        self.assertEqual(resolve_holiday(day(2031, 1, 1)), "元旦")

    def test_every_lunar_festival_resolves_on_its_own_day(self):
        """六个年份、48 个日子逐一走一遍：整张表哪一行写错都会在这里露出来。"""
        for year, table in LUNAR_FESTIVAL_DATES.items():
            for name, month_day in table.items():
                month, date = (int(part) for part in month_day.split("-"))
                with self.subTest(year=year, name=name):
                    self.assertEqual(resolve_holiday(day(year, month, date)), name)

    def test_lunar_wins_over_solar_term_on_the_same_day(self):
        """2027-08-08 七夕撞立秋、2027-10-08 重阳撞寒露：她该说过节，不该说节气。"""
        self.assertEqual(SOLAR_TERMS.get("08-08"), "立秋")
        self.assertEqual(SOLAR_TERMS.get("10-08"), "寒露")
        self.assertEqual(resolve_holiday(day(2027, 8, 8)), "七夕节")
        self.assertEqual(resolve_holiday(day(2027, 10, 8)), "重阳节")

    def test_custom_holiday_beats_lunar(self):
        custom = [{"date": "02-17", "name": "我们的纪念日"}]
        self.assertEqual(resolve_holiday(day(2026, 2, 17), custom), "我们的纪念日")
        # 只盖住同一天，别处的农历节日照旧
        self.assertEqual(resolve_holiday(day(2026, 9, 25), custom), "中秋节")

    def test_custom_holiday_beats_solar_term_and_builtin(self):
        custom = [{"date": "04-05", "name": "体检节"}, {"date": "01-01", "name": "新年计划"}]
        self.assertEqual(resolve_holiday(day(2026, 4, 5), custom), "体检节")
        self.assertEqual(resolve_holiday(day(2026, 1, 1), custom), "新年计划")

    def test_custom_dated_entry_overrides_lunar_only_that_year(self):
        """带年份的自定义节日仍然是「只在当年那天」，不会被农历表顶掉。"""
        custom = [{"date": "2026-09-25", "name": "体检节"}]
        self.assertEqual(resolve_holiday(day(2026, 9, 25), custom), "体检节")

    def test_builtin_table_is_unchanged(self):
        """v2.28.16 只在前面加了两层，原来的 12 个公历节日一个都不能少。"""
        self.assertEqual(len(BUILTIN_HOLIDAYS), 12)
        self.assertEqual(BUILTIN_HOLIDAYS["10-01"], "国庆节")
        self.assertEqual(BUILTIN_HOLIDAYS["12-25"], "圣诞节")


if __name__ == "__main__":
    unittest.main()
