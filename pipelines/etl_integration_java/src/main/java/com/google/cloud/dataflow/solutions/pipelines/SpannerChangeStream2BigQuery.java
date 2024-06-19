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

import com.google.api.services.bigquery.model.TableReference;
import com.google.api.services.bigquery.model.TableSchema;
import com.google.cloud.Timestamp;
import com.google.cloud.dataflow.solutions.data.SchemaUtils;
import com.google.cloud.dataflow.solutions.data.TaxiObjects.MergedCDCValue;
import com.google.cloud.dataflow.solutions.data.TaxiObjects.ParsingError;
import com.google.cloud.dataflow.solutions.data.TaxiObjects.TaxiEvent;
import com.google.cloud.dataflow.solutions.options.ChangeStreamOptions;
import com.google.cloud.dataflow.solutions.transform.CDCProcessor;
import com.google.cloud.dataflow.solutions.transform.TaxiEventProcessor.ParsingOutput;
import java.time.Instant;
import org.apache.beam.runners.dataflow.options.DataflowPipelineOptions;
import org.apache.beam.sdk.Pipeline;
import org.apache.beam.sdk.io.gcp.bigquery.BigQueryIO;
import org.apache.beam.sdk.io.gcp.bigquery.BigQueryUtils;
import org.apache.beam.sdk.io.gcp.spanner.SpannerIO;
import org.apache.beam.sdk.io.gcp.spanner.changestreams.model.DataChangeRecord;
import org.apache.beam.sdk.schemas.Schema;
import org.apache.beam.sdk.values.PCollection;

public class SpannerChangeStream2BigQuery {
    private static Timestamp getCatchupTimestamp(long catchupMinutes) {
        return Timestamp.ofTimeSecondsAndNanos(
                Instant.now().minusSeconds(catchupMinutes * 60).getEpochSecond(), 0);
    }

    public static Pipeline createPipeline(ChangeStreamOptions options) {
        String projectId = options.as(DataflowPipelineOptions.class).getProject();
        String bigQueryDataset = options.getBigQueryDataset();
        String bigQueryTable = options.getBigQueryTable();
        String bigQueryErrorsTable = options.getBigQueryErrorsTable();

        String spannerInstance = options.getSpannerInstance();
        String spannerChangeStream = options.getSpannerChangeStream();
        String metadataDatabase = options.getMetadataDatabase();
        String metadataTable = options.getMyMetadataTable();

        Pipeline p = Pipeline.create(options);
        PCollection<DataChangeRecord> changes =
                p.apply(
                        "Read change stream",
                        SpannerIO.readChangeStream()
                                .withProjectId(projectId)
                                .withInstanceId(spannerInstance)
                                .withDatabaseId(options.getSpannerDatabase())
                                .withMetadataInstance(spannerInstance)
                                .withMetadataDatabase(metadataDatabase)
                                .withMetadataTable(metadataTable)
                                .withChangeStreamName(spannerChangeStream)
                                .withInclusiveStartAt(
                                        getCatchupTimestamp(options.getCatchUpMinutes())));

        ParsingOutput<MergedCDCValue> parsedChanges =
                changes.apply("Parse changes", CDCProcessor.ParseCDCRecord.create());

        PCollection<MergedCDCValue> parsedData = parsedChanges.getParsedData();
        PCollection<ParsingError> errors = parsedChanges.getErrors();

        Schema cdcSchema = SchemaUtils.getSchemaForType(p, TaxiEvent.class);
        TableSchema tableSchema = BigQueryUtils.toTableSchema(cdcSchema);

        parsedData.apply(
                "Sync with BQ",
                BigQueryIO.<MergedCDCValue>write()
                        .to(
                                new TableReference()
                                        .setProjectId(projectId)
                                        .setDatasetId(bigQueryDataset)
                                        .setTableId(bigQueryTable))
                        .withPrimaryKey(TaxiEvent.primaryKeys())
                        .withFormatFunction(cdc -> cdc.getTableRow())
                        .withSchema(tableSchema)
                        .withCreateDisposition(BigQueryIO.Write.CreateDisposition.CREATE_IF_NEEDED)
                        .withRowMutationInformationFn(CDCProcessor::rowMutationInformation)
                        .withMethod(BigQueryIO.Write.Method.STORAGE_API_AT_LEAST_ONCE));

        errors.apply(
                "Write errors to BQ",
                BigQueryIO.<ParsingError>write()
                        .to(
                                new TableReference()
                                        .setProjectId(projectId)
                                        .setDatasetId(bigQueryDataset)
                                        .setTableId(bigQueryErrorsTable))
                        .useBeamSchema()
                        .withCreateDisposition(BigQueryIO.Write.CreateDisposition.CREATE_IF_NEEDED)
                        .withWriteDisposition(BigQueryIO.Write.WriteDisposition.WRITE_APPEND)
                        .withMethod(BigQueryIO.Write.Method.STORAGE_API_AT_LEAST_ONCE));

        return p;
    }
}
