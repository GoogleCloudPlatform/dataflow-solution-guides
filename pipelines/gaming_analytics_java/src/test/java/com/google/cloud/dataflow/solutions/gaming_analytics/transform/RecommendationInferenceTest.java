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
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.EnrichedEvent;
import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.GameplayEvent;
import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.ProcessingError;
import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.Recommendation;
import com.google.common.collect.ImmutableList;
import com.google.common.collect.ImmutableMap;
import java.io.ByteArrayInputStream;
import java.io.Serializable;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Base64;
import java.util.List;
import java.util.Map;
import org.apache.beam.sdk.coders.KvCoder;
import org.apache.beam.sdk.coders.RowCoder;
import org.apache.beam.sdk.schemas.Schema;
import org.apache.beam.sdk.testing.PAssert;
import org.apache.beam.sdk.testing.TestPipeline;
import org.apache.beam.sdk.transforms.Create;
import org.apache.beam.sdk.values.KV;
import org.apache.beam.sdk.values.PCollectionTuple;
import org.apache.beam.sdk.values.Row;
import org.joda.time.Instant;
import org.junit.Rule;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.junit.runners.JUnit4;

/**
 * Covers everything around the cross-language inference step: the conversion into the schema values
 * that cross the language boundary, and the conversion of the model output back into a
 * recommendation.
 *
 * <p>The cross-language step itself is deliberately not exercised here. Expanding it starts a
 * Python expansion service, which needs pip or Docker; see {@code
 * RecommendationInferenceCrossLanguageIT}.
 */
@RunWith(JUnit4.class)
public class RecommendationInferenceTest implements Serializable {

    @Rule public final transient TestPipeline pipeline = TestPipeline.create();

    private static GameplayEvent event() {
        return GameplayEvent.builder()
                .setPlayerId("player_0001")
                .setSessionId("session_1")
                .setEventType("level_failed")
                .setLevel(12)
                .setScore(9100L)
                .setEventTimestamp("2026-09-11T09:53:50.000Z")
                .build();
    }

    private static EnrichedEvent enrichedEvent(Map<String, String> features) {
        return EnrichedEvent.of(event(), features);
    }

    @Test
    public void testContextRowCarriesEveryPassThroughField() {
        Row context = RecommendationInference.toContextRow(event());

        assertEquals("player_0001", context.getString("player_id"));
        assertEquals("session_1", context.getString("session_id"));
        assertEquals("level_failed", context.getString("event_type"));
        assertEquals(Integer.valueOf(12), context.getInt32("level"));
        assertEquals(Long.valueOf(9100L), context.getInt64("score"));
        assertEquals("2026-09-11T09:53:50.000Z", context.getString("event_timestamp"));
    }

    @Test
    public void testContextRowToleratesTheOptionalFieldsBeingAbsent() {
        Row context =
                RecommendationInference.toContextRow(
                        GameplayEvent.builder()
                                .setPlayerId("player_0001")
                                .setEventTimestamp("2026-09-11T09:53:50.000Z")
                                .build());

        assertEquals("player_0001", context.getString("player_id"));
        assertNull(context.getString("session_id"));
        assertNull(context.getString("event_type"));
        assertNull(context.getInt32("level"));
        assertNull(context.getInt64("score"));
    }

    @Test
    public void testFeatureVectorLayoutMatchesTheDocumentedOrder() {
        ImmutableList<Double> vector =
                RecommendationInference.toFeatureVector(
                        enrichedEvent(
                                ImmutableMap.of(
                                        "churn_risk", "0.85",
                                        "level_failures", "4",
                                        "skill_rating", "1200",
                                        "spend_tier", "whale")));

        assertEquals(RecommendationInference.FEATURE_NAMES.size(), vector.size());
        assertEquals(12.0, vector.get(0), 1e-9); // level
        assertEquals(9100.0, vector.get(1), 1e-9); // score
        assertEquals(0.85, vector.get(2), 1e-9); // churn_risk
        assertEquals(4.0, vector.get(3), 1e-9); // level_failures
        assertEquals(1200.0, vector.get(4), 1e-9); // skill_rating
        assertEquals(2.0, vector.get(5), 1e-9); // spend_tier_score: whale
        assertEquals(2.0, vector.get(6), 1e-9); // event_type_code: level_failed
    }

    @Test
    public void testMissingAndMalformedFeaturesFallBackToZero() {
        ImmutableList<Double> vector =
                RecommendationInference.toFeatureVector(
                        enrichedEvent(
                                ImmutableMap.of(
                                        "churn_risk", "not-a-number",
                                        "level_failures", "   ",
                                        "spend_tier", "platinum")));

        assertEquals(0.0, vector.get(2), 1e-9); // unparseable
        assertEquals(0.0, vector.get(3), 1e-9); // blank
        assertEquals(0.0, vector.get(4), 1e-9); // absent
        assertEquals(0.0, vector.get(5), 1e-9); // unknown spend tier
    }

    @Test
    public void testAnEventWithoutLevelOrScoreStillProducesAFullVector() {
        ImmutableList<Double> vector =
                RecommendationInference.toFeatureVector(
                        EnrichedEvent.of(
                                GameplayEvent.builder()
                                        .setPlayerId("player_0001")
                                        .setEventTimestamp("2026-09-11T09:53:50.000Z")
                                        .build(),
                                ImmutableMap.of()));

        assertEquals(RecommendationInference.FEATURE_NAMES.size(), vector.size());
        for (Double value : vector) {
            assertEquals(0.0, value, 1e-9);
        }
    }

    @Test
    public void testArgMaxPicksTheHighestPropensity() {
        assertEquals(0, RecommendationInference.argMax(Arrays.asList(0.9, 0.1, 0.0, 0.0, 0.0)));
        assertEquals(3, RecommendationInference.argMax(Arrays.asList(0.1, 0.1, 0.2, 0.5, 0.1)));
        assertEquals(4, RecommendationInference.argMax(Arrays.asList(0.0, 0.0, 0.0, 0.0, 1.0)));
        // A tie resolves to the first label, deterministically.
        assertEquals(0, RecommendationInference.argMax(Arrays.asList(0.5, 0.5, 0.0, 0.0, 0.0)));
    }

    @Test
    public void testArgMaxToleratesNullPropensities() {
        assertEquals(2, RecommendationInference.argMax(Arrays.asList(null, null, 0.3, null, null)));
    }

    @Test
    public void testAModelWithTheWrongNumberOfOutputsIsRejected() {
        // This is what a mismatch between train_model.py and RECOMMENDATION_LABELS looks like at
        // runtime; it must be an explicit error, not a silently wrong label.
        List<Double> tooShort = Arrays.asList(0.1, 0.9);
        IllegalArgumentException thrown =
                assertThrows(
                        IllegalArgumentException.class,
                        () -> RecommendationInference.argMax(tooShort));
        assertTrue(thrown.getMessage().contains("expected 5"));

        assertThrows(IllegalArgumentException.class, () -> RecommendationInference.argMax(null));
    }

    @Test
    public void testLabelsAndFeatureNamesAreTheDocumentedContract() {
        // train_model.py hard codes both lists. If either changes here, it must change there.
        assertEquals(
                ImmutableList.of(
                        "level",
                        "score",
                        "churn_risk",
                        "level_failures",
                        "skill_rating",
                        "spend_tier_score",
                        "event_type_code"),
                RecommendationInference.FEATURE_NAMES);
        assertEquals(
                ImmutableList.of(
                        "retention_bonus_pack",
                        "difficulty_assist_boost",
                        "premium_bundle_offer",
                        "tournament_invite",
                        "daily_quest_suggestion"),
                RecommendationInference.RECOMMENDATION_LABELS);
    }

    @Test
    public void testTheExpansionServiceIsOnlyConfiguredWhenItIsSet() {
        RecommendationInference plain = RecommendationInference.withModel("gs://bucket/model.pkl");
        assertNull(plain.expansionService());
        assertNull(plain.withExpansionService("").expansionService());
        assertNull(plain.withExpansionService("   ").expansionService());
        assertNull(plain.withExpansionService(null).expansionService());
        assertEquals(
                "localhost:8097",
                plain.withExpansionService(" localhost:8097 ").expansionService());
        assertEquals("gs://bucket/model.pkl", plain.modelUri());
    }

    @Test
    public void testTheModelLoaderIsAKeyedSklearnHandler() {
        // The KV form of the cross-language RunInference only works with a keyed model handler.
        assertTrue(RecommendationInference.MODEL_LOADER.contains("KeyedModelHandler"));
        assertTrue(RecommendationInference.MODEL_LOADER.contains("SklearnModelHandlerNumpy"));
        assertTrue(
                RecommendationInference.MODEL_LOADER.contains("def get_model_handler(model_uri)"));
    }

    @Test
    public void testTheHarnessEnvironmentIsCompleteAndPinned() {
        // Passing an explicit list turns off Beam's own package inference, which would otherwise
        // have added scikit-learn and pandas. pandas is not optional: Beam's
        // apache_beam.ml.inference.sklearn_inference imports it at module scope, so dropping it
        // makes the model loader unevaluatable on a harness image that does not happen to ship it.
        assertEquals(
                ImmutableList.of("scikit-learn==1.7.2", "numpy==2.4.6", "pandas==2.3.3"),
                RecommendationInference.HARNESS_REQUIREMENTS);
        // Exact pins, never ranges: the model is a pickle.
        for (String requirement : RecommendationInference.HARNESS_REQUIREMENTS) {
            assertTrue(requirement + " must be an exact pin", requirement.contains("=="));
        }
    }

    @Test
    public void testTheSchemasCrossingTheBoundaryAreStable() {
        assertEquals(
                ImmutableList.of(
                        "player_id",
                        "session_id",
                        "event_type",
                        "level",
                        "score",
                        "event_timestamp"),
                RecommendationInference.CONTEXT_SCHEMA.getFieldNames());
        assertEquals(
                ImmutableList.of("example", "inference"),
                RecommendationInference.PREDICTION_SCHEMA.getFieldNames());
    }

    /**
     * The bytes the Python side really produces, decoded by the real Java coder.
     *
     * <p>This is the one part of the cross-language contract that no other test in this suite can
     * reach: the model returns a NumPy {@code float64} array, and it has to land in the {@code
     * ARRAY<DOUBLE>} {@code inference} field. Everything else about that hop is Beam's problem;
     * this is ours, because {@link RecommendationInference#PREDICTION_SCHEMA} is our declaration.
     *
     * <p>The fixture was captured by running the real {@code
     * KeyedModelHandler(SklearnModelHandlerNumpy(...))} of the Python Beam SDK 2.76.0 over a model
     * built by {@code scripts/train_model.py}, and encoding the resulting {@code PredictionResult}
     * with the Python {@code RowCoder} for exactly the schemas declared here — that is, with the
     * pair of coders {@code RunInference.ofKVs(MODEL_LOADER, PREDICTION_SCHEMA,
     * RowCoder.of(CONTEXT_SCHEMA))} configures. Decoding it here is byte-for-byte what a worker
     * does. The input was the {@code churn_risk=0.95} player below, whose feature vector the model
     * maps to {@code retention_bonus_pack}.
     *
     * <p>Regenerating it is only necessary if the schemas change; see the README section on the
     * cross-language integration test.
     */
    private static final String PYTHON_ENCODED_PREDICTION =
            "BgALcGxheWVyXzAwMDEJc2Vzc2lvbl8xDGxldmVsX2ZhaWxlZAyMRxgyMDI2LTA5LTExVDA5OjUzOjUwLjAw"
                    + "MFoCAAAAAAdAKAAAAAAAAEDBxgAAAAAAP+5mZmZmZmZAEAAAAAAAAECSwAAAAAAAAAAAAAAAAABA"
                    + "AAAAAAAAAAAAAAU/8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";

    @Test
    public void testAPredictionEncodedByThePythonSdkDecodesIntoARecommendation() throws Exception {
        KvCoder<Row, Row> coder =
                KvCoder.of(
                        RowCoder.of(RecommendationInference.CONTEXT_SCHEMA),
                        RowCoder.of(RecommendationInference.PREDICTION_SCHEMA));
        KV<Row, Row> decoded =
                coder.decode(
                        new ByteArrayInputStream(
                                Base64.getDecoder().decode(PYTHON_ENCODED_PREDICTION)));

        // The key survived the round trip unchanged.
        assertEquals("player_0001", decoded.getKey().getString("player_id"));
        assertEquals("session_1", decoded.getKey().getString("session_id"));
        assertEquals(Integer.valueOf(12), decoded.getKey().getInt32("level"));
        assertEquals(Long.valueOf(9100L), decoded.getKey().getInt64("score"));

        // The feature vector Python echoes back in 'example' is the one Java sent.
        assertEquals(
                ImmutableList.of(12.0, 9100.0, 0.95, 4.0, 1200.0, 0.0, 2.0),
                ImmutableList.copyOf(decoded.getValue().<Double>getArray("example")));

        // The NumPy array really does arrive as one Double per recommendation label.
        List<Double> inference =
                ImmutableList.copyOf(decoded.getValue().<Double>getArray("inference"));
        assertEquals(RecommendationInference.RECOMMENDATION_LABELS.size(), inference.size());
        assertEquals(
                "retention_bonus_pack",
                RecommendationInference.RECOMMENDATION_LABELS.get(
                        RecommendationInference.argMax(inference)));
    }

    @Test
    public void testEnrichedEventsBecomeKeyedFeatureVectors() {
        PCollectionTuple result =
                pipeline.apply(Create.of(enrichedEvent(ImmutableMap.of("churn_risk", "0.85"))))
                        .apply("ToFeatureVectors", RecommendationInference.toFeatureVectors());

        PAssert.that(result.get(RecommendationInference.FEATURES_TAG))
                .satisfies(
                        elements -> {
                            KV<Row, Iterable<Double>> element = elements.iterator().next();
                            assertEquals("player_0001", element.getKey().getString("player_id"));
                            assertEquals(
                                    RecommendationInference.FEATURE_NAMES.size(),
                                    ImmutableList.copyOf(element.getValue()).size());
                            return null;
                        });
        PAssert.that(result.get(RecommendationInference.ERROR_TAG)).empty();

        pipeline.run();
    }

    @Test
    public void testAnEventWithoutPlayerIdIsDeadLetteredRatherThanFailingTheBundle() {
        // player_id is not nullable in CONTEXT_SCHEMA, so building the key row throws. The
        // activation path must degrade, not stall.
        EnrichedEvent orphan =
                EnrichedEvent.of(
                        GameplayEvent.builder()
                                .setEventTimestamp("2026-09-11T09:53:50.000Z")
                                .build(),
                        ImmutableMap.of());

        PCollectionTuple result =
                pipeline.apply(Create.of(orphan))
                        .apply("ToFeatureVectors", RecommendationInference.toFeatureVectors());

        PAssert.that(result.get(RecommendationInference.FEATURES_TAG)).empty();
        PAssert.that(result.get(RecommendationInference.ERROR_TAG))
                .satisfies(
                        errors -> {
                            assertEquals(
                                    RecommendationInference.INFERENCE_STAGE,
                                    errors.iterator().next().getStage());
                            return null;
                        });

        pipeline.run();
    }

    @Test
    public void testModelOutputBecomesARecommendation() {
        PCollectionTuple result =
                pipeline.apply(
                                Create.of(
                                                KV.of(
                                                        RecommendationInference.toContextRow(
                                                                event()),
                                                        predictionRow(
                                                                0.05, 0.75, 0.10, 0.05, 0.05)))
                                        .withCoder(
                                                KvCoder.of(
                                                        RowCoder.of(
                                                                RecommendationInference
                                                                        .CONTEXT_SCHEMA),
                                                        RowCoder.of(
                                                                RecommendationInference
                                                                        .PREDICTION_SCHEMA))))
                        .apply("ToRecommendations", RecommendationInference.toRecommendations());

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
                                    "difficulty_assist_boost", recommendation.getRecommendation());
                            assertEquals(0.75, recommendation.getRecommendationScore(), 1e-9);
                            assertEquals(
                                    "2026-09-11T09:53:50.000Z", recommendation.getEventTimestamp());
                            // The processing timestamp must be a valid BigQuery TIMESTAMP.
                            Instant.parse(recommendation.getProcessingTimestamp());
                            return null;
                        });
        PAssert.that(result.get(RecommendationInference.ERROR_TAG)).empty();

        pipeline.run();
    }

    @Test
    public void testAPredictionWithTheWrongShapeIsDeadLettered() {
        Schema wrongSchema =
                Schema.of(
                        Schema.Field.of("example", Schema.FieldType.array(Schema.FieldType.DOUBLE)),
                        Schema.Field.of(
                                "inference", Schema.FieldType.array(Schema.FieldType.DOUBLE)));
        Row wrongPrediction =
                Row.withSchema(wrongSchema)
                        .withFieldValue("example", Arrays.asList(1.0, 2.0))
                        // Two propensities instead of five: the wrong model was staged.
                        .withFieldValue("inference", Arrays.asList(0.4, 0.6))
                        .build();

        PCollectionTuple result =
                pipeline.apply(
                                Create.of(
                                                KV.of(
                                                        RecommendationInference.toContextRow(
                                                                event()),
                                                        wrongPrediction))
                                        .withCoder(
                                                KvCoder.of(
                                                        RowCoder.of(
                                                                RecommendationInference
                                                                        .CONTEXT_SCHEMA),
                                                        RowCoder.of(wrongSchema))))
                        .apply("ToRecommendations", RecommendationInference.toRecommendations());

        PAssert.that(result.get(RecommendationInference.SUCCESS_TAG)).empty();
        PAssert.that(result.get(RecommendationInference.ERROR_TAG))
                .satisfies(
                        errors -> {
                            ProcessingError error = errors.iterator().next();
                            assertEquals(RecommendationInference.INFERENCE_STAGE, error.getStage());
                            assertTrue(error.getErrorMessage().contains("expected 5"));
                            assertTrue(error.getPayload().contains("player_0001"));
                            return null;
                        });

        pipeline.run();
    }

    private static Row predictionRow(double... propensities) {
        List<Double> boxed = new ArrayList<>();
        for (double propensity : propensities) {
            boxed.add(propensity);
        }
        return Row.withSchema(RecommendationInference.PREDICTION_SCHEMA)
                .withFieldValue("example", Arrays.asList(12.0, 9100.0, 0.0, 0.0, 0.0, 0.0, 2.0))
                .withFieldValue("inference", boxed)
                .build();
    }
}
