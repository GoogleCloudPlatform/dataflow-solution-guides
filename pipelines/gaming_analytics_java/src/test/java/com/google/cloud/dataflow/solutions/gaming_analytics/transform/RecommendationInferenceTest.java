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
package com.google.cloud.dataflow.solutions.gaming_analytics.transform;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.EnrichedEvent;
import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.GameplayEvent;
import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.ProcessingError;
import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.Recommendation;
import com.google.cloud.dataflow.solutions.gaming_analytics.inference.LocalRecommender;
import com.google.cloud.dataflow.solutions.gaming_analytics.inference.Prediction;
import com.google.cloud.dataflow.solutions.gaming_analytics.inference.Recommender;
import java.io.Serializable;
import java.util.Collections;
import java.util.HashMap;
import java.util.Map;
import org.apache.beam.sdk.testing.PAssert;
import org.apache.beam.sdk.testing.TestPipeline;
import org.apache.beam.sdk.transforms.Create;
import org.apache.beam.sdk.values.PCollectionTuple;
import org.joda.time.Instant;
import org.junit.Rule;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.junit.runners.JUnit4;

@RunWith(JUnit4.class)
public class RecommendationInferenceTest implements Serializable {

    @Rule public final transient TestPipeline pipeline = TestPipeline.create();

    private static EnrichedEvent enrichedEvent() {
        Map<String, String> features = new HashMap<>();
        features.put("churn_risk", "0.95");
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

    /** A recommender that always fails, standing in for an unavailable model. */
    private static class FailingRecommender implements Recommender {
        private static final long serialVersionUID = 1L;

        @Override
        public Prediction predict(EnrichedEvent event) throws Exception {
            throw new IllegalStateException("model unavailable");
        }
    }

    @Test
    public void testSuccessfulScoringCarriesTheEventFieldsOver() {
        PCollectionTuple result =
                pipeline.apply(Create.of(enrichedEvent()))
                        .apply("Score", RecommendationInference.of(new LocalRecommender()));

        PAssert.that(result.get(RecommendationInference.SUCCESS_TAG))
                .satisfies(
                        recommendations -> {
                            Recommendation recommendation = recommendations.iterator().next();
                            assertEquals("player_0001", recommendation.getPlayerId());
                            assertEquals("session_1", recommendation.getSessionId());
                            assertEquals("level_failed", recommendation.getEventType());
                            assertEquals(Integer.valueOf(12), recommendation.getLevel());
                            assertEquals(Long.valueOf(9100L), recommendation.getScore());
                            assertEquals(
                                    "2026-09-11T09:53:50.000Z", recommendation.getEventTimestamp());
                            assertEquals(
                                    LocalRecommender.RETENTION_BONUS,
                                    recommendation.getRecommendation());
                            assertEquals(0.95, recommendation.getRecommendationScore(), 1e-9);
                            assertNotNull(recommendation.getProcessingTimestamp());
                            // The processing timestamp must be a valid BigQuery TIMESTAMP.
                            Instant.parse(recommendation.getProcessingTimestamp());
                            return null;
                        });
        PAssert.that(result.get(RecommendationInference.ERROR_TAG)).empty();

        pipeline.run();
    }

    @Test
    public void testFailingModelDeadLettersInsteadOfFailingTheBundle() {
        PCollectionTuple result =
                pipeline.apply(Create.of(enrichedEvent()))
                        .apply("Score", RecommendationInference.of(new FailingRecommender()));

        PAssert.that(result.get(RecommendationInference.SUCCESS_TAG)).empty();
        PAssert.that(result.get(RecommendationInference.ERROR_TAG))
                .satisfies(
                        errors -> {
                            ProcessingError error = errors.iterator().next();
                            assertEquals(RecommendationInference.INFERENCE_STAGE, error.getStage());
                            assertTrue(error.getErrorMessage().contains("model unavailable"));
                            assertTrue(error.getPayload().contains("player_0001"));
                            return null;
                        });

        pipeline.run();
    }

    @Test
    public void testEventWithoutFeaturesIsStillScored() {
        EnrichedEvent withoutFeatures =
                EnrichedEvent.of(enrichedEvent().getEvent(), Collections.emptyMap());

        PCollectionTuple result =
                pipeline.apply(Create.of(withoutFeatures))
                        .apply("Score", RecommendationInference.of(new LocalRecommender()));

        PAssert.that(result.get(RecommendationInference.SUCCESS_TAG))
                .satisfies(
                        recommendations -> {
                            assertEquals(
                                    LocalRecommender.DIFFICULTY_ASSIST,
                                    recommendations.iterator().next().getRecommendation());
                            return null;
                        });

        pipeline.run();
    }
}
