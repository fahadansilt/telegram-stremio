FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/service/data
WORKDIR /service

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 service \
    && useradd --uid 10001 --gid 10001 --no-create-home service \
    && mkdir -p /service/data \
    && chown service:service /service/data
COPY --chown=service:service app ./app
USER service
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
