package com.google.dataflow.samples.solutionguides.options;

import org.apache.beam.sdk.options.Default;
import org.apache.beam.sdk.options.Description;
import org.apache.beam.sdk.options.PipelineOptions;
import org.apache.beam.sdk.options.Validation;
import org.intellij.lang.annotations.Pattern;

public final class BigQueryCommonOptions {

    private BigQueryCommonOptions() {}

    public interface WriteOptions extends PipelineOptions
    {
        @Description("BigQuery output table")
        @Validation.Required
        String getOutputTableSpec();
        void setOutputTableSpec(String outputTableSpec);

        @Description("Write Disposition to use for BigQuery")
        @Default.String("WRITE_APPEND")
        String getWriteDisposition();
        void setWriteDisposition(String writeDisposition);

        @Description("Create Disposition to use for BigQuery")
        @Default.String("CREATE_IF_NEEDED")
        String getCreateDisposition();
        void setCreateDisposition(String createDisposition);
    }
}
