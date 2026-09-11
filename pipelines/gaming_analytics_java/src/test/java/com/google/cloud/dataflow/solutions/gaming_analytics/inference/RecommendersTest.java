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

import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

import com.google.cloud.dataflow.solutions.gaming_analytics.options.GamingAnalyticsOptions;
import org.apache.beam.sdk.options.PipelineOptionsFactory;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.junit.runners.JUnit4;

@RunWith(JUnit4.class)
public class RecommendersTest {

    private static GamingAnalyticsOptions options(String... args) {
        return PipelineOptionsFactory.fromArgs(args).as(GamingAnalyticsOptions.class);
    }

    @Test
    public void testDefaultModeIsLocal() {
        assertTrue(Recommenders.fromOptions(options()) instanceof LocalRecommender);
    }

    @Test
    public void testGpuModeUsesTheInProcessRecommender() {
        // 'gpu' is what the Terraform module exports for a model running on the workers.
        assertTrue(
                Recommenders.fromOptions(options("--inferenceMode=gpu"))
                        instanceof LocalRecommender);
        assertTrue(
                Recommenders.fromOptions(options("--inferenceMode=GPU"))
                        instanceof LocalRecommender);
    }

    @Test
    public void testVertexMode() {
        assertTrue(
                Recommenders.fromOptions(
                                options(
                                        "--inferenceMode=vertex",
                                        "--modelEndpoint=projects/p/locations/us-central1/endpoints/1"))
                        instanceof VertexAiRecommender);
    }

    @Test
    public void testVertexModeRequiresAnEndpoint() {
        IllegalArgumentException thrown =
                assertThrows(
                        IllegalArgumentException.class,
                        () -> Recommenders.fromOptions(options("--inferenceMode=vertex")));
        assertTrue(thrown.getMessage().contains("modelEndpoint"));
    }

    @Test
    public void testUnknownModeIsRejected() {
        assertThrows(
                IllegalArgumentException.class,
                () -> Recommenders.fromOptions(options("--inferenceMode=magic")));
    }
}
