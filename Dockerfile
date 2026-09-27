FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    INBOX_DB_PATH=/data/inbox.db

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 收件库 SQLite 数据目录（Compose 挂载命名卷；命名卷首次创建时继承属主）
RUN mkdir -p /data && chown nobody:nogroup /data

EXPOSE 8080

USER nobody

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
