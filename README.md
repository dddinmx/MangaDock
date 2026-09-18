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
- MXS 漫画源
- 番茄图片漫画（PDF / CBZ，通过独立 API 服务）
- 番茄小说（搜索、下载、更新、EPUB 阅读，通过独立 API 服务）
- 本地漫画和 EPUB 导入

> 番茄漫画目前支持静态图片漫画，不支持漫剧或短视频作品。

## v2.2.0

- 新增番茄小说搜索、下载、更新及阅读入口。
- 新增番茄图片漫画下载，完成后直接进入漫画书库。
- 优化番茄搜索页、浅色主题和封面 CDN 回退。
- 下载进度页支持显示番茄小说封面。
- 小说首页的长简介支持折叠和展开，兼顾桌面端与手机端阅读。
- 番茄小说与漫画统一改走独立 API；包子漫画、MXS 及本地导入保持原有逻辑。

## Docker 部署

Docker Hub 镜像：[`dddinmx/mangadock:v2.2.0`](https://hub.docker.com/r/dddinmx/mangadock/tags)。`v2.2.0` 和 `latest` 同时提供 `linux/amd64` 与 `linux/arm64`。

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
    image: dddinmx/mangadock:v2.2.0
    container_name: MangaDock
    restart: unless-stopped
    ports:
      # 默认只允许本机或反向代理访问；局域网直连时改为 "5001:5001"
      - "127.0.0.1:5001:5001"
    environment:
      TZ: Asia/Shanghai
      MANGADOCK_ADMIN_PASSWORD: ${MANGADOCK_ADMIN_PASSWORD:-}
      # 仅番茄小说/漫画使用以下 API 配置。
      MANGADOCK_FANQIE_API_URL: ${MANGADOCK_FANQIE_API_URL:-https://fanqie.dddinmx.cn}
      MANGADOCK_FANQIE_API_TOKEN: ${MANGADOCK_FANQIE_API_TOKEN:-}
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

漫画章节、封面、EPUB、来源映射、SQLite 数据库和会话都保存在宿主机目录或 Docker 数据卷中，不会进入公开镜像。

### 3. 启动

```bash
export MANGADOCK_ADMIN_PASSWORD='请替换为强密码'
# 番茄功能需要由服务维护者分配的访问令牌；不要提交到 Git。
export MANGADOCK_FANQIE_API_TOKEN='请替换为你的访问令牌'
docker compose up -d
docker compose logs -f mangadock
```

访问 `http://127.0.0.1:5001`。管理员用户名为 `admin`；如果首次启动时未设置 `MANGADOCK_ADMIN_PASSWORD`，系统会在容器日志中输出随机密码。

### 番茄 API 配置

番茄功能默认连接 `https://fanqie.dddinmx.cn`，访问令牌通过 `MANGADOCK_FANQIE_API_TOKEN` 注入。令牌为空或无效时，只会停用番茄搜索、下载和更新；包子漫画、MXS、本地漫画与 EPUB 仍按原有方式工作。建议把令牌写入仅保存在部署主机上的 `.env` 文件，并限制文件权限，不要写入镜像、Compose 文件或公开仓库。

首次调用番茄 API 时，MangaDock 会在持久化的 `instance` 数据卷中生成一个随机安装 ID。API 服务会记录该 ID、请求来源公网 IP、Cloudflare 国家代码、MangaDock/Python 版本、操作系统与架构、调用接口、响应状态和调用时间，用于统计部署数量与服务运行情况。调用明细默认保留 90 天。统计模块不会记录主机名、MAC 地址、书库内容、搜索关键词、作品 ID 或下载内容；统计明细仅管理员 Token 可以查看。

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
