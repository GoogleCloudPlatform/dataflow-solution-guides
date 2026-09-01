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
package com.google.cloud.dataflow.solutions.clickstream_analytics;

import com.google.api.services.bigquery.model.TableRow;
import java.nio.charset.StandardCharsets;
import java.util.Collections;
import org.apache.beam.sdk.io.gcp.bigquery.BigQueryStorageApiInsertError;
import org.apache.beam.sdk.transforms.DoFn;
import org.apache.beam.sdk.transforms.PTransform;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.values.KV;
import org.apache.beam.sdk.values.PCollection;
import org.joda.time.Instant;

public class DeadletterConverter {

    public static PTransform<PCollection<KV<String, String>>, PCollection<TableRow>>
            fromParseErrors() {
        return new ParseErrorsToTableRowTransform();
    }

    public static PTransform<PCollection<BigQueryStorageApiInsertError>, PCollection<TableRow>>
            fromStorageApiErrors() {
        return new StorageApiErrorsToTableRowTransform();
    }

    public static TableRow formatDeadletterRow(
            String timestamp, String payloadString, String errorMessage, String stacktrace) {
        String payload = payloadString != null ? payloadString : "";
        byte[] payloadBytes = payload.getBytes(StandardCharsets.UTF_8);

        TableRow row = new TableRow();
        row.set("timestamp", timestamp != null ? timestamp : Instant.now().toString());
        row.set("payloadString", payload);
        row.set("payloadBytes", payloadBytes);
        row.set("attributes", Collections.emptyList());
        row.set("errorMessage", errorMessage != null ? errorMessage : "");
        row.set("stacktrace", stacktrace != null ? stacktrace : "");
        return row;
    }

    private static class ParseErrorsToTableRowTransform
            extends PTransform<PCollection<KV<String, String>>, PCollection<TableRow>> {
        @Override
        public PCollection<TableRow> expand(PCollection<KV<String, String>> input) {
            return input.apply(
                    "ParseErrorToDeadletterRow",
                    ParDo.of(
                            new DoFn<KV<String, String>, TableRow>() {
                                @ProcessElement
                                public void processElement(ProcessContext c) {
                                    KV<String, String> element = c.element();
                                    Metrics.deadletterMessages.inc();
                                    c.output(
                                            formatDeadletterRow(
                                                    Instant.now().toString(),
                                                    element.getValue(),
                                                    element.getKey(),
                                                    ""));
                                }
                            }));
        }
    }

    private static class StorageApiErrorsToTableRowTransform
            extends PTransform<PCollection<BigQueryStorageApiInsertError>, PCollection<TableRow>> {
        @Override
        public PCollection<TableRow> expand(PCollection<BigQueryStorageApiInsertError> input) {
            return input.apply(
                    "StorageApiErrorToDeadletterRow",
                    ParDo.of(
                            new DoFn<BigQueryStorageApiInsertError, TableRow>() {
                                @ProcessElement
                                public void processElement(ProcessContext c) {
                                    BigQueryStorageApiInsertError error = c.element();
                                    Metrics.failedInsertMessages.inc();
                                    Metrics.deadletterMessages.inc();
                                    String payload =
                                            error.getRow() != null ? error.getRow().toString() : "";
                                    c.output(
                                            formatDeadletterRow(
                                                    Instant.now().toString(),
                                                    payload,
                                                    error.getErrorMessage(),
                                                    ""));
                                }
                            }));
        }
    }
}
