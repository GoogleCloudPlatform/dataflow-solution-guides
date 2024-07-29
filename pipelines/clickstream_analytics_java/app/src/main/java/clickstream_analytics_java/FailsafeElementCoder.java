package clickstream_analytics_java;

import clickstream_analytics_java.FailsafeElement;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.Arrays;
import java.util.List;
import org.apache.beam.sdk.coders.Coder;
import org.apache.beam.sdk.coders.CoderException;
import org.apache.beam.sdk.coders.CustomCoder;
import org.apache.beam.sdk.coders.NullableCoder;
import org.apache.beam.sdk.coders.StringUtf8Coder;
import org.apache.beam.sdk.values.TypeDescriptor;
import org.apache.beam.sdk.values.TypeParameter;

/**
 * The {@link FailsafeElementCoder} encodes and decodes {@link FailsafeElement} objects.
 *
 * <p>This coder is necessary until Avro supports parameterized types (<a
 * href="https://issues.apache.org/jira/browse/AVRO-1571">AVRO-1571</a>) without requiring to
 * explicitly specifying the schema for the type.
 *
 * @param <OriginalT> The type of the original payload to be encoded.
 * @param <CurrentT> The type of the current payload to be encoded.
 */
public class FailsafeElementCoder<OriginalT, CurrentT>
        extends CustomCoder<FailsafeElement<OriginalT, CurrentT>> {

    private static final NullableCoder<String> STRING_CODER = NullableCoder.of(StringUtf8Coder.of());
    private final Coder<OriginalT> originalPayloadCoder;
    private final Coder<CurrentT> currentPayloadCoder;

    private FailsafeElementCoder(
            Coder<OriginalT> originalPayloadCoder, Coder<CurrentT> currentPayloadCoder) {
        this.originalPayloadCoder = originalPayloadCoder;
        this.currentPayloadCoder = currentPayloadCoder;
    }

    public Coder<OriginalT> getOriginalPayloadCoder() {
        return originalPayloadCoder;
    }

    public Coder<CurrentT> getCurrentPayloadCoder() {
        return currentPayloadCoder;
    }

    public static <OriginalT, CurrentT> FailsafeElementCoder<OriginalT, CurrentT> of(
            Coder<OriginalT> originalPayloadCoder, Coder<CurrentT> currentPayloadCoder) {
        return new FailsafeElementCoder<>(originalPayloadCoder, currentPayloadCoder);
    }

    @Override
    public void encode(FailsafeElement<OriginalT, CurrentT> value, OutputStream outStream)
            throws IOException {
        if (value == null) {
            throw new CoderException("The FailsafeElementCoder cannot encode a null object!");
        }

        originalPayloadCoder.encode(value.getOriginalPayload(), outStream);
        currentPayloadCoder.encode(value.getPayload(), outStream);
        STRING_CODER.encode(value.getErrorMessage(), outStream);
        STRING_CODER.encode(value.getStacktrace(), outStream);
    }

    @Override
    public FailsafeElement<OriginalT, CurrentT> decode(InputStream inStream) throws IOException {

        OriginalT originalPayload = originalPayloadCoder.decode(inStream);
        CurrentT currentPayload = currentPayloadCoder.decode(inStream);
        String errorMessage = STRING_CODER.decode(inStream);
        String stacktrace = STRING_CODER.decode(inStream);

        return FailsafeElement.of(originalPayload, currentPayload)
                .setErrorMessage(errorMessage)
                .setStacktrace(stacktrace);
    }

    @Override
    public List<? extends Coder<?>> getCoderArguments() {
        return Arrays.asList(originalPayloadCoder, currentPayloadCoder);
    }

    @Override
    public TypeDescriptor<FailsafeElement<OriginalT, CurrentT>> getEncodedTypeDescriptor() {
        return new TypeDescriptor<FailsafeElement<OriginalT, CurrentT>>() {}.where(
                        new TypeParameter<OriginalT>() {}, originalPayloadCoder.getEncodedTypeDescriptor())
                .where(new TypeParameter<CurrentT>() {}, currentPayloadCoder.getEncodedTypeDescriptor());
    }
}