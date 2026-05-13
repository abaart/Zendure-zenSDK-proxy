FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV CONFIG_PATH=/app/config.yaml

WORKDIR /app

COPY pyproject.toml README.md ./
COPY zendure_proxy ./zendure_proxy

RUN pip install --no-cache-dir .

EXPOSE 1880

CMD ["sh", "-c", "python -m uvicorn zendure_proxy.server:app --host 0.0.0.0 --port ${PORT:-1880}"]
