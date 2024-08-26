
bq --location=us-central1 mk -d \
    --description "This is my cdp dataset." \
    --project_id $PROJECT \
    $BQ_CDP_DATASET

bq mk \
 --table \
 --description "This is my table" \
 --project_id $PROJECT \
 $BQ_CDP_DATASET.hh_demographic \
 ../schema/hh_demographic.json

bq load --skip_leading_rows=1 --project_id $PROJECT --source_format=CSV $BQ_CDP_DATASET${bq_data}.hh_demographic $GCS_BUCKET/input_data/hh_demographic.csv ../schema/hh_demographic.json

bq mk \
    --table \
    --description "This is my table" \
    --project_id $PROJECT \
    $BQ_CDP_DATASET.product \
    ../schema/product.json

bq load --skip_leading_rows=1 --project_id $PROJECT --source_format=CSV $BQ_CDP_DATASET$bq_data.product $GCS_BUCKET/input_data/product.csv ../schema/product.json

bq mk \
 --table \
 --description "This is my table" \
 --project_id $PROJECT \
 $BQ_CDP_DATASET.campaign_desc \
 ../schema/campaign_desc.json

bq load --skip_leading_rows=1 --project_id $PROJECT --source_format=CSV $BQ_CDP_DATASET${bq_data}.campaign_desc $GCS_BUCKET/input_data/campaign_desc.csv ../schema/campaign_desc.json

bq mk \
    --table \
    --description "This is my table" \
    --project_id $PROJECT \ 
    $BQ_CDP_DATASET.campaign \
    ../schema/campaign.json

bq load --skip_leading_rows=1 --project_id $PROJECT --source_format=CSV $BQ_CDP_DATASET$bq_data.campaign $GCS_BUCKET/input_data/campaign_table.csv ../schema/campaign.json

bq mk \
 --table \
 --description "This is my table" \
 --project_id $PROJECT \
 $BQ_CDP_DATASET.casual_data \
 ../schema/casual_data.json

bq load --skip_leading_rows=1 --project_id $PROJECT --source_format=CSV $BQ_CDP_DATASET${bq_data}.casual_data $GCS_BUCKET/input_data/casual_data.csv ../schema/casual_data.json

bq mk \
    --table \
    --description "This is my table" \
    --project_id $PROJECT \
    $BQ_CDP_DATASET.coupon \
    ../schema/coupon.json

bq load  --skip_leading_rows=1 --project_id $PROJECT --source_format=CSV $BQ_CDP_DATASET$bq_data.coupon $GCS_BUCKET/input_data/coupon.csv ../schema/coupon.json