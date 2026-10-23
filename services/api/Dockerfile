FROM python:3.12-slim
WORKDIR /app
COPY services/api/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY services/api/app.py /app/app.py
COPY config /app/config
COPY scripts /app/scripts
ENV APP_CONFIG=/app/config/app.yaml
ENV PYTHONPATH=/app
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
