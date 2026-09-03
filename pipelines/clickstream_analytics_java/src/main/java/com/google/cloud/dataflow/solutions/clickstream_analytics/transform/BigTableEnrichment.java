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
package com.google.cloud.dataflow.solutions.clickstream_analytics.transform;

import com.google.auto.value.AutoValue;
import com.google.cloud.bigtable.data.v2.BigtableDataClient;
import com.google.cloud.bigtable.data.v2.BigtableDataSettings;
import com.google.cloud.bigtable.data.v2.models.Query;
import com.google.cloud.bigtable.data.v2.models.Row;
import com.google.cloud.bigtable.data.v2.models.RowCell;
import com.google.cloud.bigtable.data.v2.models.TableId;
import com.google.cloud.dataflow.solutions.clickstream_analytics.data.ClickstreamObjects.ClickstreamEvent;
import java.io.IOException;
import java.util.Iterator;
import javax.annotation.Nullable;
import org.apache.beam.sdk.metrics.Counter;
import org.apache.beam.sdk.metrics.Metrics;
import org.apache.beam.sdk.transforms.DoFn;
import org.apache.beam.sdk.transforms.PTransform;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.values.PCollection;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

@AutoValue
public abstract class BigTableEnrichment
        extends PTransform<PCollection<ClickstreamEvent>, PCollection<ClickstreamEvent>> {

    private static final Logger LOG = LoggerFactory.getLogger(BigTableEnrichment.class);
    public static final String DEFAULT_COLUMN_FAMILY = "cf";
    public static final String DEFAULT_LOOKUP_KEY = "curr";

    public abstract @Nullable String projectId();

    public abstract @Nullable String instanceId();

    public abstract @Nullable String tableId();

    public abstract String lookupKeyField();

    public abstract boolean enabled();

    public static BigTableEnrichment create() {
        return builder().build();
    }

    public static Builder builder() {
        return new AutoValue_BigTableEnrichment.Builder()
                .lookupKeyField(DEFAULT_LOOKUP_KEY)
                .enabled(true);
    }

    public BigTableEnrichment withProjectId(String projectId) {
        return toBuilder().projectId(projectId).build();
    }

    public BigTableEnrichment withInstanceId(String instanceId) {
        return toBuilder().instanceId(instanceId).build();
    }

    public BigTableEnrichment withTableId(String tableId) {
        return toBuilder().tableId(tableId).build();
    }

    public BigTableEnrichment withLookupKeyField(String lookupKeyField) {
        return toBuilder()
                .lookupKeyField(lookupKeyField != null ? lookupKeyField : DEFAULT_LOOKUP_KEY)
                .build();
    }

    public BigTableEnrichment withEnabled(boolean enabled) {
        return toBuilder().enabled(enabled).build();
    }

    public abstract Builder toBuilder();

    @AutoValue.Builder
    public abstract static class Builder {
        public abstract Builder projectId(@Nullable String projectId);

        public abstract Builder instanceId(@Nullable String instanceId);

        public abstract Builder tableId(@Nullable String tableId);

        public abstract Builder lookupKeyField(String lookupKeyField);

        public abstract Builder enabled(boolean enabled);

        public Builder withProjectId(String projectId) {
            return projectId(projectId);
        }

        public Builder withInstanceId(String instanceId) {
            return instanceId(instanceId);
        }

        public Builder withTableId(String tableId) {
            return tableId(tableId);
        }

        public Builder withLookupKeyField(String lookupKeyField) {
            return lookupKeyField(lookupKeyField != null ? lookupKeyField : DEFAULT_LOOKUP_KEY);
        }

        public Builder withEnabled(boolean enabled) {
            return enabled(enabled);
        }

        public abstract BigTableEnrichment build();
    }

    @Override
    public PCollection<ClickstreamEvent> expand(PCollection<ClickstreamEvent> input) {
        return input.apply(
                "EnrichWithBigtableDoFn",
                ParDo.of(
                        new BigTableEnrichmentDoFn(
                                projectId(),
                                instanceId(),
                                tableId(),
                                lookupKeyField(),
                                enabled())));
    }

    public static String resolveRowKey(ClickstreamEvent event, String lookupKeyField) {
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

    private static class BigTableEnrichmentDoFn extends DoFn<ClickstreamEvent, ClickstreamEvent> {
        private final Counter bigtableEnrichedMessages =
                Metrics.counter(BigTableEnrichment.class, "bigtable-enriched-messages");
        private final Counter bigtableCacheMisses =
                Metrics.counter(BigTableEnrichment.class, "bigtable-cache-misses");
        private final Counter bigtableErrors =
                Metrics.counter(BigTableEnrichment.class, "bigtable-errors");

        private final String projectId;
        private final String instanceId;
        private final String tableId;
        private final String lookupKeyField;
        private final boolean enabled;

        private transient BigtableDataClient bigtableDataClient;

        BigTableEnrichmentDoFn(
                String projectId,
                String instanceId,
                String tableId,
                String lookupKeyField,
                boolean enabled) {
            this.projectId = projectId;
            this.instanceId = instanceId;
            this.tableId = tableId;
            this.lookupKeyField = lookupKeyField != null ? lookupKeyField : DEFAULT_LOOKUP_KEY;
            this.enabled = enabled;
        }

        @SuppressWarnings({"unused", "EffectivelyPrivate"})
        @Setup
        public void setup() throws IOException {
            if (enabled && bigtableDataClient == null && projectId != null && instanceId != null) {
                BigtableDataSettings settings =
                        BigtableDataSettings.newBuilder()
                                .setProjectId(projectId)
                                .setInstanceId(instanceId)
                                .build();
                bigtableDataClient = BigtableDataClient.create(settings);
            }
        }

        @SuppressWarnings({"unused", "EffectivelyPrivate"})
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
                    bigtableCacheMisses.inc();
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
                            enrichedBuilder.setEnrichedData(
                                    String.format("%s:%s", qualifier, value));
                            foundEnrichment = true;
                        }
                    }
                }

                if (foundEnrichment) {
                    bigtableEnrichedMessages.inc();
                } else {
                    bigtableCacheMisses.inc();
                }

                context.output(enrichedBuilder.build());

            } catch (Exception e) {
                LOG.warn("Error looking up row key '{}' in Bigtable: {}", rowKey, e.getMessage());
                bigtableErrors.inc();
                context.output(event);
            }
        }
    }
}
