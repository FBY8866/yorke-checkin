# 约克超级品牌日打卡系统 - 容器镜像
FROM python:3.13-slim

WORKDIR /app

# 先装依赖（阿里云 PyPI 镜像加速）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/

# 安装中文字体，确保服务端照片水印的中文姓名/地址不乱码
# 容器内 Debian 官方源在国内不通，改用阿里云 Debian 镜像
RUN sed -i "s|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g" /etc/apt/sources.list /etc/apt/sources.list.d/*.sources 2>/dev/null; \
    apt-get update && apt-get install -y --no-install-recommends fonts-wqy-zenhei \
    && rm -rf /var/lib/apt/lists/*

# 复制应用代码
COPY . .

ENV DATA_DIR=/var/data/yorke \
    UPLOAD_DIR=/var/data/yorke/uploads \
    PORT=5000

EXPOSE 5000

CMD ["python", "app.py"]
