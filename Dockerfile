# One image for the pipeline jobs (fill, update, status). docker-compose.yml picks the job with `command:`
# (python fill.py or python update.py). The pinned versions in requirements.txt are the ones
# the tests were run against (pandas 3 needs Python 3.11+).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# .env is never baked into the image: docker compose passes it in at run time.
CMD ["python", "fill.py"]
