# apps/api + worker 共用镜像（§4.2 / §14.2）
# 构建上下文：仓库根目录
#   docker build -f apps/api/Dockerfile -t skillrouter-api:0.1.0 .
# 与 deploy/Dockerfile.api 内容一致；Docker Compose 使用 deploy/Dockerfile.api，
# 本文件供「单服务直接构建」与 CI 镜像校验使用——两者修改必须同步（CI 有 diff 检查）。
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 只装运行期系统依赖（无编译器残留）
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY apps/api/requirements.txt /app/requirements.txt
RUN pip install --require-hashes=false -r /app/requirements.txt

COPY apps/api /app
COPY scripts /app/scripts
COPY db /app/db

# 非 root 运行
RUN useradd --create-home --uid 10001 skillrouter \
 && mkdir -p /data \
 && chown -R skillrouter:skillrouter /app /data
USER skillrouter

ENV SKILLROUTER_ENV=production \
    SKILLROUTER_DATA_DIR=/data \
    SKILLROUTER_DB_PATH=/data/skillrouter.db

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

# api 与 worker 共用镜像，入口命令由 compose 覆盖（worker：python -m skillrouter_api.worker）
CMD ["uvicorn", "skillrouter_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
