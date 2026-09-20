# -*- coding: utf-8 -*-
"""Server-rendered web routes，按页面域拆分的子模块。

import 本包即完成全部路由注册（各模块在被导入时向共享的 ``app``
对象挂装饰器路由）。``mangadock/blueprints/web_routes.py`` 是兼容
旧导入路径的 shim。
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
