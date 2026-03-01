FROM node:20-alpine AS node-builder
RUN npm install -g js-yaml iconv-lite && rm -rf /root/.npm /tmp/*

FROM python:3.12-alpine

WORKDIR /app

COPY requirements.txt ./
RUN apk add --no-cache libstdc++ && \
    pip install --no-cache-dir -r requirements.txt

COPY --from=node-builder /usr/local/bin/node /usr/local/bin/node
COPY --from=node-builder /usr/local/lib/node_modules /usr/local/lib/node_modules

ENV NODE_PATH=/usr/local/lib/node_modules

COPY . /app

EXPOSE 19527

ENV API_KEY=""
ENV GUNICORN_TIMEOUT=300

CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:19527 --timeout ${GUNICORN_TIMEOUT} app:app"]