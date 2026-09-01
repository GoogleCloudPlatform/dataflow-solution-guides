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
package com.google.cloud.dataflow.solutions.clickstream_analytics.load;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;

import org.junit.Test;
import org.junit.runner.RunWith;
import org.junit.runners.JUnit4;

@RunWith(JUnit4.class)
public class BigQueryTest {

    @Test
    public void testWriteEventsBuilder() {
        BigQuery.WriteEvents write =
                BigQuery.writeEvents()
                        .withProjectId("test-project")
                        .withDataset("clickstream")
                        .withTable("events")
                        .build();

        assertEquals("test-project", write.projectId());
        assertEquals("clickstream", write.dataset());
        assertEquals("events", write.table());
    }

    @Test
    public void testWriteSessionsBuilder() {
        BigQuery.WriteSessions write =
                BigQuery.writeSessions()
                        .withProjectId("test-project")
                        .withDataset("clickstream")
                        .withTable("sessions")
                        .build();

        assertEquals("test-project", write.projectId());
        assertEquals("clickstream", write.dataset());
        assertEquals("sessions", write.table());
    }

    @Test
    public void testWriteDeadletterBuilder() {
        BigQuery.WriteDeadletter write =
                BigQuery.writeDeadletter()
                        .withProjectId("test-project")
                        .withDataset("clickstream")
                        .withTable("deadletter")
                        .withJsonSchema("{\"fields\":[]}")
                        .build();

        assertEquals("test-project", write.projectId());
        assertEquals("clickstream", write.dataset());
        assertEquals("deadletter", write.table());
        assertEquals("{\"fields\":[]}", write.jsonSchema());
    }

    @Test
    public void testWriteDeadletterBuilderWithoutSchema() {
        BigQuery.WriteDeadletter write =
                BigQuery.writeDeadletter()
                        .withProjectId("test-project")
                        .withDataset("clickstream")
                        .withTable("deadletter")
                        .build();

        assertNull(write.jsonSchema());
    }
}
