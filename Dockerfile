FROM python:3.11-slim

WORKDIR /app

RUN pip install requests beautifulsoup4 feedgen flask

COPY streator_fullfeed.py /app/streator_fullfeed.py

EXPOSE 9111

CMD ["python3", "/app/streator_fullfeed.py"]
