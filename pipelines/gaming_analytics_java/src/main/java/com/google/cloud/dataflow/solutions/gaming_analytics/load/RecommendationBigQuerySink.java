/*
 * Copyright 2026 Google.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *   http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
package com.google.cloud.dataflow.solutions.gaming_analytics.load;

import static org.apache.beam.sdk.io.gcp.bigquery.BigQueryIO.Write.CreateDisposition.CREATE_NEVER;
import static org.apache.beam.sdk.io.gcp.bigquery.BigQueryIO.Write.WriteDisposition.WRITE_APPEND;

import com.google.auto.value.AutoValue;
import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.ProcessingError;
import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.Recommendation;
import org.apache.beam.sdk.io.gcp.bigquery.BigQueryIO;
import org.apache.beam.sdk.io.gcp.bigquery.BigQueryStorageApiInsertError;
import org.apache.beam.sdk.io.gcp.bigquery.WriteResult;
import org.apache.beam.sdk.metrics.Counter;
import org.apache.beam.sdk.metrics.Metrics;
import org.apache.beam.sdk.transforms.DoFn;
import org.apache.beam.sdk.transforms.PTransform;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.values.PCollection;
import org.joda.time.Instant;

/**
 * Writes the scored gameplay events to the analytics table created by Terraform.
 *
 * <p>The table is never created by the pipeline ({@code CREATE_NEVER}): it is partitioned on {@code
 * event_timestamp} and clustered on {@code player_id, event_type} by the Terraform module, and an
 * implicit creation from the pipeline would silently lose those properties.
 */
public final class RecommendationBigQuerySink {

    public static final String SINK_STAGE = "bigquery";

    private RecommendationBigQuerySink() {}

    public static Write.Builder write() {
        return Write.builder();
    }

    /** Converts the rows rejected by the Storage Write API into dead-letter records. */
    public static PTransform<
                    PCollection<BigQueryStorageApiInsertError>, PCollection<ProcessingError>>
            failedInsertsToErrors() {
        return new FailedInsertsToErrors();
    }

    /**
     * Normalizes a table reference into the {@code PROJECT:DATASET.TABLE} spec expected by
     * BigQueryIO.
     *
     * <p>The Terraform module exports {@code BQ_TABLE} in the {@code PROJECT.DATASET.TABLE} form
     * used by the {@code bq} CLI, which is ambiguous to parse because project ids may contain dots.
     *
     * @throws IllegalArgumentException if the reference is not a fully qualified table
     */
    public static String normalizeTableSpec(String table) {
        if (table == null || table.trim().isEmpty()) {
            throw new IllegalArgumentException("A BigQuery table reference is required");
        }
        String trimmed = table.trim();
        if (trimmed.contains(":")) {
            return trimmed;
        }
        String[] parts = trimmed.split("\\.");
        if (parts.length != 3) {
            throw new IllegalArgumentException(
                    String.format(
                            "Malformed BigQuery table '%s', expected PROJECT:DATASET.TABLE or"
                                    + " PROJECT.DATASET.TABLE",
                            table));
        }
        return String.format("%s:%s.%s", parts[0], parts[1], parts[2]);
    }

    /** Streams recommendations into BigQuery with the Storage Write API. */
    @AutoValue
    public abstract static class Write
            extends PTransform<PCollection<Recommendation>, WriteResult> {

        public abstract String table();

        public static Builder builder() {
            return new AutoValue_RecommendationBigQuerySink_Write.Builder();
        }

        public abstract Builder toBuilder();

        /** Builder for {@link Write}. */
        @AutoValue.Builder
        public abstract static class Builder {
            public abstract Builder table(String table);

            public Builder withTable(String table) {
                return table(table);
            }

            public abstract Write build();
        }

        @Override
        public WriteResult expand(PCollection<Recommendation> input) {
            // STORAGE_API_AT_LEAST_ONCE appends to the default stream without
            // offset deduplication: the cheapest and lowest-latency Storage
            // Write API mode, and a reasonable default for an append-only
            // analytics table. The tradeoff is that a retried bundle re-appends
            // rows it had already written, so this table can hold duplicate
            // scorings of the same event. Readers are expected to deduplicate
            // on (player_id, event_timestamp, event_type). Switch to
            // Method.STORAGE_WRITE_API if exactly-once rows are worth the extra
            // latency and stream management.
            return input.apply(
                    "WriteRecommendationsToBQ",
                    BigQueryIO.<Recommendation>write()
                            .withFormatFunction(Recommendation::toTableRow)
                            .withMethod(BigQueryIO.Write.Method.STORAGE_API_AT_LEAST_ONCE)
                            .withWriteDisposition(WRITE_APPEND)
                            .withCreateDisposition(CREATE_NEVER)
                            .to(normalizeTableSpec(table())));
        }
    }

    private static class FailedInsertsToErrors
            extends PTransform<
                    PCollection<BigQueryStorageApiInsertError>, PCollection<ProcessingError>> {
        @Override
        public PCollection<ProcessingError> expand(
                PCollection<BigQueryStorageApiInsertError> input) {
            return input.apply(
                    "FailedInsertToError",
                    ParDo.of(
                            new DoFn<BigQueryStorageApiInsertError, ProcessingError>() {
                                private final Counter failedInserts =
                                        Metrics.counter(
                                                RecommendationBigQuerySink.class,
                                                "bigquery-failed-inserts");

                                @ProcessElement
                                public void processElement(
                                        @Element BigQueryStorageApiInsertError error,
                                        @Timestamp Instant timestamp,
                                        OutputReceiver<ProcessingError> output) {
                                    failedInserts.inc();
                                    output.output(
                                            ProcessingError.of(
                                                    SINK_STAGE,
                                                    error.getRow() != null
                                                            ? error.getRow().toString()
                                                            : "",
                                                    error.getErrorMessage(),
                                                    timestamp.toString()));
                                }
                            }));
        }
    }
}
