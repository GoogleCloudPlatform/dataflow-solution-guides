# Create a virtual python env

python -m venv venv
source venv/bin/activate

# Install prerequisits

pip install -r requirements.txt

# Create a bigtable table and populate the data
python create_and_populate_bigtable.py

# (optional) Create data to publish in pubsub (if you want new set of data, otherwise use the file pubsub_sample.jsonl)
python create_pubsub_data.py

# Publish messages to pubsub topic
python publish.py