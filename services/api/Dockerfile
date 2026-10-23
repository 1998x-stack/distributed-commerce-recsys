FROM python:3.12-slim
WORKDIR /app
COPY services/api/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY services/api/app.py services/api/operational.py /app/
COPY config /app/config
COPY scripts /app/scripts
ENV APP_CONFIG=/app/config/app.yaml
ENV PYTHONPATH=/app
ENV PYTHONDONTWRITEBYTECODE=1
RUN useradd --system --uid 10001 --home-dir /app appuser \
    && mkdir -p /app/data && chown -R 10001:10001 /app/data
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
