#  Copyright 2026 Google LLC
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
  # The evaluator's registry (scripts/06_run_evaluation.sh writes it): four
  # tables, one per schema file the package ships, and the two views of its
  # views.sql. The same files `sdfb-eval schemas` reads, so nothing is copied.
  evaluation_schema_dir = "${local.pipeline_dir}/packages/sdfb-evaluation/src/sdfb_evaluation/schemas"
  evaluation_tables = {
    for f in fileset(local.evaluation_schema_dir, "*.schema.json") :
    trimsuffix(f, ".schema.json") => file("${local.evaluation_schema_dir}/${f}")
  }

  # Partitioning and clustering as `sdfb-eval schemas` creates them
  # (PARTITIONING and CLUSTERING in the package's schemas/__init__.py; the
  # source repository's tests keep the two equal). Row flags are the most
  # sensitive rows the evaluator writes: their partitions expire after 180 days.
  evaluation_layout = {
    evaluation_data_history = { partition = "recorded_at", type = "DAY", expiration_days = null, clustering = ["relationship_model", "engine", "evaluation_id"] }
    evaluation_metrics      = { partition = "evaluated_at", type = "MONTH", expiration_days = null, clustering = ["table_name", "metric_id", "evaluation_id"] }
    evaluation_profiles     = { partition = "evaluated_at", type = "MONTH", expiration_days = null, clustering = ["table_name", "profile_kind", "evaluation_id"] }
    evaluation_row_flags    = { partition = "evaluated_at", type = "MONTH", expiration_days = 180, clustering = ["table_name", "check", "evaluation_id"] }
  }

  # views.sql holds `CREATE OR REPLACE VIEW `{project}.{dataset}.<name>` AS
  # <query>;` statements; each becomes a view with that name and query.
  evaluation_views_sql = replace(replace(file("${local.evaluation_schema_dir}/views.sql"),
  "{project}", var.project_id), "{dataset}", local.quality_dataset)
  evaluation_views = {
    for m in regexall("(?s)CREATE OR REPLACE VIEW `[^`]*\\.([A-Za-z0-9_]+)` AS\\s*(.*?);", local.evaluation_views_sql) :
    m[0] => trimspace(m[1])
  }
}

resource "google_bigquery_table" "evaluation" {
  for_each            = local.evaluation_tables
  project             = var.project_id
  dataset_id          = module.pipeline_datasets[local.quality_dataset].dataset_id
  table_id            = each.key
  schema              = each.value
  clustering          = local.evaluation_layout[each.key].clustering
  deletion_protection = !var.destroy_all_resources

  time_partitioning {
    type          = local.evaluation_layout[each.key].type
    field         = local.evaluation_layout[each.key].partition
    expiration_ms = local.evaluation_layout[each.key].expiration_days == null ? null : local.evaluation_layout[each.key].expiration_days * 86400000
  }
}

resource "google_bigquery_table" "evaluation_view" {
  for_each            = local.evaluation_views
  project             = var.project_id
  dataset_id          = module.pipeline_datasets[local.quality_dataset].dataset_id
  table_id            = each.key
  deletion_protection = !var.destroy_all_resources

  view {
    query          = each.value
    use_legacy_sql = false
  }

  # The views read evaluation_data_history; BigQuery checks that at creation.
  depends_on = [google_bigquery_table.evaluation]
}
