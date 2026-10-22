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
package io.github.dsheirer.module.decode.twotone;

import java.util.function.Consumer;
import org.jtransforms.fft.FloatFFT_1D;

/**
 * Detects two-tone sequential paging (Motorola Quick Call II style) in 8 kHz demodulated audio.
 *
 * A page is a steady tone A (typically 1 second) followed straight away by a different steady tone B (typically 3
 * seconds).  A single steady tone of 6 seconds or more (an all-call or "long tone") is reported on its own.
 *
 * Audio is cut into 64 ms Hann windowed blocks every 32 ms.  Each block is zero padded to a 4096 point FFT and the
 * strongest peak in the paging band is measured.  A block counts as a tone when that peak holds most of the band
 * energy (voice and noise spread their energy out).  Consecutive tone blocks at the same frequency form a segment,
 * and the frequency of a segment is the mean of its blocks, which is accurate to well under 1 Hz on clean audio.
 */
public class TwoToneDetector
{
    public static final int SAMPLE_RATE = 8000;
    private static final int BLOCK = 512;
    private static final int HOP = 256;
    private static final int FFT_SIZE = 4096;
    private static final double BIN_HZ = (double)SAMPLE_RATE / FFT_SIZE;
    //Paging tones run from about 280 Hz to 3 kHz.  The low edge stays clear of CTCSS tones (up to 254 Hz).
    private static final double BAND_LOW_HZ = 270.0;
    private static final double BAND_HIGH_HZ = 3200.0;
    //Half width of the Hann main lobe for a 512 sample block, in FFT bins
    private static final int LOBE_BINS = 2 * FFT_SIZE / BLOCK;
    private static final double MIN_PURITY = 0.5;
    private static final double MIN_BAND_POWER = 1e-7;
    //A block may differ from its segment by this fraction of the segment frequency (or MIN_SAME_HZ if larger)
    private static final double SAME_TONE_FRACTION = 0.012;
    private static final double MIN_SAME_HZ = 4.0;
    //Tone A and tone B must differ by at least this fraction
    private static final double DIFFERENT_TONE_FRACTION = 0.025;
    //Up to this many non-tone blocks (noise clicks) may interrupt a segment without ending it
    private static final int MAX_MISSED_BLOCKS = 2;
    private static final double MIN_TONE_SECONDS = 0.3;
    private static final double MAX_TONE_A_SECONDS = 5.0;
    private static final double MAX_GAP_SECONDS = 0.3;
    //Tone B is reported once it has lasted this long, without waiting for it to end
    private static final double REPORT_B_SECONDS = 1.0;
    private static final double LONG_TONE_SECONDS = 6.0;

    private final FloatFFT_1D mFFT = new FloatFFT_1D(FFT_SIZE);
    private final float[] mWindow = new float[BLOCK];
    private final float[] mBlock = new float[BLOCK];
    private final float[] mFFTBuffer = new float[FFT_SIZE];
    private final double[] mPower = new double[FFT_SIZE / 2];
    private final int mLowBin = (int)Math.ceil(BAND_LOW_HZ / BIN_HZ);
    private final int mHighBin = (int)Math.floor(BAND_HIGH_HZ / BIN_HZ);
    private final Consumer<Page> mListener;
    private int mBlockFill = 0;
    private long mBlockStartSample = 0;
    private Segment mCurrent;
    private Segment mPendingA;
    private int mMissedBlocks = 0;

    /**
     * Constructs an instance
     * @param listener to receive detected pages
     */
    public TwoToneDetector(Consumer<Page> listener)
    {
        mListener = listener;

        for(int x = 0; x < BLOCK; x++)
        {
            mWindow[x] = (float)(0.5 - 0.5 * Math.cos(2.0 * Math.PI * x / (BLOCK - 1)));
        }
    }

    /**
     * Processes a buffer of 8 kHz demodulated audio samples.
     */
    public void receive(float[] samples)
    {
        int offset = 0;

        while(offset < samples.length)
        {
            int count = Math.min(BLOCK - mBlockFill, samples.length - offset);
            System.arraycopy(samples, offset, mBlock, mBlockFill, count);
            mBlockFill += count;
            offset += count;

            if(mBlockFill == BLOCK)
            {
                processBlock();
                System.arraycopy(mBlock, HOP, mBlock, 0, BLOCK - HOP);
                mBlockFill = BLOCK - HOP;
                mBlockStartSample += HOP;
            }
        }
    }

    /**
     * Ends any tone in progress, as though silence followed.  Use when the audio stream stops.
     */
    public void flush()
    {
        closeSegment();
        mPendingA = null;
        mBlockFill = 0;
    }

    /**
     * Measures the current block and updates the tone segments.
     */
    private void processBlock()
    {
        double frequency = measureTone();

        if(Double.isNaN(frequency))
        {
            if(mCurrent != null && ++mMissedBlocks > MAX_MISSED_BLOCKS)
            {
                closeSegment();
            }
            return;
        }

        if(mCurrent != null && !mCurrent.isSameTone(frequency))
        {
            closeSegment();
        }

        if(mCurrent == null)
        {
            mCurrent = new Segment(mBlockStartSample);
        }

        mCurrent.add(frequency, mBlockStartSample);
        mMissedBlocks = 0;
        checkProgress(mCurrent);
    }

    /**
     * Frequency of the dominant tone in the current block, or NaN when the block is not a clean tone.
     */
    private double measureTone()
    {
        for(int x = 0; x < BLOCK; x++)
        {
            mFFTBuffer[x] = mBlock[x] * mWindow[x];
        }
        java.util.Arrays.fill(mFFTBuffer, BLOCK, FFT_SIZE, 0f);
        mFFT.realForward(mFFTBuffer);

        double bandPower = 0;
        int peakBin = -1;
        double peakPower = 0;

        for(int bin = mLowBin - LOBE_BINS; bin <= mHighBin + LOBE_BINS; bin++)
        {
            double re = mFFTBuffer[2 * bin];
            double im = mFFTBuffer[2 * bin + 1];
            mPower[bin] = re * re + im * im;

            if(bin >= mLowBin && bin <= mHighBin)
            {
                bandPower += mPower[bin];

                if(mPower[bin] > peakPower)
                {
                    peakPower = mPower[bin];
                    peakBin = bin;
                }
            }
        }

        //Zero padded FFT power sums are FFT_SIZE/BLOCK times the true block energy; scale so the floor is in sample units
        if(peakBin < 0 || bandPower * BLOCK / FFT_SIZE < MIN_BAND_POWER * BLOCK)
        {
            return Double.NaN;
        }

        double tonePower = 0;

        for(int bin = peakBin - LOBE_BINS; bin <= peakBin + LOBE_BINS; bin++)
        {
            tonePower += mPower[bin];
        }

        if(tonePower / bandPower < MIN_PURITY)
        {
            return Double.NaN;
        }

        //Parabolic interpolation on the log power of the peak and its neighbours
        double left = Math.log(mPower[peakBin - 1] + 1e-30);
        double centre = Math.log(mPower[peakBin] + 1e-30);
        double right = Math.log(mPower[peakBin + 1] + 1e-30);
        double denominator = left - 2 * centre + right;
        double delta = denominator != 0 ? 0.5 * (left - right) / denominator : 0;
        return (peakBin + delta) * BIN_HZ;
    }

    /**
     * Reports a page as soon as the segment in progress qualifies as tone B, or as a long tone.
     */
    private void checkProgress(Segment segment)
    {
        if(segment.mReported)
        {
            return;
        }

        if(pairsWith(segment) && segment.getSeconds() >= REPORT_B_SECONDS)
        {
            report(mPendingA, segment);
        }
        else if(segment.getSeconds() >= LONG_TONE_SECONDS)
        {
            report(null, segment);
        }
    }

    /**
     * Ends the segment in progress and decides whether it is tone B of a page or a candidate tone A.
     */
    private void closeSegment()
    {
        Segment segment = mCurrent;
        mCurrent = null;
        mMissedBlocks = 0;

        if(segment == null || segment.mReported)
        {
            return;
        }

        double seconds = segment.getSeconds();

        if(seconds < MIN_TONE_SECONDS)
        {
            return;
        }

        if(pairsWith(segment))
        {
            report(mPendingA, segment);
        }
        else if(seconds <= MAX_TONE_A_SECONDS)
        {
            mPendingA = segment;
        }
        else
        {
            mPendingA = null;
        }
    }

    /**
     * Indicates if the segment can be tone B following the pending tone A.
     */
    private boolean pairsWith(Segment segment)
    {
        if(mPendingA == null)
        {
            return false;
        }

        double gapSeconds = (segment.mFirstSample - (mPendingA.mLastSample + BLOCK)) / (double)SAMPLE_RATE;
        double a = mPendingA.getFrequency();
        return gapSeconds <= MAX_GAP_SECONDS && Math.abs(segment.getFrequency() - a) >= a * DIFFERENT_TONE_FRACTION;
    }

    private void report(Segment toneA, Segment toneB)
    {
        toneB.mReported = true;
        mPendingA = null;

        if(mListener != null)
        {
            if(toneA != null)
            {
                mListener.accept(new Page(toneA.getFrequency(), toneB.getFrequency(), toneA.getSeconds(),
                        toneB.mFirstSample));
            }
            else
            {
                mListener.accept(new Page(toneB.getFrequency(), Double.NaN, toneB.getSeconds(), toneB.mFirstSample));
            }
        }
    }

    /**
     * Run of consecutive blocks holding the same steady tone.
     */
    private static class Segment
    {
        private final long mFirstSample;
        private long mLastSample;
        private double mFrequencySum;
        private int mBlocks;
        private boolean mReported;

        Segment(long firstSample)
        {
            mFirstSample = firstSample;
            mLastSample = firstSample;
        }

        void add(double frequency, long blockStart)
        {
            mFrequencySum += frequency;
            mBlocks++;
            mLastSample = blockStart;
        }

        double getFrequency()
        {
            return mFrequencySum / mBlocks;
        }

        boolean isSameTone(double frequency)
        {
            double mean = getFrequency();
            return Math.abs(frequency - mean) <= Math.max(MIN_SAME_HZ, mean * SAME_TONE_FRACTION);
        }

        /**
         * Approximate duration: from the start of the first block to the start of the last block, plus one hop.
         */
        double getSeconds()
        {
            return (mLastSample - mFirstSample + HOP) / (double)SAMPLE_RATE;
        }
    }

    /**
     * A detected page.
     * @param toneA frequency of the first tone in Hz
     * @param toneB frequency of the second tone in Hz, or NaN for a single long tone
     * @param toneASeconds measured length of the first tone
     * @param sampleIndex sample count at the start of tone B (or of the long tone)
     */
    public record Page(double toneA, double toneB, double toneASeconds, long sampleIndex)
    {
        public boolean isLongTone()
        {
            return Double.isNaN(toneB);
        }
    }
}
