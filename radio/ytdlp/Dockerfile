# syntax=docker/dockerfile:1

FROM alpine:3.24.1

LABEL org.opencontainers.image.title="music-radio-worker"

RUN apk add --no-cache ca-certificates ffmpeg nodejs python3 yt-dlp \
    && addgroup -S -g 10000 radio \
    && adduser -S -D -H -u 10001 -G radio radio \
    && install -d -o radio -g radio -m 0750 /app

COPY ytdlp/worker.py /app/worker.py
RUN chmod 0755 /app/worker.py

USER radio
ENTRYPOINT ["python3", "/app/worker.py"]
