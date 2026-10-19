FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY riddle ./riddle

# Hugging Face Space 用 UID 1000 运行容器。
RUN useradd --create-home --uid 1000 user
USER user
EXPOSE 7860
# 密钥从环境变量读取；PUBLIC_URL 不填时，在 Space 上自动用 https://$SPACE_HOST。
CMD ["agentcade-game", "serve", "riddle", "--host", "0.0.0.0", "--port", "7860"]
