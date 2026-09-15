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
  description = "The GCP region for Dataflow jobs, Cloud Build, Artifact Registry and the bucket. Needs capacity for the chosen gpu (e.g. us-central1)."
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
  description = "LLM staged by scripts/02_stage_models.sh and used by the job: gemma4-e4b-it (google/gemma-4-E4B-it) or qwen3-4b (Qwen/Qwen3-4B-Instruct-2507). Both are Apache-2.0 open weights."
  type        = string
  default     = "gemma4-e4b-it"
  validation {
    condition     = contains(["gemma4-e4b-it", "qwen3-4b"], var.model)
    error_message = "model must be gemma4-e4b-it or qwen3-4b."
  }
}

variable "model_source" {
  description = "Where scripts/02_stage_models.sh downloads the weights from, once, before copying them to the bucket: huggingface (Hugging Face Hub) or modelscope (a mirror with the same repository ids). Workers only read GCS."
  type        = string
  default     = "huggingface"
  validation {
    condition     = contains(["huggingface", "modelscope"], var.model_source)
    error_message = "model_source must be huggingface or modelscope."
  }
}

variable "gpu" {
  description = "One GPU per worker: l4 (NVIDIA L4 on G2 machines, vLLM dtype auto, both models) or t4 (NVIDIA T4 on N1 machines, vLLM dtype float16, qwen3-4b only)."
  type        = string
  default     = "l4"
  validation {
    condition     = contains(["l4", "t4"], var.gpu)
    error_message = "gpu must be l4 or t4."
  }
}

variable "machine_type" {
  description = "Dataflow GPU worker machine type, from the machine family of the gpu (g2-* for l4, n1-* for t4). Defaults to g2-standard-8 or n1-standard-8."
  type        = string
  default     = null
}

variable "num_rows" {
  description = "Rows generated for the root table (users); orders and order_items follow from the measured source fan-out."
  type        = number
  default     = 1000
}

variable "launch_job" {
  description = "Launch the generation job from Terraform (google_dataflow_flex_template_job) instead of scripts/04_run_dataflow.sh. Needs the image, the weights and the template spec first (scripts 01-03). A finished batch job is launched again on the next apply while this stays true."
  type        = bool
  default     = false
}

variable "destroy_all_resources" {
  description = "Destroy all resources when calling tf destroy. Use false for production deployments. For test environments, set to true to remove all resources."
  type        = bool
  default     = true
}
