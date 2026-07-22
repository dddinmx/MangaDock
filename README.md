# 📚 棧-MangaDock
![Head diagram](https://github.com/user-attachments/assets/701fe952-a866-4e4a-8ded-10a5ade1b9fd)
<p align="center">
  <a href="https://github.com/dddinmx/bzmh-downloader"><img alt="Release" src="https://img.shields.io/badge/crawler-bule"></a>
  <a href="https://github.com/dddinmx/bzmh-downloader"><img alt="Release" src="https://img.shields.io/badge/python-3.8%2B-8A2BE2"></a>
  <a href="https://github.com/dddinmx/bzmh-downloader"><img alt="Release" src="https://img.shields.io/badge/Version-1.2-yellow"></a>
  <a href="https://github.com/dddinmx/hxs-downloader/"><img alt="GitHub Repo stars" src="https://img.shields.io/github/stars/dddinmx/bzmh-downloader?color=gree"></a>
</p>

**棧**（MangaDock）漫画阅读 / 下载器，基于 Python3 开发。   

**支持站点：**  
- [x] 漫小肆韓漫  
- [x] 包子漫画（baozimh.org)  
- [x] 包子漫画（cn.baozimhcn.com）

**如果本项目对你有帮助，欢迎点个 Star⭐ 支持！你的支持是我持续更新维护的动力🙏**

# 🖥️ 界面

## Web PWA  
<img width="2910" height="1961" alt="image" src="https://github.com/user-attachments/assets/2e247713-311a-4832-80b0-ae8b9691696b" />

## Tachimanga  
<img width="850" alt="img" src="https://github.com/user-attachments/assets/56501558-bd5e-4c7e-859a-65ab01c666da" />  

## Aidoku  
<img width="567" alt="img" src="https://github.com/user-attachments/assets/774ce995-a1c9-49d0-800a-34f60cc96848" />  

# 📖 使用方法

### 📝 Docker 部署步骤

#### 1. 保存 docker-compose.yml 文件到本地

```docker
services:
  mangadock:
    image: dddinmx/mangadock:v1.2
    container_name: MangaDock
    restart: unless-stopped
    ports:
      - "5001:5001"
    environment:
      TZ: Asia/Shanghai
      MANGADOCK_COOKIE_SECURE: "false"
    command:
      - python3
      - app.py
    volumes:
      - ./data/comic:/app/comic
      - ./data/cover:/app/static/cover
      - ./data/comic.json:/app/comic.json
      - mangadock_instance:/app/instance

volumes:
  mangadock_instance:
```

#### 2.创建映射文件

```
在本地创建好映射目录和comic.json文件

mkdir -p ./data/comic ./data/instance ./data/cover

touch ./data/comic.json  
```

#### 3.运行
```
docker compose up -d  （首次启动生成默认密码）
```
<img width="311" height="`180" alt="image" src="https://github.com/user-attachments/assets/e210a0b8-f4d0-4f2c-9676-8a9e97c29c6d" />

### 📝 iOS 客户端
```
Releases 下载对应的插件，支持 Aidoku 和 Tachimanga 软件；

Aidoku 下载 package.aix 导入安装；

Tachimanga 下载 tachiyomi-all.mangadock-v1.4.1-debug.apk 导入安装；

下载后在对应软件上安装插件并连接到上面使用 Docker 部署的服务地址
```

### 📝 Android 客户端
```
安卓的 Mihon 客户端不知道是否兼容 Tachimanga 的插件，使用安卓的可以尝试一下，我没有安卓手机无法测试。
```

# ⚠️ 免责声明

- 本工具仅作学习、研究、交流使用，使用本工具的用户应自行承担风险
- 作者不对使用本工具导致的任何损失、法律纠纷或其他后果负责
- 作者不对用户使用本工具的行为负责，包括但不限于用户违反法律或任何第三方权益的行为

# 💬 其他

任何使用中遇到的问题、任何希望添加的功能，都欢迎提交issue或开discussion交流，我会尽力解决。  


