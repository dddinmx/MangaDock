# 📚 棧 · MangaDock

<p align="center">
  <img src="docs/banner.png?v=7ec7fb89" alt="MangaDock banner" width="100%">
</p>

<p align="center">
  <a href="https://hub.docker.com/r/dddinmx/mangadock"><img alt="Docker Hub" src="https://img.shields.io/docker/v/dddinmx/mangadock?label=Docker%20Hub"></a>
  <a href="https://github.com/dddinmx/MangaDock"><img alt="Python" src="https://img.shields.io/badge/Python-3.11%2B-3776AB"></a>
  <a href="https://hub.docker.com/r/dddinmx/mangadock"><img alt="Platforms" src="https://img.shields.io/badge/platform-linux%2Famd64%20%7C%20linux%2Farm64-2496ED"></a>
</p>

MangaDock 是一个自托管的漫画与 EPUB 小说下载、管理和阅读工具。

## 支持的内容来源

- 包子漫画（`baozimh.org` / `cn.baozimhcn.com`）
- 漫画柜（`manhuagui.com`）
- 嬉皮漫畫（`hipmh.com`）
- 漫小肆漫画源
- 瓜子漫画 (`guazimanhua.com`)
- 番茄漫画
- 番茄小说（搜索、下载、更新、EPUB 阅读）
- 本地漫画和 EPUB 导入

> 番茄漫画目前支持静态图片漫画，不支持漫剧或短视频作品。
>
> 部分图床在中国大陆无法直连（漫画柜的 `*.hamreus.com` 需要通过容器出网代理访问，见 [网络与代理](#网络与代理)。

## v2.11.3

- **漫小肆更新修复**：修正章节下载函数与统一分发器的参数数量，更新任务可以进入章节下载流程。
- **包子漫画缺图容错**：单章最多容忍 5 张图片下载失败；缺页章节写入补下标记，更新检查会将其列为待更新，后续任务继续补齐。其他来源仍使用原有阈值。

## v2.11.2

- **Tachimanga / Mihon 封面修复**：封面静态路由现在识别扩展使用的 Basic Auth，并继续按漫画分组检查权限，修复部分书架封面返回 403 的问题。

## v2.11.1

- **登录页封面内容过滤加固**  

## v2.11.0

- **首页横版主视觉**：首页 Hero 会为焦点漫画自动匹配横版主视觉图（检索 AniList / Kitsu），后台超分后缓存到封面卷，升级后首次启动自动补扫一次现有书库（已完成的扫描不会重复）。找不到合适横图时沿用原竖版封面版式。
- **底部书轨铺满**：首页横滑书轨的左右留白由 8.33vw 收窄到约 28px，卡片铺满底部；窄屏布局不受影响。
- **封面权限收口**：封面静态路由改为按分组校验，无权账号不再能直接取到受限分组的封面；封面响应改为 `private, no-store`，避免中间缓存串号。登录页封面墙因此改走专用路由（`/login-cover/<slot>.jpg`），未登录也能正常显示。
- **任务可靠性**：
  - 任务记录领取它的 worker PID，worker 意外退出时**精确**退回该 worker 名下的任务（此前只有全部 worker 离线才会退回，部分退出时任务会永远卡在「运行中」）。
  - 缺页章节会落 `.incomplete` 标记，后续更新任务自动补下，完整重下后清除标记。
  - 同一本漫画的下载 / 更新加写锁，避免并发任务互相覆盖产物。
  - 任务终态统一收口，已取消的任务不会被后续状态覆盖。
- **下载入口权限对齐**：下载页与 API 按漫画 URL 归一化比对，用旧链接提交无权分组的漫画会被拒绝。
- **账号安全**：Basic 认证缓存改为回存数据库确认凭据未变（保留跳过 PBKDF2 的性能收益）；登录失败锁定改为「用户名 + IP」精确组合，不再因单一维度误锁。
- **统计页**：阅读时长排行按分组权限过滤，受限漫画不再出现在无权账号的榜单里。
- **阅读器分页缓存**：API 分页缓存按源文件版本化，重新下载章节后自动失效，并清理超过 10 分钟未访问的旧版本缓存。


## Docker 部署

Docker Hub 镜像：[`dddinmx/mangadock:v2.11.3`](https://hub.docker.com/r/dddinmx/mangadock/tags)。`v2.11.3` 和 `latest` 同时提供 `linux/amd64` 与 `linux/arm64`。

### 1. 准备目录

```bash
mkdir -p mangadock/data/comic mangadock/data/novels
cd mangadock
printf '{}\n' > data/comic.json
```

`comic.json` 必须是有效 JSON；空库请使用 `{}`，不要只创建空文件。

### 2. 创建 `docker-compose.yml`

```yaml
services:
  mangadock:
    image: dddinmx/mangadock:latest
    container_name: MangaDock
    restart: unless-stopped
    ports:
      # 默认只允许本机或反向代理访问；局域网直连时改为 "5001:5001"
      - "127.0.0.1:5001:5001"
    environment:
      TZ: Asia/Shanghai
      MANGADOCK_ADMIN_PASSWORD: ${MANGADOCK_ADMIN_PASSWORD:-}
    volumes:
      - ./data/comic:/app/comic
      - ./data/novels:/app/小说
      - ./data/comic.json:/app/comic.json
      - mangadock_covers:/app/static/cover
      - mangadock_instance:/app/instance

volumes:
  mangadock_covers:
  mangadock_instance:
```

漫画章节、用户书库封面、EPUB、来源映射、SQLite 数据库和会话都保存在宿主机目录或 Docker 数据卷中，不会进入公开镜像。

### 3. 启动

```bash
docker compose up -d
docker compose logs -f mangadock
```

访问 `http://127.0.0.1:5001`。管理员用户名为 `admin`；如果首次启动时未设置 `MANGADOCK_ADMIN_PASSWORD`，系统会在容器日志中输出随机密码。

### 网络与代理

应用通过标准环境变量出网，容器内设置 `HTTP_PROXY` / `HTTPS_PROXY`（可选 `NO_PROXY`）即可：

```yaml
    environment:
      TZ: Asia/Shanghai
      HTTP_PROXY: http://192.168.1.10:7890
      HTTPS_PROXY: http://192.168.1.10:7890
      NO_PROXY: localhost,127.0.0.1
```

- **漫画柜**的图片图床 `*.hamreus.com` 在中国大陆无法直连，必须经代理，否则章节会下载失败。  

### 番茄功能

番茄小说与漫画功能已内置，部署时无需填写额外地址或参数。首次使用时会自动完成初始化；请持续挂载 `instance` 数据卷，以便升级或重启后继续使用。

### 更新

```bash
docker compose pull
docker compose up -d
```

## 本地构建

```bash
git clone https://github.com/dddinmx/MangaDock.git
cd MangaDock
docker compose up -d --build
```

Docker 容器内监听 `0.0.0.0:5001`，Compose 默认只将它绑定到宿主机 `127.0.0.1:5001`。

## 界面与客户端

### Web  
<img width="3828" height="1962" alt="image" src="https://github.com/user-attachments/assets/31b8a061-e270-4453-8ae1-aab0f6d3e1e4" />  
<img width="3828" height="1962" alt="fd98db46654bde21296a6f815e5865c8" src="https://github.com/user-attachments/assets/95a90a97-fa14-41c3-8fa1-be1bfeeabe11" />  

### PWA  
<img width="345" height="720" alt="19400078c7223005a3d4bce1d17816ba" src="https://github.com/user-attachments/assets/2e3623de-55f5-49ef-87b0-545846a8bca1" />  

### Tachimanga / Aidoku

客户端插件或安装包请查看 [Releases](https://github.com/dddinmx/MangaDock/releases)。Android 上可尝试通过 Mihon 使用兼容插件，兼容性以对应客户端实际表现为准。

**阅读进度同步（扩展 v1.6.3+）**：在 Tachimanga（iOS）/ Mihon 中安装 MangaDock 扩展后，进入扩展设置填写 Server URL、用户名与密码，并保持 "Sync reading progress" 开启。此后在 App 内打开章节时会自动把进度上报到 web 端：

- 同步方向为 App → web 单向，只前进不后退，不会覆盖 web 上更新的章节进度；
- 记录粒度为「打开章节」；
- web 端继续阅读会自动定位到该章节。

**扩展仓库安装**：在 Tachimanga / Mihon 的扩展仓库设置中添加：

```
https://raw.githubusercontent.com/dddinmx/MangaDock/main/static/ext-repo/index.min.json
```

即可在线搜索并安装 MangaDock 扩展（无需手动下载 APK）。

## Project Team

| Role | Member |
|---|---|
| Project Owner | [@dddinmx](https://github.com/dddinmx) |
| AI Maintainer | OpenAI Codex |
| Code Review | OpenAI Codex & [@dddinmx](https://github.com/dddinmx) |

## 免责声明

- 本工具仅作学习、研究、交流使用。
- 使用者应确保使用方式符合所在地法律和第三方服务条款，并尊重版权与其他权利。
- 作者不对使用本工具造成的损失或纠纷承担责任。

问题与建议请提交 [Issue](https://github.com/dddinmx/MangaDock/issues) 或 Discussion。
