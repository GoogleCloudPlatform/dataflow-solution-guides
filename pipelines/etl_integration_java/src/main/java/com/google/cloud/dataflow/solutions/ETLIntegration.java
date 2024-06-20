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

package com.google.cloud.dataflow.solutions;

import com.google.cloud.dataflow.solutions.data.TaxiObjects;
import com.google.cloud.dataflow.solutions.load.Spanner;
import com.google.cloud.dataflow.solutions.options.SpannerPublisherOptions;
import com.google.cloud.dataflow.solutions.transform.TaxiEventProcessor;
import org.apache.beam.runners.dataflow.options.DataflowPipelineOptions;
import org.apache.beam.sdk.Pipeline;
import org.apache.beam.sdk.io.gcp.pubsub.PubsubIO;
import org.apache.beam.sdk.io.gcp.pubsub.PubsubMessage;
import org.apache.beam.sdk.options.PipelineOptionsFactory;
import org.apache.beam.sdk.values.PCollection;

public class ETLIntegration {
    public static void main(String[] args) {
        String jobName = "pubsub-to-spanner";
        SpannerPublisherOptions spannerPublisherOptions =
                PipelineOptionsFactory.fromArgs(args)
                        .withoutStrictParsing()
                        .as(SpannerPublisherOptions.class);

        Pipeline p = createPipeline(spannerPublisherOptions);
        p.getOptions().setJobName(jobName);
        p.run();
    }

    public static Pipeline createPipeline(SpannerPublisherOptions options) {
        String projectId = options.as(DataflowPipelineOptions.class).getProject();

        Pipeline p = Pipeline.create(options);

        PCollection<PubsubMessage> msgs =
                p.apply("Read topic", PubsubIO.readMessages().fromTopic(options.getPubsubTopic()));

        TaxiEventProcessor.ParsingOutput<TaxiObjects.TaxiEvent> parsed =
                msgs.apply("Parse", TaxiEventProcessor.FromPubsubMessage.parse());
        PCollection<TaxiObjects.TaxiEvent> taxiEvents = parsed.getParsedData();

        taxiEvents.apply(
                "Write",
                Spanner.Writer.builder()
                        .projectId(projectId)
                        .instanceId(options.getSpannerInstance())
                        .databaseId(options.getSpannerDatabase())
                        .tableName(options.getSpannerTable())
                        .build());

        return p;
    }
}
