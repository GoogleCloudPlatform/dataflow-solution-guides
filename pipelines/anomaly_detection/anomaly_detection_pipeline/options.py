"""Anomaly detection options; project is provided by GoogleCloudOptions."""
from apache_beam.options.pipeline_options import PipelineOptions


class MyPipelineOptions(PipelineOptions):

  @classmethod
  def _add_argparse_args(cls, parser):
    for name in ('messages_subscription', 'model_endpoint', 'location',
                 'responses_topic', 'error_topic', 'bigtable_instance',
                 'bigquery_table'):
      parser.add_argument('--' + name, required=True)
    parser.add_argument('--bigtable_table', default='customer_profiles')
    parser.add_argument('--bigtable_column_family', default='profile')
