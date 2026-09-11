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
package com.google.cloud.dataflow.solutions.gaming_analytics;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import com.google.cloud.dataflow.solutions.gaming_analytics.transform.RecommendationInference;
import com.google.common.collect.ImmutableMap;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.junit.runners.JUnit4;

/**
 * Guards the Python package versions, which are stated in three places that must agree.
 *
 * <ul>
 *   <li>{@link RecommendationInference#HARNESS_REQUIREMENTS} is what gets installed into the Python
 *       SDK harness that runs the model on the worker;
 *   <li>{@code scripts/requirements-model.txt} is what a developer installs locally to run {@code
 *       scripts/train_model.py} and the other helper scripts;
 *   <li>{@code PINNED_SKLEARN_VERSION} and {@code PINNED_NUMPY_VERSION} in {@code train_model.py}
 *       are checked before the model is written, because scikit-learn does not guarantee pickle
 *       compatibility across versions.
 * </ul>
 *
 * <p>A disagreement between them is not a build failure anywhere else: it surfaces as an unpickling
 * error on a Dataflow worker, long after submission. Comments in all three files ask whoever bumps
 * the Beam version to move them together; this test is what actually enforces it.
 */
@RunWith(JUnit4.class)
public class PinnedVersionsConsistencyTest {

    /** {@code name==version}, the only form allowed for the pinned packages. */
    private static final Pattern PINNED_REQUIREMENT =
            Pattern.compile("^([A-Za-z0-9._-]+)==([A-Za-z0-9._-]+)\\s*$");

    /** {@code CONSTANT = "version"} in the training script. */
    private static final Pattern PYTHON_CONSTANT =
            Pattern.compile("^\\s*(PINNED_[A-Z_]+_VERSION)\\s*=\\s*\"([^\"]+)\"");

    private static final String REQUIREMENTS = "scripts/requirements-model.txt";
    private static final String TRAIN_MODEL = "scripts/train_model.py";

    @Test
    public void testHarnessRequirementsMatchTheScriptRequirements() throws IOException {
        Map<String, String> harness = parsePins(RecommendationInference.HARNESS_REQUIREMENTS);
        Map<String, String> requirements = parsePins(readLines(REQUIREMENTS));

        // Every package the harness installs must be pinned identically for local use, so that a
        // model trained on a workstation is loadable by the worker.
        for (Map.Entry<String, String> pin : harness.entrySet()) {
            assertEquals(
                    "Package '"
                            + pin.getKey()
                            + "' is pinned in RecommendationInference.HARNESS_REQUIREMENTS but"
                            + " differs in "
                            + REQUIREMENTS
                            + ". These versions must move together.",
                    pin.getValue(),
                    requirements.get(pin.getKey()));
        }
    }

    @Test
    public void testTrainingScriptPinsMatchTheHarness() throws IOException {
        Map<String, String> harness = parsePins(RecommendationInference.HARNESS_REQUIREMENTS);
        Map<String, String> constants = parsePythonConstants(readLines(TRAIN_MODEL));

        // Only the two pickle-critical packages are mirrored in the training script; pandas is an
        // import-time requirement of the harness and is deliberately not checked there.
        assertEquals(
                "PINNED_SKLEARN_VERSION in "
                        + TRAIN_MODEL
                        + " must match the scikit-learn version installed in the harness.",
                harness.get("scikit-learn"),
                constants.get("PINNED_SKLEARN_VERSION"));
        assertEquals(
                "PINNED_NUMPY_VERSION in "
                        + TRAIN_MODEL
                        + " must match the numpy version installed in the harness.",
                harness.get("numpy"),
                constants.get("PINNED_NUMPY_VERSION"));
    }

    @Test
    public void testTheHarnessRequirementsAreCompleteAndExact() {
        Map<String, String> harness = parsePins(RecommendationInference.HARNESS_REQUIREMENTS);

        // Passing an explicit package list disables Beam's own inference of the model handler's
        // dependencies, so pandas has to be listed even though the pickle holds no pandas objects:
        // apache_beam.ml.inference.sklearn_inference imports it at module scope.
        assertEquals(
                "The harness needs exactly scikit-learn, numpy and pandas. Adding or removing one"
                        + " changes what is installed on every worker.",
                ImmutableMap.of("scikit-learn", "", "numpy", "", "pandas", "").keySet(),
                harness.keySet());
        assertEquals(
                "Every harness requirement must be '==' pinned.",
                RecommendationInference.HARNESS_REQUIREMENTS.size(),
                harness.size());
    }

    /** Fails loudly rather than vacuously passing if a file is moved or renamed. */
    private static List<String> readLines(String relativePath) throws IOException {
        Path path = resolve(relativePath);
        assertTrue(
                "Expected to find "
                        + relativePath
                        + " at "
                        + path.toAbsolutePath()
                        + ". If it moved, update this test: it is the only thing keeping the"
                        + " pinned versions in step.",
                Files.isRegularFile(path));
        return Files.readAllLines(path, StandardCharsets.UTF_8);
    }

    /**
     * Resolves a path relative to the Gradle project directory, which is the working directory of
     * the test JVM, and tolerates being run from the repository root.
     */
    private static Path resolve(String relativePath) {
        Path fromWorkingDir = Paths.get(relativePath);
        if (Files.isRegularFile(fromWorkingDir)) {
            return fromWorkingDir;
        }
        return Paths.get("pipelines/gaming_analytics_java").resolve(relativePath);
    }

    /** Extracts the {@code name==version} entries, ignoring comments, blanks and range pins. */
    private static Map<String, String> parsePins(List<String> lines) {
        Map<String, String> pins = new LinkedHashMap<>();
        for (String line : lines) {
            Matcher matcher = PINNED_REQUIREMENT.matcher(line.trim());
            if (matcher.matches()) {
                pins.put(matcher.group(1), matcher.group(2));
            }
        }
        return pins;
    }

    private static Map<String, String> parsePythonConstants(List<String> lines) {
        Map<String, String> constants = new LinkedHashMap<>();
        for (String line : lines) {
            Matcher matcher = PYTHON_CONSTANT.matcher(line);
            if (matcher.find()) {
                constants.put(matcher.group(1), matcher.group(2));
            }
        }
        return constants;
    }
}
