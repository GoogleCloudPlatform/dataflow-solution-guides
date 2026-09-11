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
package com.google.cloud.dataflow.solutions.gaming_analytics.inference;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.EnrichedEvent;
import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.GameplayEvent;
import java.io.IOException;
import java.util.HashMap;
import java.util.Map;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.junit.runners.JUnit4;

@RunWith(JUnit4.class)
public class VertexAiRecommenderTest {

    private static EnrichedEvent enrichedEvent() {
        Map<String, String> features = new HashMap<>();
        features.put("churn_risk", "0.81");
        return EnrichedEvent.of(
                GameplayEvent.builder()
                        .setPlayerId("player_0001")
                        .setSessionId("session_1")
                        .setEventType("level_failed")
                        .setLevel(12)
                        .setScore(9100L)
                        .setEventTimestamp("2026-09-11T09:53:50.000Z")
                        .build(),
                features);
    }

    @Test
    public void testPredictUrlIsRegional() {
        assertEquals(
                "https://us-central1-aiplatform.googleapis.com/v1/"
                        + "projects/my-project/locations/us-central1/endpoints/123:predict",
                VertexAiRecommender.predictUrl(
                        "projects/my-project/locations/us-central1/endpoints/123"));
        assertTrue(
                VertexAiRecommender.predictUrl("  projects/p/locations/europe-west4/endpoints/7  ")
                        .startsWith("https://europe-west4-aiplatform.googleapis.com/"));
    }

    @Test
    public void testPredictUrlRejectsMalformedEndpoints() {
        assertThrows(IllegalArgumentException.class, () -> VertexAiRecommender.predictUrl(null));
        assertThrows(
                IllegalArgumentException.class,
                () -> VertexAiRecommender.predictUrl("my-endpoint"));
        assertThrows(
                IllegalArgumentException.class,
                () -> VertexAiRecommender.predictUrl("projects/p/locations/l/models/m"));
    }

    @Test
    public void testRequestBodyFlattensEventAndFeatures() {
        assertEquals(
                "{\"instances\":[{\"player_id\":\"player_0001\",\"session_id\":\"session_1\","
                    + "\"event_type\":\"level_failed\",\"level\":12,\"score\":9100,"
                    + "\"event_timestamp\":\"2026-09-11T09:53:50.000Z\",\"churn_risk\":\"0.81\"}]}",
                VertexAiRecommender.buildRequestBody(enrichedEvent()));
    }

    @Test
    public void testParseStructuredPrediction() throws Exception {
        Prediction prediction =
                VertexAiRecommender.parsePrediction(
                        "{\"predictions\": [{\"recommendation\": \"retention_bonus_pack\","
                                + " \"recommendation_score\": 0.77}]}",
                        "recommendation",
                        "recommendation_score");

        assertEquals("retention_bonus_pack", prediction.label());
        assertEquals(0.77, prediction.score(), 1e-9);
    }

    @Test
    public void testParsePredictionWithCustomFieldNames() throws Exception {
        Prediction prediction =
                VertexAiRecommender.parsePrediction(
                        "{\"predictions\": [{\"label\": \"premium_bundle_offer\","
                                + " \"confidence\": 0.5}]}",
                        "label",
                        "confidence");

        assertEquals("premium_bundle_offer", prediction.label());
        assertEquals(0.5, prediction.score(), 1e-9);
    }

    @Test
    public void testParseBarePredictionHasNoScore() throws Exception {
        Prediction prediction =
                VertexAiRecommender.parsePrediction(
                        "{\"predictions\": [\"daily_quest_suggestion\"]}",
                        "recommendation",
                        "recommendation_score");

        assertEquals("daily_quest_suggestion", prediction.label());
        assertNull(prediction.score());
    }

    @Test
    public void testParsePredictionWithoutScoreField() throws Exception {
        Prediction prediction =
                VertexAiRecommender.parsePrediction(
                        "{\"predictions\": [{\"recommendation\": \"tournament_invite\"}]}",
                        "recommendation",
                        "recommendation_score");

        assertEquals("tournament_invite", prediction.label());
        assertNull(prediction.score());
    }

    @Test
    public void testEmptyOrMissingPredictionsAreRejected() {
        assertThrows(
                IOException.class,
                () ->
                        VertexAiRecommender.parsePrediction(
                                "{\"predictions\": []}", "recommendation", "score"));
        assertThrows(
                IOException.class,
                () ->
                        VertexAiRecommender.parsePrediction(
                                "{\"deployedModelId\": \"1\"}", "recommendation", "score"));
    }

    @Test
    public void testPredictionWithoutTheLabelFieldIsRejected() {
        assertThrows(
                IOException.class,
                () ->
                        VertexAiRecommender.parsePrediction(
                                "{\"predictions\": [{\"score\": 0.4}]}",
                                "recommendation",
                                "score"));
    }

    @Test
    public void testMalformedResponseIsRejected() {
        assertThrows(
                IOException.class,
                () ->
                        VertexAiRecommender.parsePrediction(
                                "NOT JSON AT ALL", "recommendation", "score"));
    }
}
