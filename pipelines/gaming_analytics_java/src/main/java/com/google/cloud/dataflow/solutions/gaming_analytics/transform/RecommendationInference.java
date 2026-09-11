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
import com.google.common.collect.ImmutableList;
import com.google.common.collect.ImmutableMap;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import javax.annotation.Nullable;
import org.apache.beam.sdk.coders.DoubleCoder;
import org.apache.beam.sdk.coders.IterableCoder;
import org.apache.beam.sdk.coders.KvCoder;
import org.apache.beam.sdk.coders.RowCoder;
import org.apache.beam.sdk.extensions.python.transforms.RunInference;
import org.apache.beam.sdk.metrics.Counter;
import org.apache.beam.sdk.metrics.Metrics;
import org.apache.beam.sdk.schemas.Schema;
import org.apache.beam.sdk.transforms.DoFn;
import org.apache.beam.sdk.transforms.Flatten;
import org.apache.beam.sdk.transforms.PTransform;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.values.KV;
import org.apache.beam.sdk.values.PCollection;
import org.apache.beam.sdk.values.PCollectionList;
import org.apache.beam.sdk.values.PCollectionTuple;
import org.apache.beam.sdk.values.Row;
import org.apache.beam.sdk.values.TupleTag;
import org.apache.beam.sdk.values.TupleTagList;
import org.joda.time.Instant;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Scores enriched gameplay events with Apache Beam {@code RunInference}.
 *
 * <p>{@code RunInference} is not a Python-only transform: the Java SDK reaches it through the
 * cross-language wrapper {@link RunInference}, which runs the Python {@code
 * apache_beam.ml.inference.base.RunInference} behind an expansion service. See <a
 * href="https://beam.apache.org/documentation/ml/multi-language-inference/">Multi-language
 * inference</a>. The model handler used here is the Python {@code SklearnModelHandlerNumpy}, so the
 * model runs in the Python SDK harness on the same worker as the Java code: there is no remote
 * endpoint and no per-element network call.
 *
 * <p>Consequences you must be aware of before using this transform:
 *
 * <ul>
 *   <li><b>The transform expands at pipeline-construction time.</b> Submitting a pipeline that
 *       contains it starts (or contacts) a Python expansion service, which needs either a local
 *       Python interpreter that can {@code pip install} Beam, or Docker. It is therefore never
 *       exercised by the unit test suite; see {@code RecommendationInferenceCrossLanguageIT}.
 *   <li><b>Dataflow Runner v2 is required</b> ({@code --experiments=use_runner_v2}). Multi-language
 *       pipelines do not run on the original Dataflow runner.
 *   <li><b>The Python package versions must match the ones the model was pickled with.</b> {@link
 *       #HARNESS_REQUIREMENTS} is what gets installed in the Python SDK harness. Of those, {@code
 *       scikit-learn} and {@code numpy} are the pickle-critical pair, and {@code
 *       scripts/train_model.py} refuses to write a model pickled with any other versions of them.
 * </ul>
 *
 * <p>Elements crossing the language boundary are schema values, which is why the transform is a
 * three-step composite:
 *
 * <ol>
 *   <li>{@code ToFeatureVectors} turns each {@link EnrichedEvent} into a {@code KV} of a {@link
 *       #CONTEXT_SCHEMA} {@link Row} (the fields that must survive the round trip, starting with
 *       the player id) and the numeric feature vector;
 *   <li>{@code RunInference} scores the vector and returns a {@code KV} of the same key and a
 *       {@link #PREDICTION_SCHEMA} row;
 *   <li>{@code ToRecommendations} rebuilds a {@link Recommendation} from the key and the
 *       prediction.
 * </ol>
 *
 * <p>The key is carried across the boundary rather than re-joined afterwards, so no shuffle is
 * added and the ordering guarantees of the activation path are untouched.
 *
 * <p>Anything that cannot be turned into a feature vector, or any prediction that cannot be read
 * back, becomes a dead-letter record instead of failing the bundle: an in-game activation path must
 * degrade rather than stall.
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

    /**
     * The complete Python environment the model handler needs, installed into the Python SDK
     * harness.
     *
     * <p>These are the exact versions the Apache Beam 2.76.0 Python SDK harness image already ships
     * (see {@code sdks/python/container/py3XX/base_image_requirements.txt} in the Beam repository),
     * which is deliberate on both counts:
     *
     * <ul>
     *   <li>installing them is a no-op on the stock harness, so nothing is upgraded underneath the
     *       rest of the SDK;
     *   <li>they are nonetheless pinned rather than left to Beam, because {@code
     *       scripts/train_model.py} pickles the model with {@code scikit-learn} and {@code numpy}
     *       and scikit-learn does not guarantee pickle compatibility across versions. Those two,
     *       and only those two, are mirrored as constants in that script and checked at training
     *       time by {@code check_pinned_versions()}. When the Beam version in {@code build.gradle}
     *       moves, re-read that requirements file and move this list, {@code
     *       scripts/requirements.txt} and those constants with it.
     * </ul>
     *
     * <p>{@code pandas} is in the list for a different reason: it is an import-time requirement of
     * the harness, not part of the pickle. The model handler is the NumPy one and the pickle holds
     * no pandas objects, but {@code apache_beam.ml.inference.sklearn_inference} imports pandas at
     * module scope, so the model loader cannot even be evaluated without it. Beam adds it
     * automatically when no extra packages are given at all — supplying this list turns that
     * inference off, so it has to be spelled out. {@code train_model.py} neither imports it nor
     * checks it.
     */
    public static final ImmutableList<String> HARNESS_REQUIREMENTS =
            ImmutableList.of("scikit-learn==1.7.2", "numpy==2.4.6", "pandas==2.3.3");

    /**
     * Layout of the feature vector sent to the model. The order is part of the contract with {@code
     * scripts/train_model.py}: changing it on one side only silently produces nonsense predictions.
     */
    public static final ImmutableList<String> FEATURE_NAMES =
            ImmutableList.of(
                    "level",
                    "score",
                    "churn_risk",
                    "level_failures",
                    "skill_rating",
                    "spend_tier_score",
                    "event_type_code");

    /**
     * Recommendation labels, indexed by the position of the propensity in the model output. Also
     * mirrored in {@code scripts/train_model.py}.
     */
    public static final ImmutableList<String> RECOMMENDATION_LABELS =
            ImmutableList.of(
                    "retention_bonus_pack",
                    "difficulty_assist_boost",
                    "premium_bundle_offer",
                    "tournament_invite",
                    "daily_quest_suggestion");

    /** Ordinal encoding of {@code spend_tier}. Unknown tiers are treated as free players. */
    static final ImmutableMap<String, Double> SPEND_TIER_SCORES =
            ImmutableMap.of("free", 0.0, "paying", 1.0, "whale", 2.0);

    /** Ordinal encoding of {@code event_type}. Unknown types are treated as {@code level_start}. */
    static final ImmutableMap<String, Double> EVENT_TYPE_CODES =
            ImmutableMap.of(
                    "level_start", 0.0,
                    "level_complete", 1.0,
                    "level_failed", 2.0,
                    "purchase", 3.0,
                    "item_used", 4.0);

    /**
     * Fields carried across the language boundary as the {@code KV} key, so that the recommendation
     * can be rebuilt without re-joining the original event.
     */
    public static final Schema CONTEXT_SCHEMA =
            Schema.builder()
                    .addStringField("player_id")
                    .addNullableStringField("session_id")
                    .addNullableStringField("event_type")
                    .addNullableInt32Field("level")
                    .addNullableInt64Field("score")
                    .addStringField("event_timestamp")
                    .build();

    /**
     * Output schema of the Python {@code RunInference}. {@code example} is the feature vector that
     * was sent, {@code inference} is what {@code model.predict()} returned: one propensity per
     * entry of {@link #RECOMMENDATION_LABELS}.
     */
    public static final Schema PREDICTION_SCHEMA =
            Schema.of(
                    Schema.Field.of("example", Schema.FieldType.array(Schema.FieldType.DOUBLE)),
                    Schema.Field.of("inference", Schema.FieldType.array(Schema.FieldType.DOUBLE)));

    /**
     * The Python model loader. {@code RunInference.from_callable} calls it with the {@code
     * model_uri} keyword argument set below. {@code KeyedModelHandler} is what makes the {@code KV}
     * form work: it strips the key, scores the value, and pairs the key back with the prediction.
     */
    static final String MODEL_LOADER =
            "from apache_beam.ml.inference.base import KeyedModelHandler\n"
                    + "from apache_beam.ml.inference.sklearn_inference import"
                    + " SklearnModelHandlerNumpy\n"
                    + "\n"
                    + "def get_model_handler(model_uri):\n"
                    + "  return KeyedModelHandler(SklearnModelHandlerNumpy(model_uri))\n";

    /** Output tag of {@link #toFeatureVectors()}: the keyed feature vectors sent to the model. */
    public static final TupleTag<KV<Row, Iterable<Double>>> FEATURES_TAG =
            new TupleTag<KV<Row, Iterable<Double>>>("FEATURES_TAG") {};

    /** Coder of the elements handed to the Python side. */
    public static final KvCoder<Row, Iterable<Double>> FEATURES_CODER =
            KvCoder.of(RowCoder.of(CONTEXT_SCHEMA), IterableCoder.of(DoubleCoder.of()));

    /** Location of the pickled scikit-learn model, normally a {@code gs://} object. */
    public abstract String modelUri();

    /**
     * Address of an already running Python expansion service, as {@code host:port}. When it is not
     * set, Beam starts a transient one, which is when {@link #HARNESS_REQUIREMENTS} are installed
     * into its virtualenv and staged for the workers.
     */
    public abstract @Nullable String expansionService();

    public static RecommendationInference withModel(String modelUri) {
        return new AutoValue_RecommendationInference(modelUri, null);
    }

    public RecommendationInference withExpansionService(@Nullable String expansionService) {
        String trimmed = expansionService == null ? null : expansionService.trim();
        return new AutoValue_RecommendationInference(
                modelUri(), trimmed == null || trimmed.isEmpty() ? null : trimmed);
    }

    /**
     * The half of this transform that runs before the language boundary: {@link EnrichedEvent} to a
     * keyed feature vector, with unconvertible events dead-lettered.
     *
     * <p>Exposed separately so that it can be tested, and composed with a stand-in for the
     * cross-language hop, without starting an expansion service.
     */
    public static PTransform<PCollection<EnrichedEvent>, PCollectionTuple> toFeatureVectors() {
        return new ToFeatureVectors();
    }

    /**
     * The half of this transform that runs after the language boundary: the keyed model output back
     * into a {@link Recommendation}, with unreadable predictions dead-lettered.
     */
    public static PTransform<PCollection<KV<Row, Row>>, PCollectionTuple> toRecommendations() {
        return new ToRecommendations();
    }

    /** Builds the configured cross-language {@code RunInference}. */
    RunInference<KV<Row, Row>> runInference() {
        RunInference<KV<Row, Row>> inference =
                RunInference.<Row>ofKVs(
                                MODEL_LOADER, PREDICTION_SCHEMA, RowCoder.of(CONTEXT_SCHEMA))
                        .withKwarg("model_uri", modelUri());
        if (expansionService() != null) {
            // Beam rejects extra packages when the expansion service is provided: whoever runs
            // that service owns its environment, and so its Python package versions.
            return inference.withExpansionService(expansionService());
        }
        return inference.withExtraPackages(HARNESS_REQUIREMENTS);
    }

    @Override
    public PCollectionTuple expand(PCollection<EnrichedEvent> input) {
        PCollectionTuple vectors = input.apply("ToFeatureVectors", toFeatureVectors());

        PCollection<KV<Row, Row>> predictions =
                vectors.get(FEATURES_TAG).apply("RunInference", runInference());

        PCollectionTuple recommendations =
                predictions.apply("ToRecommendations", toRecommendations());

        PCollection<ProcessingError> errors =
                PCollectionList.of(vectors.get(ERROR_TAG))
                        .and(recommendations.get(ERROR_TAG))
                        .apply("FlattenInferenceErrors", Flatten.pCollections());

        return PCollectionTuple.of(SUCCESS_TAG, recommendations.get(SUCCESS_TAG))
                .and(ERROR_TAG, errors);
    }

    /** Builds the key row carrying the fields that must survive the round trip through Python. */
    public static Row toContextRow(GameplayEvent event) {
        return Row.withSchema(CONTEXT_SCHEMA)
                .withFieldValue("player_id", event.getPlayerId())
                .withFieldValue("session_id", event.getSessionId())
                .withFieldValue("event_type", event.getEventType())
                .withFieldValue("level", event.getLevel())
                .withFieldValue("score", event.getScore())
                .withFieldValue("event_timestamp", event.getEventTimestamp())
                .build();
    }

    /**
     * Builds the numeric feature vector the model expects.
     *
     * <p>The categorical features are ordinal encoded rather than one-hot encoded: the model is a
     * decision tree, which isolates a single ordinal value with two splits, and it keeps the vector
     * short enough to stay readable in the guide.
     *
     * @param enriched the event, hydrated with the feature store
     * @return one value per entry of {@link #FEATURE_NAMES}, in that order
     */
    public static ImmutableList<Double> toFeatureVector(EnrichedEvent enriched) {
        GameplayEvent event = enriched.getEvent();
        Map<String, String> features = enriched.getFeatures();
        return ImmutableList.of(
                event.getLevel() == null ? 0.0 : event.getLevel().doubleValue(),
                event.getScore() == null ? 0.0 : event.getScore().doubleValue(),
                numericFeature(features, "churn_risk"),
                numericFeature(features, "level_failures"),
                numericFeature(features, "skill_rating"),
                categoricalFeature(features.get("spend_tier"), SPEND_TIER_SCORES),
                categoricalFeature(event.getEventType(), EVENT_TYPE_CODES));
    }

    /**
     * Reads a numeric feature, falling back to {@code 0.0} when it is absent, empty or not a
     * number. A malformed feature must not take the activation path down.
     */
    static double numericFeature(Map<String, String> features, String name) {
        String raw = features == null ? null : features.get(name);
        if (raw == null || raw.trim().isEmpty()) {
            return 0.0;
        }
        try {
            return Double.parseDouble(raw.trim());
        } catch (NumberFormatException e) {
            return 0.0;
        }
    }

    /** Ordinal encodes a categorical value, defaulting to {@code 0.0} for unknown values. */
    static double categoricalFeature(@Nullable String value, Map<String, Double> encoding) {
        if (value == null) {
            return 0.0;
        }
        Double code = encoding.get(value.trim());
        return code == null ? 0.0 : code;
    }

    /**
     * Turns the propensities returned by the model into a label and a confidence.
     *
     * @param propensities one value per entry of {@link #RECOMMENDATION_LABELS}
     * @return the index of the highest propensity
     * @throws IllegalArgumentException if the model did not return one value per label
     */
    static int argMax(List<Double> propensities) {
        if (propensities == null || propensities.size() != RECOMMENDATION_LABELS.size()) {
            throw new IllegalArgumentException(
                    String.format(
                            "The model returned %s propensities, expected %s. Is '%s' the model"
                                    + " produced by scripts/train_model.py?",
                            propensities == null ? "no" : propensities.size(),
                            RECOMMENDATION_LABELS.size(),
                            "--modelUri"));
        }
        int best = 0;
        for (int i = 1; i < propensities.size(); i++) {
            Double candidate = propensities.get(i);
            Double incumbent = propensities.get(best);
            if (candidate != null && (incumbent == null || candidate > incumbent)) {
                best = i;
            }
        }
        return best;
    }

    static double clamp(double value) {
        return Math.max(0.0, Math.min(1.0, value));
    }

    private static class ToFeatureVectors
            extends PTransform<PCollection<EnrichedEvent>, PCollectionTuple> {
        @Override
        public PCollectionTuple expand(PCollection<EnrichedEvent> input) {
            PCollectionTuple outputs =
                    input.apply(
                            "BuildFeatureVectors",
                            ParDo.of(new ToFeatureVectorFn())
                                    .withOutputTags(FEATURES_TAG, TupleTagList.of(ERROR_TAG)));
            // Set explicitly: the coder is part of the cross-language contract, not an inference
            // detail. Python decodes exactly these bytes.
            outputs.get(FEATURES_TAG).setCoder(FEATURES_CODER);
            return outputs;
        }
    }

    private static class ToRecommendations
            extends PTransform<PCollection<KV<Row, Row>>, PCollectionTuple> {
        @Override
        public PCollectionTuple expand(PCollection<KV<Row, Row>> input) {
            return input.apply(
                    "BuildRecommendations",
                    ParDo.of(new ToRecommendationFn())
                            .withOutputTags(SUCCESS_TAG, TupleTagList.of(ERROR_TAG)));
        }
    }

    private static class ToFeatureVectorFn extends DoFn<EnrichedEvent, KV<Row, Iterable<Double>>> {
        private final Counter vectorizedEvents =
                Metrics.counter(RecommendationInference.class, "vectorized-events");
        private final Counter vectorizationErrors =
                Metrics.counter(RecommendationInference.class, "vectorization-errors");

        @ProcessElement
        public void processElement(
                @Element EnrichedEvent enriched,
                @Timestamp Instant timestamp,
                MultiOutputReceiver output) {
            try {
                Row key = toContextRow(enriched.getEvent());
                // A defensive copy: the RunInference wrapper keeps the iterable around until the
                // bundle is encoded.
                List<Double> vector = new ArrayList<>(toFeatureVector(enriched));
                vectorizedEvents.inc();
                output.get(FEATURES_TAG).output(KV.of(key, vector));
            } catch (Exception e) {
                LOG.warn("Could not build the feature vector: {}", e.getMessage());
                vectorizationErrors.inc();
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

    private static class ToRecommendationFn extends DoFn<KV<Row, Row>, Recommendation> {
        private final Counter scoredEvents =
                Metrics.counter(RecommendationInference.class, "scored-events");
        private final Counter inferenceErrors =
                Metrics.counter(RecommendationInference.class, "inference-errors");

        @ProcessElement
        public void processElement(
                @Element KV<Row, Row> prediction,
                @Timestamp Instant timestamp,
                MultiOutputReceiver output) {
            Row context = prediction.getKey();
            try {
                List<Double> propensities =
                        ImmutableList.copyOf(prediction.getValue().<Double>getArray("inference"));
                int best = argMax(propensities);
                scoredEvents.inc();
                output.get(SUCCESS_TAG)
                        .output(
                                Recommendation.builder()
                                        .setPlayerId(context.getString("player_id"))
                                        .setSessionId(context.getString("session_id"))
                                        .setEventType(context.getString("event_type"))
                                        .setLevel(context.getInt32("level"))
                                        .setScore(context.getInt64("score"))
                                        .setRecommendation(RECOMMENDATION_LABELS.get(best))
                                        .setRecommendationScore(clamp(propensities.get(best)))
                                        .setEventTimestamp(context.getString("event_timestamp"))
                                        .setProcessingTimestamp(Instant.now().toString())
                                        .build());
            } catch (Exception e) {
                LOG.warn("Could not read the prediction back: {}", e.getMessage());
                inferenceErrors.inc();
                output.get(ERROR_TAG)
                        .output(
                                ProcessingError.of(
                                        INFERENCE_STAGE,
                                        String.valueOf(context),
                                        e.toString(),
                                        timestamp.toString()));
            }
        }
    }
}
