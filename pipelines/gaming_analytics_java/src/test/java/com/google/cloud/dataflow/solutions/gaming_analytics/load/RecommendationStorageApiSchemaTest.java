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

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertThrows;

import com.google.api.services.bigquery.model.TableFieldSchema;
import com.google.api.services.bigquery.model.TableRow;
import com.google.api.services.bigquery.model.TableSchema;
import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.Recommendation;
import com.google.protobuf.Descriptors.Descriptor;
import com.google.protobuf.Descriptors.FieldDescriptor;
import com.google.protobuf.DynamicMessage;
import java.util.Arrays;
import org.apache.beam.sdk.io.gcp.bigquery.TableRowToStorageApiProto;
import org.joda.time.Instant;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.junit.runners.JUnit4;

/**
 * Verifies that the rows produced by {@link Recommendation#toTableRow()} are accepted by the same
 * encoder the BigQuery Storage Write API uses at runtime, against the exact schema the Terraform
 * module of this solution guide creates.
 *
 * <p>Without this check, a rejected value (a timestamp in the wrong representation, for example)
 * would only show up once the pipeline runs on Dataflow, as every row silently landing in the
 * dead-letter topic.
 */
@RunWith(JUnit4.class)
public class RecommendationStorageApiSchemaTest {

    /** The schema of {@code gaming_analytics.player_recommendations}, as created by Terraform. */
    private static TableSchema terraformTableSchema() {
        return new TableSchema()
                .setFields(
                        Arrays.asList(
                                field("player_id", "STRING", "REQUIRED"),
                                field("session_id", "STRING", "NULLABLE"),
                                field("event_type", "STRING", "NULLABLE"),
                                field("level", "INTEGER", "NULLABLE"),
                                field("score", "INTEGER", "NULLABLE"),
                                field("recommendation", "STRING", "NULLABLE"),
                                field("recommendation_score", "FLOAT", "NULLABLE"),
                                field("event_timestamp", "TIMESTAMP", "REQUIRED"),
                                field("processing_timestamp", "TIMESTAMP", "NULLABLE")));
    }

    private static TableFieldSchema field(String name, String type, String mode) {
        return new TableFieldSchema().setName(name).setType(type).setMode(mode);
    }

    private static DynamicMessage encode(TableRow row) throws Exception {
        com.google.cloud.bigquery.storage.v1.TableSchema protoSchema =
                TableRowToStorageApiProto.schemaToProtoTableSchema(terraformTableSchema());
        Descriptor descriptor =
                TableRowToStorageApiProto.getDescriptorFromTableSchema(protoSchema, true, false);
        return TableRowToStorageApiProto.messageFromTableRow(
                TableRowToStorageApiProto.SchemaInformation.fromTableSchema(protoSchema),
                descriptor,
                row,
                false,
                false,
                null,
                null,
                -1L,
                TableRowToStorageApiProto.ErrorCollector.DONT_COLLECT);
    }

    private static Object encodedField(DynamicMessage message, String name) {
        FieldDescriptor fieldDescriptor = message.getDescriptorForType().findFieldByName(name);
        assertNotNull("no proto field named " + name, fieldDescriptor);
        return message.getField(fieldDescriptor);
    }

    private static Recommendation.Builder fullRecommendation() {
        return Recommendation.builder()
                .setPlayerId("player_0001")
                .setSessionId("session_1")
                .setEventType("level_failed")
                .setLevel(12)
                .setScore(9100L)
                .setRecommendation("difficulty_assist_boost")
                .setRecommendationScore(0.74)
                .setEventTimestamp("2026-09-11T09:53:50.000Z")
                .setProcessingTimestamp("2026-09-11T09:53:51.250Z");
    }

    @Test
    public void testFullRowIsAcceptedByTheStorageWriteApiEncoder() throws Exception {
        DynamicMessage message = encode(fullRecommendation().build().toTableRow());

        assertEquals("player_0001", encodedField(message, "player_id"));
        assertEquals("session_1", encodedField(message, "session_id"));
        assertEquals("level_failed", encodedField(message, "event_type"));
        assertEquals(12L, encodedField(message, "level"));
        assertEquals(9100L, encodedField(message, "score"));
        assertEquals("difficulty_assist_boost", encodedField(message, "recommendation"));
        assertEquals(0.74, (Double) encodedField(message, "recommendation_score"), 1e-9);
        // TIMESTAMP columns are encoded as microseconds since the epoch.
        assertEquals(
                Instant.parse("2026-09-11T09:53:50.000Z").getMillis() * 1000L,
                encodedField(message, "event_timestamp"));
        assertEquals(
                Instant.parse("2026-09-11T09:53:51.250Z").getMillis() * 1000L,
                encodedField(message, "processing_timestamp"));
    }

    @Test
    public void testRowWithOnlyTheRequiredColumnsIsAccepted() throws Exception {
        // Everything the pipeline can leave out: a payload with just a player id and a timestamp
        // backfilled from the Pub/Sub element, scored by a model that reports no confidence.
        Recommendation minimal =
                Recommendation.builder()
                        .setPlayerId("player_9999")
                        .setEventTimestamp("2026-09-11T09:53:50.000Z")
                        .build();

        DynamicMessage message = encode(minimal.toTableRow());

        assertEquals("player_9999", encodedField(message, "player_id"));
        assertEquals(
                Instant.parse("2026-09-11T09:53:50.000Z").getMillis() * 1000L,
                encodedField(message, "event_timestamp"));
        FieldDescriptor sessionId = message.getDescriptorForType().findFieldByName("session_id");
        assertFalse(message.hasField(sessionId));
    }

    @Test
    public void testTimestampsProducedByTheParserAreAccepted() throws Exception {
        // JsonToGameplayEvents normalizes every event timestamp through Instant.toString(), and
        // the processing timestamp is produced the same way, so this is the only representation
        // the sink ever sees in production.
        Instant eventTime = Instant.ofEpochMilli(1789034030123L);
        String normalized = eventTime.toString();
        assertEquals("2026-09-10T09:53:50.123Z", normalized);

        DynamicMessage message =
                encode(
                        fullRecommendation()
                                .setEventTimestamp(normalized)
                                .setProcessingTimestamp(normalized)
                                .build()
                                .toTableRow());

        assertEquals(eventTime.getMillis() * 1000L, encodedField(message, "event_timestamp"));
    }

    @Test
    public void testMissingRequiredColumnIsRejected() {
        // Guards the invariant the parser enforces: no player id, no row.
        TableRow row = fullRecommendation().build().toTableRow();
        row.remove("player_id");

        assertThrows(Exception.class, () -> encode(row));
    }
}
