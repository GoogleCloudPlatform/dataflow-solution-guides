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
package com.google.cloud.dataflow.solutions.clickstream_analytics;

import com.google.cloud.bigtable.data.v2.BigtableDataClient;
import com.google.cloud.bigtable.data.v2.BigtableDataSettings;
import com.google.cloud.bigtable.data.v2.models.Query;
import com.google.cloud.bigtable.data.v2.models.Row;
import com.google.cloud.bigtable.data.v2.models.RowCell;
import com.google.cloud.bigtable.data.v2.models.TableId;
import com.google.cloud.dataflow.solutions.clickstream_analytics.data.ClickstreamObjects.ClickstreamEvent;
import java.io.IOException;
import java.util.Iterator;
import org.apache.beam.sdk.transforms.DoFn;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

public class BigTableEnrichment extends DoFn<ClickstreamEvent, ClickstreamEvent> {

    private static final Logger LOG = LoggerFactory.getLogger(BigTableEnrichment.class);
    public static final String DEFAULT_COLUMN_FAMILY = "cf";

    private final String projectId;
    private final String instanceId;
    private final String tableId;
    private final String lookupKeyField;
    private final boolean enabled;

    private transient BigtableDataClient bigtableDataClient;

    public BigTableEnrichment(
            String projectId,
            String instanceId,
            String tableId,
            String lookupKeyField,
            boolean enabled) {
        this.projectId = projectId;
        this.instanceId = instanceId;
        this.tableId = tableId;
        this.lookupKeyField = lookupKeyField != null ? lookupKeyField : "curr";
        this.enabled = enabled;
    }

    public BigTableEnrichment(
            String projectId, String instanceId, String tableId, String lookupKeyField) {
        this(projectId, instanceId, tableId, lookupKeyField, true);
    }

    // Package-private setter for unit testing
    void setBigtableDataClient(BigtableDataClient client) {
        this.bigtableDataClient = client;
    }

    @Setup
    public void setup() throws IOException {
        if (enabled && bigtableDataClient == null) {
            BigtableDataSettings settings =
                    BigtableDataSettings.newBuilder()
                            .setProjectId(projectId)
                            .setInstanceId(instanceId)
                            .build();
            bigtableDataClient = BigtableDataClient.create(settings);
        }
    }

    @Teardown
    public void teardown() {
        if (bigtableDataClient != null) {
            bigtableDataClient.close();
            bigtableDataClient = null;
        }
    }

    @ProcessElement
    public void processElement(ProcessContext context) {
        ClickstreamEvent event = context.element();

        if (!enabled || bigtableDataClient == null) {
            context.output(event);
            return;
        }

        String rowKey = resolveRowKey(event, lookupKeyField);
        if (rowKey == null || rowKey.trim().isEmpty()) {
            context.output(event);
            return;
        }

        try {
            Iterator<Row> rows =
                    bigtableDataClient
                            .readRows(Query.create(TableId.of(tableId)).rowKey(rowKey))
                            .iterator();
            if (!rows.hasNext()) {
                Metrics.bigtableCacheMisses.inc();
                context.output(event);
                return;
            }
            Row row = rows.next();

            ClickstreamEvent.Builder enrichedBuilder = event.toBuilder();
            boolean foundEnrichment = false;

            for (RowCell cell : row.getCells()) {
                String family = cell.getFamily();
                String qualifier = cell.getQualifier().toStringUtf8();
                String value = cell.getValue().toStringUtf8();

                if (DEFAULT_COLUMN_FAMILY.equals(family)
                        || "category".equalsIgnoreCase(qualifier)) {
                    if ("category".equalsIgnoreCase(qualifier)) {
                        enrichedBuilder.setCategory(value);
                        foundEnrichment = true;
                    } else if ("enriched_data".equalsIgnoreCase(qualifier)) {
                        enrichedBuilder.setEnrichedData(value);
                        foundEnrichment = true;
                    } else if (event.getEnrichedData() == null) {
                        enrichedBuilder.setEnrichedData(String.format("%s:%s", qualifier, value));
                        foundEnrichment = true;
                    }
                }
            }

            if (foundEnrichment) {
                Metrics.bigtableEnrichedMessages.inc();
            } else {
                Metrics.bigtableCacheMisses.inc();
            }

            context.output(enrichedBuilder.build());

        } catch (Exception e) {
            LOG.warn("Error looking up row key '{}' in Bigtable: {}", rowKey, e.getMessage());
            Metrics.bigtableErrors.inc();
            context.output(event);
        }
    }

    static String resolveRowKey(ClickstreamEvent event, String lookupKeyField) {
        if (event == null) {
            return null;
        }
        if ("curr".equalsIgnoreCase(lookupKeyField) && event.getCurr() != null) {
            return event.getCurr();
        }
        if ("prev".equalsIgnoreCase(lookupKeyField) && event.getPrev() != null) {
            return event.getPrev();
        }
        if ("user_id".equalsIgnoreCase(lookupKeyField) && event.getUserId() != null) {
            return event.getUserId();
        }
        if (event.getCurr() != null) {
            return event.getCurr();
        }
        return event.getPrev();
    }
}
