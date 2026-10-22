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

import java.util.ArrayList;
import java.util.List;
import java.util.Random;

/**
 * Generates complex baseband test signals: Gaussian noise plus FM voice carriers and unmodulated tones that can be
 * keyed on and off.
 */
public class SyntheticBaseband
{
    private final double mSampleRate;
    private final double mNoiseSigma;
    private final Random mRandom = new Random(1234);
    private final List<Emitter> mEmitters = new ArrayList<>();
    private long mSampleIndex = 0;

    /**
     * Constructs an instance
     * @param sampleRate in Hertz
     * @param noiseSigma standard deviation of the I and Q noise components
     */
    public SyntheticBaseband(double sampleRate, double noiseSigma)
    {
        mSampleRate = sampleRate;
        mNoiseSigma = noiseSigma;
    }

    /**
     * Adds a narrowband FM carrier modulated by voice-like audio and a 136.5 Hz CTCSS tone.
     * @param offset from the centre frequency in Hertz
     * @param amplitude of the carrier
     * @param deviation peak deviation in Hertz (2500 for 12.5 kHz channels, 5000 for 25 kHz channels)
     * @param onSeconds key up time
     * @param offSeconds key down time
     */
    public SyntheticBaseband addFmCarrier(double offset, double amplitude, double deviation, double onSeconds,
                                          double offSeconds)
    {
        mEmitters.add(new Emitter(offset, amplitude, deviation, onSeconds, offSeconds));
        return this;
    }

    /**
     * Adds an unmodulated carrier (e.g. a birdie or a keyed-up transmitter with no audio).
     */
    public SyntheticBaseband addTone(double offset, double amplitude, double onSeconds, double offSeconds)
    {
        mEmitters.add(new Emitter(offset, amplitude, 0, onSeconds, offSeconds));
        return this;
    }

    /**
     * Generates the next block of samples.
     * @return {i, q}
     */
    public float[][] next(int count)
    {
        float[] i = new float[count];
        float[] q = new float[count];

        for(int x = 0; x < count; x++)
        {
            double t = (mSampleIndex + x) / mSampleRate;
            double re = mRandom.nextGaussian() * mNoiseSigma;
            double im = mRandom.nextGaussian() * mNoiseSigma;

            for(Emitter emitter : mEmitters)
            {
                double phase = emitter.advance(t, mSampleRate);

                if(emitter.isOn(t))
                {
                    re += emitter.mAmplitude * Math.cos(phase);
                    im += emitter.mAmplitude * Math.sin(phase);
                }
            }

            i[x] = (float)re;
            q[x] = (float)im;
        }

        mSampleIndex += count;
        return new float[][]{i, q};
    }

    /**
     * Feeds the requested duration of samples to the processor in tuner-sized buffers.
     */
    public void feed(NBFMDiscoveryProcessor processor, double seconds)
    {
        int total = (int)Math.round(seconds * mSampleRate);
        int block = 16384;

        for(int x = 0; x < total; x += block)
        {
            float[][] samples = next(Math.min(block, total - x));
            processor.process(samples[0], samples[1]);
        }
    }

    private static class Emitter
    {
        private final double mOffset;
        private final double mAmplitude;
        private final double mDeviation;
        private final double mOn;
        private final double mOff;
        private double mPhase;

        Emitter(double offset, double amplitude, double deviation, double on, double off)
        {
            mOffset = offset;
            mAmplitude = amplitude;
            mDeviation = deviation;
            mOn = on;
            mOff = off;
        }

        boolean isOn(double t)
        {
            return t >= mOn && t < mOff;
        }

        /**
         * Advances the carrier phase by one sample and returns it.
         */
        double advance(double t, double sampleRate)
        {
            //Voice-like audio: three tones with a 4 Hz syllabic envelope, plus a CTCSS tone at 20% of deviation
            double syllabic = 0.5 + 0.5 * Math.sin(2 * Math.PI * 4 * t);
            double voice = syllabic * (0.4 * Math.sin(2 * Math.PI * 400 * t) + 0.25 * Math.sin(2 * Math.PI * 1100 * t) +
                    0.15 * Math.sin(2 * Math.PI * 2300 * t));
            double ctcss = 0.2 * Math.sin(2 * Math.PI * 136.5 * t);
            double frequency = mOffset + mDeviation * (voice + ctcss);
            mPhase = Math.IEEEremainder(mPhase + 2 * Math.PI * frequency / sampleRate, 2 * Math.PI);
            return mPhase;
        }
    }
}
