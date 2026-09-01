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
package com.google.cloud.dataflow.solutions.clickstream_analytics.transform;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import com.google.api.services.bigquery.model.TableRow;
import java.nio.charset.StandardCharsets;
import java.util.List;
import org.apache.beam.sdk.io.gcp.bigquery.BigQueryStorageApiInsertError;
import org.apache.beam.sdk.io.gcp.bigquery.BigQueryStorageApiInsertErrorCoder;
import org.apache.beam.sdk.testing.PAssert;
import org.apache.beam.sdk.testing.TestPipeline;
import org.apache.beam.sdk.transforms.Create;
import org.apache.beam.sdk.transforms.SerializableFunction;
import org.apache.beam.sdk.values.KV;
import org.apache.beam.sdk.values.PCollection;
import org.junit.Rule;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.junit.runners.JUnit4;

@RunWith(JUnit4.class)
public class DeadletterConverterTest {

    @Rule public final transient TestPipeline pipeline = TestPipeline.create();

    @Test
    public void testFormatDeadletterRow() {
        String timestamp = "2026-09-01T12:00:00Z";
        String payload = "{\"test\": 123}";
        String error = "Invalid JSON syntax";
        String stacktrace = "java.lang.RuntimeException";

        TableRow row =
                DeadletterConverter.formatDeadletterRow(timestamp, payload, error, stacktrace);

        assertEquals(timestamp, row.get("timestamp"));
        assertEquals(payload, row.get("payloadString"));
        assertArrayEquals(
                payload.getBytes(StandardCharsets.UTF_8), (byte[]) row.get("payloadBytes"));
        assertEquals(error, row.get("errorMessage"));
        assertEquals(stacktrace, row.get("stacktrace"));
        assertTrue(((List<?>) row.get("attributes")).isEmpty());
    }

    @Test
    public void testFormatDeadletterRowNullHandling() {
        TableRow row = DeadletterConverter.formatDeadletterRow(null, null, null, null);

        assertNotNull(row.get("timestamp"));
        assertEquals("", row.get("payloadString"));
        assertArrayEquals(new byte[0], (byte[]) row.get("payloadBytes"));
        assertEquals("", row.get("errorMessage"));
        assertEquals("", row.get("stacktrace"));
        assertTrue(((List<?>) row.get("attributes")).isEmpty());
    }

    @Test
    public void testFromParseErrorsTransform() {
        KV<String, String> parseError =
                KV.of("JSON_PARSING_ERROR: Invalid syntax", "{\"bad\": json}");

        PCollection<TableRow> rows =
                pipeline.apply("CreateErrors", Create.of(parseError))
                        .apply("ConvertToDeadletter", DeadletterConverter.fromParseErrors());

        PAssert.that(rows)
                .satisfies(
                        (SerializableFunction<Iterable<TableRow>, Void>)
                                input -> {
                                    int count = 0;
                                    for (TableRow row : input) {
                                        count++;
                                        assertEquals("{\"bad\": json}", row.get("payloadString"));
                                        assertEquals(
                                                "JSON_PARSING_ERROR: Invalid syntax",
                                                row.get("errorMessage"));
                                        assertNotNull(row.get("timestamp"));
                                    }
                                    assertEquals(1, count);
                                    return null;
                                });

        pipeline.run().waitUntilFinish();
    }

    @Test
    public void testFromStorageApiErrorsTransform() {
        TableRow failedRow = new TableRow().set("user_id", "user_123");
        BigQueryStorageApiInsertError insertError =
                new BigQueryStorageApiInsertError(failedRow, "Schema mismatch for column user_id");

        PCollection<TableRow> rows =
                pipeline.apply(
                                "CreateInsertErrors",
                                Create.of(insertError)
                                        .withCoder(BigQueryStorageApiInsertErrorCoder.of()))
                        .apply("ConvertToDeadletter", DeadletterConverter.fromStorageApiErrors());

        PAssert.that(rows)
                .satisfies(
                        (SerializableFunction<Iterable<TableRow>, Void>)
                                input -> {
                                    int count = 0;
                                    for (TableRow row : input) {
                                        count++;
                                        assertTrue(
                                                row.get("payloadString")
                                                        .toString()
                                                        .contains("user_123"));
                                        assertEquals(
                                                "Schema mismatch for column user_id",
                                                row.get("errorMessage"));
                                        assertNotNull(row.get("timestamp"));
                                    }
                                    assertEquals(1, count);
                                    return null;
                                });

        pipeline.run().waitUntilFinish();
    }
}
