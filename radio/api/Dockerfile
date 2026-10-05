# syntax=docker/dockerfile:1

FROM alpine:3.24.1

LABEL org.opencontainers.image.title="music-radio-api"

RUN apk add --no-cache ca-certificates ffmpeg python3 py3-psycopg2 wget \
    && addgroup -S -g 10000 radio \
    && adduser -S -D -H -u 10002 -G radio radioapi \
    && install -d -o radioapi -g radio -m 0750 /app

COPY api/server.py /app/server.py
COPY api/migrations/ /app/migrations/
RUN chmod 0755 /app/server.py

USER radioapi
EXPOSE 8080
ENTRYPOINT ["python3", "/app/server.py"]
