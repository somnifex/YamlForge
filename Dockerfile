FROM python:3.12-slim-bookworm

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN apt-get update && apt-get install -y \
    curl \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g js-yaml iconv-lite

COPY . /app

EXPOSE 19527

# 设置环境变量 API_KEY
ENV API_KEY=""
ENV GUNICORN_TIMEOUT=300

CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:19527 --timeout ${GUNICORN_TIMEOUT} app:app"]