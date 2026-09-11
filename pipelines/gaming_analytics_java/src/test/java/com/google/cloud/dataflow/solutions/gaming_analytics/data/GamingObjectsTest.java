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
package com.google.cloud.dataflow.solutions.gaming_analytics.data;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;

import com.google.api.services.bigquery.model.TableRow;
import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.EnrichedEvent;
import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.GameplayEvent;
import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.ProcessingError;
import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.Recommendation;
import com.google.common.collect.ImmutableMap;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.util.HashMap;
import java.util.Map;
import org.apache.beam.sdk.coders.Coder;
import org.apache.beam.sdk.schemas.SchemaRegistry;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.junit.runners.JUnit4;

@RunWith(JUnit4.class)
public class GamingObjectsTest {

    private static Recommendation.Builder recommendationBuilder() {
        return Recommendation.builder()
                .setPlayerId("player_0042")
                .setSessionId("session_7")
                .setEventType("level_failed")
                .setLevel(12)
                .setScore(9100L)
                .setRecommendation("difficulty_assist_boost")
                .setRecommendationScore(0.64)
                .setEventTimestamp("2026-09-11T09:53:50.000Z")
                .setProcessingTimestamp("2026-09-11T09:53:51.000Z");
    }

    @Test
    public void testEnrichedEventSurvivesItsSchemaCoder() throws Exception {
        // Regression test for the narrowed ImmutableMap feature map. Beam derives the coder from
        // the getter type and generates the decoder with ByteBuddy; if the builder setter were
        // also declared to take an ImmutableMap, that generated code would hand it a
        // TransformingMap and the JVM would reject the class with a VerifyError, at runtime only.
        EnrichedEvent original =
                EnrichedEvent.of(
                        GameplayEvent.builder()
                                .setPlayerId("player_0042")
                                .setEventType("level_failed")
                                .setLevel(12)
                                .setScore(9100L)
                                .setEventTimestamp("2026-09-11T09:53:50.000Z")
                                .build(),
                        ImmutableMap.of("churn_risk", "0.85", "spend_tier", "whale"));

        Coder<EnrichedEvent> coder =
                SchemaRegistry.createDefault().getSchemaCoder(EnrichedEvent.class);

        ByteArrayOutputStream encoded = new ByteArrayOutputStream();
        coder.encode(original, encoded);
        EnrichedEvent decoded = coder.decode(new ByteArrayInputStream(encoded.toByteArray()));

        assertEquals(original.getEvent(), decoded.getEvent());
        assertEquals(original.getFeatures(), decoded.getFeatures());
    }

    @Test
    public void testEnrichedEventCopiesTheFeatureMapItIsGiven() {
        Map<String, String> mutable = new HashMap<>();
        mutable.put("churn_risk", "0.85");

        EnrichedEvent enriched =
                EnrichedEvent.of(
                        GameplayEvent.builder().setPlayerId("player_0042").build(), mutable);

        // A copy was taken, so later writes to the caller's map are not visible.
        mutable.put("churn_risk", "0.05");
        mutable.put("spend_tier", "whale");
        assertEquals(ImmutableMap.of("churn_risk", "0.85"), enriched.getFeatures());

        // There is deliberately no "mutating the returned map throws" assertion here: the getter is
        // declared as ImmutableMap, so Error Prone's DoNotCall check rejects any such call at
        // compile time. That is a stronger guarantee than a runtime exception, but it also means
        // the call cannot be written in a test.
    }

    @Test
    public void testRecommendationToTableRowMatchesBigQuerySchema() {
        TableRow row = recommendationBuilder().build().toTableRow();

        assertEquals("player_0042", row.get("player_id"));
        assertEquals("session_7", row.get("session_id"));
        assertEquals("level_failed", row.get("event_type"));
        assertEquals(12, row.get("level"));
        assertEquals(9100L, row.get("score"));
        assertEquals("difficulty_assist_boost", row.get("recommendation"));
        assertEquals(0.64, (Double) row.get("recommendation_score"), 1e-9);
        assertEquals("2026-09-11T09:53:50.000Z", row.get("event_timestamp"));
        assertEquals("2026-09-11T09:53:51.000Z", row.get("processing_timestamp"));
    }

    @Test
    public void testRecommendationToTableRowOmitsNullableColumns() {
        TableRow row =
                Recommendation.builder()
                        .setPlayerId("player_1")
                        .setEventTimestamp("2026-09-11T09:53:50.000Z")
                        .build()
                        .toTableRow();

        assertEquals(2, row.size());
        assertEquals("player_1", row.get("player_id"));
        assertEquals("2026-09-11T09:53:50.000Z", row.get("event_timestamp"));
        assertFalse(row.containsKey("session_id"));
        assertFalse(row.containsKey("recommendation_score"));
    }

    @Test
    public void testRecommendationJsonPayload() {
        String json = recommendationBuilder().build().toJsonString();

        assertEquals(
                "{\"player_id\":\"player_0042\",\"session_id\":\"session_7\",\"event_type\":\"level_failed\","
                    + "\"level\":12,\"score\":9100,\"recommendation\":\"difficulty_assist_boost\","
                    + "\"recommendation_score\":0.64,\"event_timestamp\":\"2026-09-11T09:53:50.000Z\","
                    + "\"processing_timestamp\":\"2026-09-11T09:53:51.000Z\"}",
                json);
    }

    @Test
    public void testRecommendationJsonPayloadWithNullFields() {
        String json =
                Recommendation.builder()
                        .setPlayerId("player_1")
                        .setEventTimestamp("2026-09-11T09:53:50.000Z")
                        .build()
                        .toJsonString();

        assertEquals(
                "{\"player_id\":\"player_1\",\"session_id\":null,\"event_type\":null,\"level\":null,"
                    + "\"score\":null,\"recommendation\":null,\"recommendation_score\":null,"
                    + "\"event_timestamp\":\"2026-09-11T09:53:50.000Z\",\"processing_timestamp\":null}",
                json);
    }

    @Test
    public void testProcessingErrorJsonRoundTrip() throws Exception {
        ProcessingError error =
                ProcessingError.of(
                        "parse",
                        "{\"player_id\": \"p1\", broken}",
                        "Unexpected character",
                        "2026-09-11T09:53:50.000Z");

        ProcessingError parsed = ProcessingError.fromJsonString(error.toJsonString());

        assertEquals(error, parsed);
        assertEquals("parse", parsed.getStage());
        assertEquals("{\"player_id\": \"p1\", broken}", parsed.getPayload());
    }

    @Test
    public void testProcessingErrorNormalizesNulls() {
        ProcessingError error =
                ProcessingError.of("enrich", null, null, "2026-09-11T09:53:50.000Z");

        assertEquals("", error.getPayload());
        assertEquals("", error.getErrorMessage());
        assertEquals("enrich", error.getStage());
    }
}
