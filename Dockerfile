FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt && groupadd --gid 10001 chronicle && useradd --uid 10001 --gid 10001 --no-create-home chronicle
COPY *.py entrypoint.sh /app/
COPY scripts /app/scripts
RUN mkdir -p /app/data && chown chronicle:chronicle /app/data && chmod 755 /app/entrypoint.sh
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOST=0.0.0.0 PORT=8000 MEMORY_DB_PATH=/app/data/memories.sqlite3
# Bootstrap starts as root solely to set data-volume ownership, then execs as UID/GID 10001.
VOLUME ["/app/data"]
EXPOSE 8000
ENTRYPOINT ["/app/entrypoint.sh"]
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8000')+'/health',timeout=2)" || exit 1
