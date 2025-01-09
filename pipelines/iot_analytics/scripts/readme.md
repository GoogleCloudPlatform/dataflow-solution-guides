# Create a virtual python env

python -m venv venv
source venv/bin/activate

# Install prerequisits
pip install -r requirements.txt


source scripts/00_set_environment.sh 
python scripts/01_create_data.py
python scripts/02_create_and_populate_bigtable.py
python scripts/03_publish_on_pubsub.py

python main.py \
--runner DataflowRunner \
--region us-central1 \
--project learnings-421714 \
--staging_location gs://learnings-421714/staging \
--temp_location gs://learnings-421714/temp \
--service_account_email dataflow-svc-account@learnings-421714.iam.gserviceaccount.com \
--project_id learnings-421714 \
--subscription projects/learnings-421714/subscriptions/messages-sub \
--dataset test \
--table maintenance_analytics \
--bigtable_instance_id iot-analytics \
--bigtable_table_id maintenance_data \
--row_key vehicle_id \
--streaming \
--setup_file ./setup.py