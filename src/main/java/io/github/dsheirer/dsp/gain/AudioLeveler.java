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

package io.github.dsheirer.dsp.gain;

/**
 * Loudness based audio leveler for 8 kHz mono audio, followed by a peak limiter.
 *
 * Unlike the peak based gain controls, this measures the average (RMS) level of the audio and turns the gain up or
 * down so that quiet and loud channels end up at about the same loudness.  Steady tones, such as two-tone paging,
 * have a high average level for their peak, so they get turned down to about the same loudness as speech instead of
 * being much louder than it.
 *
 * Gain comes down quickly when the audio gets louder and goes up slowly when it gets quieter.  Gain is held while the
 * audio is below the gate level so that pauses between words and background hiss are not boosted.  The limiter
 * ensures that no sample exceeds the ceiling, so raising the gain never causes clipping.
 *
 * The gain carries over from one call to the next on the same channel, since a channel tends to have a consistent
 * level, so later calls start at about the right level.
 */
public class AudioLeveler
{
    private static final float SAMPLE_RATE = 8000.0f;
    public static final float DEFAULT_TARGET_DB = -20.0f;
    private static final float MAX_GAIN_DB = 24.0f;
    private static final float MIN_GAIN_DB = -24.0f;
    private static final float GATE_DB = -50.0f;
    private static final float CEILING = 0.89f; // -1 dBFS
    //Input is clamped to this so its square can't overflow
    private static final float MAX_INPUT = 100.0f;

    //Level detector time constants
    private static final float DETECTOR_ATTACK_SECONDS = 0.010f;
    private static final float DETECTOR_RELEASE_SECONDS = 0.300f;
    //Gain moves down quickly and up slowly
    private static final float GAIN_DOWN_SECONDS = 0.020f;
    private static final float GAIN_UP_DB_PER_SECOND = 12.0f;
    //Limiter recovers over this time after a peak
    private static final float LIMITER_RELEASE_SECONDS = 0.050f;

    private final float mDetectorAttack = coefficient(DETECTOR_ATTACK_SECONDS);
    private final float mDetectorRelease = coefficient(DETECTOR_RELEASE_SECONDS);
    private final float mGainDown = coefficient(GAIN_DOWN_SECONDS);
    private final float mGainUpPerSample = (float)Math.pow(10.0, GAIN_UP_DB_PER_SECOND / 20.0 / SAMPLE_RATE);
    private final float mLimiterRelease = coefficient(LIMITER_RELEASE_SECONDS);
    private final float mMaxGain = dbToLinear(MAX_GAIN_DB);
    private final float mMinGain = dbToLinear(MIN_GAIN_DB);
    private final float mGatePower = square(dbToLinear(GATE_DB));

    private float mTargetRms;
    private float mPower = 0.0f;
    private float mGain = 1.0f;
    private float mLimiterEnvelope = 0.0f;
    private float mDcEstimate = 0.0f;

    /**
     * Constructs an instance
     * @param targetDb average (RMS) level to aim for, in dB relative to full scale, e.g. -20.0
     */
    public AudioLeveler(float targetDb)
    {
        setTargetDb(targetDb);
    }

    /**
     * Constructs an instance with the default target level.
     */
    public AudioLeveler()
    {
        this(DEFAULT_TARGET_DB);
    }

    /**
     * Sets the average (RMS) level to aim for, in dB relative to full scale.
     */
    public void setTargetDb(float targetDb)
    {
        mTargetRms = dbToLinear(targetDb);
    }

    /**
     * Current gain in dB, for testing and display.
     */
    public float getGainDb()
    {
        return (float)(20.0 * Math.log10(mGain));
    }

    /**
     * Resets the level detector and limiter at the start of a call.  The gain is kept from the previous call.
     */
    public void resetDetector()
    {
        mPower = 0.0f;
        mLimiterEnvelope = 0.0f;
    }

    /**
     * Returns everything, including the gain, to the starting state.
     */
    private void resetState()
    {
        mPower = 0.0f;
        mGain = 1.0f;
        mLimiterEnvelope = 0.0f;
        mDcEstimate = 0.0f;
    }

    /**
     * Levels a buffer of audio samples.
     * @param samples to level, not modified
     * @return new buffer of leveled samples
     */
    public float[] process(float[] samples)
    {
        float[] processed = new float[samples.length];

        for(int x = 0; x < samples.length; x++)
        {
            //Decoders occasionally produce a NaN or infinite sample.  Drop it here, before it reaches the
            //filter state below, or it would stay there and silence every call after it.
            float input = samples[x];

            if(!Float.isFinite(input))
            {
                input = 0.0f;
            }

            input = Math.max(-MAX_INPUT, Math.min(MAX_INPUT, input));

            //Slowly track and remove any DC offset so it doesn't count as loudness
            mDcEstimate += (input - mDcEstimate) * 0.0005f;
            float sample = input - mDcEstimate;

            float power = sample * sample;
            mPower += (power - mPower) * (power > mPower ? mDetectorAttack : mDetectorRelease);

            //Hold the gain steady during pauses and background noise
            if(mPower > mGatePower)
            {
                float desired = mTargetRms / (float)Math.sqrt(mPower);
                desired = Math.max(mMinGain, Math.min(mMaxGain, desired));

                if(desired < mGain)
                {
                    mGain += (desired - mGain) * mGainDown;
                }
                else
                {
                    mGain = Math.min(desired, mGain * mGainUpPerSample);
                }
            }

            float leveled = sample * mGain;

            //Peak limiter: instant attack, so the output never exceeds the ceiling
            float magnitude = Math.abs(leveled);

            if(magnitude > mLimiterEnvelope)
            {
                mLimiterEnvelope = magnitude;
            }
            else
            {
                mLimiterEnvelope += (magnitude - mLimiterEnvelope) * mLimiterRelease;
            }

            if(mLimiterEnvelope > CEILING)
            {
                leveled *= CEILING / mLimiterEnvelope;
            }

            if(!Float.isFinite(leveled))
            {
                //Should never happen with the input checks above, but never let the state get stuck
                resetState();
                leveled = 0.0f;
            }

            processed[x] = leveled;
        }

        return processed;
    }

    /**
     * One pole smoothing coefficient that reaches about 63% of a step change over the time constant.
     */
    private static float coefficient(float seconds)
    {
        return (float)(1.0 - Math.exp(-1.0 / (seconds * SAMPLE_RATE)));
    }

    private static float dbToLinear(float db)
    {
        return (float)Math.pow(10.0, db / 20.0);
    }

    private static float square(float value)
    {
        return value * value;
    }
}
