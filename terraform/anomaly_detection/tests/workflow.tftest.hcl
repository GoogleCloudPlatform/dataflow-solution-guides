mock_provider "google" {}
mock_provider "google-beta" {}
mock_provider "local" {}

variables {
  project_id = "anomaly-test-project"
  region     = "us-central1"
}

run "application_contract" {
  command = plan

  assert {
    condition     = local.machine_type == "n1-standard-2"
    error_message = "The inference client must use a CPU worker."
  }
  assert {
    condition     = google_bigtable_table_iam_member.worker_features.table == "customer_profiles" && google_bigtable_table_iam_member.worker_features.role == "roles/bigtable.reader"
    error_message = "Worker enrichment permissions must be table scoped and read only."
  }
  assert {
    condition     = google_project_iam_custom_role.predictor.permissions == toset(["aiplatform.endpoints.predict"])
    error_message = "Workers may only predict, never train or deploy."
  }
  assert {
    condition     = google_bigquery_table.detections.time_partitioning[0].field == "timestamp"
    error_message = "Archive partitioning must use transaction time."
  }
  assert {
    condition     = length(module.buckets) == 0
    error_message = "Existing bucket reuse is the default."
  }
  assert {
    condition     = google_storage_bucket_iam_member.training_objects.condition[0].expression == "resource.name.startsWith('projects/_/buckets/anomaly-test-project/objects/anomaly-training/')"
    error_message = "Training writes must stay within the training artifact prefix."
  }
}
