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
package io.github.dsheirer.module.decode.nbfm.discovery;

import io.github.dsheirer.dsp.window.WindowFactory;
import io.github.dsheirer.dsp.window.WindowType;
import io.github.dsheirer.sample.Listener;
import java.util.Arrays;
import org.jtransforms.fft.FloatFFT_1D;

/**
 * Welch-style averaged power spectrum estimator for complex baseband samples.
 *
 * Samples are cut into non-overlapping frames of FFT size, windowed, transformed, and the bin powers of a fixed number
 * of frames are averaged.  Each averaged spectrum is delivered in decibels, re-ordered so that index 0 is the lowest
 * frequency (-sampleRate/2) and index fftSize/2 is the centre (DC) bin.
 *
 * Uses a Blackman-Harris 4 window: a narrower main lobe than the 7-term window (better carrier width measurement) with
 * ~92 dB sidelobe suppression, which is well beyond the dynamic range of an 8-bit tuner.
 */
public class AveragedPowerSpectrum
{
    private final int mFftSize;
    private final int mFramesToAverage;
    private final float[] mWindow;
    private final FloatFFT_1D mFFT;
    private final float[] mFrame;
    private final double[] mAccumulator;
    private int mFrameSampleCount = 0;
    private int mFrameCount = 0;
    private Listener<float[]> mListener;

    /**
     * Constructs an instance
     * @param fftSize power of 2 FFT length
     * @param framesToAverage number of FFT frames averaged into each delivered spectrum
     */
    public AveragedPowerSpectrum(int fftSize, int framesToAverage)
    {
        mFftSize = fftSize;
        mFramesToAverage = Math.max(1, framesToAverage);
        mWindow = WindowFactory.getWindow(WindowType.BLACKMAN_HARRIS_4, fftSize);
        mFFT = new FloatFFT_1D(fftSize);
        mFrame = new float[fftSize * 2];
        mAccumulator = new double[fftSize];
    }

    /**
     * Number of frames that gives the requested averaging time at the sample rate.
     */
    public static int framesFor(int fftSize, double sampleRate, int averagingTimeMs)
    {
        return Math.max(1, (int)Math.round(sampleRate * averagingTimeMs / 1000.0 / fftSize));
    }

    public int getFftSize()
    {
        return mFftSize;
    }

    /**
     * Registers the listener to receive each averaged spectrum (dB).  The array is newly allocated for each delivery.
     */
    public void setListener(Listener<float[]> listener)
    {
        mListener = listener;
    }

    /**
     * Discards any partially accumulated frame or average, for example after a tuner frequency change.
     */
    public void reset()
    {
        mFrameSampleCount = 0;
        mFrameCount = 0;
        Arrays.fill(mAccumulator, 0.0);
    }

    /**
     * Processes complex samples.
     * @param i inphase samples
     * @param q quadrature samples, same length as i
     */
    public void process(float[] i, float[] q)
    {
        int offset = 0;

        while(offset < i.length)
        {
            int count = Math.min(i.length - offset, mFftSize - mFrameSampleCount);

            for(int x = 0; x < count; x++)
            {
                int index = 2 * (mFrameSampleCount + x);
                mFrame[index] = i[offset + x];
                mFrame[index + 1] = q[offset + x];
            }

            offset += count;
            mFrameSampleCount += count;

            if(mFrameSampleCount == mFftSize)
            {
                processFrame();
                mFrameSampleCount = 0;
            }
        }
    }

    private void processFrame()
    {
        for(int x = 0; x < mFftSize; x++)
        {
            mFrame[2 * x] *= mWindow[x];
            mFrame[2 * x + 1] *= mWindow[x];
        }

        mFFT.complexForward(mFrame);

        //Accumulate bin power, swapping halves so that the output runs from -fs/2 to +fs/2
        int half = mFftSize / 2;

        for(int bin = 0; bin < mFftSize; bin++)
        {
            float re = mFrame[2 * bin];
            float im = mFrame[2 * bin + 1];
            int shifted = bin < half ? bin + half : bin - half;
            mAccumulator[shifted] += (double)re * re + (double)im * im;
        }

        mFrameCount++;

        if(mFrameCount == mFramesToAverage)
        {
            float[] spectrum = new float[mFftSize];

            for(int bin = 0; bin < mFftSize; bin++)
            {
                double power = mAccumulator[bin] / mFramesToAverage;
                spectrum[bin] = (float)(10.0 * Math.log10(power + 1e-20));
            }

            Arrays.fill(mAccumulator, 0.0);
            mFrameCount = 0;

            if(mListener != null)
            {
                mListener.receive(spectrum);
            }
        }
    }
}
