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

import org.apache.beam.sdk.options.Default;
import org.apache.beam.sdk.options.Description;
import org.apache.beam.sdk.options.Validation;

public interface ChangeStreamOptions extends SpannerOptions {
    @Validation.Required
    @Description("Spanner change stream to read from")
    void setSpannerChangeStream(String s);

    String getSpannerChangeStream();

    @Validation.Required
    @Description("BigQuery destination dataset")
    void setBigQueryDataset(String d);

    String getBigQueryDataset();

    @Validation.Required
    @Description("BigQuery destination table")
    void setBigQueryTable(String t);

    String getBigQueryTable();

    @Description("Errors table in BigQuery")
    void setBigQueryErrorsTable(String t);

    @Default.String("errors")
    String getBigQueryErrorsTable();

    @Description("Catch up time for the change stream")
    void setCatchUpMinutes(long t);

    @Default.Long(10)
    long getCatchUpMinutes();

    @Description("Metadata database name")
    void setMetadataDatabase(String d);

    @Default.String("metadata")
    String getMetadataDatabase();

    @Description("Metadata table name")
    void setMyMetadataTable(String t);

    @Default.String("change_stream")
    String getMyMetadataTable();
}
