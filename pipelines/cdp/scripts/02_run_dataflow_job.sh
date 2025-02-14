python3 -m main \
  --streaming \
  --runner=DataflowRunner \
  --project=$PROJECT \
  --temp_location=gs://$PROJECT/tmp \
  --region=$REGION \
  --save_main_session \
  --service_account_email=$SERVICE_ACCOUNT \
  --subnetwork=$SUBNETWORK \
  --sdk_container_image=$CONTAINER_URI \
  --max_workers=$MAX_DATAFLOW_WORKERS \
  --disk_size_gb=$DISK_SIZE_GB \
  --machine_type=$MACHINE_TYPE \
  --transactions_topic=$TRANSACTIONS_TOPIC \
  --coupons_redemption_topic=$COUPON_REDEMPTION_TOPIC \
  --output_dataset=$BQ_DATASET \
  --output_table=$BQ_UNIFIED_TABLE \
  --project_id=$PROJECT \
  --enable_streaming_engine
