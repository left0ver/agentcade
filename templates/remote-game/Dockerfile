# 默认用国内的镜像源：基础镜像从 DaoCloud 拉，Python 依赖从清华镜像装，GitHub 上的源码经 gh-proxy.com 下载。
# 镜像站不能用了就用 --build-arg 换一个；海外部署时换回官方源：
#   --build-arg PYTHON_IMAGE=python:3.11-slim --build-arg PIP_INDEX_URL=https://pypi.org/simple --build-arg GITHUB_PROXY=
ARG PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.11-slim
FROM ${PYTHON_IMAGE}

ARG PIP_INDEX_URL=https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple
ARG GITHUB_PROXY=https://gh-proxy.com/
WORKDIR /app
COPY requirements.txt .
# requirements.txt 里 GitHub 的地址前面加上镜像站，例如 https://gh-proxy.com/https://github.com/...
RUN sed "s#https://github.com/#${GITHUB_PROXY}https://github.com/#g" requirements.txt > /tmp/requirements.txt \
    && pip install --no-cache-dir --disable-pip-version-check --index-url "$PIP_INDEX_URL" -r /tmp/requirements.txt
COPY riddle ./riddle

# Hugging Face Space 用 UID 1000 运行容器。
RUN useradd --create-home --uid 1000 user
USER user
EXPOSE 7860
# 密钥从环境变量读取；PUBLIC_URL 不填时，在 Space 上自动用 https://$SPACE_HOST。
CMD ["agentcade-game", "serve", "riddle", "--host", "0.0.0.0", "--port", "7860"]
