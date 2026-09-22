# -*- coding: utf-8 -*-
"""18+ 内容来源（漫小肆韩漫 / mxs12.cc）的全局开关。

默认关闭。开启后「整本下载」页才显示漫小肆韩漫入口，并允许从该站点下载。
取值落在 AppSetting 表，与 comic_update_mode 同一套持久化机制。
"""
from mangadock.services.updates import get_app_setting, set_app_setting
from mangadock.settings import ADULT_CONTENT_SETTING_KEY

_TRUE_VALUES = {'1', 'true', 'yes', 'on'}


def is_adult_content_enabled():
    """返回 18+ 内容来源开关状态；未设置或取值非法时一律视为关闭。"""
    raw = get_app_setting(ADULT_CONTENT_SETTING_KEY, '0')
    return str(raw or '').strip().lower() in _TRUE_VALUES


def is_adult_content_enabled_for(user):
    """按用户生效的 18+ 开关：全局开启，或该用户被单独授予 can_view_adult。

    全局关闭时被单独授权的用户仍可使用 18+ 内容来源；管理员不走用户级覆盖
    （管理员需要 18+ 内容时直接开全局开关，保持既有语义）。
    """
    if user is not None and getattr(user, 'can_view_adult', False):
        return True
    return is_adult_content_enabled()


def set_adult_content_enabled(enabled):
    """写入开关状态并返回最终生效值。"""
    value = '1' if enabled else '0'
    set_app_setting(ADULT_CONTENT_SETTING_KEY, value)
    return value == '1'
