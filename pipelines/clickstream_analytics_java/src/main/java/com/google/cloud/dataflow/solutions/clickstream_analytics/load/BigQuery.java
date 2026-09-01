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
package com.google.cloud.dataflow.solutions.clickstream_analytics.load;

import static org.apache.beam.sdk.io.gcp.bigquery.BigQueryIO.Write.CreateDisposition.CREATE_IF_NEEDED;
import static org.apache.beam.sdk.io.gcp.bigquery.BigQueryIO.Write.CreateDisposition.CREATE_NEVER;
import static org.apache.beam.sdk.io.gcp.bigquery.BigQueryIO.Write.WriteDisposition.WRITE_APPEND;

import com.google.api.services.bigquery.model.TableRow;
import com.google.auto.value.AutoValue;
import com.google.cloud.dataflow.solutions.clickstream_analytics.data.ClickstreamObjects.ClickstreamEvent;
import com.google.cloud.dataflow.solutions.clickstream_analytics.data.ClickstreamObjects.UserSession;
import java.util.Collections;
import javax.annotation.Nullable;
import org.apache.beam.sdk.io.gcp.bigquery.BigQueryIO;
import org.apache.beam.sdk.io.gcp.bigquery.RowMutationInformation;
import org.apache.beam.sdk.io.gcp.bigquery.WriteResult;
import org.apache.beam.sdk.transforms.MapElements;
import org.apache.beam.sdk.transforms.PTransform;
import org.apache.beam.sdk.transforms.SimpleFunction;
import org.apache.beam.sdk.values.PCollection;

public final class BigQuery {

    private BigQuery() {}

    public static WriteEvents.Builder writeEvents() {
        return WriteEvents.builder();
    }

    public static WriteSessions.Builder writeSessions() {
        return WriteSessions.builder();
    }

    public static WriteDeadletter.Builder writeDeadletter() {
        return WriteDeadletter.builder();
    }

    @AutoValue
    public abstract static class WriteEvents
            extends PTransform<PCollection<ClickstreamEvent>, WriteResult> {

        public abstract String projectId();

        public abstract String dataset();

        public abstract String table();

        public static Builder builder() {
            return new AutoValue_BigQuery_WriteEvents.Builder();
        }

        public WriteEvents withProjectId(String projectId) {
            return toBuilder().projectId(projectId).build();
        }

        public WriteEvents withDataset(String dataset) {
            return toBuilder().dataset(dataset).build();
        }

        public WriteEvents withTable(String table) {
            return toBuilder().table(table).build();
        }

        public abstract Builder toBuilder();

        @AutoValue.Builder
        public abstract static class Builder {
            public abstract Builder projectId(String projectId);

            public abstract Builder dataset(String dataset);

            public abstract Builder table(String table);

            public Builder withProjectId(String projectId) {
                return projectId(projectId);
            }

            public Builder withDataset(String dataset) {
                return dataset(dataset);
            }

            public Builder withTable(String table) {
                return table(table);
            }

            public abstract WriteEvents build();
        }

        @Override
        public WriteResult expand(PCollection<ClickstreamEvent> input) {
            PCollection<TableRow> rows =
                    input.apply(
                            "EventsToTableRow",
                            MapElements.via(
                                    new SimpleFunction<ClickstreamEvent, TableRow>() {
                                        @Override
                                        public TableRow apply(ClickstreamEvent event) {
                                            return event.toTableRow();
                                        }
                                    }));

            return rows.apply(
                    "WriteEventsToBQ",
                    BigQueryIO.writeTableRows()
                            .withMethod(BigQueryIO.Write.Method.STORAGE_API_AT_LEAST_ONCE)
                            .withWriteDisposition(WRITE_APPEND)
                            .withCreateDisposition(CREATE_NEVER)
                            .ignoreUnknownValues()
                            .to(String.format("%s:%s.%s", projectId(), dataset(), table())));
        }
    }

    @AutoValue
    public abstract static class WriteSessions
            extends PTransform<PCollection<UserSession>, WriteResult> {

        public abstract String projectId();

        public abstract String dataset();

        public abstract String table();

        public static Builder builder() {
            return new AutoValue_BigQuery_WriteSessions.Builder();
        }

        public WriteSessions withProjectId(String projectId) {
            return toBuilder().projectId(projectId).build();
        }

        public WriteSessions withDataset(String dataset) {
            return toBuilder().dataset(dataset).build();
        }

        public WriteSessions withTable(String table) {
            return toBuilder().table(table).build();
        }

        public abstract Builder toBuilder();

        @AutoValue.Builder
        public abstract static class Builder {
            public abstract Builder projectId(String projectId);

            public abstract Builder dataset(String dataset);

            public abstract Builder table(String table);

            public Builder withProjectId(String projectId) {
                return projectId(projectId);
            }

            public Builder withDataset(String dataset) {
                return dataset(dataset);
            }

            public Builder withTable(String table) {
                return table(table);
            }

            public abstract WriteSessions build();
        }

        @Override
        public WriteResult expand(PCollection<UserSession> input) {
            PCollection<TableRow> rows =
                    input.apply(
                            "SessionsToTableRow",
                            MapElements.via(
                                    new SimpleFunction<UserSession, TableRow>() {
                                        @Override
                                        public TableRow apply(UserSession session) {
                                            return session.toTableRow();
                                        }
                                    }));

            return rows.apply(
                    "WriteSessionsToBQ",
                    BigQueryIO.writeTableRows()
                            .withMethod(BigQueryIO.Write.Method.STORAGE_API_AT_LEAST_ONCE)
                            .withWriteDisposition(WRITE_APPEND)
                            .withCreateDisposition(CREATE_NEVER)
                            .withPrimaryKey(Collections.singletonList("session_id"))
                            .withRowMutationInformationFn(
                                    row ->
                                            RowMutationInformation.of(
                                                    RowMutationInformation.MutationType.UPSERT,
                                                    String.valueOf(
                                                            ((Number)
                                                                            row.getOrDefault(
                                                                                    "event_count",
                                                                                    1))
                                                                    .longValue())))
                            .to(String.format("%s:%s.%s", projectId(), dataset(), table())));
        }
    }

    @AutoValue
    public abstract static class WriteDeadletter
            extends PTransform<PCollection<TableRow>, WriteResult> {

        public abstract String projectId();

        public abstract String dataset();

        public abstract String table();

        public abstract @Nullable String jsonSchema();

        public static Builder builder() {
            return new AutoValue_BigQuery_WriteDeadletter.Builder();
        }

        public WriteDeadletter withProjectId(String projectId) {
            return toBuilder().projectId(projectId).build();
        }

        public WriteDeadletter withDataset(String dataset) {
            return toBuilder().dataset(dataset).build();
        }

        public WriteDeadletter withTable(String table) {
            return toBuilder().table(table).build();
        }

        public WriteDeadletter withJsonSchema(@Nullable String jsonSchema) {
            return toBuilder().jsonSchema(jsonSchema).build();
        }

        public abstract Builder toBuilder();

        @AutoValue.Builder
        public abstract static class Builder {
            public abstract Builder projectId(String projectId);

            public abstract Builder dataset(String dataset);

            public abstract Builder table(String table);

            public abstract Builder jsonSchema(@Nullable String jsonSchema);

            public Builder withProjectId(String projectId) {
                return projectId(projectId);
            }

            public Builder withDataset(String dataset) {
                return dataset(dataset);
            }

            public Builder withTable(String table) {
                return table(table);
            }

            public Builder withJsonSchema(@Nullable String jsonSchema) {
                return jsonSchema(jsonSchema);
            }

            public abstract WriteDeadletter build();
        }

        @Override
        public WriteResult expand(PCollection<TableRow> input) {
            BigQueryIO.Write<TableRow> write =
                    BigQueryIO.writeTableRows()
                            .withMethod(BigQueryIO.Write.Method.STORAGE_API_AT_LEAST_ONCE)
                            .withWriteDisposition(WRITE_APPEND)
                            .withCreateDisposition(CREATE_IF_NEEDED)
                            .to(String.format("%s:%s.%s", projectId(), dataset(), table()));

            if (jsonSchema() != null) {
                write = write.withJsonSchema(jsonSchema());
            }

            return input.apply("WriteDeadletterToBigQuery", write);
        }
    }
}
