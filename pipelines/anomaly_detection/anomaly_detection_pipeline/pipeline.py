"""Enrich transactions, perform keyed inference and publish structured results."""
import json
import apache_beam as beam
from apache_beam.io.gcp.bigquery_tools import RetryStrategy
from apache_beam.metrics import Metrics
from apache_beam.ml.inference.base import KeyedModelHandler, RunInference
from apache_beam.options.pipeline_options import GoogleCloudOptions
from google.cloud import bigtable
from .features import detection, feature_vector, parse_transaction
from .inference import VertexHandler


def error_record(stage, value, error):
  if isinstance(value, bytes):
    value = value.decode('utf-8', errors='replace')
    try:
      value = json.loads(value)
    except ValueError:
      pass
  return json.dumps({
      'stage': stage,
      'input': value,
      'error': str(error)
  },
                    default=str).encode()


class Enrich(beam.DoFn):
  """Read the customer average; fail invalid or unknown customers to a side output."""

  def __init__(self, project, instance, table, family):
    super().__init__()
    self.project, self.instance, self.table_id, self.family = project, instance, table, family

  def setup(self):
    self.table = bigtable.Client(project=self.project).instance(
        self.instance).table(self.table_id)

  def process(self, element):
    raw = element
    Metrics.counter('anomaly', 'received').inc()
    try:
      value = parse_transaction(raw)
      row = self.table.read_row(value['customer_id'].encode())
      if row is None:
        raise ValueError('missing customer profile')
      average = row.cells[self.family][b'average_amount'][0].value.decode()
      vector = feature_vector(value, average)
      Metrics.counter('anomaly', 'enriched').inc()
      yield (json.dumps(value, sort_keys=True), vector)
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
      Metrics.counter('anomaly', 'invalid_or_missing_profile').inc()
      yield beam.pvalue.TaggedOutput('errors',
                                     error_record('enrichment', raw, exc))


def format_prediction(element, endpoint):
  key, result = element
  Metrics.counter('anomaly', 'predictions').inc()
  return detection(
      json.loads(key), list(result.example), result.inference, endpoint)


def inference_error(element):
  Metrics.counter('anomaly', 'inference_failures').inc()
  return error_record('inference', element,
                      'prediction failed after handler retries')


def bq_error(element):
  Metrics.counter('anomaly', 'bigquery_failures').inc()
  destination, row, errors = element
  return error_record('bigquery', row, {
      'destination': destination,
      'errors': errors
  })


def create_pipeline(options, pipeline=None):
  if pipeline is None:
    pipeline = beam.Pipeline(options=options)
  project = options.view_as(GoogleCloudOptions).project
  enriched = (
      pipeline | 'Read transactions' >>
      beam.io.ReadFromPubSub(subscription=options.messages_subscription)
      | 'Enrich' >> beam.ParDo(
          Enrich(project, options.bigtable_instance, options.bigtable_table,
                 options.bigtable_column_family)).with_outputs(
                     'errors', main='valid'))
  handler = KeyedModelHandler(
      VertexHandler(
          endpoint=options.model_endpoint,
          project=project,
          location=options.location))
  predictions, failures = enriched.valid | 'Predict' >> RunInference(
      handler).with_exception_handling()
  endpoint = (
      options.model_endpoint
      if options.model_endpoint.startswith('projects/') else
      f'projects/{project}/locations/{options.location}/endpoints/{options.model_endpoint}'
  )
  rows = predictions | 'Format' >> beam.Map(format_prediction, endpoint)
  rows | 'Encode detections' >> beam.Map(lambda row: json.dumps(row).encode(
  )) | 'Publish detections' >> beam.io.WriteToPubSub(options.responses_topic)
  written = rows | 'Archive' >> beam.io.WriteToBigQuery(
      options.bigquery_table,
      method=beam.io.WriteToBigQuery.Method.STREAMING_INSERTS,
      create_disposition=beam.io.BigQueryDisposition.CREATE_NEVER,
      write_disposition=beam.io.BigQueryDisposition.WRITE_APPEND,
      insert_retry_strategy=RetryStrategy.RETRY_ON_TRANSIENT_ERROR)
  inference_errors = failures.failed_inferences | 'Inference errors' >> beam.Map(
      inference_error)
  archive_errors = written.failed_rows_with_errors | 'Archive errors' >> beam.Map(
      bq_error)
  (enriched.errors, inference_errors,
   archive_errors) | 'Merge errors' >> beam.Flatten(
   ) | 'Publish errors' >> beam.io.WriteToPubSub(options.error_topic)
  return pipeline
