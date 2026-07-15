FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV DB_PATH=/data/feed_cache.db

RUN pip install --no-cache-dir requests beautifulsoup4 feedgen flask

COPY streator_fullfeed.py /app/streator_fullfeed.py

VOLUME ["/data"]

EXPOSE 9111

CMD ["python3", "/app/streator_fullfeed.py"]