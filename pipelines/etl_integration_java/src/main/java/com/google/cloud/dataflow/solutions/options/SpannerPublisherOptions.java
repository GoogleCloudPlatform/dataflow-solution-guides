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

package com.google.cloud.dataflow.solutions.options;

import org.apache.beam.sdk.options.Description;
import org.apache.beam.sdk.options.PipelineOptions;
import org.apache.beam.sdk.options.Validation;

public interface SpannerPublisherOptions extends PipelineOptions {
    @Validation.Required()
    @Description("Input topic with data to replicate in Spanner")
    void setPubsubTopic(String t);

    String getPubsubTopic();

    @Validation.Required()
    @Description("Spanner table to write the data to")
    void setSpannerTable(String t);

    String getSpannerTable();

    @Validation.Required()
    @Description("Spanner instance to write the data to")
    void setSpannerInstance(String s);

    String getSpannerInstance();

    @Validation.Required()
    @Description("Spanner database to write the data to")
    void setSpannerDatabase(String d);

    String getSpannerDatabase();
}
