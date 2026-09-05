FROM python:3.14-slim
WORKDIR /app
COPY training/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY anomaly_detection_pipeline/__init__.py anomaly_detection_pipeline/features.py /app/anomaly_detection_pipeline/
COPY training/train.py /app/train.py
ENTRYPOINT ["python", "/app/train.py"]
