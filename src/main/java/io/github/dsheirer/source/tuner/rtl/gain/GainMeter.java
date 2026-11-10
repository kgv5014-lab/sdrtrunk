/*
 * *****************************************************************************
 * Copyright (C) 2014-2022 Dennis Sheirer
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
package io.github.dsheirer.source.tuner.rtl.gain;

import io.github.dsheirer.buffer.INativeBuffer;
import io.github.dsheirer.module.decode.nbfm.discovery.AveragedPowerSpectrum;
import io.github.dsheirer.sample.Listener;
import io.github.dsheirer.sample.complex.ComplexSamples;
import java.util.Iterator;

/**
 * Measures the dongle's sample stream during a gain sweep: the averaged power spectrum of everything the dongle hears
 * and how often the analog to digital converter clips.
 */
public class GainMeter implements Listener<INativeBuffer>
{
    public static final int FFT_SIZE = 4096;
    private final AveragedPowerSpectrum mSpectrum = new AveragedPowerSpectrum(FFT_SIZE, 1);
    private final double[] mPower = new double[FFT_SIZE];
    private int mFrames;
    private long mSamples;
    private long mClipped;

    public GainMeter()
    {
        mSpectrum.setListener(this::frame);
    }

    //Called from process(), inside the lock held by receive()
    private void frame(float[] spectrum)
    {
        for(int bin = 0; bin < FFT_SIZE; bin++)
        {
            mPower[bin] += Math.pow(10, spectrum[bin] / 10.0);
        }

        mFrames++;
    }

    @Override
    public synchronized void receive(INativeBuffer buffer)
    {
        Iterator<ComplexSamples> iterator = buffer.iterator();

        while(iterator.hasNext())
        {
            process(iterator.next());
        }
    }

    synchronized void process(ComplexSamples samples)
    {
        float[] i = samples.i();
        float[] q = samples.q();

        for(int x = 0; x < i.length; x++)
        {
            if(Math.abs(i[x]) >= GainSelector.CLIP_LEVEL || Math.abs(q[x]) >= GainSelector.CLIP_LEVEL)
            {
                mClipped++;
            }
        }

        mSamples += i.length;
        mSpectrum.process(i, q);
    }

    /**
     * Starts a new measurement, dropping what was heard so far.
     */
    public synchronized void reset()
    {
        mSpectrum.reset();
        java.util.Arrays.fill(mPower, 0);
        mFrames = 0;
        mSamples = 0;
        mClipped = 0;
    }

    /**
     * Number of spectrum frames averaged since the reset.
     */
    public synchronized int frames()
    {
        return mFrames;
    }

    /**
     * Averaged power spectrum since the reset, in dB, lowest frequency first, or null when nothing was heard.
     */
    public synchronized float[] spectrum()
    {
        if(mFrames == 0)
        {
            return null;
        }

        float[] spectrum = new float[FFT_SIZE];

        for(int bin = 0; bin < FFT_SIZE; bin++)
        {
            spectrum[bin] = (float)(10 * Math.log10(mPower[bin] / mFrames + 1e-20));
        }

        return spectrum;
    }

    /**
     * Fraction of the samples since the reset that were at the converter's limit.
     */
    public synchronized double clipFraction()
    {
        return mSamples == 0 ? 0 : (double)mClipped / mSamples;
    }
}
