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
import java.util.ArrayList;
import java.util.List;
import java.util.Random;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * End-to-end regression test for {@link ComplexPolyphaseChannelizerM2}: feeds a fixed pseudo-random sample stream
 * through the full channelizer (polyphase filter, block reordering and the IFFT dispatcher thread) and compares a hash
 * of every output value, using {@link Float#floatToRawIntBits(float)}, against a golden value captured from the
 * single-threaded channelizer at upstream commit b0428a1.  The parallel IFFT must reproduce those values exactly.
 *
 * Any change to the channelizer's processing or threading that alters even one output bit fails this test.  If an
 * intentional DSP change alters the output, re-capture the golden values and say so in the commit.
 */
public class ComplexPolyphaseChannelizerM2OutputTest
{
    private static final int TAPS_PER_CHANNEL = 9; //Matches PolyphaseChannelManager.POLYPHASE_CHANNELIZER_TAPS_PER_CHANNEL
    private static final int BLOCKS_PER_DISPATCH = 1024; //Matches PROCESSED_CHANNEL_RESULTS_THRESHOLD
    private static final long TIMEOUT_MS = 30_000;

    //Golden hashes captured from the single-threaded IFFT implementation at upstream commit b0428a1
    private static final long GOLDEN_2_4_MHZ = 0x436558856E33E607L;
    private static final long GOLDEN_10_MHZ = 0xA71DAC87D431F42DL;
    private static final long GOLDEN_20_MHZ = 0xD81C73A27AB8AEA0L;

    @Test
    void outputMatchesGolden_2_4MHz() throws Exception
    {
        assertOutput(2_400_000, 4, 4, GOLDEN_2_4_MHZ);
    }

    @Test
    void outputMatchesGolden_10MHz_singleThreadIFFT() throws Exception
    {
        assertOutput(10_000_000, 1, 4, GOLDEN_10_MHZ);
    }

    @Test
    void outputMatchesGolden_10MHz_parallelIFFT() throws Exception
    {
        assertOutput(10_000_000, 4, 4, GOLDEN_10_MHZ);
    }

    @Test
    void outputMatchesGolden_20MHz_parallelIFFT() throws Exception
    {
        assertOutput(20_000_000, 3, 4, GOLDEN_20_MHZ);
    }

    private static void assertOutput(double sampleRate, int ifftThreads, int dispatches, long expectedHash)
        throws Exception
    {
        CapturingChannelizer channelizer = new CapturingChannelizer(sampleRate, ifftThreads);
        int channelCount = channelizer.getChannelCount();
        int expectedBlocks = dispatches * BLOCKS_PER_DISPATCH;

        //Each block consumes channelCount floats (channelCount / 2 complex samples)
        int totalFloats = expectedBlocks * channelCount;

        Random random = new Random(0x5D27L);
        channelizer.start();

        try
        {
            int fed = 0;

            while(fed < totalFloats)
            {
                //Deliberately not a multiple of the block size, so blocks straddle buffer boundaries
                int length = Math.min(12_346, totalFloats - fed);
                float[] samples = new float[length];

                for(int x = 0; x < length; x++)
                {
                    samples[x] = random.nextFloat() * 2.0f - 1.0f;
                }

                channelizer.receive(new InterleavedComplexSamples(samples, 0));
                fed += length;
            }

            long deadline = System.currentTimeMillis() + TIMEOUT_MS;

            while(channelizer.getBlockCount() < expectedBlocks && System.currentTimeMillis() < deadline)
            {
                Thread.sleep(10);
            }
        }
        finally
        {
            channelizer.stop();
        }

        assertEquals(expectedBlocks, channelizer.getBlockCount(), "Output block count");

        long hash = channelizer.getHash();
        System.out.printf("Channelizer output: sample rate %.0f, %d channels, %d IFFT threads, %d blocks, " +
            "hash 0x%016XL%n", sampleRate, channelCount, ifftThreads, channelizer.getBlockCount(), hash);
        assertEquals(expectedHash, hash, "Channelizer output hash for sample rate " + sampleRate);
    }

    /**
     * Channelizer that captures each dispatched block of channel results, in dispatch order, into a running hash.
     */
    private static class CapturingChannelizer extends ComplexPolyphaseChannelizerM2
    {
        private final List<float[]> mBlocks = new ArrayList<>();

        CapturingChannelizer(double sampleRate, int ifftThreads) throws Exception
        {
            super(sampleRate, TAPS_PER_CHANNEL, ifftThreads);
        }

        @Override
        protected void dispatch(List<float[]> channelResultsList)
        {
            synchronized(mBlocks)
            {
                for(float[] block: channelResultsList)
                {
                    assertTrue(block.length == getSubChannelCount(), "Block length");
                    mBlocks.add(block.clone());
                }
            }
        }

        int getBlockCount()
        {
            synchronized(mBlocks)
            {
                return mBlocks.size();
            }
        }

        long getHash()
        {
            long hash = 1125899906842597L;

            synchronized(mBlocks)
            {
                for(float[] block: mBlocks)
                {
                    for(float value: block)
                    {
                        hash = 31 * hash + Float.floatToRawIntBits(value);
                    }
                }
            }

            return hash;
        }
    }
}
