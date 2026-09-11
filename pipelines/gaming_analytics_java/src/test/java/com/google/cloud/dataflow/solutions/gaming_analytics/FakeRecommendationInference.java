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
import com.google.cloud.dataflow.solutions.gaming_analytics.transform.RecommendationInference;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import org.apache.beam.sdk.coders.KvCoder;
import org.apache.beam.sdk.coders.RowCoder;
import org.apache.beam.sdk.transforms.DoFn;
import org.apache.beam.sdk.transforms.PTransform;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.values.KV;
import org.apache.beam.sdk.values.PCollection;
import org.apache.beam.sdk.values.PCollectionTuple;
import org.apache.beam.sdk.values.Row;

/**
 * An in-process stand-in for the cross-language scoring step.
 *
 * <p>The production {@link RecommendationInference} contacts a Python expansion service when it
 * expands, which needs pip or Docker and is therefore out of bounds for the unit test suite. This
 * transform replaces <em>only</em> the language-boundary hop: it reuses the real {@code
 * toFeatureVectors()} and {@code toRecommendations()} halves, and fakes the model in between by
 * emitting a fixed propensity vector. Everything the pipeline does around inference — including the
 * feature layout, the key round trip and the dead-letter routing — is therefore exercised for real.
 *
 * <p>See {@code RecommendationInferenceCrossLanguageIT} for the test that does run the real thing.
 */
final class FakeRecommendationInference
        extends PTransform<PCollection<EnrichedEvent>, PCollectionTuple> {

    private final int labelIndex;

    private FakeRecommendationInference(int labelIndex) {
        this.labelIndex = labelIndex;
    }

    /** Always predicts the label at {@code labelIndex} with a propensity of 1.0. */
    static FakeRecommendationInference alwaysPredicting(int labelIndex) {
        return new FakeRecommendationInference(labelIndex);
    }

    @Override
    public PCollectionTuple expand(PCollection<EnrichedEvent> input) {
        PCollectionTuple vectors =
                input.apply("ToFeatureVectors", RecommendationInference.toFeatureVectors());

        PCollection<KV<Row, Row>> predictions =
                vectors.get(RecommendationInference.FEATURES_TAG)
                        .apply("FakeModel", ParDo.of(new FakeModelFn(labelIndex)))
                        .setCoder(
                                KvCoder.of(
                                        RowCoder.of(RecommendationInference.CONTEXT_SCHEMA),
                                        RowCoder.of(RecommendationInference.PREDICTION_SCHEMA)));

        PCollectionTuple recommendations =
                predictions.apply("ToRecommendations", RecommendationInference.toRecommendations());

        // The real transform flattens the two error branches; the pipeline only sees one tag.
        return PCollectionTuple.of(
                        RecommendationInference.SUCCESS_TAG,
                        recommendations.get(RecommendationInference.SUCCESS_TAG))
                .and(
                        RecommendationInference.ERROR_TAG,
                        recommendations.get(RecommendationInference.ERROR_TAG));
    }

    private static class FakeModelFn extends DoFn<KV<Row, Iterable<Double>>, KV<Row, Row>> {
        private final int labelIndex;

        FakeModelFn(int labelIndex) {
            this.labelIndex = labelIndex;
        }

        @ProcessElement
        public void processElement(
                @Element KV<Row, Iterable<Double>> element, OutputReceiver<KV<Row, Row>> output) {
            List<Double> example = new ArrayList<>();
            element.getValue().forEach(example::add);

            List<Double> propensities =
                    new ArrayList<>(
                            Collections.nCopies(
                                    RecommendationInference.RECOMMENDATION_LABELS.size(), 0.0));
            propensities.set(labelIndex, 1.0);

            output.output(
                    KV.of(
                            element.getKey(),
                            Row.withSchema(RecommendationInference.PREDICTION_SCHEMA)
                                    .withFieldValue("example", example)
                                    .withFieldValue("inference", propensities)
                                    .build()));
        }
    }
}
