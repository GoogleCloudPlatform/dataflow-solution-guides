from argparse import ArgumentParser

from apache_beam.options.pipeline_options import PipelineOptions


class MyPipelineOptions(PipelineOptions):
  @classmethod
  def _add_argparse_args(cls, parser: ArgumentParser):
    parser.add_argument("--transactions_topic", type=str)
    parser.add_argument("--coupons_redemption_topic", type=str)
    parser.add_argument("--project_id", type=str)
    parser.add_argument("--location", type=str)
    parser.add_argument("--output_dataset", type=str)
    parser.add_argument("--output_table", type=str)