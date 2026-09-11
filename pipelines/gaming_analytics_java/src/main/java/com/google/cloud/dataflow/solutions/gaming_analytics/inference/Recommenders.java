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

import com.google.cloud.dataflow.solutions.gaming_analytics.options.GamingAnalyticsOptions;
import java.util.Locale;

/** Builds the {@link Recommender} matching the requested inference mode. */
public final class Recommenders {

    private Recommenders() {}

    /** In-process scoring on the workers. */
    public static final String LOCAL_MODE = "local";

    /**
     * Alias of {@link #LOCAL_MODE}, accepted because it is the value the Terraform module writes to
     * {@code INFERENCE_MODE} when the model is meant to run on the workers themselves.
     */
    public static final String GPU_MODE = "gpu";

    /** Scoring through a Vertex AI online prediction endpoint. */
    public static final String VERTEX_MODE = "vertex";

    /**
     * @throws IllegalArgumentException if the mode is unknown, or if the Vertex AI mode is selected
     *     without an endpoint
     */
    public static Recommender fromOptions(GamingAnalyticsOptions options) {
        String mode =
                options.getInferenceMode() == null
                        ? LOCAL_MODE
                        : options.getInferenceMode().trim().toLowerCase(Locale.ROOT);

        switch (mode) {
            case LOCAL_MODE:
            case GPU_MODE:
                return new LocalRecommender();
            case VERTEX_MODE:
                if (options.getModelEndpoint() == null
                        || options.getModelEndpoint().trim().isEmpty()) {
                    throw new IllegalArgumentException(
                            "--modelEndpoint is required when --inferenceMode=vertex");
                }
                return new VertexAiRecommender(
                        options.getModelEndpoint().trim(),
                        options.getPredictionLabelField(),
                        options.getPredictionScoreField(),
                        options.getPredictionTimeoutSeconds());
            default:
                throw new IllegalArgumentException(
                        String.format(
                                "Unknown --inferenceMode '%s', expected one of: %s, %s, %s",
                                options.getInferenceMode(), LOCAL_MODE, GPU_MODE, VERTEX_MODE));
        }
    }
}
