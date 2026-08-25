# 约克销售30天打卡系统 - 容器镜像
# 基于 Python 3.13，使用持久盘保存数据库与上传照片
FROM python:3.13-slim

WORKDIR /app

# 先装依赖（利用 Docker 层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY . .

# 持久盘挂载点：在 Render 中把磁盘挂到 /var/data/yorke
# 本地运行时这些环境变量不设置，自动回退到 ./data 和 ./static/uploads
ENV DATA_DIR=/var/data/yorke \
    UPLOAD_DIR=/var/data/yorke/uploads \
    PORT=5000

EXPOSE 5000

CMD ["python", "app.py"]
