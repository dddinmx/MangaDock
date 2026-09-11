# 📚 棧 · MangaDock

![Head diagram](https://github.com/user-attachments/assets/701fe952-a866-4e4a-8ded-10a5ade1b9fd)

<p align="center">
  <a href="https://hub.docker.com/r/dddinmx/mangadock"><img alt="Docker Hub" src="https://img.shields.io/docker/v/dddinmx/mangadock?label=Docker%20Hub"></a>
  <a href="https://github.com/dddinmx/MangaDock"><img alt="Python" src="https://img.shields.io/badge/Python-3.9%2B-3776AB"></a>
  <a href="https://hub.docker.com/r/dddinmx/mangadock"><img alt="Platforms" src="https://img.shields.io/badge/platform-linux%2Famd64%20%7C%20linux%2Farm64-2496ED"></a>
</p>

**棧**（MangaDock）是一个基于 Python 的本地漫画阅读、下载与管理工具，并支持本地 EPUB 小说阅读。

**支持站点：**

- [x] 漫小肆韩漫
- [x] 包子漫画（`baozimh.org`）
- [x] 包子漫画（`cn.baozimhcn.com`）

## 🖥️ 界面

### Web PWA

<img width="2910" height="1961" alt="MangaDock Web PWA" src="https://github.com/user-attachments/assets/2e247713-311a-4832-80b0-ae8b9691696b" />

### Tachimanga

<img width="850" alt="Tachimanga" src="https://github.com/user-attachments/assets/56501558-bd5e-4c7e-859a-65ab01c666da" />

### Aidoku

<img width="567" alt="Aidoku" src="https://github.com/user-attachments/assets/774ce995-a1c9-49d0-800a-34f60cc96848" />

## 📖 Docker 部署

Docker Hub 镜像：[`dddinmx/mangadock:v2.1`](https://hub.docker.com/r/dddinmx/mangadock/tags)。`v2.1` 和 `latest` 都同时提供 `linux/amd64` 与 `linux/arm64`；Docker 会自动选择与主机匹配的版本。

### 1. 创建数据目录和映射文件

```bash
mkdir -p data/comic data/cover data/novels
printf '{}\n' > data/comic.json
```

`comic.json` 必须是有效 JSON；空库请使用 `{}`，不要只执行 `touch data/comic.json`。

### 2. 创建 `docker-compose.yml`

```yaml
services:
  mangadock:
    image: dddinmx/mangadock:v2.1
    container_name: MangaDock
    restart: unless-stopped
    ports:
      # 仅允许本机或反向代理访问；需要局域网直连时改为 "5001:5001"
      - "127.0.0.1:5001:5001"
    environment:
      TZ: Asia/Shanghai
      # 建议首次启动前设置：export MANGADOCK_ADMIN_PASSWORD='替换为强密码'
      MANGADOCK_ADMIN_PASSWORD: ${MANGADOCK_ADMIN_PASSWORD:-}
    volumes:
      - ./data/comic:/app/comic
      - ./data/cover:/app/static/cover
      - ./data/novels:/app/小说
      - ./data/comic.json:/app/comic.json
      - mangadock_instance:/app/instance

volumes:
  mangadock_instance:
```

漫画章节、封面、EPUB、来源映射、SQLite 数据库和会话均存放在数据卷中，不会被打进 Docker 镜像。

### 3. 启动

```bash
export MANGADOCK_ADMIN_PASSWORD='替换为强密码'
docker compose up -d
docker compose logs -f mangadock
```

访问 `http://127.0.0.1:5001`。管理员用户名为 `admin`；如果未设置 `MANGADOCK_ADMIN_PASSWORD`，首次启动时会在容器日志中生成并输出随机密码。

### 更新镜像

```bash
docker compose pull
docker compose up -d
```

## 📱 客户端

在 [Releases](https://github.com/dddinmx/MangaDock/releases) 下载当前可用的 Aidoku、Tachimanga 等客户端插件或安装包，并连接到上述 MangaDock 服务地址。

Android 上可尝试通过 Mihon 使用兼容的插件；兼容性以对应客户端的实际表现为准。

## ⚠️ 免责声明

- 本工具仅作学习、研究、交流使用，使用者应自行承担风险。
- 作者不对使用本工具导致的任何损失、法律纠纷或其他后果负责。
- 使用者应确保其使用方式符合所在地法律及第三方服务条款，并尊重版权与其他权利。

## 💬 交流

使用中遇到问题、希望添加功能或有改进建议，欢迎提交 [Issue](https://github.com/dddinmx/MangaDock/issues) 或发起 Discussion。
