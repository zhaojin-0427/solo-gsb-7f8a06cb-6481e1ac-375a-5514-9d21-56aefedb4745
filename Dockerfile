# 同时包含 Python 3（FastAPI）与 GCC（容器内构建桥接）
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gcc \
        libc6-dev \
        binutils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir -r /srv/requirements.txt

COPY app /srv/app

# 非 root 运行（临时编译目录落在 /tmp）
RUN useradd --create-home --uid 10001 bridge \
    && chown -R bridge:bridge /srv
USER bridge

EXPOSE 8080

CMD ["python", "-m", "uvicorn", "app.service:app", "--host", "0.0.0.0", "--port", "8080"]
