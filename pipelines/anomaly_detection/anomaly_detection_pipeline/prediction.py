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
