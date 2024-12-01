# Stage 1: Builder
FROM python:3-alpine AS builder

WORKDIR /app

# 创建虚拟环境
RUN python3 -m venv venv
ENV VIRTUAL_ENV=/app/venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# 复制 requirements.txt 并安装依赖
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Stage 2: Runner
FROM python:3-alpine AS runner

WORKDIR /app

# 从 builder 阶段复制虚拟环境和依赖
COPY --from=builder /app/venv venv

# 复制应用代码
COPY main.py ./

# 设置环境变量
ENV VIRTUAL_ENV=/app/venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# 暴露端口
EXPOSE 8080

# 运行应用（使用 Flask 内建的服务器）
CMD ["python", "main.py"]