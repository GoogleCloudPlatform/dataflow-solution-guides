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
package com.google.cloud.dataflow.solutions.gaming_analytics;

import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.EnrichedEvent;
import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.GameplayEvent;
import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.ProcessingError;
import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.Recommendation;
import com.google.cloud.dataflow.solutions.gaming_analytics.extract.GameplayEventReader;
import com.google.cloud.dataflow.solutions.gaming_analytics.load.PubSubPublishers;
import com.google.cloud.dataflow.solutions.gaming_analytics.load.RecommendationBigQuerySink;
import com.google.cloud.dataflow.solutions.gaming_analytics.options.GamingAnalyticsOptions;
import com.google.cloud.dataflow.solutions.gaming_analytics.transform.JsonToGameplayEvents;
import com.google.cloud.dataflow.solutions.gaming_analytics.transform.PlayerFeatureEnrichment;
import com.google.cloud.dataflow.solutions.gaming_analytics.transform.RecommendationInference;
import org.apache.beam.sdk.Pipeline;
import org.apache.beam.sdk.extensions.gcp.options.GcpOptions;
import org.apache.beam.sdk.io.gcp.bigquery.WriteResult;
import org.apache.beam.sdk.options.PipelineOptionsFactory;
import org.apache.beam.sdk.transforms.Flatten;
import org.apache.beam.sdk.transforms.PTransform;
import org.apache.beam.sdk.values.PCollection;
import org.apache.beam.sdk.values.PCollectionList;
import org.apache.beam.sdk.values.PCollectionTuple;

/**
 * Real-time gaming analytics and in-game activation pipeline.
 *
 * <p>Gameplay events are read from Pub/Sub, hydrated with the player features held in Cloud
 * Bigtable, scored with Apache Beam {@code RunInference} (through its cross-language wrapper, see
 * {@link RecommendationInference}), and then published back to Pub/Sub for immediate in-game
 * activation while an analytical copy is streamed into BigQuery. Every element the pipeline cannot
 * process is routed to the dead-letter topic rather than being dropped.
 *
 * <p>Because the scoring step is a multi-language transform, this pipeline requires <b>Dataflow
 * Runner v2</b>: {@code scripts/03_launch_pipeline.sh} passes {@code --experiments=use_runner_v2}.
 */
public class GamingAnalyticsPipeline {

    public static void main(String[] args) {
        PipelineOptionsFactory.register(GamingAnalyticsOptions.class);
        GamingAnalyticsOptions options =
                PipelineOptionsFactory.fromArgs(args)
                        .withValidation()
                        .as(GamingAnalyticsOptions.class);

        Pipeline p = createPipeline(options, inferenceFromOptions(options));
        p.run();
    }

    /** Builds the cross-language scoring step described by the pipeline options. */
    public static RecommendationInference inferenceFromOptions(GamingAnalyticsOptions options) {
        return RecommendationInference.withModel(options.getModelUri())
                .withExpansionService(options.getExpansionService());
    }

    /**
     * Builds the pipeline.
     *
     * @param options the pipeline configuration
     * @param inference the scoring step, injected rather than built here. The production
     *     implementation is a multi-language transform that contacts a Python expansion service
     *     when it expands, so the tests that assert on the shape of the graph substitute an
     *     in-process stand-in and stay hermetic. Whatever is passed must emit {@link
     *     RecommendationInference#SUCCESS_TAG} and {@link RecommendationInference#ERROR_TAG}.
     */
    public static Pipeline createPipeline(
            GamingAnalyticsOptions options,
            PTransform<PCollection<EnrichedEvent>, PCollectionTuple> inference) {
        Pipeline p = Pipeline.create(options);

        String bigtableProject =
                options.getBigtableProject() != null && !options.getBigtableProject().isEmpty()
                        ? options.getBigtableProject()
                        : options.as(GcpOptions.class).getProject();

        // E: read the raw gameplay events published by the gaming platform servers.
        PCollection<String> payloads =
                p.apply(
                        "ReadGameplayEvents",
                        GameplayEventReader.fromSubscription(options.getInputSubscription()));

        // T: parse and validate the payloads.
        PCollectionTuple parsed = payloads.apply("ParseEvents", JsonToGameplayEvents.create());
        PCollection<GameplayEvent> events = parsed.get(JsonToGameplayEvents.SUCCESS_TAG);

        // T: hydrate the id-only events with the Bigtable feature store.
        PCollectionTuple enriched =
                events.apply(
                        "EnrichWithPlayerFeatures",
                        PlayerFeatureEnrichment.create()
                                .withProjectId(bigtableProject)
                                .withInstanceId(options.getBigtableInstance())
                                .withTableId(options.getBigtableTable())
                                .withColumnFamily(options.getBigtableColumnFamily())
                                .withEnabled(
                                        options.getEnableEnrichment() == null
                                                || options.getEnableEnrichment()));

        // T: score the events with RunInference.
        PCollectionTuple scored =
                enriched.get(PlayerFeatureEnrichment.SUCCESS_TAG).apply("ScoreEvents", inference);

        PCollection<Recommendation> recommendations =
                scored.get(RecommendationInference.SUCCESS_TAG);

        // L: activation path. Pub/Sub buffers the recommendations for the gaming platform.
        recommendations.apply(
                "PublishRecommendations",
                PubSubPublishers.publishRecommendations(options.getOutputTopic()));

        // L: analytics path. The same records are streamed into BigQuery.
        WriteResult writeResult =
                recommendations.apply(
                        "WriteRecommendationsToBigQuery",
                        RecommendationBigQuerySink.write()
                                .withTable(options.getBigQueryTable())
                                .build());

        // L: everything that could not be processed goes to the dead-letter topic.
        PCollection<ProcessingError> bigQueryErrors =
                writeResult
                        .getFailedStorageApiInserts()
                        .apply(
                                "FailedInsertsToErrors",
                                RecommendationBigQuerySink.failedInsertsToErrors());

        PCollectionList.of(parsed.get(JsonToGameplayEvents.ERROR_TAG))
                .and(enriched.get(PlayerFeatureEnrichment.ERROR_TAG))
                .and(scored.get(RecommendationInference.ERROR_TAG))
                .and(bigQueryErrors)
                .apply("FlattenErrors", Flatten.pCollections())
                .apply("PublishErrors", PubSubPublishers.publishErrors(options.getErrorTopic()));

        return p;
    }
}
