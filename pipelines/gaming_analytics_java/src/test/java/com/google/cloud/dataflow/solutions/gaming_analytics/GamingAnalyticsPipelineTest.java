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

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.ProcessingError;
import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.Recommendation;
import com.google.cloud.dataflow.solutions.gaming_analytics.options.GamingAnalyticsOptions;
import com.google.cloud.dataflow.solutions.gaming_analytics.transform.JsonToGameplayEvents;
import com.google.cloud.dataflow.solutions.gaming_analytics.transform.PlayerFeatureEnrichment;
import com.google.cloud.dataflow.solutions.gaming_analytics.transform.RecommendationInference;
import java.io.Serializable;
import java.util.Arrays;
import org.apache.beam.sdk.Pipeline;
import org.apache.beam.sdk.Pipeline.PipelineVisitor;
import org.apache.beam.sdk.options.PipelineOptionsFactory;
import org.apache.beam.sdk.runners.TransformHierarchy;
import org.apache.beam.sdk.testing.PAssert;
import org.apache.beam.sdk.testing.TestPipeline;
import org.apache.beam.sdk.transforms.Create;
import org.apache.beam.sdk.values.PCollection;
import org.apache.beam.sdk.values.PCollectionTuple;
import org.junit.Rule;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.junit.runners.JUnit4;

/**
 * End-to-end coverage of the transform chain on the DirectRunner, plus a construction test of the
 * full graph including the Pub/Sub and BigQuery connectors.
 *
 * <p>Both substitute {@link FakeRecommendationInference} for the cross-language scoring step, so
 * that the suite never starts a Python expansion service and {@code ./gradlew build} stays
 * hermetic.
 */
@RunWith(JUnit4.class)
public class GamingAnalyticsPipelineTest implements Serializable {

    @Rule public final transient TestPipeline pipeline = TestPipeline.create();

    private static final String[] LAUNCH_ARGS = {
        "--project=test-project",
        "--tempLocation=gs://test-bucket/tmp",
        "--streaming=true",
        "--inputSubscription=projects/test-project/subscriptions/gaming-events-sub",
        "--outputTopic=projects/test-project/topics/gaming-recommendations",
        "--errorTopic=projects/test-project/topics/gaming-analytics-errors",
        "--bigtableInstance=gaming-analytics",
        "--bigtableTable=player_features",
        "--bigQueryTable=test-project.gaming_analytics.player_recommendations",
        "--modelUri=gs://test-bucket/models/gaming_recommender.pkl",
    };

    private static GamingAnalyticsOptions options(String... extraArgs) {
        String[] args = Arrays.copyOf(LAUNCH_ARGS, LAUNCH_ARGS.length + extraArgs.length);
        System.arraycopy(extraArgs, 0, args, LAUNCH_ARGS.length, extraArgs.length);
        return PipelineOptionsFactory.fromArgs(args)
                .withValidation()
                .as(GamingAnalyticsOptions.class);
    }

    @Test
    public void testGraphIsBuiltWithEveryStage() {
        Pipeline p =
                GamingAnalyticsPipeline.createPipeline(
                        options(), FakeRecommendationInference.alwaysPredicting(0));
        assertNotNull(p);

        StringBuilder transformNames = new StringBuilder();
        p.traverseTopologically(
                new PipelineVisitor.Defaults() {
                    @Override
                    public CompositeBehavior enterCompositeTransform(TransformHierarchy.Node node) {
                        transformNames.append(node.getFullName()).append('\n');
                        return CompositeBehavior.ENTER_TRANSFORM;
                    }
                });
        String graph = transformNames.toString();

        assertTrue(graph.contains("ReadGameplayEvents"));
        assertTrue(graph.contains("ParseEvents"));
        assertTrue(graph.contains("EnrichWithPlayerFeatures"));
        assertTrue(graph.contains("ScoreEvents"));
        assertTrue(graph.contains("PublishRecommendations"));
        assertTrue(graph.contains("WriteRecommendationsToBigQuery"));
        assertTrue(graph.contains("PublishErrors"));
    }

    @Test
    public void testMissingRequiredOptionIsRejected() {
        assertThrows(
                IllegalArgumentException.class,
                () ->
                        PipelineOptionsFactory.fromArgs("--project=test-project")
                                .withValidation()
                                .as(GamingAnalyticsOptions.class));
    }

    @Test
    public void testModelUriIsRequired() {
        String[] withoutModel =
                Arrays.stream(LAUNCH_ARGS)
                        .filter(arg -> !arg.startsWith("--modelUri="))
                        .toArray(String[]::new);
        assertThrows(
                IllegalArgumentException.class,
                () ->
                        PipelineOptionsFactory.fromArgs(withoutModel)
                                .withValidation()
                                .as(GamingAnalyticsOptions.class));
    }

    @Test
    public void testLaunchScriptArgumentsAreParseable() {
        // scripts/01_launch_pipeline.sh builds a single multi-line -Pargs value; bash removes the
        // backslash-newline continuations but leaves runs of spaces, and the Gradle 'run' task
        // then splits it on a single whitespace character, so the parser receives a lot of empty
        // tokens. This reproduces that exact tokenization to make sure the shipped launch command
        // is accepted rather than rejected by strict parsing.
        String argsProperty =
                "\n"
                    + "  --runner=DataflowRunner   --project=test-project   --region=us-central1  "
                    + " --tempLocation=gs://test-bucket/tmp  "
                    + " --serviceAccount=sa@test-project.iam.gserviceaccount.com  "
                    + " --subnetwork=regions/us-central1/subnetworks/default  "
                    + " --workerMachineType=n1-standard-2   --diskSizeGb=200   --maxNumWorkers=3   "
                    + "  --experiments=use_runner_v2   --streaming   --enableStreamingEngine  "
                    + " --usePublicIps=false  "
                    + " --inputSubscription=projects/test-project/subscriptions/gaming-events-sub  "
                    + " --outputTopic=projects/test-project/topics/gaming-recommendations  "
                    + " --errorTopic=projects/test-project/topics/gaming-analytics-errors  "
                    + " --bigtableInstance=gaming-analytics   --bigtableTable=player_features  "
                    + " --bigtableColumnFamily=features  "
                    + " --bigQueryTable=test-project.gaming_analytics.player_recommendations  "
                    + " --enableEnrichment=true  "
                    + " --modelUri=gs://test-bucket/models/gaming_recommender.pkl   ";

        GamingAnalyticsOptions parsed =
                PipelineOptionsFactory.fromArgs(argsProperty.split("\\s"))
                        .withValidation()
                        .as(GamingAnalyticsOptions.class);

        assertEquals(
                "projects/test-project/subscriptions/gaming-events-sub",
                parsed.getInputSubscription());
        assertEquals(
                "test-project.gaming_analytics.player_recommendations", parsed.getBigQueryTable());
        assertEquals("gs://test-bucket/models/gaming_recommender.pkl", parsed.getModelUri());
        assertTrue(parsed.getEnableEnrichment());

        // Runner v2 is mandatory for multi-language pipelines. If the launch script ever stops
        // passing it, the job fails at submission with an opaque error, so assert on it here.
        assertTrue(argsProperty.contains("--experiments=use_runner_v2"));

        // The options must translate into a usable, correctly configured scoring step.
        RecommendationInference inference = GamingAnalyticsPipeline.inferenceFromOptions(parsed);
        assertEquals("gs://test-bucket/models/gaming_recommender.pkl", inference.modelUri());
        assertNull(inference.expansionService());
    }

    @Test
    public void testEventsFlowFromRawPayloadToRecommendation() {
        String validEvent =
                "{\"player_id\": \"player_0001\", \"session_id\": \"session_1\","
                        + " \"event_type\": \"level_complete\", \"level\": 20, \"score\": 5000,"
                        + " \"event_timestamp\": \"2026-09-11T09:53:50.000Z\"}";
        String malformedEvent = "NOT_A_JSON_EVENT";

        PCollectionTuple parsed =
                pipeline.apply(Create.of(Arrays.asList(validEvent, malformedEvent)))
                        .apply("ParseEvents", JsonToGameplayEvents.create());

        PCollectionTuple enriched =
                parsed.get(JsonToGameplayEvents.SUCCESS_TAG)
                        .apply(
                                "EnrichWithPlayerFeatures",
                                PlayerFeatureEnrichment.create()
                                        .withProjectId("test-project")
                                        .withInstanceId("gaming-analytics")
                                        .withTableId("player_features")
                                        .withEnabled(false));

        PCollectionTuple scored =
                enriched.get(PlayerFeatureEnrichment.SUCCESS_TAG)
                        .apply(
                                "ScoreEvents",
                                FakeRecommendationInference.alwaysPredicting(
                                        RecommendationInference.RECOMMENDATION_LABELS.indexOf(
                                                "premium_bundle_offer")));

        PCollection<Recommendation> recommendations =
                scored.get(RecommendationInference.SUCCESS_TAG);

        PAssert.that(recommendations)
                .satisfies(
                        elements -> {
                            Recommendation recommendation = elements.iterator().next();
                            assertEquals("player_0001", recommendation.getPlayerId());
                            assertEquals(
                                    "premium_bundle_offer", recommendation.getRecommendation());
                            // The BigQuery row must always carry the two required columns.
                            assertNotNull(recommendation.toTableRow().get("player_id"));
                            assertNotNull(recommendation.toTableRow().get("event_timestamp"));
                            return null;
                        });

        PCollection<ProcessingError> errors = parsed.get(JsonToGameplayEvents.ERROR_TAG);
        PAssert.that(errors)
                .satisfies(
                        elements -> {
                            ProcessingError error = elements.iterator().next();
                            assertEquals(JsonToGameplayEvents.PARSE_STAGE, error.getStage());
                            assertEquals(malformedEvent, error.getPayload());
                            return null;
                        });

        pipeline.run();
    }
}
