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
import static org.junit.Assert.assertTrue;

import com.google.cloud.bigtable.data.v2.models.RowCell;
import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.EnrichedEvent;
import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.GameplayEvent;
import com.google.protobuf.ByteString;
import java.io.Serializable;
import java.util.Arrays;
import java.util.Collections;
import java.util.Map;
import org.apache.beam.sdk.testing.PAssert;
import org.apache.beam.sdk.testing.TestPipeline;
import org.apache.beam.sdk.transforms.Create;
import org.apache.beam.sdk.values.PCollectionTuple;
import org.junit.Rule;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.junit.runners.JUnit4;

@RunWith(JUnit4.class)
public class PlayerFeatureEnrichmentTest implements Serializable {

    @Rule public final transient TestPipeline pipeline = TestPipeline.create();

    private static RowCell cell(String family, String qualifier, long timestamp, String value) {
        return RowCell.create(
                family,
                ByteString.copyFromUtf8(qualifier),
                timestamp,
                Collections.emptyList(),
                ByteString.copyFromUtf8(value));
    }

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

    @Test
    public void testExtractFeaturesFlattensTheColumnFamily() {
        Map<String, String> features =
                PlayerFeatureEnrichment.extractFeatures(
                        Arrays.asList(
                                cell("features", "churn_risk", 2000L, "0.81"),
                                cell("features", "spend_tier", 2000L, "whale")),
                        "features");

        assertEquals(2, features.size());
        assertEquals("0.81", features.get("churn_risk"));
        assertEquals("whale", features.get("spend_tier"));
    }

    @Test
    public void testExtractFeaturesKeepsTheLatestCellPerQualifier() {
        // Bigtable returns cells ordered by descending timestamp within a qualifier.
        Map<String, String> features =
                PlayerFeatureEnrichment.extractFeatures(
                        Arrays.asList(
                                cell("features", "churn_risk", 3000L, "0.91"),
                                cell("features", "churn_risk", 1000L, "0.10")),
                        "features");

        assertEquals("0.91", features.get("churn_risk"));
    }

    @Test
    public void testExtractFeaturesIgnoresOtherColumnFamilies() {
        Map<String, String> features =
                PlayerFeatureEnrichment.extractFeatures(
                        Arrays.asList(
                                cell("features", "churn_risk", 2000L, "0.81"),
                                cell("audit", "churn_risk", 2000L, "SHOULD_BE_IGNORED")),
                        "features");

        assertEquals(1, features.size());
        assertEquals("0.81", features.get("churn_risk"));
    }

    @Test
    public void testExtractFeaturesHandlesEmptyRows() {
        assertTrue(PlayerFeatureEnrichment.extractFeatures(null, "features").isEmpty());
        assertTrue(
                PlayerFeatureEnrichment.extractFeatures(Collections.emptyList(), "features")
                        .isEmpty());
    }

    @Test
    public void testDisabledEnrichmentPassesEventsThrough() {
        PCollectionTuple result =
                pipeline.apply(Create.of(event()))
                        .apply(
                                "Enrich",
                                PlayerFeatureEnrichment.create()
                                        .withProjectId("test-project")
                                        .withInstanceId("gaming-analytics")
                                        .withTableId("player_features")
                                        .withColumnFamily("features")
                                        .withEnabled(false));

        PAssert.that(result.get(PlayerFeatureEnrichment.SUCCESS_TAG))
                .satisfies(
                        enriched -> {
                            EnrichedEvent element = enriched.iterator().next();
                            assertEquals("player_0001", element.getEvent().getPlayerId());
                            assertTrue(element.getFeatures().isEmpty());
                            return null;
                        });
        PAssert.that(result.get(PlayerFeatureEnrichment.ERROR_TAG)).empty();

        pipeline.run();
    }

    @Test
    public void testColumnFamilyFallsBackToTheTerraformDefault() {
        assertEquals(
                PlayerFeatureEnrichment.DEFAULT_COLUMN_FAMILY,
                PlayerFeatureEnrichment.create().withColumnFamily(null).columnFamily());
        assertEquals(
                PlayerFeatureEnrichment.DEFAULT_COLUMN_FAMILY,
                PlayerFeatureEnrichment.create().withColumnFamily("  ").columnFamily());
        assertEquals(
                "other", PlayerFeatureEnrichment.create().withColumnFamily("other").columnFamily());
    }
}
