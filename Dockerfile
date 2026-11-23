FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* && pip install --no-cache-dir -U yt-dlp
WORKDIR /app
COPY skills/clip/clipper.py skills/clip/clipper.py
COPY server/app.py server/app.py
ENV PORT=8000
EXPOSE 8000
CMD ["python3", "server/app.py"]
