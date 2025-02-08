package com.google.dataflow.samples.solutionguides.pipelines;

import com.google.ads.googleads.v17.services.GoogleAdsRow;
import com.google.dataflow.samples.solutionguides.options.BigQueryCommonOptions.WriteOptions;
import com.google.dataflow.samples.solutionguides.transforms.GoogleAdsRowToReportRowJsonFn;
import com.google.dataflow.samples.solutionguides.utils.GoogleAdsRateLimitPolicyFactory;
import com.google.dataflow.samples.solutionguides.utils.GoogleAdsUtils;
import com.google.dataflow.samples.solutionguides.transforms.BigQueryConverters;

import com.google.common.collect.ImmutableList;

import java.util.List;

import org.apache.beam.sdk.Pipeline;
import org.apache.beam.sdk.PipelineResult;
import org.apache.beam.sdk.io.gcp.bigquery.BigQueryIO;
import org.apache.beam.sdk.io.gcp.bigquery.BigQueryIO.Write;
import org.apache.beam.sdk.io.gcp.bigquery.BigQueryIO.Write.CreateDisposition;
import org.apache.beam.sdk.io.gcp.bigquery.BigQueryIO.Write.WriteDisposition;
import org.apache.beam.sdk.io.googleads.GoogleAdsIO;
import org.apache.beam.sdk.io.googleads.GoogleAdsOptions;
import org.apache.beam.sdk.options.Description;
import org.apache.beam.sdk.options.PipelineOptions;
import org.apache.beam.sdk.options.PipelineOptionsFactory;
import org.apache.beam.sdk.options.Validation;
import org.apache.beam.sdk.transforms.Create;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.values.PCollection;
import org.checkerframework.checker.initialization.qual.Initialized;
import org.checkerframework.checker.nullness.qual.NonNull;
import org.checkerframework.checker.nullness.qual.Nullable;
import org.checkerframework.checker.nullness.qual.UnknownKeyFor;
import org.intellij.lang.annotations.Pattern;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * A template for writing <a href=
 * "https://developers.google.com/google-ads/api/docs/reporting/overview">Google Ads reports</a> to
 * BigQuery.
 *
 * <p>Nested fields are lifted to top-level fields by replacing the dots in field paths with
 * underscores.
 *
 * <p>Check out <a
 * href="https://github.com/GoogleCloudPlatform/DataflowTemplates/blob/main/v2/README_Google_Ads_to_BigQuery.md">README</a>
 * for instructions on how to use or modify this template.
 */
public class AdsAnalyticsPipeline {

    public interface AdsAnalyticsPipelineOptions extends PipelineOptions, WriteOptions, GoogleAdsOptions
    {
        @Description("Google Ads manager account ID")
        Long getLoginCustomerId();
        void setLoginCustomerId(Long loginCustomerId);

        @Description("A list of Google Ads account IDs to use to execute the query")
        @Validation.Required
        List<Long> getCustomerIds();
        void setCustomerIds(List<Long> customerIds);

        @Description("The query to use to get the data. See Google Ads Query Language. For example: `SELECT campaign.id, campaign.name FROM campaign`.")
        @Validation.Required
        String getQuery();
        void setQuery(String query);

        @Description("Required Google Ads request rate per worker")
        Double getQpsPerWorker();
        void setQpsPerWorker(Double qpsPerWorker);

        @Override
        @UnknownKeyFor
        @NonNull
        @Initialized
        @Description("OAuth 2.0 Client Secret for the specified Client ID")
        default String getGoogleAdsClientSecret() {
            return "";
        }

        @Override
        default void setGoogleAdsClientSecret(@UnknownKeyFor @NonNull @Initialized String clientSecret) {

        }

        @Override
        @UnknownKeyFor
        @NonNull
        @Initialized
        @Description("OAuth 2.0 Refresh Token for the user connecting to the Google Ads API")
        default String getGoogleAdsRefreshToken() {
            return "";
        }

        @Override
        default void setGoogleAdsRefreshToken(@UnknownKeyFor @NonNull @Initialized String refreshToken) {

        }

        @Override
        @Nullable
        @UnknownKeyFor
        @Initialized
        @Description("Google Ads developer token for the user connecting to the Google Ads API")
        default String getGoogleAdsDeveloperToken() {
            return "";
        }

        @Override
        default void setGoogleAdsDeveloperToken(@UnknownKeyFor @NonNull @Initialized String developerToken) {

        }

    }

    private static final Logger LOG = LoggerFactory.getLogger(AdsAnalyticsPipeline.class);

    public static void main(String[] args) {
        run(
                PipelineOptionsFactory.fromArgs(args)
                        .withValidation()
                        .as(AdsAnalyticsPipelineOptions.class));
    }

    public static PipelineResult run(AdsAnalyticsPipelineOptions options) {
        Pipeline pipeline = Pipeline.create(options);
        double qps = options.getQpsPerWorker();
        String query = options.getQuery();

        PCollection<GoogleAdsRow> googleAdsRows =
                pipeline
                        .apply(
                                Create.of(
                                        options.getCustomerIds().stream()
                                                .map(Object::toString)
                                                .collect(ImmutableList.toImmutableList())))
                        .apply(
                                GoogleAdsIO.v17()
                                        .read()
                                        .withDeveloperToken(options.getGoogleAdsDeveloperToken())
                                        .withLoginCustomerId(options.getLoginCustomerId())
                                        .withQuery(options.getQuery())
                                        .withRateLimitPolicy(new GoogleAdsRateLimitPolicyFactory(qps)));

        PCollection<String> reportRows =
                googleAdsRows.apply(ParDo.of(new GoogleAdsRowToReportRowJsonFn(query)));

        Write<String> write =
                BigQueryIO.<String>write()
                        .withoutValidation()
                        .withWriteDisposition(WriteDisposition.valueOf(options.getWriteDisposition()))
                        .withCreateDisposition(CreateDisposition.valueOf(options.getCreateDisposition()))
                        .withFormatFunction(BigQueryConverters::convertJsonToTableRow)
                        .to(options.getOutputTableSpec());

        write = write.withSchema(GoogleAdsUtils.createBigQuerySchema(query));

        reportRows.apply("WriteToBigQuery", write);

        return pipeline.run();
    }
}
