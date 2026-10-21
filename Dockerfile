# python:3.13-alpine (Python 3.13.16 on Alpine 3.24.2), pinned to its multi-arch index digest
FROM python:3.15-alpine@sha256:2699b90f4d562a22e2dd076c82f964032f64814204bff36d271a54ba4cc2c7e2
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN adduser -D -u 10001 exporter
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt
COPY gpon_exporter.py .
USER 10001
EXPOSE 9617
ENTRYPOINT ["python", "/app/gpon_exporter.py"]
