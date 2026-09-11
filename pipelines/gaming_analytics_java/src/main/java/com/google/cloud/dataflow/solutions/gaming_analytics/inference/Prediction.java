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

import com.google.auto.value.AutoValue;
import java.io.Serializable;
import javax.annotation.Nullable;

/** The outcome of scoring a single gameplay event. */
@AutoValue
public abstract class Prediction implements Serializable {

    /** The recommendation to activate in game, for example {@code retention_bonus_pack}. */
    public abstract String label();

    /** Confidence of the recommendation, in the {@code [0, 1]} range, when the model reports it. */
    public abstract @Nullable Double score();

    public static Prediction of(String label, @Nullable Double score) {
        return new AutoValue_Prediction(label, score);
    }
}
