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

variable "project_id" {
  description = "Project ID of the existing GCP project where resources will be provisioned."
  type        = string
}

variable "region" {
  description = "The GCP region for application resources and Dataflow jobs."
  type        = string
}

variable "subnetwork" {
  description = "Optional subnetwork URL or path for Dataflow workers (e.g. regions/europe-southwest1/subnetworks/dev-default or full URI). If omitted, the default network is used."
  type        = string
  default     = null
}

variable "bucket_name" {
  description = "Optional GCS bucket name for Dataflow temp/staging files. Defaults to project_id if not specified."
  type        = string
  default     = null
}

variable "service_account_name" {
  description = "Name of the dedicated Dataflow worker service account to create."
  type        = string
  default     = "cdp-dataflow-sa"
}

variable "artifact_registry_name" {
  description = "Name of the Artifact Registry repository for custom Dataflow worker containers."
  type        = string
  default     = "cdp-containers"
}

variable "pubsub_transactions_topic" {
  description = "Name of the Pub/Sub topic for streaming customer transaction events."
  type        = string
  default     = "cdp-transactions"
}

variable "pubsub_coupon_redemption_topic" {
  description = "Name of the Pub/Sub topic for streaming coupon redemption events."
  type        = string
  default     = "cdp-coupon-redemption"
}

variable "create_bucket" {
  description = "Whether to create a new GCS bucket for temp/staging files. Set to false if using an existing bucket."
  type        = bool
  default     = false
}

variable "destroy_all_resources" {
  description = "Destroy all resources when calling tf destroy. Use false for production deployments. For test environments, set to true to remove all resources."
  type        = bool
  default     = true
}

variable "bq_dataset" {
  description = "The BigQuery output dataset name for customer data unification."
  type        = string
  default     = "cdp_dataset"
}

variable "bq_table" {
  description = "The BigQuery output table name for unified customer data."
  type        = string
  default     = "unified_customer_data"
}

variable "bq_sessions_table" {
  description = "The BigQuery output table name for sessionized customer 360 profiles."
  type        = string
  default     = "customer_sessions"
}

variable "bq_deadletter_table" {
  description = "The BigQuery dead-letter table name for malformed or rejected records."
  type        = string
  default     = "cdp_deadletter"
}
