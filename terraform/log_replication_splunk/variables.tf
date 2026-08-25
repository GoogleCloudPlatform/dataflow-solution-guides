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

variable "zone" {
  description = "The GCP zone for demo resources (e.g. Splunk demo VM). Defaults to {region}-a if not specified."
  type        = string
  default     = null
}

variable "subnetwork" {
  description = "Optional subnetwork URL or path for Dataflow workers and demo VM (e.g. regions/europe-southwest1/subnetworks/dev-default or full URI). If omitted, the default network is used."
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
  default     = "splunk-replication-sa"
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

variable "deploy_demo_splunk" {
  description = "Whether to deploy an in-project Compute Engine VM running the official Splunk Enterprise container for demo and testing purposes."
  type        = bool
  default     = false
}

variable "splunk_hec_url" {
  description = "The URL of an external Splunk HEC endpoint (used when deploy_demo_splunk is false). E.g. https://http-inputs-my-domain.splunkcloud.com:8088"
  type        = string
  default     = "http://some-endpoint:8088"
}

variable "splunk_token" {
  description = "The token for the external Splunk HEC endpoint (used when deploy_demo_splunk is false). It will be stored in Secret Manager."
  type        = string
  default     = "WRITE_YOUR_TOKEN_HERE"
}

variable "splunk_admin_password" {
  description = "Admin password for the Splunk demo VM Web UI (used when deploy_demo_splunk is true)."
  type        = string
  default     = "SplunkDemoPass123!"
  sensitive   = true
}

