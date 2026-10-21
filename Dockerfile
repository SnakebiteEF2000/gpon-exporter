# python:3.13-alpine (Python 3.13.16 on Alpine 3.24.2), pinned to its multi-arch index digest
FROM python:3.13-alpine@sha256:2d9aefe2fef018a7eb2c13064c89c71929800fd2e5dccdbf52ea5da5bb8d929a
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN adduser -D -u 10001 exporter
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt
COPY gpon_exporter.py .
USER 10001
EXPOSE 9617
ENTRYPOINT ["python", "/app/gpon_exporter.py"]
