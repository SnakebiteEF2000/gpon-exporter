# Official python alpine image, pinned to its multi-arch index digest (Dependabot keeps it current).
# Keep the CI test job (.github/workflows/build.yml) on the same Python minor version.
FROM python:3.15-alpine@sha256:20a82dbc2783112ddb91d1534603f802d222a3e63e593cd002eb2ac534e70211
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN adduser -D -u 10001 exporter
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt
COPY gpon_exporter.py .
USER 10001
EXPOSE 9617
ENTRYPOINT ["python", "/app/gpon_exporter.py"]
