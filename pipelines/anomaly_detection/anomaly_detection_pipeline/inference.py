"""Batched Vertex AI prediction with bounded retries and strict association."""
from apache_beam.ml.inference.base import PredictionResult, RemoteModelHandler
from google.api_core.exceptions import DeadlineExceeded, ServerError, TooManyRequests
from google.cloud import aiplatform_v1
from .prediction import predict_batch


def retryable(error):
  """Retry transient endpoint failures, but not invalid requests."""
  return isinstance(error, (DeadlineExceeded, ServerError, TooManyRequests))


class VertexHandler(RemoteModelHandler):
  """Use predict-only endpoint access; preserve every input in each batch."""

  def __init__(self, endpoint, project, location):
    super().__init__(
        namespace='anomaly_vertex', num_retries=3, retry_filter=retryable)
    self.endpoint = endpoint if endpoint.startswith('projects/') else (
        f'projects/{project}/locations/{location}/endpoints/{endpoint}')
    self.location = location

  def create_client(self):
    return aiplatform_v1.PredictionServiceClient(client_options={
        'api_endpoint': f'{self.location}-aiplatform.googleapis.com'
    })

  def batch_elements_kwargs(self):
    return {'min_batch_size': 1, 'max_batch_size': 32}

  def request(self, batch, model, inference_args=None):
    predictions = predict_batch(model, self.endpoint, batch)
    return [
        PredictionResult(vector, int(value), self.endpoint)
        for vector, value in zip(batch, predictions)
    ]
