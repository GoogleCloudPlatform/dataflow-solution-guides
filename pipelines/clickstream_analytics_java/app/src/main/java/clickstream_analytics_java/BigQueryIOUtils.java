package clickstream_analytics_java;

import com.google.api.services.bigquery.model.ErrorProto;
import com.google.api.services.bigquery.model.TableDataInsertAllResponse.InsertErrors;
import com.google.api.services.bigquery.model.TableReference;
import java.util.List;
import org.apache.beam.sdk.io.gcp.bigquery.BigQueryInsertError;
import org.apache.beam.sdk.io.gcp.bigquery.BigQueryInsertErrorCoder;
import org.apache.beam.sdk.io.gcp.bigquery.BigQueryOptions;
import org.apache.beam.sdk.io.gcp.bigquery.BigQueryStorageApiInsertError;
import org.apache.beam.sdk.io.gcp.bigquery.WriteResult;
import org.apache.beam.sdk.transforms.MapElements;
import org.apache.beam.sdk.transforms.SimpleFunction;
import org.apache.beam.sdk.values.PCollection;

public final class BigQueryIOUtils {
    private static final TableReference EMPTY_TABLE_REFERENCE =
            new TableReference().setTableId("unknown").setProjectId("unknown").setDatasetId("unknown");

    private BigQueryIOUtils() {}

    public static PCollection<BigQueryInsertError> writeResultToBigQueryInsertErrors(
            WriteResult writeResult, BigQueryOptions options) {
        if (options.getUseStorageWriteApi()) {
            return writeResult
                    .getFailedStorageApiInserts()
                    .apply(
                            MapElements.via(
                                    new SimpleFunction<BigQueryStorageApiInsertError, BigQueryInsertError>() {
                                        public BigQueryInsertError apply(BigQueryStorageApiInsertError error) {
                                            return new BigQueryInsertError(
                                                    error.getRow(),
                                                    new InsertErrors()
                                                            .setErrors(
                                                                    List.of(new ErrorProto().setMessage(error.getErrorMessage()))),
                                                    EMPTY_TABLE_REFERENCE);
                                        }
                                    }))
                    .setCoder(BigQueryInsertErrorCoder.of());
        }
        return writeResult.getFailedInsertsWithErr();
    }
}