/*
 * *****************************************************************************
 * Copyright (C) 2014-2026 Dennis Sheirer
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <http://www.gnu.org/licenses/>
 * ****************************************************************************
 */

package io.github.dsheirer.dsp.filter.channelizer;

import io.github.dsheirer.sample.complex.InterleavedComplexSamples;
import java.util.List;
import java.util.Random;
import java.util.concurrent.atomic.AtomicInteger;
import org.jtransforms.fft.FloatFFT_1D;

/**
 * Manual benchmark (not run by the test suite) for the two stages of {@link ComplexPolyphaseChannelizerM2}: the
 * polyphase filter that runs on the tuner buffer thread, and the per-block IFFT that runs on the IFFT dispatcher
 * thread.  Reports throughput and the share of one CPU core each stage needs to keep up with real time, then the
 * end-to-end throughput of the whole channelizer with a single-threaded and a multi-threaded IFFT.
 *
 * Run from an IDE, or after './gradlew testClasses' with the test runtime classpath and the JVM arguments from
 * build.gradle (--add-modules=jdk.incubator.vector --enable-preview).
 */
public class ComplexPolyphaseChannelizerM2Benchmark
{
    private static final int TAPS_PER_CHANNEL = 9;
    private static final int BUFFER_SAMPLES = 131_072; //Complex samples per tuner buffer
    private static final double SECONDS = 3.0;

    public static void main(String[] args) throws Exception
    {
        double[] sampleRates = {2_400_000, 6_000_000, 10_000_000, 20_000_000};

        for(double sampleRate: sampleRates)
        {
            benchmark(sampleRate);
        }

        System.out.println();

        for(double sampleRate: sampleRates)
        {
            benchmarkEndToEnd(sampleRate, 1);
            benchmarkEndToEnd(sampleRate, 4);
        }
    }

    /**
     * Measures how fast the full channelizer (filter on the calling thread, IFFT on the dispatcher and worker
     * threads) can process a fixed amount of sample data, from the first buffer until the last block is dispatched.
     * The dispatcher queue is unbounded, so this run is sensitive to garbage collection and varies by up to 2x from
     * run to run: compare several runs rather than single numbers.
     */
    private static void benchmarkEndToEnd(double sampleRate, int ifftThreads) throws Exception
    {
        AtomicInteger dispatched = new AtomicInteger();
        ComplexPolyphaseChannelizerM2 channelizer = new ComplexPolyphaseChannelizerM2(sampleRate, TAPS_PER_CHANNEL,
            ifftThreads)
        {
            @Override
            protected void dispatch(List<float[]> channelResultsList)
            {
                dispatched.addAndGet(channelResultsList.size());
            }
        };

        float[] samples = new float[BUFFER_SAMPLES * 2];
        Random random = new Random(1);

        for(int x = 0; x < samples.length; x++)
        {
            samples[x] = random.nextFloat() * 2.0f - 1.0f;
        }

        InterleavedComplexSamples buffer = new InterleavedComplexSamples(samples, 0);
        int blocksPerBuffer = samples.length / channelizer.getChannelCount();
        channelizer.start();

        try
        {
            //Warm up with about 2 seconds of data, then time about 10 seconds of data
            for(int pass = 0; pass < 2; pass++)
            {
                int buffers = (int)((pass == 0 ? 2 : 10) * sampleRate / BUFFER_SAMPLES);
                //Only whole dispatch batches are released by the channelizer
                long expected = dispatched.get() + (long)buffers * blocksPerBuffer / 1024 * 1024;
                long start = System.nanoTime();

                for(int x = 0; x < buffers; x++)
                {
                    channelizer.receive(buffer);
                }

                while(dispatched.get() < expected)
                {
                    Thread.sleep(1);
                }

                double seconds = (System.nanoTime() - start) / 1e9;

                if(pass == 1)
                {
                    double samplesPerSecond = (double)buffers * BUFFER_SAMPLES / seconds;
                    System.out.printf("%5.1f MS/s, %d IFFT thread(s) | end to end: %6.1f MS/s (%.1fx real time)%n",
                        sampleRate / 1e6, ifftThreads, samplesPerSecond / 1e6, samplesPerSecond / sampleRate);
                }
            }
        }
        finally
        {
            channelizer.stop();
        }
    }

    private static void benchmark(double sampleRate) throws Exception
    {
        AtomicInteger dispatched = new AtomicInteger();
        ComplexPolyphaseChannelizerM2 channelizer = new ComplexPolyphaseChannelizerM2(sampleRate, TAPS_PER_CHANNEL)
        {
            @Override
            protected void dispatch(List<float[]> channelResultsList)
            {
                dispatched.addAndGet(channelResultsList.size());
            }
        };

        int channelCount = channelizer.getChannelCount();
        float[] samples = new float[BUFFER_SAMPLES * 2];
        Random random = new Random(1);

        for(int x = 0; x < samples.length; x++)
        {
            samples[x] = random.nextFloat() * 2.0f - 1.0f;
        }

        InterleavedComplexSamples buffer = new InterleavedComplexSamples(samples, 0);

        //Polyphase filter stage: the IFFT dispatcher is not started, so its input is discarded
        run(() -> channelizer.receive(buffer), SECONDS / 3); //Warm up
        long buffers = run(() -> channelizer.receive(buffer), SECONDS);
        double filterSamplesPerSecond = buffers * BUFFER_SAMPLES / SECONDS;

        //IFFT stage: one transform of channelCount complex values per block, on a freshly allocated array as the
        //channelizer does today.  Each block covers channelCount / 2 complex samples.
        FloatFFT_1D fft = new FloatFFT_1D(channelCount);
        int subChannelCount = channelizer.getSubChannelCount();
        Runnable ifft = () -> {
            float[] block = new float[subChannelCount];
            block[1] = 1.0f;
            fft.complexInverse(block, true);
        };
        run(ifft, SECONDS / 3); //Warm up
        long transforms = run(ifft, SECONDS);
        double ifftSamplesPerSecond = transforms * (channelCount / 2.0) / SECONDS;

        System.out.printf("%5.1f MS/s, %4d channels | filter: %6.1f MS/s, %5.1f%% of a core | " +
                "IFFT: %6.1f MS/s, %5.1f%% of a core (%.2f us each)%n",
            sampleRate / 1e6, channelCount,
            filterSamplesPerSecond / 1e6, 100 * sampleRate / filterSamplesPerSecond,
            ifftSamplesPerSecond / 1e6, 100 * sampleRate / ifftSamplesPerSecond,
            SECONDS * 1e6 / transforms);
    }

    /**
     * Runs the task repeatedly for the specified duration and returns the number of iterations completed.
     */
    private static long run(Runnable task, double seconds)
    {
        long end = System.nanoTime() + (long)(seconds * 1e9);
        long count = 0;

        while(System.nanoTime() < end)
        {
            task.run();
            count++;
        }

        return count;
    }
}
