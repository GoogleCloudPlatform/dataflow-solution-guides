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

import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.EnrichedEvent;
import com.google.cloud.dataflow.solutions.gaming_analytics.data.GamingObjects.GameplayEvent;
import java.util.Map;

/**
 * A deterministic, in-process recommender.
 *
 * <p>It stands in for the local model that, in the reference architecture, runs on the workers
 * (optionally on a GPU) and keeps the activation latency to a minimum. It is intentionally a set of
 * explicit rules rather than a model artifact: it needs no accelerator, no model download and no
 * network call, which makes the guide runnable end to end and the behaviour fully testable. Replace
 * it with your own model wrapper, or switch to {@link VertexAiRecommender}, when you plug in a real
 * model.
 *
 * <p>The rules are evaluated in order and the first match wins:
 *
 * <ol>
 *   <li>a player at risk of churning gets a retention offer;
 *   <li>a player failing a level repeatedly gets a difficulty assist;
 *   <li>a paying player completing content or buying gets a premium bundle;
 *   <li>a player performing well above their skill rating gets a tournament invitation;
 *   <li>everyone else gets a daily quest suggestion.
 * </ol>
 */
public final class LocalRecommender implements Recommender {

    private static final long serialVersionUID = 1L;

    public static final String RETENTION_BONUS = "retention_bonus_pack";
    public static final String DIFFICULTY_ASSIST = "difficulty_assist_boost";
    public static final String PREMIUM_BUNDLE = "premium_bundle_offer";
    public static final String TOURNAMENT_INVITE = "tournament_invite";
    public static final String DAILY_QUEST = "daily_quest_suggestion";

    private static final double CHURN_RISK_THRESHOLD = 0.7;
    private static final int LEVEL_FAILURE_THRESHOLD = 3;

    @Override
    public Prediction predict(EnrichedEvent enriched) {
        GameplayEvent event = enriched.getEvent();
        Map<String, String> features = enriched.getFeatures();

        String eventType = event.getEventType() != null ? event.getEventType() : "";
        double churnRisk = numericFeature(features, "churn_risk", 0.0);
        double levelFailures = numericFeature(features, "level_failures", 0.0);
        double skillRating = numericFeature(features, "skill_rating", 0.0);
        String spendTier = textFeature(features, "spend_tier", "free");
        long score = event.getScore() != null ? event.getScore() : 0L;
        int level = event.getLevel() != null ? event.getLevel() : 0;

        if (churnRisk >= CHURN_RISK_THRESHOLD) {
            return Prediction.of(RETENTION_BONUS, clamp(churnRisk));
        }

        if ("level_failed".equals(eventType)) {
            if (levelFailures >= LEVEL_FAILURE_THRESHOLD) {
                return Prediction.of(DIFFICULTY_ASSIST, clamp(0.6 + 0.05 * levelFailures));
            }
            return Prediction.of(DIFFICULTY_ASSIST, clamp(0.4 + 0.02 * level));
        }

        boolean paying = "paying".equals(spendTier) || "whale".equals(spendTier);
        if (paying && ("level_complete".equals(eventType) || "purchase".equals(eventType))) {
            double boost = "whale".equals(spendTier) ? 0.2 : 0.0;
            return Prediction.of(PREMIUM_BUNDLE, clamp(0.6 + boost + 0.005 * level));
        }

        if (skillRating > 0 && score > skillRating * 1.5) {
            return Prediction.of(TOURNAMENT_INVITE, clamp(score / (skillRating * 3.0)));
        }

        return Prediction.of(DAILY_QUEST, 0.25);
    }

    /**
     * Reads a numeric feature, falling back to {@code defaultValue} when it is absent, empty or not
     * a number. A malformed feature must not take the activation path down.
     */
    static double numericFeature(Map<String, String> features, String name, double defaultValue) {
        if (features == null) {
            return defaultValue;
        }
        String raw = features.get(name);
        if (raw == null || raw.trim().isEmpty()) {
            return defaultValue;
        }
        try {
            return Double.parseDouble(raw.trim());
        } catch (NumberFormatException e) {
            return defaultValue;
        }
    }

    static String textFeature(Map<String, String> features, String name, String defaultValue) {
        if (features == null) {
            return defaultValue;
        }
        String raw = features.get(name);
        if (raw == null || raw.trim().isEmpty()) {
            return defaultValue;
        }
        return raw.trim();
    }

    static double clamp(double value) {
        return Math.max(0.0, Math.min(1.0, value));
    }
}
