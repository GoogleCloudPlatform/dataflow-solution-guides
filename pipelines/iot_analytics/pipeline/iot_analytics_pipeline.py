import apache_beam as beam
from apache_beam.options.pipeline_options import PipelineOptions
import argparse, json
from datetime import datetime, timedelta
import pickle
import logging
from transformations.aggregate_metrics import AggregateMetrics
from transformations.parse_timestamp import VehicleStateEvent
from transformations.trigger_inference import RunInference
from apache_beam.transforms.window import  FixedWindows
from apache_beam.transforms.trigger import  AccumulationMode, AfterWatermark
from typing import Any, Dict, Tuple

from apache_beam.transforms.enrichment import Enrichment
from apache_beam.transforms.enrichment_handlers.bigtable import BigTableEnrichmentHandler

def custom_join(left: Dict[str, Any], right: Dict[str, Any]):
    enriched = {}
    enriched['vehicle_id'] = left['vehicle_id']
    enriched['max_temperature'] = left['max_temperature']
    enriched['max_vibration'] = left['max_vibration']
    enriched['latest_timestamp'] = left['max_timestamp']
    enriched['avg_mileage'] = left['avg_mileage']
    enriched['last_service_date'] = right['maintenance']['last_service_date']
    enriched['maintenance_type'] = right['maintenance']['maintenance_type']
    enriched['model'] = right['maintenance']['model']
    logging.info(f" Enriched ====> {enriched}")
    return enriched


with open('transformations/maintenance_model.pkl', 'rb') as model_file:
    sklearn_model_handler = pickle.load(model_file)


def run(argv=None, save_main_session=True):
    parser = argparse.ArgumentParser()
    parser.add_argument('--subscription', dest='subscription', help='Provide pub/sub subscription name - "projects/your_project_id/subscriptions/subscription"')
    parser.add_argument('--project_id', dest='project_id', help='Enter BigQuery Project ID')
    parser.add_argument('--dataset', dest='dataset', help='Enter BigQuery Dataset Id')
    parser.add_argument('--table', dest='table', help='Enter BigQuery Table Id')
    parser.add_argument('--bigtable_instance_id', dest='bigtable_instance_id', help='Enter BigTable Instance Id')
    parser.add_argument('--bigtable_table_id', dest='bigtable_table_id', help='Enter BigTable Table Id')
    parser.add_argument('--row_key', dest='row_key', help='Enter BigTable row key')
    known_args, pipeline_args = parser.parse_known_args(argv)
    pipeline_args.append(f'--project={known_args.project_id}')
    # Define your pipeline options
    options = PipelineOptions(pipeline_args)
    options.view_as(beam.options.pipeline_options.StandardOptions).streaming = True
    bigtable_handler = BigTableEnrichmentHandler(project_id=known_args.project_id,
                                             instance_id=known_args.bigtable_instance_id,
                                             table_id=known_args.bigtable_table_id,
                                             row_key=known_args.row_key)
    BQ_SCHEMA = 'vehicle_id:STRING, max_temperature:INTEGER, max_vibration:FLOAT, latest_timestamp:TIMESTAMP, last_service_date:STRING, maintenance_type:STRING, model:STRING, needs_maintenance:INTEGER'
    with beam.Pipeline(options=options) as p:
        data = (
            p 
            | 'ReadFromPubSub' >> beam.io.ReadFromPubSub(subscription=known_args.subscription)
            | "Read JSON" >> beam.Map(json.loads)
            | 'Parse&EventTimestamp' >> beam.Map(VehicleStateEvent.convert_json_to_vehicleobj).with_output_types(VehicleStateEvent)
            | 'AddKeys' >> beam.WithKeys(lambda event: event.vehicle_id).with_output_types(Tuple[str,VehicleStateEvent])
            | 'Window' >> beam.WindowInto(FixedWindows(60 * 60), trigger=AfterWatermark(), accumulation_mode=AccumulationMode.ACCUMULATING)
            | 'AggregateMetrics' >> beam.ParDo(AggregateMetrics()).with_output_types(VehicleStateEvent).with_input_types(Tuple[str,VehicleStateEvent])
            | 'EnrichWithBigtable' >> Enrichment(bigtable_handler, join_fn=custom_join, timeout=10)
            | 'RunInference' >> beam.ParDo(RunInference(model=sklearn_model_handler))
            | 'WriteToBigQuery' >> beam.io.gcp.bigquery.WriteToBigQuery(
                method=beam.io.WriteToBigQuery.Method.STORAGE_WRITE_API,
                project=known_args.project_id,
                dataset=known_args.dataset,
                table=known_args.table,
                schema=BQ_SCHEMA,
                create_disposition=beam.io.BigQueryDisposition.CREATE_IF_NEEDED,
                write_disposition=beam.io.BigQueryDisposition.WRITE_APPEND
            )
        )


if __name__ == '__main__':
    logging.getLogger().setLevel(logging.INFO)
    run()