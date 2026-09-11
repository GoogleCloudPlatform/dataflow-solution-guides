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
import com.google.cloud.bigtable.data.v2.BigtableDataClient;
import com.google.cloud.bigtable.data.v2.BigtableDataSettings;
import com.google.cloud.bigtable.data.v2.models.Query;
import com.google.cloud.bigtable.data.v2.models.Row;
import com.google.cloud.bigtable.data.v2.models.RowCell;
import com.google.cloud.bigtable.data.v2.models.TableId;
import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.EnrichedEvent;
import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.GameplayEvent;
import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.ProcessingError;
import java.io.IOException;
import java.util.Collections;
import java.util.HashMap;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import javax.annotation.Nullable;
import org.apache.beam.sdk.metrics.Counter;
import org.apache.beam.sdk.metrics.Metrics;
import org.apache.beam.sdk.transforms.DoFn;
import org.apache.beam.sdk.transforms.PTransform;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.values.PCollection;
import org.apache.beam.sdk.values.PCollectionTuple;
import org.apache.beam.sdk.values.TupleTag;
import org.apache.beam.sdk.values.TupleTagList;
import org.joda.time.Instant;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Hydrates id-only gameplay events with the player features held in the Cloud Bigtable feature
 * store.
 *
 * <p>The player id is used as the Bigtable row key, and every column of the configured column
 * family becomes an entry of {@link EnrichedEvent#getFeatures()}. A feature store miss is not an
 * error: the event flows through with an empty feature map so that inference can fall back to
 * defaults. Lookup failures, on the other hand, are routed to {@link #ERROR_TAG} and end up in the
 * dead-letter topic.
 *
 * <p>Beam Java has no turnkey {@code Enrichment} transform (it is a Python-only transform at the
 * time of writing), so the lookups are implemented with the Cloud Bigtable data client directly,
 * following the same approach as the clickstream analytics guide.
 */
@AutoValue
public abstract class PlayerFeatureEnrichment
        extends PTransform<PCollection<GameplayEvent>, PCollectionTuple> {

    private static final Logger LOG = LoggerFactory.getLogger(PlayerFeatureEnrichment.class);

    public static final TupleTag<EnrichedEvent> SUCCESS_TAG =
            new TupleTag<EnrichedEvent>("SUCCESS_TAG") {};
    public static final TupleTag<ProcessingError> ERROR_TAG =
            new TupleTag<ProcessingError>("ERROR_TAG") {};

    public static final String ENRICH_STAGE = "enrich";
    public static final String DEFAULT_COLUMN_FAMILY = "features";

    public abstract @Nullable String projectId();

    public abstract @Nullable String instanceId();

    public abstract @Nullable String tableId();

    public abstract String columnFamily();

    public abstract boolean enabled();

    public static PlayerFeatureEnrichment create() {
        return builder().build();
    }

    public static Builder builder() {
        return new AutoValue_PlayerFeatureEnrichment.Builder()
                .columnFamily(DEFAULT_COLUMN_FAMILY)
                .enabled(true);
    }

    public PlayerFeatureEnrichment withProjectId(String projectId) {
        return toBuilder().projectId(projectId).build();
    }

    public PlayerFeatureEnrichment withInstanceId(String instanceId) {
        return toBuilder().instanceId(instanceId).build();
    }

    public PlayerFeatureEnrichment withTableId(String tableId) {
        return toBuilder().tableId(tableId).build();
    }

    public PlayerFeatureEnrichment withColumnFamily(@Nullable String columnFamily) {
        return toBuilder()
                .columnFamily(
                        columnFamily != null && !columnFamily.trim().isEmpty()
                                ? columnFamily
                                : DEFAULT_COLUMN_FAMILY)
                .build();
    }

    public PlayerFeatureEnrichment withEnabled(boolean enabled) {
        return toBuilder().enabled(enabled).build();
    }

    public abstract Builder toBuilder();

    /** Builder for {@link PlayerFeatureEnrichment}. */
    @AutoValue.Builder
    public abstract static class Builder {
        public abstract Builder projectId(@Nullable String projectId);

        public abstract Builder instanceId(@Nullable String instanceId);

        public abstract Builder tableId(@Nullable String tableId);

        public abstract Builder columnFamily(String columnFamily);

        public abstract Builder enabled(boolean enabled);

        public abstract PlayerFeatureEnrichment build();
    }

    /**
     * Flattens the cells of a Bigtable row into a feature map.
     *
     * <p>Bigtable returns the cells of a row ordered by family, then qualifier, then descending
     * timestamp, so the first cell seen for a qualifier is the most recent one.
     *
     * @param cells the cells of the row
     * @param columnFamily the only column family taken into account
     * @return an immutable map of qualifier to UTF-8 decoded value
     */
    public static Map<String, String> extractFeatures(List<RowCell> cells, String columnFamily) {
        if (cells == null || cells.isEmpty()) {
            return Collections.emptyMap();
        }
        Map<String, String> features = new HashMap<>();
        for (RowCell cell : cells) {
            if (!columnFamily.equals(cell.getFamily())) {
                continue;
            }
            features.putIfAbsent(
                    cell.getQualifier().toStringUtf8(), cell.getValue().toStringUtf8());
        }
        return Collections.unmodifiableMap(features);
    }

    @Override
    public PCollectionTuple expand(PCollection<GameplayEvent> input) {
        return input.apply(
                "LookUpPlayerFeatures",
                ParDo.of(
                                new PlayerFeatureLookupDoFn(
                                        projectId(),
                                        instanceId(),
                                        tableId(),
                                        columnFamily(),
                                        enabled()))
                        .withOutputTags(SUCCESS_TAG, TupleTagList.of(ERROR_TAG)));
    }

    private static class PlayerFeatureLookupDoFn extends DoFn<GameplayEvent, EnrichedEvent> {
        private final Counter enrichedEvents =
                Metrics.counter(PlayerFeatureEnrichment.class, "player-features-found");
        private final Counter featureStoreMisses =
                Metrics.counter(PlayerFeatureEnrichment.class, "player-features-missing");
        private final Counter lookupErrors =
                Metrics.counter(PlayerFeatureEnrichment.class, "player-features-errors");

        private final String projectId;
        private final String instanceId;
        private final String tableId;
        private final String columnFamily;
        private final boolean enabled;

        private transient BigtableDataClient bigtableDataClient;

        PlayerFeatureLookupDoFn(
                String projectId,
                String instanceId,
                String tableId,
                String columnFamily,
                boolean enabled) {
            this.projectId = projectId;
            this.instanceId = instanceId;
            this.tableId = tableId;
            this.columnFamily = columnFamily;
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
        public void processElement(
                @Element GameplayEvent event,
                @Timestamp Instant timestamp,
                MultiOutputReceiver output) {
            if (!enabled || bigtableDataClient == null) {
                output.get(SUCCESS_TAG).output(EnrichedEvent.of(event, Collections.emptyMap()));
                return;
            }

            String rowKey = event.getPlayerId();
            if (rowKey == null || rowKey.trim().isEmpty()) {
                featureStoreMisses.inc();
                output.get(SUCCESS_TAG).output(EnrichedEvent.of(event, Collections.emptyMap()));
                return;
            }

            try {
                Iterator<Row> rows =
                        bigtableDataClient
                                .readRows(Query.create(TableId.of(tableId)).rowKey(rowKey))
                                .iterator();
                if (!rows.hasNext()) {
                    featureStoreMisses.inc();
                    output.get(SUCCESS_TAG).output(EnrichedEvent.of(event, Collections.emptyMap()));
                    return;
                }

                Map<String, String> features =
                        extractFeatures(rows.next().getCells(), columnFamily);
                if (features.isEmpty()) {
                    featureStoreMisses.inc();
                } else {
                    enrichedEvents.inc();
                }
                output.get(SUCCESS_TAG).output(EnrichedEvent.of(event, features));
            } catch (Exception e) {
                LOG.warn("Bigtable lookup failed for player '{}': {}", rowKey, e.getMessage());
                lookupErrors.inc();
                output.get(ERROR_TAG)
                        .output(
                                ProcessingError.of(
                                        ENRICH_STAGE,
                                        event.toString(),
                                        String.format(
                                                "Bigtable lookup failed for row key '%s': %s",
                                                rowKey, e),
                                        timestamp.toString()));
            }
        }
    }
}
