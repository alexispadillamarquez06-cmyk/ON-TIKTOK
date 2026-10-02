FROM python:3.11-slim

# ffmpeg (trae ffprobe) para leer resolución/fps; git para instalar el repo del descargador
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && (pip install --no-cache-dir git+https://github.com/krypton-byte/tiktok-downloader \
        || pip install --no-cache-dir tiktok_downloader)

# Muestra en los logs de build qué servicios trae el paquete (para confirmar que existe ttdownloader)
RUN python -c "import tiktok_downloader as td; print('servicios disponibles:', [n for n in dir(td) if not n.startswith('_')])" \
    || echo "AVISO: no se pudo importar tiktok_downloader"

COPY main.py .

ENV PORT=8000
EXPOSE 8000

# 1 solo worker: el límite de descargas simultáneas vive dentro del proceso
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
