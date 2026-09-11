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

import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.ProcessingError;
import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.Recommendation;
import com.google.common.collect.ImmutableMap;
import java.nio.charset.StandardCharsets;
import org.apache.beam.sdk.io.gcp.pubsub.PubsubIO;
import org.apache.beam.sdk.io.gcp.pubsub.PubsubMessage;
import org.apache.beam.sdk.metrics.Counter;
import org.apache.beam.sdk.metrics.Metrics;
import org.apache.beam.sdk.transforms.DoFn;
import org.apache.beam.sdk.transforms.PTransform;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.values.PCollection;
import org.apache.beam.sdk.values.PDone;

/**
 * Pub/Sub sinks of the pipeline.
 *
 * <p>Pub/Sub is the primary output of this architecture: recommendations are buffered there so the
 * gaming platform can activate them in game with minimal delay. Unprocessable elements go to the
 * separate dead-letter topic, where they can be inspected and reprocessed.
 *
 * <p><b>On message attributes.</b> An attribute is only worth setting when a consumer uses it
 * without reading the payload, which in practice means a Pub/Sub subscription filter or an ordering
 * key. Recommendations therefore carry <b>no</b> attributes: {@code player_id}, {@code event_type}
 * and {@code recommendation} are all in the JSON body, no consumer in this guide filters on them,
 * and duplicating them would create a second copy of the record — including the model output — that
 * can silently drift from the body. If your activation consumer needs per-player ordering, set a
 * Pub/Sub ordering key rather than an attribute.
 *
 * <p>Dead-letter records do carry a {@code stage} attribute, because that one has a concrete use:
 * it lets an operator create a filtered subscription per failure stage (for example {@code
 * attributes.stage = "enrich"}) and triage failures without parsing every payload.
 */
public final class PubSubPublishers {

    private PubSubPublishers() {}

    public static PTransform<PCollection<Recommendation>, PDone> publishRecommendations(
            String topic) {
        return new PublishRecommendations(topic);
    }

    public static PTransform<PCollection<ProcessingError>, PDone> publishErrors(String topic) {
        return new PublishErrors(topic);
    }

    /** Builds the Pub/Sub message carrying a recommendation. */
    public static PubsubMessage toPubsubMessage(Recommendation recommendation) {
        return new PubsubMessage(
                recommendation.toJsonString().getBytes(StandardCharsets.UTF_8), ImmutableMap.of());
    }

    /** Builds the Pub/Sub message carrying a dead-letter record. */
    public static PubsubMessage toPubsubMessage(ProcessingError error) {
        return new PubsubMessage(
                error.toJsonString().getBytes(StandardCharsets.UTF_8),
                ImmutableMap.of("stage", error.getStage()));
    }

    private static class PublishRecommendations
            extends PTransform<PCollection<Recommendation>, PDone> {
        private final String topic;

        PublishRecommendations(String topic) {
            this.topic = topic;
        }

        @Override
        public PDone expand(PCollection<Recommendation> input) {
            return input.apply(
                            "RecommendationToPubsubMessage",
                            ParDo.of(
                                    new DoFn<Recommendation, PubsubMessage>() {
                                        private final Counter publishedRecommendations =
                                                Metrics.counter(
                                                        PubSubPublishers.class,
                                                        "published-recommendations");

                                        @ProcessElement
                                        public void processElement(
                                                @Element Recommendation recommendation,
                                                OutputReceiver<PubsubMessage> output) {
                                            publishedRecommendations.inc();
                                            output.output(toPubsubMessage(recommendation));
                                        }
                                    }))
                    .apply("PublishRecommendations", PubsubIO.writeMessages().to(topic));
        }
    }

    private static class PublishErrors extends PTransform<PCollection<ProcessingError>, PDone> {
        private final String topic;

        PublishErrors(String topic) {
            this.topic = topic;
        }

        @Override
        public PDone expand(PCollection<ProcessingError> input) {
            return input.apply(
                            "ErrorToPubsubMessage",
                            ParDo.of(
                                    new DoFn<ProcessingError, PubsubMessage>() {
                                        private final Counter deadletteredElements =
                                                Metrics.counter(
                                                        PubSubPublishers.class,
                                                        "deadlettered-elements");

                                        @ProcessElement
                                        public void processElement(
                                                @Element ProcessingError error,
                                                OutputReceiver<PubsubMessage> output) {
                                            deadletteredElements.inc();
                                            output.output(toPubsubMessage(error));
                                        }
                                    }))
                    .apply("PublishErrors", PubsubIO.writeMessages().to(topic));
        }
    }
}
