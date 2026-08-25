#!/usr/bin/env bash
# 约克超级品牌日打卡系统 - 阿里云一键部署脚本
# 适用：阿里云轻量应用服务器（Ubuntu 22.04 / Alibaba Cloud Linux 3，2核2G 足够）
# 用法：
#   bash deploy-aliyun.sh
# 说明：本脚本会安装 Docker、拉取 GitHub 代码、构建镜像并后台常驻运行。
# 部署完成后访问 http://<服务器公网IP>:5000
# 注意：需在阿里云控制台「防火墙」放行 TCP 5000 端口。
set -e

echo "==> [1/4] 检查并安装 Docker"
if command -v docker >/dev/null 2>&1; then
  echo "Docker 已安装: $(docker --version)"
else
  echo "正在安装 Docker（使用国内镜像加速）..."
  curl -fsSL https://get.daocloud.io/docker | bash
  systemctl enable docker
  systemctl start docker
  # 等待 docker 就绪
  for i in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 2; done
  echo "Docker 安装完成: $(docker --version)"
fi

echo "==> [2/4] 拉取最新代码"
cd /opt
if [ -d yorke-checkin ]; then
  cd yorke-checkin && git pull --ff-only
else
  git clone https://github.com/FBY8866/yorke-checkin.git
  cd yorke-checkin
fi

echo "==> [3/4] 构建并后台启动 (restart=unless-stopped)"
docker compose up -d --build

echo "==> [4/4] 等待启动并验证"
sleep 6
HTTP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 http://127.0.0.1:5000/ || echo 000)
echo "本地健康检查 HTTP=$HTTP"
if [ "$HTTP" = "200" ]; then
  echo "✅ 部署成功！现在打开 http://<你的服务器公网IP>:5000 即可访问"
  echo "   后台管理：/admin ，邀请码 yorke2026"
  echo "   查看日志：docker compose logs -f"
  echo "   重启服务：docker compose restart"
else
  echo "⚠️ 健康检查未返回 200，请查看日志：docker compose logs"
fi
