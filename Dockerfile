FROM python:3.13-slim
LABEL org.opencontainers.image.title=captionizerr
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd -u 1000 -M -d /config captionizerr
USER 1000:1000
EXPOSE 8095
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8095/health',timeout=4)"
CMD ["captionizerr", "serve", "--port", "8095"]
