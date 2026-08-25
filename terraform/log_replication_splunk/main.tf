#  Copyright 2025 Google LLC
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

locals {
  bucket_name              = var.bucket_name != null ? var.bucket_name : var.project_id
  dataflow_service_account = var.service_account_name != null ? var.service_account_name : "splunk-replication-sa"
  pubsub_logging_topic     = "all-logs"
  pubsub_deadletter_topic  = "deadletter-topic"
  pubsub_sink_name         = "pubsub-sink"
  max_dataflow_workers     = 10
  zone                     = var.zone != null ? var.zone : "${var.region}-a"

  splunk_hec_token = var.deploy_demo_splunk ? "00000000-0000-0000-0000-000000000000" : var.splunk_token
  splunk_hec_url   = var.deploy_demo_splunk ? "http://${google_compute_instance.splunk_demo[0].network_interface[0].network_ip}:8088" : var.splunk_hec_url
}

// Dedicated Dataflow Worker Service Account
module "dataflow_sa" {
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/iam-service-account?ref=v57.0.0"
  project_id = var.project_id
  name       = local.dataflow_service_account
  iam_project_roles = {
    (var.project_id) = [
      "roles/storage.admin",
      "roles/dataflow.worker",
      "roles/monitoring.metricWriter",
      "roles/pubsub.editor"
    ]
  }
}

// Optional GCS Bucket for staging & temp location
module "buckets" {
  count         = var.create_bucket ? 1 : 0
  source        = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/gcs?ref=v57.0.0"
  project_id    = var.project_id
  name          = local.bucket_name
  location      = var.region
  storage_class = "STANDARD"
  force_destroy = var.destroy_all_resources
}

// Pub/Sub topic to receive all logs
module "logging_topic" {
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/pubsub?ref=v57.0.0"
  project_id = var.project_id
  name       = local.pubsub_logging_topic
  subscriptions = {
    all-logs-sub = {}
  }
}

// Pub/Sub topic to receive deadletter logs
module "deadletter_topic" {
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/pubsub?ref=v57.0.0"
  project_id = var.project_id
  name       = local.pubsub_deadletter_topic
  subscriptions = {
    deadletter-sub = {}
  }
}

// Cloud Logging sink in Pub/Sub
resource "google_logging_project_sink" "my_logging_sink" {
  name                   = local.pubsub_sink_name
  project                = var.project_id
  destination            = "pubsub.googleapis.com/${module.logging_topic.id}"
  unique_writer_identity = true
}

resource "google_pubsub_topic_iam_member" "pubsub_log_writer" {
  project = var.project_id
  topic   = module.logging_topic.id
  role    = "roles/pubsub.publisher"
  member  = google_logging_project_sink.my_logging_sink.writer_identity
}

// Splunk token in Secret Manager
module "splunk_token_secret" {
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/secret-manager?ref=v57.0.0"
  project_id = var.project_id
  secrets = {
    splunk-token = {
      versions = {
        v1 = { enabled = true, data = local.splunk_hec_token }
      }
      iam = {
        "roles/secretmanager.secretAccessor" = [module.dataflow_sa.iam_email]
      }
    }
  }
}

// Optional Splunk Enterprise demo VM on GCP running official container
data "google_compute_image" "cos" {
  count   = var.deploy_demo_splunk ? 1 : 0
  family  = "cos-stable"
  project = "cos-cloud"
}

resource "google_compute_instance" "splunk_demo" {
  count        = var.deploy_demo_splunk ? 1 : 0
  name         = "splunk-demo"
  project      = var.project_id
  zone         = local.zone
  machine_type = "e2-standard-2"

  tags = ["splunk-demo", "http-server", "https-server"]

  boot_disk {
    initialize_params {
      image = data.google_compute_image.cos[0].self_link
      size  = 30
      type  = "pd-balanced"
    }
  }

  network_interface {
    network    = var.subnetwork != null ? null : "default"
    subnetwork = var.subnetwork != null ? var.subnetwork : null
  }

  metadata = {
    startup-script = <<-EOT
      #!/bin/bash
      docker run -d \
        --name=splunk \
        --restart=always \
        -p 8000:8000 \
        -p 8088:8088 \
        -e "SPLUNK_GENERAL_TERMS=--accept-sgt-current-at-splunk-com" \
        -e "SPLUNK_START_ARGS=--accept-license" \
        -e "SPLUNK_PASSWORD=${var.splunk_admin_password}" \
        -e "SPLUNK_HEC_TOKEN=${local.splunk_hec_token}" \
        -e "SPLUNK_HEC_SSL=false" \
        -e "SPLUNK_HEC_ENABLE=true" \
        splunk/splunk:latest
    EOT
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  service_account {
    scopes = ["cloud-platform"]
  }
}

// Script with variables to launch the Dataflow jobs
resource "local_file" "variables_script" {
  filename        = "${path.module}/../../pipelines/log_replication_splunk/scripts/01_set_variables.sh"
  file_permission = "0644"
  content         = <<FILE
# This file is generated by the Terraform code of this Solution Guide.
# We recommend that you modify this file only through the Terraform deployment.
export PROJECT=${var.project_id}
export REGION=${var.region}
export ZONE=${local.zone}
export SUBNETWORK=${var.subnetwork != null ? var.subnetwork : ""}
export NETWORK=$${SUBNETWORK}
export TEMP_LOCATION=gs://${local.bucket_name}/tmp
export SERVICE_ACCOUNT=${module.dataflow_sa.email}

export MAX_DATAFLOW_WORKERS=${local.max_dataflow_workers}

export INPUT_SUBSCRIPTION=${module.logging_topic.subscriptions["all-logs-sub"].id}
export DEADLETTER_TOPIC=${module.deadletter_topic.id}
export TOKEN_SECRET_ID=${module.splunk_token_secret.version_ids["splunk-token/v1"]}
export SPLUNK_HEC_URL=${local.splunk_hec_url}
export SPLUNK_DEMO_INSTANCE=${var.deploy_demo_splunk ? google_compute_instance.splunk_demo[0].name : ""}
FILE
}
