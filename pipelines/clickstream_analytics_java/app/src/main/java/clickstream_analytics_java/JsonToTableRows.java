package clickstream_analytics_java;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.io.InputStream;

import java.nio.charset.StandardCharsets;

import com.google.api.services.bigquery.model.TableRow;
import org.apache.beam.sdk.transforms.PTransform;
import org.apache.beam.sdk.values.PCollection;
import org.apache.beam.sdk.transforms.ParDo;
import org.apache.beam.sdk.transforms.DoFn;
import org.apache.beam.sdk.values.TupleTag;
import org.apache.beam.sdk.values.TupleTagList;
import org.apache.beam.sdk.values.PCollectionTuple;
import org.apache.beam.sdk.values.KV;
import org.apache.beam.sdk.coders.Coder.Context;
import org.apache.beam.sdk.io.gcp.bigquery.TableRowJsonCoder;

public class JsonToTableRows {

    public static PTransform<PCollection<String>, PCollectionTuple> run() {
        return new JsonToTableRows.JsonToTableRow();
    }

    static final TupleTag<TableRow> SUCCESS_TAG =
            new TupleTag<TableRow>(){};
    static final TupleTag<KV<String, String>> FAILURE_TAG =
            new TupleTag<KV<String, String>>(){};

    private static class JsonToTableRow
            extends PTransform<PCollection<String>, PCollectionTuple> {

        @Override
        public PCollectionTuple expand(PCollection<String> jsonStrings) {
            return jsonStrings
                    .apply(ParDo.of(new DoFn<String, TableRow>() {
                        @ProcessElement
                        public void processElement(ProcessContext context) {
                            String jsonString = context.element();

                            byte[] message_in_bytes = jsonString.getBytes(StandardCharsets.UTF_8);

                            if (message_in_bytes.length >= 10 * 1024 * 1024) {
                                System.out.println("Error: too big row of size {} bytes in type {}");
                                Metrics.tooBigMessages.inc();
                                context.output(FAILURE_TAG, KV.of("TooBigRow", jsonString));
                            }

                            TableRow row;
                            try (InputStream inputStream = new ByteArrayInputStream(message_in_bytes))
                            {
                                row = TableRowJsonCoder.of().decode(inputStream, Context.OUTER);
                                Metrics.successfulMessages.inc();
                                context.output(row);

                            } catch (IOException e) {
                                System.out.println("Error:" + e.getMessage());
                                Metrics.jsonParseErrorMessages.inc();
                                context.output(FAILURE_TAG, KV.of("JsonParseError", jsonString));
                            }
                        }
                    }).withOutputTags(SUCCESS_TAG, TupleTagList.of(FAILURE_TAG)));
        }
    }
}
