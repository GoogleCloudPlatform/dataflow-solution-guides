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
import static org.junit.Assert.assertTrue;

import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.EnrichedEvent;
import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.GameplayEvent;
import java.util.Collections;
import java.util.HashMap;
import java.util.Map;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.junit.runners.JUnit4;

@RunWith(JUnit4.class)
public class LocalRecommenderTest {

    private final LocalRecommender recommender = new LocalRecommender();

    private static EnrichedEvent event(
            String eventType, Integer level, Long score, Map<String, String> features) {
        GameplayEvent gameplayEvent =
                GameplayEvent.builder()
                        .setPlayerId("player_0001")
                        .setSessionId("session_1")
                        .setEventType(eventType)
                        .setLevel(level)
                        .setScore(score)
                        .setEventTimestamp("2026-09-11T09:53:50.000Z")
                        .build();
        return EnrichedEvent.of(gameplayEvent, features);
    }

    private static Map<String, String> features(String... keyValues) {
        Map<String, String> features = new HashMap<>();
        for (int i = 0; i < keyValues.length; i += 2) {
            features.put(keyValues[i], keyValues[i + 1]);
        }
        return features;
    }

    @Test
    public void testChurnRiskWinsOverEveryOtherRule() {
        Prediction prediction =
                recommender.predict(
                        event(
                                "purchase",
                                40,
                                50_000L,
                                features("churn_risk", "0.92", "spend_tier", "whale")));

        assertEquals(LocalRecommender.RETENTION_BONUS, prediction.label());
        assertEquals(0.92, prediction.score(), 1e-9);
    }

    @Test
    public void testChurnRiskBelowThresholdDoesNotTriggerRetention() {
        Prediction prediction =
                recommender.predict(event("level_start", 3, 10L, features("churn_risk", "0.69")));

        assertEquals(LocalRecommender.DAILY_QUEST, prediction.label());
    }

    @Test
    public void testRepeatedLevelFailuresTriggerTheDifficultyAssist() {
        Prediction prediction =
                recommender.predict(
                        event("level_failed", 12, 900L, features("level_failures", "4")));

        assertEquals(LocalRecommender.DIFFICULTY_ASSIST, prediction.label());
        assertEquals(0.8, prediction.score(), 1e-9);
    }

    @Test
    public void testFirstLevelFailureUsesTheLowConfidenceAssist() {
        Prediction prediction =
                recommender.predict(
                        event("level_failed", 10, 900L, features("level_failures", "1")));

        assertEquals(LocalRecommender.DIFFICULTY_ASSIST, prediction.label());
        assertEquals(0.6, prediction.score(), 1e-9);
    }

    @Test
    public void testPayingPlayerCompletingALevelGetsABundle() {
        Prediction prediction =
                recommender.predict(
                        event("level_complete", 20, 5_000L, features("spend_tier", "paying")));

        assertEquals(LocalRecommender.PREMIUM_BUNDLE, prediction.label());
        assertEquals(0.7, prediction.score(), 1e-9);
    }

    @Test
    public void testWhaleGetsAStrongerBundleSignal() {
        Prediction prediction =
                recommender.predict(event("purchase", 20, 5_000L, features("spend_tier", "whale")));

        assertEquals(LocalRecommender.PREMIUM_BUNDLE, prediction.label());
        assertEquals(0.9, prediction.score(), 1e-9);
    }

    @Test
    public void testFreePlayerDoesNotGetABundle() {
        Prediction prediction =
                recommender.predict(
                        event("level_complete", 20, 5L, features("spend_tier", "free")));

        assertEquals(LocalRecommender.DAILY_QUEST, prediction.label());
    }

    @Test
    public void testOutperformingPlayersAreInvitedToTournaments() {
        Prediction prediction =
                recommender.predict(
                        event("level_complete", 20, 3_000L, features("skill_rating", "1000")));

        assertEquals(LocalRecommender.TOURNAMENT_INVITE, prediction.label());
        assertEquals(1.0, prediction.score(), 1e-9);
    }

    @Test
    public void testScoresAreAlwaysWithinTheUnitRange() {
        Prediction prediction =
                recommender.predict(
                        event("level_failed", 99, 1L, features("level_failures", "1000000")));

        assertTrue(prediction.score() >= 0.0 && prediction.score() <= 1.0);
    }

    @Test
    public void testMissingFeaturesFallBackToTheDefaultRecommendation() {
        Prediction prediction =
                recommender.predict(event("level_start", null, null, Collections.emptyMap()));

        assertEquals(LocalRecommender.DAILY_QUEST, prediction.label());
        assertEquals(0.25, prediction.score(), 1e-9);
    }

    @Test
    public void testMalformedFeaturesAreIgnoredRatherThanFatal() {
        Prediction prediction =
                recommender.predict(
                        event(
                                "level_start",
                                5,
                                100L,
                                features(
                                        "churn_risk",
                                        "not_a_number",
                                        "skill_rating",
                                        "",
                                        "spend_tier",
                                        "   ")));

        assertEquals(LocalRecommender.DAILY_QUEST, prediction.label());
    }

    @Test
    public void testNullEventTypeAndEmptyFeaturesAreSafe() {
        Prediction prediction =
                recommender.predict(event(null, null, null, Collections.emptyMap()));

        assertEquals(LocalRecommender.DAILY_QUEST, prediction.label());
    }
}
