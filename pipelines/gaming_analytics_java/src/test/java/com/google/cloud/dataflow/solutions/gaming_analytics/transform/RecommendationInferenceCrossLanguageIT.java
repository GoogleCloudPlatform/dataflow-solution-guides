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
import static org.junit.Assert.assertThrows;
import static org.junit.Assume.assumeTrue;

import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.EnrichedEvent;
import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.GameplayEvent;
import com.google.common.collect.ImmutableList;
import com.google.common.collect.ImmutableMap;
import java.io.Serializable;
import org.apache.beam.sdk.Pipeline;
import org.apache.beam.sdk.coders.KvCoder;
import org.apache.beam.sdk.coders.RowCoder;
import org.apache.beam.sdk.extensions.python.transforms.RunInference;
import org.apache.beam.sdk.testing.TestPipeline;
import org.apache.beam.sdk.transforms.Create;
import org.apache.beam.sdk.values.KV;
import org.apache.beam.sdk.values.PCollection;
import org.apache.beam.sdk.values.PCollectionTuple;
import org.apache.beam.sdk.values.Row;
import org.junit.Before;
import org.junit.Rule;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.junit.runners.JUnit4;

/**
 * Opt-in integration test of the real cross-language {@link RecommendationInference}.
 *
 * <p><b>This test is skipped unless you ask for it.</b> Expanding a multi-language transform starts
 * a Python expansion service, which builds a virtualenv and downloads Apache Beam and the pinned
 * model packages from PyPI. That may not be a requirement of {@code ./gradlew build} or of CI, so
 * the test aborts through {@link org.junit.Assume} when {@code runCrossLanguageTests} is not set.
 *
 * <h2>What this test does and does not prove</h2>
 *
 * <p>It expands the transform for real: the Python expansion service is started, it evaluates
 * {@link RecommendationInference#MODEL_LOADER}, constructs {@code KeyedModelHandler(
 * SklearnModelHandlerNumpy(model_uri))}, and returns the expanded subgraph. That is what validates
 * the model loader source, the keyword arguments, the schemas and the coders against the real
 * Python API — the parts most likely to be wrong.
 *
 * <p>It deliberately does <b>not</b> execute the pipeline. The Java {@code DirectRunner} is not a
 * portable runner and cannot run an expanded external transform; attempting it fails with {@code
 * NullPointerException: No evaluator for PTransform "beam:transform:external:v1"}. Executing this
 * path requires a portable runner — in this guide, Dataflow Runner v2. End-to-end execution is
 * therefore verified by deploying the pipeline, not by this test.
 *
 * <h2>How to run it</h2>
 *
 * <pre>{@code
 * # 1. Produce the model artifact with the pinned package versions.
 * python3 -m venv .venv && source .venv/bin/activate
 * pip install -r scripts/requirements-model.txt
 * python scripts/train_model.py --output_path=/tmp/gaming_recommender.pkl
 *
 * # 2. Run the test. PyPI must be reachable: Beam builds a virtualenv for the expansion service.
 * ./gradlew test --tests '*RecommendationInferenceCrossLanguageIT' \
 *   -DrunCrossLanguageTests=true \
 *   -DcrossLanguageModelUri=/tmp/gaming_recommender.pkl
 * }</pre>
 *
 * <p>{@code crossLanguageModelUri} accepts anything the Python {@code SklearnModelHandlerNumpy} can
 * open: a local file for a laptop run, or a {@code gs://} object.
 *
 * <p>Expect the first run to take several minutes; the virtualenv is cached under {@code
 * ~/.apache_beam/cache/venvs} afterwards.
 */
@RunWith(JUnit4.class)
public class RecommendationInferenceCrossLanguageIT implements Serializable {

    /** System property that opts in. Forwarded to the test JVM by {@code build.gradle}. */
    private static final String ENABLED_PROPERTY = "runCrossLanguageTests";

    /** System property with the model artifact the transform is configured with. */
    private static final String MODEL_URI_PROPERTY = "crossLanguageModelUri";

    @Rule
    public final transient TestPipeline pipeline =
            TestPipeline.create().enableAbandonedNodeEnforcement(false);

    private String modelUri;

    @Before
    public void skipUnlessExplicitlyEnabled() {
        assumeTrue(
                "Skipped. Re-run with -D"
                        + ENABLED_PROPERTY
                        + "=true to exercise the cross-language RunInference expansion (needs PyPI"
                        + " access).",
                Boolean.parseBoolean(System.getProperty(ENABLED_PROPERTY, "false")));
        modelUri = System.getProperty(MODEL_URI_PROPERTY);
        assumeTrue(
                "Skipped. -D"
                        + MODEL_URI_PROPERTY
                        + "=<path or gs:// URI> must point at the artifact produced by"
                        + " scripts/train_model.py.",
                modelUri != null && !modelUri.trim().isEmpty());
    }

    private static EnrichedEvent churningPlayer() {
        return EnrichedEvent.of(
                GameplayEvent.builder()
                        .setPlayerId("player_0001")
                        .setSessionId("session_1")
                        .setEventType("level_failed")
                        .setLevel(12)
                        .setScore(9100L)
                        .setEventTimestamp("2026-09-11T09:53:50.000Z")
                        .build(),
                ImmutableMap.of(
                        "churn_risk", "0.95",
                        "level_failures", "4",
                        "skill_rating", "1200",
                        "spend_tier", "free"));
    }

    /**
     * The Python side accepts the transform exactly as the pipeline configures it.
     *
     * <p>Expansion happens inside {@code apply}, so reaching the assertions means the expansion
     * service evaluated the model loader and accepted the schemas, the key coder and the {@code
     * model_uri} keyword argument.
     */
    @Test
    public void testTheTransformExpandsAgainstThePythonExpansionService() {
        PCollectionTuple vectors =
                pipeline.apply(Create.of(churningPlayer()))
                        .apply("ToFeatureVectors", RecommendationInference.toFeatureVectors());

        PCollection<KV<Row, Row>> predictions =
                vectors.get(RecommendationInference.FEATURES_TAG)
                        .apply(
                                "RunInference",
                                RecommendationInference.withModel(modelUri).runInference());

        assertNotNull(predictions);
        assertEquals(
                KvCoder.of(
                        RowCoder.of(RecommendationInference.CONTEXT_SCHEMA),
                        RowCoder.of(RecommendationInference.PREDICTION_SCHEMA)),
                predictions.getCoder());

        // The rest of the composite still has to accept what comes back.
        PCollectionTuple recommendations =
                predictions.apply("ToRecommendations", RecommendationInference.toRecommendations());
        assertNotNull(recommendations.get(RecommendationInference.SUCCESS_TAG));

        // Not run on purpose: see the class javadoc. The DirectRunner cannot execute an external
        // transform.
    }

    /**
     * Guards the test above against passing vacuously.
     *
     * <p>If expansion were silently skipped, a model loader that cannot possibly work would expand
     * just as happily as the real one. It must not.
     */
    @Test
    public void testABrokenModelLoaderIsRejectedByTheExpansionService() {
        Pipeline other = TestPipeline.create().enableAbandonedNodeEnforcement(false);
        PCollection<KV<Row, Iterable<Double>>> features =
                other.apply(
                        Create.of(
                                        KV.<Row, Iterable<Double>>of(
                                                RecommendationInference.toContextRow(
                                                        churningPlayer().getEvent()),
                                                ImmutableList.of(
                                                        1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0)))
                                .withCoder(RecommendationInference.FEATURES_CODER));

        RunInference<KV<Row, Row>> broken =
                RunInference.<Row>ofKVs(
                                "def get_model_handler(model_uri):\n"
                                        + "  raise ValueError('deliberately broken loader')\n",
                                RecommendationInference.PREDICTION_SCHEMA,
                                RowCoder.of(RecommendationInference.CONTEXT_SCHEMA))
                        .withKwarg("model_uri", modelUri)
                        .withExtraPackages(RecommendationInference.HARNESS_REQUIREMENTS);

        assertThrows(RuntimeException.class, () -> features.apply("BrokenInference", broken));
    }
}
