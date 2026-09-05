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
"""DirectRunner tests with cloud services replaced by deterministic fakes."""
import json
from types import SimpleNamespace
import unittest
from unittest import mock
import apache_beam as beam
from apache_beam.io.gcp.bigquery import WriteResult
from apache_beam.ml.inference.base import KeyedModelHandler, ModelHandler, PredictionResult, RunInference
from apache_beam.testing.util import assert_that, equal_to
from google.cloud import aiplatform_v1
from google.protobuf import json_format, struct_pb2
from anomaly_detection_pipeline import pipeline as module
from anomaly_detection_pipeline.features import feature_vector, profiles, transaction
from anomaly_detection_pipeline.inference import VertexHandler
from anomaly_detection_pipeline.pipeline import Enrich, bq_error, format_prediction, inference_error


class FakeHandler(ModelHandler):
  """Model stub that handles multiple records in each inference call."""

  def load_model(self):
    return None

  def run_inference(self, batch, model, inference_args=None):
    del model, inference_args
    return [
        PredictionResult(value, -1 if value[0] > 4 else 1) for value in batch
    ]

  def batch_elements_kwargs(self):
    return {'min_batch_size': 4, 'max_batch_size': 4}


class FailedHandler(FakeHandler):
  """Represent an endpoint failure after retries have been exhausted."""

  def run_inference(self, batch, model, inference_args=None):
    del batch, model, inference_args
    raise ValueError('endpoint failure')


class FakeEnrich(Enrich):
  """Provide a customer row without any cloud RPC."""

  def setup(self):
    average = profiles()[0]['average_amount']
    row = SimpleNamespace(
        cells={
            'profile': {
                b'average_amount':
                    [SimpleNamespace(value=str(average).encode())]
            }
        })
    self.table = SimpleNamespace(
        read_row=lambda key: row if key == b'customer-0000' else None)


class PipelineTest(unittest.TestCase):
  """Verify enrichment, keyed association and failure side outputs."""

  def test_enrichment_errors_directrunner(self):
    value = transaction(profiles()[0], 0)
    unknown = value | {'customer_id': 'missing'}
    with beam.Pipeline() as pipeline:
      output = (
          pipeline | beam.Create(
              [json.dumps(value).encode(), b'{}',
               json.dumps(unknown).encode()])
          | beam.ParDo(FakeEnrich('p', 'i', 'customer_profiles',
                                  'profile')).with_outputs(
                                      'errors', main='valid'))
      assert_that(
          output.valid
          | beam.Map(lambda item: json.loads(item[0])['transaction_id']),
          equal_to([value['transaction_id']]))
      assert_that(
          output.errors | beam.Map(lambda item: json.loads(item)['stage']),
          equal_to(['enrichment', 'enrichment']),
          label='errors')

  def test_keyed_batches_preserve_association(self):
    profile = profiles()[0]
    values = [
        transaction(profile, index, anomalous=index % 2 == 0)
        for index in range(8)
    ]
    inputs = [(json.dumps(value),
               feature_vector(value, profile['average_amount']))
              for value in values]
    expected = [(value['transaction_id'], -1 if index % 2 == 0 else 1)
                for index, value in enumerate(values)]
    with beam.Pipeline() as pipeline:
      output = (
          pipeline | beam.Create(inputs)
          | RunInference(KeyedModelHandler(FakeHandler()))
          | beam.Map(format_prediction, 'endpoint')
          | beam.Map(lambda row: (row['transaction_id'], row['prediction'])))
      assert_that(output, equal_to(expected))

  def test_inference_failures_directrunner(self):
    with beam.Pipeline() as pipeline:
      _, failures = (
          pipeline | beam.Create([('key', [1., 2., 3.])])
          | RunInference(KeyedModelHandler(
              FailedHandler())).with_exception_handling())
      errors = failures.failed_inferences | beam.Map(inference_error)
      assert_that(errors | beam.Map(lambda value: json.loads(value)['stage']),
                  equal_to(['inference']))

  def test_permanent_archive_error_encoding(self):
    row = {'transaction_id': 't1'}
    result = json.loads(bq_error(('p:d.t', row, [{'reason': 'invalid'}])))
    self.assertEqual(result['stage'], 'bigquery')
    self.assertEqual(result['input'], row)

  def test_vertex_prediction_association_and_validation(self):
    handler = VertexHandler('1', 'p', 'r')
    client = mock.Mock()
    inputs = [[1., 2., 3.], [9., 8., 7.]]

    def response(values):
      result = aiplatform_v1.PredictResponse()
      aiplatform_v1.PredictResponse.pb(result).predictions.extend(
          json_format.ParseDict(value, struct_pb2.Value()) for value in values)
      return result

    client.predict.return_value = response([1, -1])
    result = handler.request(inputs, client)
    self.assertEqual([(item.example, item.inference) for item in result],
                     [(inputs[0], 1), (inputs[1], -1)])
    self.assertEqual(client.predict.call_args.kwargs['timeout'], 30)
    for values in ([1], [True, -1], [1.5, -1]):
      client.predict.return_value = response(values)
      with self.assertRaises(ValueError):
        handler.request(inputs, client)


class FakeArchive(beam.PTransform):
  """Validate archived rows and simulate one permanent insertion failure."""

  def expand(self, input_or_inputs):
    rows = input_or_inputs
    assert_that(
        rows | 'Archive IDs' >> beam.Map(lambda row: row['transaction_id']),
        equal_to(['transaction-42-0']),
        label='Archive rows')
    failures = rows | 'Fake insert error' >> beam.Map(
        lambda row: ('p:d.t', row, ['invalid']))
    return WriteResult(
        method=beam.io.WriteToBigQuery.Method.STREAMING_INSERTS,
        failed_rows_with_errors=failures)


class FakePublish(beam.PTransform):
  """Assert the real graph publishes equivalent detections and both error paths."""

  def __init__(self, topic):
    super().__init__()
    self.topic = topic

  def expand(self, input_or_inputs):
    values = input_or_inputs
    if self.topic.endswith('/anomaly-detection-detections'):
      rows = values | beam.Map(json.loads)
      assert_that(
          rows
          | beam.Map(lambda row: (row['transaction_id'], row['is_anomaly'])),
          equal_to([('transaction-42-0', False)]))
    else:
      assert_that(values | beam.Map(lambda value: json.loads(value)['stage']),
                  equal_to(['enrichment', 'bigquery']))
    return values


class GraphTest(unittest.TestCase):
  """Construct and execute the production graph with only cloud IO replaced."""

  def test_complete_graph(self):
    options = SimpleNamespace(
        view_as=lambda cls: SimpleNamespace(project='test-project'),
        messages_subscription=('projects/test-project/subscriptions/'
                               'anomaly-detection-transactions-sub'),
        bigtable_instance='instance',
        bigtable_table='customer_profiles',
        bigtable_column_family='profile',
        model_endpoint='123',
        location='us-central1',
        responses_topic='projects/test-project/topics/anomaly-detection-detections',
        error_topic='projects/test-project/topics/anomaly-detection-errors',
        bigquery_table='test-project:d.t')
    inputs = [json.dumps(transaction(profiles()[0], 0)).encode(), b'{}']
    write_method = beam.io.WriteToBigQuery.Method
    with mock.patch.object(module, 'Enrich', FakeEnrich), mock.patch.object(
        module, 'VertexHandler', return_value=FakeHandler()), mock.patch.object(
            beam.io, 'ReadFromPubSub',
            return_value=beam.Create(inputs)), mock.patch.object(
                beam.io, 'WriteToPubSub',
                side_effect=FakePublish), mock.patch.object(
                    beam.io, 'WriteToBigQuery',
                    wraps=beam.io.WriteToBigQuery) as archive:
      archive.Method = write_method
      archive.side_effect = lambda *args, **kwargs: FakeArchive()
      with beam.Pipeline() as pipeline:
        module.create_pipeline(options, pipeline=pipeline)
