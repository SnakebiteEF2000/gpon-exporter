# Official python alpine image, pinned to its multi-arch index digest (Dependabot keeps it current).
# Keep the CI test job (.github/workflows/build.yml) on the same Python minor version.
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
