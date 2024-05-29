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

import com.google.cloud.dataflow.solutions.options.ChangeStreamOptions;
import com.google.cloud.dataflow.solutions.options.RunOptions;
import com.google.cloud.dataflow.solutions.options.SpannerPublisherOptions;
import com.google.cloud.dataflow.solutions.pipelines.Pubsub2Spanner;
import com.google.cloud.dataflow.solutions.pipelines.SpannerChangeStream2Pubsub;
import org.apache.beam.sdk.Pipeline;
import org.apache.beam.sdk.options.PipelineOptionsFactory;

public class ETLIntegration {
    private static String getJobName(String pipelineToRun) {
        return pipelineToRun.replace("_", "-");
    }

    public static void main(String[] args) {
        PipelineOptionsFactory.register(RunOptions.class);
        RunOptions runOptions =
                PipelineOptionsFactory.fromArgs(args).withoutStrictParsing().as(RunOptions.class);

        RunOptions.PipelineToRun pipelineToRun = runOptions.getPipeline();

        Pipeline p = null;
        String jobName = getJobName(pipelineToRun.toString());
        switch (pipelineToRun) {
            case PUBSUB_TO_SPANNER:
                SpannerPublisherOptions spannerPublisherOptions =
                        PipelineOptionsFactory.fromArgs(args)
                                .withoutStrictParsing()
                                .as(SpannerPublisherOptions.class);
                p = Pubsub2Spanner.createPipeline(spannerPublisherOptions);
                break;
            case SPANNER_CHANGE_STREAM:
                ChangeStreamOptions changeStreamOptions =
                        PipelineOptionsFactory.fromArgs(args)
                                .withoutStrictParsing()
                                .as(ChangeStreamOptions.class);
                p = SpannerChangeStream2Pubsub.createPipeline(changeStreamOptions);
                jobName +=
                        String.format(
                                "-%d-topics", changeStreamOptions.getPubsubOutputTopicCount());
                break;
        }

        if (p != null) {
            p.getOptions().setJobName(jobName);
            p.run();
        } else {
            System.out.println("Unrecognized pipeline type " + pipelineToRun);
            System.exit(1);
        }
    }
}
