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

import com.google.auto.value.AutoValue;
import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.EnrichedEvent;
import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.GameplayEvent;
import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.ProcessingError;
import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.Recommendation;
import com.google.cloud.dataflow.solutions.gaming_analytics.inference.Prediction;
import com.google.cloud.dataflow.solutions.gaming_analytics.inference.Recommender;
import org.apache.beam.sdk.metrics.Counter;
import org.apache.beam.sdk.metrics.Metrics;
import org.apache.beam.sdk.transforms.DoFn;
import org.apache.beam.sdk.transforms.PTransform;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.values.PCollection;
import org.apache.beam.sdk.values.PCollectionTuple;
import org.apache.beam.sdk.values.TupleTag;
import org.apache.beam.sdk.values.TupleTagList;
import org.joda.time.Instant;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Scores enriched gameplay events with the configured {@link Recommender}.
 *
 * <p>Anything the recommender throws is turned into a dead-letter record instead of failing the
 * bundle: an in-game activation path must degrade rather than stall when the model misbehaves.
 */
@AutoValue
public abstract class RecommendationInference
        extends PTransform<PCollection<EnrichedEvent>, PCollectionTuple> {

    private static final Logger LOG = LoggerFactory.getLogger(RecommendationInference.class);

    public static final TupleTag<Recommendation> SUCCESS_TAG =
            new TupleTag<Recommendation>("SUCCESS_TAG") {};
    public static final TupleTag<ProcessingError> ERROR_TAG =
            new TupleTag<ProcessingError>("ERROR_TAG") {};

    public static final String INFERENCE_STAGE = "inference";

    public abstract Recommender recommender();

    public static RecommendationInference of(Recommender recommender) {
        return new AutoValue_RecommendationInference(recommender);
    }

    @Override
    public PCollectionTuple expand(PCollection<EnrichedEvent> input) {
        return input.apply(
                "ScoreEvents",
                ParDo.of(new InferenceDoFn(recommender()))
                        .withOutputTags(SUCCESS_TAG, TupleTagList.of(ERROR_TAG)));
    }

    private static class InferenceDoFn extends DoFn<EnrichedEvent, Recommendation> {
        private final Counter scoredEvents =
                Metrics.counter(RecommendationInference.class, "scored-events");
        private final Counter inferenceErrors =
                Metrics.counter(RecommendationInference.class, "inference-errors");

        private final Recommender recommender;

        InferenceDoFn(Recommender recommender) {
            this.recommender = recommender;
        }

        @SuppressWarnings({"unused", "EffectivelyPrivate"})
        @Setup
        public void setup() throws Exception {
            recommender.setUp();
        }

        @SuppressWarnings({"unused", "EffectivelyPrivate"})
        @Teardown
        public void teardown() {
            recommender.tearDown();
        }

        @ProcessElement
        public void processElement(
                @Element EnrichedEvent enriched,
                @Timestamp Instant timestamp,
                MultiOutputReceiver output) {
            try {
                Prediction prediction = recommender.predict(enriched);
                GameplayEvent event = enriched.getEvent();
                scoredEvents.inc();
                output.get(SUCCESS_TAG)
                        .output(
                                Recommendation.builder()
                                        .setPlayerId(event.getPlayerId())
                                        .setSessionId(event.getSessionId())
                                        .setEventType(event.getEventType())
                                        .setLevel(event.getLevel())
                                        .setScore(event.getScore())
                                        .setRecommendation(prediction.label())
                                        .setRecommendationScore(prediction.score())
                                        .setEventTimestamp(event.getEventTimestamp())
                                        .setProcessingTimestamp(Instant.now().toString())
                                        .build());
            } catch (Exception e) {
                LOG.warn("Could not score gameplay event: {}", e.getMessage());
                inferenceErrors.inc();
                output.get(ERROR_TAG)
                        .output(
                                ProcessingError.of(
                                        INFERENCE_STAGE,
                                        enriched.toString(),
                                        e.toString(),
                                        timestamp.toString()));
            }
        }
    }
}
