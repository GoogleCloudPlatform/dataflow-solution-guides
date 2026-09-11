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

import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.ProcessingError;
import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.Recommendation;
import java.nio.charset.StandardCharsets;
import org.apache.beam.sdk.io.gcp.pubsub.PubsubMessage;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.junit.runners.JUnit4;

@RunWith(JUnit4.class)
public class PubSubPublishersTest {

    @Test
    public void testRecommendationMessageCarriesFilterableAttributes() {
        Recommendation recommendation =
                Recommendation.builder()
                        .setPlayerId("player_0042")
                        .setEventType("level_failed")
                        .setRecommendation("difficulty_assist_boost")
                        .setRecommendationScore(0.64)
                        .setEventTimestamp("2026-09-11T09:53:50.000Z")
                        .build();

        PubsubMessage message = PubSubPublishers.toPubsubMessage(recommendation);

        assertEquals("player_0042", message.getAttribute("player_id"));
        assertEquals("level_failed", message.getAttribute("event_type"));
        assertEquals("difficulty_assist_boost", message.getAttribute("recommendation"));
        assertEquals(
                recommendation.toJsonString(),
                new String(message.getPayload(), StandardCharsets.UTF_8));
    }

    @Test
    public void testRecommendationMessageOmitsAbsentAttributes() {
        Recommendation recommendation =
                Recommendation.builder()
                        .setPlayerId("player_1")
                        .setEventTimestamp("2026-09-11T09:53:50.000Z")
                        .build();

        PubsubMessage message = PubSubPublishers.toPubsubMessage(recommendation);

        assertEquals("player_1", message.getAttribute("player_id"));
        assertFalse(message.getAttributeMap().containsKey("event_type"));
        assertFalse(message.getAttributeMap().containsKey("recommendation"));
    }

    @Test
    public void testErrorMessageIsTaggedWithTheFailingStage() {
        ProcessingError error =
                ProcessingError.of(
                        "inference", "{\"player_id\":\"p1\"}", "boom", "2026-09-11T09:53:50.000Z");

        PubsubMessage message = PubSubPublishers.toPubsubMessage(error);

        assertEquals("inference", message.getAttribute("stage"));
        assertEquals(
                error.toJsonString(), new String(message.getPayload(), StandardCharsets.UTF_8));
    }
}
