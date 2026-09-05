#  Copyright 2026 Google LLC
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
"""Vertex AI custom prediction server for anomaly detection on Python 3.14.

Implements the Vertex AI HTTP prediction contract:
- Health check route (default: /health)
- Prediction route (default: /predict)
"""
import contextlib
import logging
import os
from pathlib import Path
import tempfile

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from google.cloud import storage
import joblib
import uvicorn

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Model state
MODEL = None


def load_model(storage_uri: str = None):
  """Load model.joblib from Cloud Storage or local filesystem."""
  global MODEL  # pylint: disable=global-statement
  uri = storage_uri or os.environ.get("AIP_STORAGE_URI") or os.environ.get(
      "MODEL_DIR", "/model")
  logger.info("Loading model artifact from: %s", uri)

  if uri.startswith("gs://"):
    bucket_name, _, prefix = uri[5:].partition("/")
    client = storage.Client()
    bucket = client.bucket(bucket_name)

    temp_dir = tempfile.mkdtemp(prefix="vertex_model_")
    local_path = Path(temp_dir) / "model.joblib"

    # Download model.joblib
    blob_path = prefix.rstrip("/") + "/model.joblib"
    blob = bucket.blob(blob_path)
    if not blob.exists():
      raise FileNotFoundError(
          f"model.joblib not found at gs://{bucket_name}/{blob_path}")
    blob.download_to_filename(str(local_path))
    MODEL = joblib.load(str(local_path))
    logger.info("Successfully loaded model from GCS: gs://%s/%s", bucket_name,
                blob_path)
  else:
    path = Path(uri)
    if path.is_dir():
      model_file = path / "model.joblib"
    else:
      model_file = path
    if not model_file.exists():
      raise FileNotFoundError(f"Model file not found at: {model_file}")
    MODEL = joblib.load(str(model_file))
    logger.info("Successfully loaded model from local path: %s", model_file)

  return MODEL


@contextlib.asynccontextmanager
async def lifespan(application: FastAPI):
  """FastAPI lifespan context manager for startup and shutdown."""
  del application
  try:
    load_model()
  except Exception as exc:  # pylint: disable=broad-exception-caught
    logger.warning("Initial model loading deferred: %s", exc)
  yield


app = FastAPI(title="Anomaly Detection Predictor", lifespan=lifespan)

health_route = os.environ.get("AIP_HEALTH_ROUTE", "/health")
predict_route = os.environ.get("AIP_PREDICT_ROUTE", "/predict")


@app.get(health_route)
@app.get("/ping")
@app.get("/healthz")
async def health():
  """Health check endpoint for Vertex AI load balancer and probes."""
  if MODEL is None:
    return JSONResponse(status_code=503, content={"status": "model_not_loaded"})
  return {"status": "healthy"}


@app.post(predict_route)
async def predict(request: Request):
  """Vertex AI prediction endpoint.

  Expects: {"instances": [[f1, f2, f3], ...]}
  Returns: {"predictions": [-1, 1, ...]}
  """
  if MODEL is None:
    raise HTTPException(status_code=503, detail="Model is not loaded")

  try:
    body = await request.json()
  except Exception as exc:
    raise HTTPException(
        status_code=400, detail=f"Invalid JSON payload: {exc}") from exc

  if not isinstance(body, dict) or "instances" not in body:
    raise HTTPException(
        status_code=400, detail="Missing 'instances' field in request body")

  instances = body["instances"]
  if not isinstance(instances, list) or not instances:
    raise HTTPException(
        status_code=400, detail="'instances' must be a non-empty list")

  try:
    raw_predictions = MODEL.predict(instances)
    predictions = [int(p) for p in raw_predictions]
    return {"predictions": predictions}
  except Exception as exc:
    logger.error("Inference failure: %s", exc)
    raise HTTPException(
        status_code=500, detail=f"Inference error: {exc}") from exc


def main():
  port = int(os.environ.get("AIP_HTTP_PORT", "8080"))
  logger.info("Starting anomaly detection prediction server on 0.0.0.0:%d",
              port)
  uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
  main()
