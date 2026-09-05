# Application permissions are additive and scoped to the resources used.
resource "google_service_account" "training" {
  project      = var.project_id
  account_id   = var.training_service_account_name
  display_name = "Anomaly detection training"
  depends_on   = [google_project_service.application]
}

resource "google_storage_bucket_iam_member" "worker_objects" {
  bucket     = local.bucket_name
  role       = "roles/storage.objectAdmin"
  member     = module.dataflow_sa.iam_email
  depends_on = [module.buckets]
}

resource "google_storage_bucket_iam_member" "training_objects" {
  bucket     = local.bucket_name
  role       = "roles/storage.objectAdmin"
  member     = google_service_account.training.member
  depends_on = [module.buckets]
  condition {
    title       = "training_artifacts_only"
    description = "Training artifacts are isolated under anomaly-training/."
    expression  = "resource.name.startsWith('projects/_/buckets/${local.bucket_name}/objects/anomaly-training/')"
  }
}

resource "google_pubsub_topic" "errors" {
  project    = var.project_id
  name       = "anomaly-detection-errors"
  depends_on = [google_project_service.application]
}

resource "google_pubsub_subscription" "errors" {
  project = var.project_id
  name    = "anomaly-detection-errors-sub"
  topic   = google_pubsub_topic.errors.id
}

resource "google_pubsub_subscription_iam_member" "worker_input" {
  project      = var.project_id
  subscription = "anomaly-detection-transactions-sub"
  role         = "roles/pubsub.subscriber"
  member       = module.dataflow_sa.iam_email
  depends_on   = [module.input_topic]
}

resource "google_pubsub_topic_iam_member" "worker_output" {
  for_each   = toset(["anomaly-detection-detections", "anomaly-detection-errors"])
  project    = var.project_id
  topic      = each.key
  role       = "roles/pubsub.publisher"
  member     = module.dataflow_sa.iam_email
  depends_on = [module.output_topic, google_pubsub_topic.errors]
}

resource "google_bigtable_table_iam_member" "worker_features" {
  project       = var.project_id
  instance_name = local.bigtable_instance
  table         = "customer_profiles"
  role          = "roles/bigtable.reader"
  member        = module.dataflow_sa.iam_email
  depends_on    = [module.enrichment_table]
}

resource "google_bigquery_table" "detections" {
  project             = var.project_id
  dataset_id          = module.output_dataset.dataset_id
  table_id            = "detections"
  deletion_protection = !var.destroy_all_resources
  schema              = file("${path.module}/detections.schema.json")
  time_partitioning {
    type  = "DAY"
    field = "timestamp"
  }
  clustering = ["customer_id", "transaction_id"]
}

resource "google_bigquery_table_iam_member" "worker_detections" {
  project    = var.project_id
  dataset_id = module.output_dataset.dataset_id
  table_id   = google_bigquery_table.detections.table_id
  role       = "roles/bigquery.dataEditor"
  member     = module.dataflow_sa.iam_email
}

# The workflow binds this role on its endpoint. External endpoint owners must
# grant the same permission before launch; workers cannot create models/jobs.
resource "google_project_iam_custom_role" "predictor" {
  project     = var.project_id
  role_id     = "anomalyDetectionPredictor"
  title       = "Anomaly detection predictor"
  permissions = ["aiplatform.endpoints.predict"]
  depends_on  = [google_project_service.application]
}
