# 📚 棧 · MangaDock

![Head diagram](https://github.com/user-attachments/assets/701fe952-a866-4e4a-8ded-10a5ade1b9fd)

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
- 漫小肆漫画源（18+，需在「我的」页开启「18+ 内容来源」后才会出现在下载页）
- 番茄漫画
- 番茄小说（搜索、下载、更新、EPUB 阅读）
- 本地漫画和 EPUB 导入

> 番茄漫画目前支持静态图片漫画，不支持漫剧或短视频作品。
>
> 部分图床在中国大陆无法直连（漫画柜的 `*.hamreus.com`、嬉皮漫畫的 `*.s3imgs.top` 等），需要通过容器出网代理访问，见 [网络与代理](#网络与代理)。

## v2.9.0

- **统一下载入口自动识别番茄小说**：「整本下载」页与 `/api/v1/downloads` 粘贴番茄链接时会先判定作品类型 —— 小说自动分流到 EPUB 小说流水线并入小说书架，漫画仍走原有图片漫画管线，两个入口产出的任务形态完全一致。
- **分享文本容错**：直接粘贴 App 分享出来的「书名 + 链接」整段文本也能识别（按「链接 → 长数字 ID」顺序抽取），不必手工裁出链接。

## Docker 部署

Docker Hub 镜像：[`dddinmx/mangadock:v2.9.0`](https://hub.docker.com/r/dddinmx/mangadock/tags)。`v2.9.0` 和 `latest` 同时提供 `linux/amd64` 与 `linux/arm64`。

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
- **嬉皮漫畫**的图床 `*.s3imgs.top` 可直连，但经代理访问时可能偶发抖动；日志里出现 `503`、`SSLEOFError` 后会自动重试并补齐，一般无需干预。
- 番茄相关接口直连即可，不需要代理。

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
