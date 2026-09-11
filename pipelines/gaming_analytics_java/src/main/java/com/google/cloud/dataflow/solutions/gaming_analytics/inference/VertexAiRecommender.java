/*
 * Copyright 2026 Google.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *   http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
package com.google.cloud.dataflow.solutions.gaming_analytics.inference;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.google.auth.oauth2.GoogleCredentials;
import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.EnrichedEvent;
import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.GameplayEvent;
import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Scores events with a model deployed behind a Vertex AI online prediction endpoint.
 *
 * <p>This is the {@code inference_mode = "vertex"} topology of the Terraform module: the workers
 * run on CPU machines and only hold {@code aiplatform.endpoints.predict} on the endpoint, through
 * the {@code gamingAnalyticsPredictor} custom role.
 *
 * <p>The endpoint is called over REST with Application Default Credentials, one request per event.
 * That keeps the activation latency predictable and the code dependency-light; if your endpoint
 * benefits from batching, group the events upstream (for example with {@code GroupIntoBatches}) and
 * send several instances per request.
 *
 * <p>The request follows the standard prediction protocol, with one instance carrying the event
 * fields and the Bigtable features flattened at the same level:
 *
 * <pre>{@code
 * {"instances": [{"player_id": "p1", "event_type": "level_failed", "level": 12,
 *                 "score": 900, "churn_risk": "0.81", "spend_tier": "paying"}]}
 * }</pre>
 */
public final class VertexAiRecommender implements Recommender {

    private static final long serialVersionUID = 1L;

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String CLOUD_PLATFORM_SCOPE =
            "https://www.googleapis.com/auth/cloud-platform";
    private static final Pattern ENDPOINT_PATTERN =
            Pattern.compile("^projects/[^/]+/locations/([^/]+)/endpoints/[^/]+$");

    private final String endpoint;
    private final String labelField;
    private final String scoreField;
    private final int timeoutSeconds;

    private transient HttpClient httpClient;
    private transient GoogleCredentials credentials;
    private transient String predictUrl;

    public VertexAiRecommender(
            String endpoint, String labelField, String scoreField, int timeoutSeconds) {
        this.endpoint = endpoint;
        this.labelField = labelField;
        this.scoreField = scoreField;
        this.timeoutSeconds = timeoutSeconds > 0 ? timeoutSeconds : 10;
    }

    /**
     * Builds the online prediction URL of a Vertex AI endpoint.
     *
     * @param endpoint endpoint resource name, {@code projects/P/locations/L/endpoints/E}
     * @return the regional {@code :predict} URL
     * @throws IllegalArgumentException if the resource name is malformed
     */
    public static String predictUrl(String endpoint) {
        if (endpoint == null) {
            throw new IllegalArgumentException("A Vertex AI endpoint resource name is required");
        }
        Matcher matcher = ENDPOINT_PATTERN.matcher(endpoint.trim());
        if (!matcher.matches()) {
            throw new IllegalArgumentException(
                    String.format(
                            "Malformed Vertex AI endpoint '%s', expected"
                                    + " projects/PROJECT/locations/LOCATION/endpoints/ENDPOINT",
                            endpoint));
        }
        return String.format(
                "https://%s-aiplatform.googleapis.com/v1/%s:predict",
                matcher.group(1), endpoint.trim());
    }

    /** Builds the prediction request body for a single event. */
    public static String buildRequestBody(EnrichedEvent enriched) {
        GameplayEvent event = enriched.getEvent();
        ObjectNode instance = MAPPER.createObjectNode();
        instance.put("player_id", event.getPlayerId());
        instance.put("session_id", event.getSessionId());
        instance.put("event_type", event.getEventType());
        if (event.getLevel() != null) {
            instance.put("level", event.getLevel());
        }
        if (event.getScore() != null) {
            instance.put("score", event.getScore());
        }
        instance.put("event_timestamp", event.getEventTimestamp());
        for (Map.Entry<String, String> feature : enriched.getFeatures().entrySet()) {
            instance.put(feature.getKey(), feature.getValue());
        }
        ObjectNode body = MAPPER.createObjectNode();
        body.putArray("instances").add(instance);
        return body.toString();
    }

    /**
     * Extracts the recommendation from a Vertex AI prediction response.
     *
     * <p>Both shapes of the standard protocol are accepted: a structured prediction, from which
     * {@code labelField} and {@code scoreField} are read, and a bare string prediction, which is
     * used as the label with no score.
     *
     * @throws IOException if the response does not carry a usable prediction
     */
    public static Prediction parsePrediction(
            String responseBody, String labelField, String scoreField) throws IOException {
        JsonNode root = MAPPER.readTree(responseBody);
        JsonNode predictions = root.path("predictions");
        if (!predictions.isArray() || predictions.isEmpty()) {
            throw new IOException(
                    String.format("Vertex AI response carries no prediction: %s", responseBody));
        }
        JsonNode prediction = predictions.get(0);
        if (prediction.isTextual()) {
            return Prediction.of(prediction.asText(), null);
        }
        if (!prediction.isObject()) {
            throw new IOException(
                    String.format("Unsupported Vertex AI prediction format: %s", prediction));
        }
        JsonNode label = prediction.path(labelField);
        if (label.isMissingNode() || label.isNull()) {
            throw new IOException(
                    String.format(
                            "Vertex AI prediction has no '%s' field: %s", labelField, prediction));
        }
        JsonNode score = prediction.path(scoreField);
        Double scoreValue = score.isNumber() ? score.asDouble() : null;
        return Prediction.of(label.asText(), scoreValue);
    }

    @Override
    public void setUp() throws IOException {
        predictUrl = predictUrl(endpoint);
        credentials = GoogleCredentials.getApplicationDefault().createScoped(CLOUD_PLATFORM_SCOPE);
        httpClient =
                HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(timeoutSeconds)).build();
    }

    @Override
    public Prediction predict(EnrichedEvent event) throws IOException, InterruptedException {
        credentials.refreshIfExpired();
        HttpRequest request =
                HttpRequest.newBuilder(URI.create(predictUrl))
                        .timeout(Duration.ofSeconds(timeoutSeconds))
                        .header(
                                "Authorization",
                                "Bearer " + credentials.getAccessToken().getTokenValue())
                        .header("Content-Type", "application/json; charset=utf-8")
                        .POST(
                                HttpRequest.BodyPublishers.ofString(
                                        buildRequestBody(event), StandardCharsets.UTF_8))
                        .build();

        HttpResponse<String> response =
                httpClient.send(
                        request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        if (response.statusCode() < 200 || response.statusCode() >= 300) {
            throw new IOException(
                    String.format(
                            "Vertex AI endpoint %s returned HTTP %d: %s",
                            endpoint, response.statusCode(), response.body()));
        }
        return parsePrediction(response.body(), labelField, scoreField);
    }

    @Override
    public void tearDown() {
        httpClient = null;
        credentials = null;
    }
}
