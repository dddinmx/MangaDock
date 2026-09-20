# 📚 棧 · MangaDock

![Head diagram](https://github.com/user-attachments/assets/701fe952-a866-4e4a-8ded-10a5ade1b9fd)

<p align="center">
  <a href="https://hub.docker.com/r/dddinmx/mangadock"><img alt="Docker Hub" src="https://img.shields.io/docker/v/dddinmx/mangadock?label=Docker%20Hub"></a>
  <a href="https://github.com/dddinmx/MangaDock"><img alt="Python" src="https://img.shields.io/badge/Python-3.11%2B-3776AB"></a>
  <a href="https://hub.docker.com/r/dddinmx/mangadock"><img alt="Platforms" src="https://img.shields.io/badge/platform-linux%2Famd64%20%7C%20linux%2Farm64-2496ED"></a>
</p>

MangaDock 是一个自托管的漫画与 EPUB 小说下载、管理和阅读工具，提供响应式 Web/PWA 界面、阅读进度、书架分组、更新检查和 Basic Auth API。

## 支持的内容来源

- 包子漫画（`baozimh.org` / `cn.baozimhcn.com`）
- 漫小肆漫画源
- 漫画柜（`manhuagui.com`）
- 番茄漫画
- 番茄小说（搜索、下载、更新、EPUB 阅读）
- 本地漫画和 EPUB 导入

> 番茄漫画目前支持静态图片漫画，不支持漫剧或短视频作品。

## v2.6.0

- 多层级用户管理：管理员可在「用户管理」中创建账号、逐用户分配权限，并支持改密与删除；库写入操作按权限校验。
- 登录页封面墙升级为 24 小说 + 24 漫画封面交错（漫画封面按日轮换）。
- 架构重构：漫画源 Provider 注册表化、web 路由拆分模块化；修复 18+ 开关误伤库内收藏、番茄 API 健康探测误报。

## v2.5.0

- 新增漫画源：漫画柜（`manhuagui.com`），纯 HTTP 实现整本下载，图床直链带服务端签名并自动携带 Referer。
- Tachiyomi / Tachimanga 扩展新增阅读进度同步：在 App 内打开章节时自动上报到 web 端（单向 App → web，只前进不后退，不会覆盖 web 上更新的进度），扩展设置内可开关。
- 扩展 v1.6.3 修复章节索引持久化，避免进程重启后进度同步静默失效。
- 修复 CBZ/PDF 打包时误收 SMB AppleDouble（`._*`）文件的问题（v2.4.0 重发内容，随本版携带）。

## Docker 部署

Docker Hub 镜像：[`dddinmx/mangadock:v2.6.0`](https://hub.docker.com/r/dddinmx/mangadock/tags)。`v2.6.0` 和 `latest` 同时提供 `linux/amd64` 与 `linux/arm64`。

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
export MANGADOCK_ADMIN_PASSWORD='请替换为强密码'
docker compose up -d
docker compose logs -f mangadock
```

访问 `http://127.0.0.1:5001`。管理员用户名为 `admin`；如果首次启动时未设置 `MANGADOCK_ADMIN_PASSWORD`，系统会在容器日志中输出随机密码。

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

### Web PWA

<img width="2910" height="1961" alt="MangaDock Web PWA" src="https://github.com/user-attachments/assets/2e247713-311a-4832-80b0-ae8b9691696b" />

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
