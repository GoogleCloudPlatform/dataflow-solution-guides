import time

from apache_beam.options.pipeline_options import PipelineOptions, GoogleCloudOptions

from cdp_pipeline.options import MyPipelineOptions
from cdp_pipeline.customer_data_platform import create_pipeline


def main(options: MyPipelineOptions):
  pipeline = create_pipeline(options)
  pipeline.run()


if __name__ == "__main__":
  pipeline_options: PipelineOptions = PipelineOptions()
  dataflow_options: GoogleCloudOptions = pipeline_options.view_as(GoogleCloudOptions)
  now_epoch_ms = int(time.time()*1000)
  dataflow_options.job_name = f"customer-data-platform-{now_epoch_ms}"
  custom_options: MyPipelineOptions = pipeline_options.view_as(MyPipelineOptions)
  main(custom_options)