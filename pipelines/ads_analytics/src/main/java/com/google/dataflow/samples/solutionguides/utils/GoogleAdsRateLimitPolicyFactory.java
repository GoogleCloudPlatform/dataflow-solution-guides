package com.google.dataflow.samples.solutionguides.utils;

import com.google.ads.googleads.v17.errors.GoogleAdsError;
import com.google.common.util.concurrent.RateLimiter;
import com.google.protobuf.Message;
import org.apache.beam.sdk.io.googleads.GoogleAdsV17.RateLimitPolicy;
import org.apache.beam.sdk.io.googleads.GoogleAdsV17.RateLimitPolicyFactory;
import org.checkerframework.checker.initialization.qual.Initialized;
import org.checkerframework.checker.nullness.qual.NonNull;
import org.checkerframework.checker.nullness.qual.Nullable;
import org.checkerframework.checker.nullness.qual.UnknownKeyFor;

import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentMap;

public class GoogleAdsRateLimitPolicyFactory implements RateLimitPolicyFactory {

    static final ConcurrentMap<Double, RateLimitPolicy> CACHE = new ConcurrentHashMap<>();

    private final double permitsPerSecond;

    public GoogleAdsRateLimitPolicyFactory(double permitsPerSecond) {
        this.permitsPerSecond = permitsPerSecond;
    }

    @Override
    public RateLimitPolicy getRateLimitPolicy() {
        return CACHE.computeIfAbsent(
                permitsPerSecond,
                k -> new RateLimitPolicy() {

                    private final RateLimiter rateLimiter = RateLimiter.create(k);

                    @Override
                    public void onBeforeRequest(@UnknownKeyFor @NonNull @Initialized String developerToken, @UnknownKeyFor @NonNull @Initialized String customerId, @UnknownKeyFor @NonNull @Initialized Message request) throws InterruptedException {
                        rateLimiter.acquire();
                    }

                    @Override
                    public void onSuccess(@UnknownKeyFor @NonNull @Initialized String developerToken, @UnknownKeyFor @NonNull @Initialized String customerId, @UnknownKeyFor @NonNull @Initialized Message request) {

                    }

                    @Override
                    public void onError(@Nullable @UnknownKeyFor @Initialized String developerToken, @UnknownKeyFor @NonNull @Initialized String customerId, @UnknownKeyFor @NonNull @Initialized Message request, @UnknownKeyFor @NonNull @Initialized GoogleAdsError error) {

                    }
                }
        );
    }
}
