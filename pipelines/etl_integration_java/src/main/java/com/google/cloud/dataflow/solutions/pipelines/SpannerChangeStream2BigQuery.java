/*
*  Copyright 2024 Google LLC
*
*  Licensed under the Apache License, Version 2.0 (the "License");
*  you may not use this file except in compliance with the License.
*  You may obtain a copy of the License at
*
*      https://www.apache.org/licenses/LICENSE-2.0
*
*  Unless required by applicable law or agreed to in writing, software
*  distributed under the License is distributed on an "AS IS" BASIS,
*  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*  See the License for the specific language governing permissions and
*  limitations under the License.
*/

package com.google.cloud.dataflow.solutions.pipelines;

import com.google.cloud.Timestamp;
import com.google.cloud.dataflow.solutions.options.ChangeStreamOptions;
import java.time.Instant;
import org.apache.beam.runners.dataflow.options.DataflowPipelineOptions;
import org.apache.beam.sdk.Pipeline;
import org.apache.beam.sdk.io.gcp.spanner.SpannerIO;
import org.apache.beam.sdk.io.gcp.spanner.changestreams.model.DataChangeRecord;
import org.apache.beam.sdk.values.PCollection;

public class SpannerChangeStream2BigQuery {
    private static Timestamp getCatchupTimestamp(long catchupMinutes) {
        return Timestamp.ofTimeSecondsAndNanos(
                Instant.now().minusSeconds(catchupMinutes * 60).getEpochSecond(), 0);
    }

    public static Pipeline createPipeline(ChangeStreamOptions options) {
        String projectId = options.as(DataflowPipelineOptions.class).getProject();

        Pipeline p = Pipeline.create(options);

        PCollection<DataChangeRecord> changes =
                p.apply(
                        "Read change stream",
                        SpannerIO.readChangeStream()
                                .withProjectId(projectId)
                                .withInstanceId(options.getSpannerInstance())
                                .withDatabaseId(options.getSpannerDatabase())
                                .withMetadataInstance(options.getSpannerInstance())
                                .withMetadataDatabase("metadata")
                                .withMetadataTable("change_streams_fix")
                                .withChangeStreamName(options.getSpannerChangeStream())
                                .withInclusiveStartAt(
                                        getCatchupTimestamp(options.getCatchUpMinutes())));

        return p;
    }
}
