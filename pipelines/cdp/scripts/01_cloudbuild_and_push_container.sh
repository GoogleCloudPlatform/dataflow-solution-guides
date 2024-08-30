gcloud builds submit \
  --region=$REGION \
  --default-buckets-behavior=regional-user-owned-bucket \
  --substitutions _TAG=$CONTAINER_URI \
  .
