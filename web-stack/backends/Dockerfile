FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl libxml2-dev libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Heavy optional deps — fail soft if install issues
COPY requirements-heavy.txt .
RUN pip install --no-cache-dir -r requirements-heavy.txt || true

COPY app ./app

ENV PYTHONPATH=/app
EXPOSE 8791

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8791"]
