#  Copyright 2026 The synthetic-llm-dataflow-bigquery Authors
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
  description = "The GCP region for Dataflow jobs, Cloud Build, Artifact Registry and the bucket. Needs NVIDIA L4 capacity (e.g. us-central1)."
  type        = string
}

variable "bq_location" {
  description = "BigQuery location of every dataset. Must match the public source data (bigquery-public-data.thelook_ecommerce lives in US)."
  type        = string
  default     = "US"
}

variable "subnetwork" {
  description = "Optional subnetwork URL or path for Dataflow workers (e.g. regions/us-central1/subnetworks/dev-default or full URI). If omitted, the default network is used. Workers use private IPs only, so the subnetwork needs Private Google Access."
  type        = string
  default     = null
}

variable "bucket_name" {
  description = "Optional GCS bucket name for model weights, Dataflow temp/staging files and the Flex Template spec. Defaults to project_id if not specified."
  type        = string
  default     = null
}

variable "create_bucket" {
  description = "Whether to create a new GCS bucket. Set to false if using an existing bucket."
  type        = bool
  default     = false
}

variable "service_account_name" {
  description = "Name of the dedicated Dataflow launcher and worker service account to create."
  type        = string
  default     = "synthetic-llm-dataflow-sa"
}

variable "build_service_account_name" {
  description = "Name of the dedicated Cloud Build service account that builds the image and stages the model weights."
  type        = string
  default     = "synthetic-llm-build-sa"
}

variable "model" {
  description = "LLM staged by scripts/02_stage_models.sh and used by the job: gemma4-e4b-it (Kaggle, needs the Gemma license accepted) or qwen3-4b (ModelScope, no credentials)."
  type        = string
  default     = "gemma4-e4b-it"
  validation {
    condition     = contains(["gemma4-e4b-it", "qwen3-4b"], var.model)
    error_message = "model must be gemma4-e4b-it or qwen3-4b."
  }
}

variable "machine_type" {
  description = "Dataflow GPU worker machine type."
  type        = string
  default     = "g2-standard-8"
}

variable "accelerator" {
  description = "Dataflow worker_accelerator experiment value (one NVIDIA L4 per worker)."
  type        = string
  default     = "type:nvidia-l4;count:1;install-nvidia-driver"
}

variable "num_rows" {
  description = "Rows generated for the root table (users); orders and order_items follow from the measured source fan-out."
  type        = number
  default     = 1000
}

variable "destroy_all_resources" {
  description = "Destroy all resources when calling tf destroy. Use false for production deployments. For test environments, set to true to remove all resources."
  type        = bool
  default     = true
}
