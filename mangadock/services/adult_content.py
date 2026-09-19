# -*- coding: utf-8 -*-
"""18+ 内容来源（漫小肆韩漫 / mxs12.cc）的全局开关。

默认关闭。开启后「下载漫画」页才显示漫小肆韩漫入口，并允许从该站点下载。
取值落在 AppSetting 表，与 comic_update_mode 同一套持久化机制。
"""
from mangadock.services.updates import get_app_setting, set_app_setting
from mangadock.settings import ADULT_CONTENT_SETTING_KEY

_TRUE_VALUES = {'1', 'true', 'yes', 'on'}


def is_adult_content_enabled():
    """返回 18+ 内容来源开关状态；未设置或取值非法时一律视为关闭。"""
    raw = get_app_setting(ADULT_CONTENT_SETTING_KEY, '0')
    return str(raw or '').strip().lower() in _TRUE_VALUES


def set_adult_content_enabled(enabled):
    """写入开关状态并返回最终生效值。"""
    value = '1' if enabled else '0'
    set_app_setting(ADULT_CONTENT_SETTING_KEY, value)
    return value == '1'
