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

locals {
  public_dataset = "bigquery-public-data.thelook_ecommerce"
  source_fqn     = "${var.project_id}.${local.source_dataset}"
  landing_fqn    = "${var.project_id}.${local.landing_dataset}"

  # Tables the pipeline writes besides the landing tables: one per schema file
  # in the pipeline's config/bq_schema/<dataset>/<table>.schema.json, with the
  # same dataset, name and schema.
  schema_dir = "${local.pipeline_dir}/config/bq_schema"
  pipeline_tables = {
    for f in fileset(local.schema_dir, "*/*.schema.json") :
    trimsuffix(f, ".schema.json") => {
      dataset = dirname(f)
      table   = trimsuffix(basename(f), ".schema.json")
      schema  = file("${local.schema_dir}/${f}")
    }
  }
  pipeline_datasets = toset([for t in local.pipeline_tables : t.dataset])

  # DAY partitioning, as docs/DEPLOYMENT_PREREQUISITES.md specifies; every
  # other pipeline table is unpartitioned.
  partition_fields = {
    "synthetic_data_quality/dlq"             = "dlq_inserted_at"
    "synthetic_data_quality/validation_runs" = "created_at"
    "synthetic_rag/rag_chunks"               = "created_at"
  }

  # The tables config/relationships/gcp_public_fk_example.yaml generates.
  generated_tables = ["users", "orders", "order_items"]
  landing_sql = join("\n", [for t in local.generated_tables :
    "CREATE TABLE IF NOT EXISTS `${local.landing_fqn}.${t}`\nLIKE `${local.public_dataset}.${t}`;"
  ])

  # One script, run once at apply time:
  #  - products: the catalog order_items.product_id draws from, copied into
  #    the landing dataset where the launch reads already-landed parents;
  #  - source snapshots with the exact public schemas, filtered so every
  #    child row has its parent; GEOGRAPHY values are nulled because the
  #    generator does not synthesize WKT, and an all-NULL column generates
  #    NULLs;
  #  - empty landing tables with the same names and schemas as the public
  #    tables, which the job loads into (create_if_not_exists=false).
  thelook_sql = <<-SQL
    CREATE OR REPLACE TABLE `${local.landing_fqn}.products` AS
    SELECT * FROM `${local.public_dataset}.products`;

    CREATE OR REPLACE TABLE `${local.source_fqn}.users` AS
    SELECT * REPLACE (CAST(NULL AS GEOGRAPHY) AS user_geom)
    FROM `${local.public_dataset}.users`;

    CREATE OR REPLACE TABLE `${local.source_fqn}.orders` AS
    SELECT o.* FROM `${local.public_dataset}.orders` AS o
    WHERE o.user_id IN (SELECT id FROM `${local.source_fqn}.users`);

    CREATE OR REPLACE TABLE `${local.source_fqn}.order_items` AS
    SELECT i.* FROM `${local.public_dataset}.order_items` AS i
    WHERE EXISTS (
      SELECT 1 FROM `${local.source_fqn}.orders` AS o
      WHERE o.order_id = i.order_id AND o.user_id = i.user_id)
    AND i.product_id IN (SELECT id FROM `${local.landing_fqn}.products`);

    ${local.landing_sql}
  SQL
}

module "source_dataset" {
  depends_on = [google_project_service.application]
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/bigquery-dataset?ref=v58.0.0"
  project_id = var.project_id
  id         = local.source_dataset
  location   = var.bq_location
  options    = { delete_contents_on_destroy = var.destroy_all_resources }
}

module "landing_dataset" {
  depends_on = [google_project_service.application]
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/bigquery-dataset?ref=v58.0.0"
  project_id = var.project_id
  id         = local.landing_dataset
  location   = var.bq_location
  options    = { delete_contents_on_destroy = var.destroy_all_resources }
}

// synthetic_data_quality and synthetic_rag, from the schema directory names
module "pipeline_datasets" {
  for_each   = local.pipeline_datasets
  depends_on = [google_project_service.application]
  source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/bigquery-dataset?ref=v58.0.0"
  project_id = var.project_id
  id         = each.value
  location   = var.bq_location
  options    = { delete_contents_on_destroy = var.destroy_all_resources }
}

resource "google_bigquery_table" "pipeline" {
  for_each            = local.pipeline_tables
  project             = var.project_id
  dataset_id          = module.pipeline_datasets[each.value.dataset].dataset_id
  table_id            = each.value.table
  schema              = each.value.schema
  deletion_protection = !var.destroy_all_resources

  dynamic "time_partitioning" {
    for_each = contains(keys(local.partition_fields), each.key) ? [local.partition_fields[each.key]] : []
    content {
      type  = "DAY"
      field = time_partitioning.value
    }
  }
}

resource "random_id" "thelook_tables" {
  byte_length = 4
  keepers = {
    sql = sha256(local.thelook_sql)
  }
}

resource "google_bigquery_job" "thelook_tables" {
  project  = var.project_id
  location = var.bq_location
  job_id   = "sdfb-thelook-tables-${random_id.thelook_tables.hex}"

  query {
    query              = local.thelook_sql
    use_legacy_sql     = false
    create_disposition = ""
    write_disposition  = ""
  }

  depends_on = [module.source_dataset, module.landing_dataset]
}
