ARG BUILD_FROM=python:3.12-slim
FROM ${BUILD_FROM}

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV CONFIG_PATH=/config/zendure-proxy.yaml

WORKDIR /app

COPY pyproject.toml README.md ./
COPY zendure_proxy ./zendure_proxy
COPY home-assistant-addon/run.sh /run.sh

RUN pip install --no-cache-dir . && chmod +x /run.sh

CMD ["/run.sh"]

