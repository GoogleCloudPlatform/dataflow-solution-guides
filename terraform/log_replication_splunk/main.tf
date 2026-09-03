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
  pubsub_logging_topic     = "splunk-logs"
  pubsub_deadletter_topic  = "splunk-deadletter-topic"
  pubsub_sink_name         = "splunk-logging-sink"
  max_dataflow_workers     = 10
  zone                     = var.zone != null ? var.zone : "${var.region}-a"

  splunk_hec_token = var.deploy_demo_splunk ? "00000000-0000-0000-0000-000000000000" : var.splunk_token
  splunk_hec_url   = var.deploy_demo_splunk ? "http://${google_compute_instance.splunk_demo[0].network_interface[0].network_ip}:8088" : var.splunk_hec_url
}

// Dedicated Dataflow Worker Service Account
module "dataflow_sa" {
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/iam-service-account?ref=v58.0.0"
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
  source        = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/gcs?ref=v58.0.0"
  project_id    = var.project_id
  name          = local.bucket_name
  location      = var.region
  storage_class = "STANDARD"
  force_destroy = var.destroy_all_resources
}

data "google_project" "project" {
  project_id = var.project_id
}

// Pub/Sub topic to receive all logs for Splunk replication
module "logging_topic" {
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/pubsub?ref=v58.0.0"
  project_id = var.project_id
  name       = local.pubsub_logging_topic
  subscriptions = {
    splunk-logs-sub = {}
  }
}

// Pub/Sub topic to receive deadletter logs
module "deadletter_topic" {
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/pubsub?ref=v58.0.0"
  project_id = var.project_id
  name       = local.pubsub_deadletter_topic
  subscriptions = {
    splunk-deadletter-sub = {}
  }
}

// IAM publisher permission for Cloud Logging Project Service Agent on the logs topic
resource "google_pubsub_topic_iam_member" "pubsub_log_writer" {
  project = var.project_id
  topic   = module.logging_topic.id
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:service-${data.google_project.project.number}@gcp-sa-logging.iam.gserviceaccount.com"
}

// Cloud Logging sink in Pub/Sub
// Explicitly depends on the IAM permission and Pub/Sub topic so that:
// 1. On creation: Topic and IAM publisher permission exist before the sink starts routing.
// 2. On destruction: The sink is destroyed first, preventing 'topic_not_found' errors while deleting.
resource "google_logging_project_sink" "my_logging_sink" {
  name                   = local.pubsub_sink_name
  project                = var.project_id
  destination            = "pubsub.googleapis.com/${module.logging_topic.id}"
  unique_writer_identity = true

  depends_on = [
    google_pubsub_topic_iam_member.pubsub_log_writer
  ]
}

// Splunk HEC token in Secret Manager
module "splunk_token_secret" {
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/secret-manager?ref=v58.0.0"
  project_id = var.project_id
  secrets = {
    splunk-hec-token = {
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

// Firewall rule for demo Splunk VM (HEC on 8088 from Dataflow workers & Web UI on 8000 for IAP)
resource "google_compute_firewall" "allow_splunk_demo" {
  count       = var.deploy_demo_splunk ? 1 : 0
  name        = "allow-splunk-demo-internal"
  project     = length(regexall("projects/([^/]+)/", google_compute_instance.splunk_demo[0].network_interface[0].network)) > 0 ? regex("projects/([^/]+)/", google_compute_instance.splunk_demo[0].network_interface[0].network)[0] : var.project_id
  network     = google_compute_instance.splunk_demo[0].network_interface[0].network
  description = "Allow ingress traffic to Splunk HEC (8088) and Web UI (8000) for demo instance"

  allow {
    protocol = "tcp"
    ports    = ["8088", "8000"]
  }

  target_tags   = ["splunk-demo"]
  source_ranges = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "35.235.240.0/20"]
}

// Script with variables to launch the Dataflow jobs
resource "local_file" "variables_script" {
  filename        = "${path.module}/../../pipelines/log_replication_splunk/scripts/00_set_variables.sh"
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

export INPUT_SUBSCRIPTION=${module.logging_topic.subscriptions["splunk-logs-sub"].id}
export DEADLETTER_TOPIC=${module.deadletter_topic.id}
export TOKEN_SECRET_ID=${module.splunk_token_secret.version_ids["splunk-hec-token/v1"]}
export SPLUNK_HEC_URL=${local.splunk_hec_url}
export SPLUNK_DEMO_INSTANCE=${var.deploy_demo_splunk ? google_compute_instance.splunk_demo[0].name : ""}
FILE
}
