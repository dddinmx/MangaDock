# -*- coding: utf-8 -*-
"""Server-rendered web routes（兼容 shim）。

实现已按页面域拆分至 ``mangadock/blueprints/web/``：
- ``auth_pages``   登录 / 登出 / 修改密码
- ``home``         首页 / 搜索 / 历史
- ``downloads``    下载 / 更新 / 任务
- ``users_pages``  用户管理
- ``library``      书架 / 分组 / 管理员隐藏区
- ``statistics``   阅读统计
- ``reader``       详情 / 阅读器 / 进度接口 / 漫画文件
- ``settings_pages`` 设置页

导入本模块即完成全部路由注册，旧导入路径保持不变。
"""
from mangadock.blueprints.web import (  # noqa: F401
    auth_pages,
    downloads,
    home,
    library,
    reader,
    settings_pages,
    statistics,
    users_pages,
)
