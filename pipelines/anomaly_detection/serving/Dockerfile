FROM python:3.14-slim
WORKDIR /app

COPY serving/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY serving/predict_server.py predict_server.py

ENV AIP_HTTP_PORT=8080
ENV AIP_HEALTH_ROUTE=/health
ENV AIP_PREDICT_ROUTE=/predict

EXPOSE 8080

ENTRYPOINT ["python", "predict_server.py"]
