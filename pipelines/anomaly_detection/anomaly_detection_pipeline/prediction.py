"""Numeric Vertex request/response contract shared by verification and Beam."""
from google.cloud import aiplatform_v1
from google.protobuf import json_format, struct_pb2


def predict_batch(client, endpoint, batch):
  """Request one scalar label per vector, rejecting truncated or invalid batches."""
  request = aiplatform_v1.PredictRequest()
  raw_request = aiplatform_v1.PredictRequest.pb(request)
  raw_request.endpoint = endpoint
  raw_request.instances.extend(
      json_format.ParseDict(list(vector), struct_pb2.Value())
      for vector in batch)
  response = client.predict(request=request, retry=None, timeout=30)
  predictions = [
      json_format.MessageToDict(value)
      for value in aiplatform_v1.PredictResponse.pb(response).predictions
  ]
  if len(predictions) != len(batch):
    raise ValueError('endpoint returned a different number of predictions')
  if any(
      isinstance(value, bool) or value not in (-1, 1) for value in predictions):
    raise ValueError('endpoint predictions must be -1 or 1')
  return [int(value) for value in predictions]
