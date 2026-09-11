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

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertThrows;

import org.junit.Test;
import org.junit.runner.RunWith;
import org.junit.runners.JUnit4;

@RunWith(JUnit4.class)
public class RecommendationBigQuerySinkTest {

    @Test
    public void testTerraformTableReferenceIsNormalized() {
        // BQ_TABLE is exported by Terraform as PROJECT.DATASET.TABLE.
        assertEquals(
                "my-project:gaming_analytics.player_recommendations",
                RecommendationBigQuerySink.normalizeTableSpec(
                        "my-project.gaming_analytics.player_recommendations"));
    }

    @Test
    public void testLegacyTableSpecIsLeftUntouched() {
        assertEquals(
                "my-project:gaming_analytics.player_recommendations",
                RecommendationBigQuerySink.normalizeTableSpec(
                        "my-project:gaming_analytics.player_recommendations"));
    }

    @Test
    public void testSurroundingWhitespaceIsTrimmed() {
        assertEquals("p:d.t", RecommendationBigQuerySink.normalizeTableSpec("  p.d.t  "));
    }

    @Test
    public void testPartiallyQualifiedTablesAreRejected() {
        assertThrows(
                IllegalArgumentException.class,
                () -> RecommendationBigQuerySink.normalizeTableSpec("gaming_analytics.table"));
        assertThrows(
                IllegalArgumentException.class,
                () -> RecommendationBigQuerySink.normalizeTableSpec("table"));
        assertThrows(
                IllegalArgumentException.class,
                () -> RecommendationBigQuerySink.normalizeTableSpec(null));
        assertThrows(
                IllegalArgumentException.class,
                () -> RecommendationBigQuerySink.normalizeTableSpec("   "));
    }
}
