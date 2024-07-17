package clickstream_analytics_java;

import com.google.cloud.bigtable.data.v2.BigtableDataSettings;
import org.apache.beam.sdk.transforms.DoFn;
import com.google.cloud.bigtable.data.v2.BigtableDataClient;
import com.google.cloud.bigtable.data.v2.models.Query;
import com.google.cloud.bigtable.data.v2.models.Row;
import com.google.cloud.bigtable.data.v2.models.RowCell;
import com.google.protobuf.ByteString;
import com.google.gson.Gson;
import com.google.gson.JsonObject;
import java.util.Base64;

public class BigTableEnrichment extends DoFn<String, String> {
    private final Gson gson = new Gson();
    private final String projectId;
    private final String instanceId;
    private final String tableId;

    private transient BigtableDataClient bigtableDataClient;
    public BigTableEnrichment(String projectId, String instanceId, String tableId) {
        this.projectId = projectId;
        this.instanceId = instanceId;
        this.tableId = tableId;

    }

    @Setup
    public void setup() throws Exception {
        BigtableDataSettings settings = BigtableDataSettings.newBuilder()
                .setProjectId(projectId)
                .setInstanceId(instanceId)
                .build();
        bigtableDataClient = BigtableDataClient.create(settings);
    }

    @Teardown
    public void teardown() throws Exception {
        if (bigtableDataClient != null) {
            bigtableDataClient.close();
        }
    }
    @ProcessElement
    public void processElement(ProcessContext context) {
        String jsonString = context.element();
        JsonObject jsonObject = gson.fromJson(jsonString, JsonObject.class);

        // Extract key from JSON (adjust as needed)
        String key = jsonObject.get("lookup_key").getAsString();

        // Construct Bigtable query
        String rowKey = Base64.getEncoder().encodeToString(ByteString.copyFromUtf8(key).toByteArray());
        Query query = Query.create(tableId).rowKey(rowKey);

        // Execute query and process results
        bigtableDataClient.readRows(query).forEach(row -> {
            // Extract enriching data from Bigtable row
            String enrichedValue = extractEnrichedValue(row);

            // Add enriched data to JSON
            jsonObject.addProperty("enriched_data", enrichedValue);

            // Output enriched JSON string
            context.output(gson.toJson(jsonObject));
        });
    }

    private String extractEnrichedValue(Row row) {
        for (RowCell cell : row.getCells()) {
            if (cell.getFamily().equals("cf") && cell.getQualifier().toStringUtf8().equals("col")) {
                return cell.getValue().toStringUtf8();
            }
        }
        return "";
    }
}
