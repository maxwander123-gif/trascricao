FROM node:22-bookworm-slim AS javascript
FROM python:3.12-slim-bookworm
COPY --from=javascript /usr/local/bin/node /usr/local/bin/node
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 APP_MODE=cloud DATA_DIR=/data IMAGEIO_FFMPEG_EXE=/usr/bin/ffmpeg
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py core.py downloads.py editorial.py history.py deployment.py run.py ./
COPY static ./static
CMD ["python", "run.py"]
