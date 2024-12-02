# Flex Template

## Step1 - login to cloud shell and copy all the files

python -m venv venv
source venv/bin/activate

cd iot_analytics/pipeline

# Dataflow
pip install -r requirements.txt
pip install -e transformations

python iot_analytics_pipeline.py \
--project_id <project-id> \
--job_name iot-analytics \
--runner DataflowRunner \
--region <region> \
--staging_location gs://<bucket>/dataflow/staging \
--temp_location gs://<bucket>/dataflow/temp \
--service_account_email <service-account> \
--subscription projects/<project-id>/subscriptions/<subscription-id> \
--dataset <dataset-id> \
--table <table_id> \
--bigtable_instance_id <bigtable-instance-id> \
--bigtable_table_id <bigtable_table_id> \
--row_key <bigtable_row_key> \
--streaming \
--setup_file ./setup.py


# Push data to pubsub after launching pipeline

cd create_publish_and_model_files

python publish.py
