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
import io.github.dsheirer.sample.Listener;
import io.github.dsheirer.source.SourceException;
import io.github.dsheirer.source.tuner.channel.TunerChannel;
import java.util.ArrayList;
import java.util.List;
import java.util.function.BooleanSupplier;

/**
 * Finds the best gain for one dongle on the frequencies it is listening to right now.
 *
 * Steps the dongle through every gain it supports while it keeps running, measures the noise floor, the signal above
 * noise on each channel the dongle is decoding and the converter overload at each step, and has
 * {@link GainSelector} pick the best.  The dongle is left at its original gain: the caller applies the choice.
 * Reception is worse than normal while the sweep passes through the gains that are too low or too high, for the
 * second or two each one takes.
 */
public class GainOptimizer
{
    /** Wait after a gain change before measuring, for the tuner to settle and old samples to drain. */
    static final long SETTLE_MS = 400;
    /** Measuring time at each gain. */
    static final long MEASURE_MS = 1000;
    /** A step is measured again when the dongle changed frequency during it, up to this many times. */
    private static final int ATTEMPTS = 3;

    /**
     * The dongle, as the sweep needs it.
     */
    public interface Dongle
    {
        /** Supported gains in tenths of a dB. */
        int[] gains();
        /** Switches to manual gain at the value. */
        void setGain(int gain) throws SourceException;
        /** Centre frequency in Hz. */
        long frequency();
        /** Sample rate in Hz. */
        double sampleRate();
        /** Channels being decoded on the dongle. */
        List<TunerChannel> channels();
        void addListener(Listener<INativeBuffer> listener);
        void removeListener(Listener<INativeBuffer> listener);
    }

    /**
     * Told how far the sweep is.
     */
    public interface Progress
    {
        void step(int done, int total, int gain);
    }

    private final Dongle mDongle;
    private final long mSettleMs;
    private final long mMeasureMs;

    public GainOptimizer(Dongle dongle)
    {
        this(dongle, SETTLE_MS, MEASURE_MS);
    }

    GainOptimizer(Dongle dongle, long settleMs, long measureMs)
    {
        mDongle = dongle;
        mSettleMs = settleMs;
        mMeasureMs = measureMs;
    }

    /**
     * Seconds a sweep takes, for telling the user beforehand.
     */
    public int seconds()
    {
        return (int)Math.ceil(mDongle.gains().length * (mSettleMs + mMeasureMs) / 1000.0);
    }

    /**
     * Channels the sweep found on the dongle, in the order of {@link GainMeasurement#snrDb()}.
     */
    public record Result(GainSelector.Choice choice, List<TunerChannel> targets)
    {
    }

    /**
     * Runs the sweep.  Blocks for {@link #seconds()}, so not for the event thread.
     * @param originalGain to put back afterwards, in tenths of a dB
     * @param progress told after each gain, or null
     * @param cancelled checked between gains
     * @return the choice, or null when cancelled
     * @throws SourceException when the dongle can't be set or sends no samples
     */
    public Result run(int originalGain, Progress progress, BooleanSupplier cancelled)
            throws SourceException, InterruptedException
    {
        int[] gains = mDongle.gains();

        if(gains == null || gains.length < 2)
        {
            throw new SourceException("This tuner has no adjustable gain");
        }

        List<TunerChannel> targets = List.copyOf(mDongle.channels());
        List<GainMeasurement> measurements = new ArrayList<>();
        GainMeter meter = new GainMeter();
        mDongle.addListener(meter);

        try
        {
            for(int step = 0; step < gains.length; step++)
            {
                if(cancelled != null && cancelled.getAsBoolean())
                {
                    return null;
                }

                mDongle.setGain(gains[step]);
                measurements.add(measure(meter, gains[step], targets));

                if(progress != null)
                {
                    progress.step(step + 1, gains.length, gains[step]);
                }
            }
        }
        finally
        {
            mDongle.removeListener(meter);
            mDongle.setGain(originalGain);
        }

        return new Result(GainSelector.choose(measurements), targets);
    }

    private GainMeasurement measure(GainMeter meter, int gain, List<TunerChannel> targets)
            throws SourceException, InterruptedException
    {
        float[] spectrum = null;
        long frequency = 0;
        boolean steady = false;

        //A scanning channel moves the dongle between frequencies: measure again if it moved during the step, since
        //the spectrum would be a mix of two places.  After the last attempt the noise floor is still good enough.
        for(int attempt = 0; attempt < ATTEMPTS && !steady; attempt++)
        {
            Thread.sleep(mSettleMs);
            frequency = mDongle.frequency();
            meter.reset();
            Thread.sleep(mMeasureMs);
            spectrum = meter.spectrum();
            steady = mDongle.frequency() == frequency;
        }

        if(spectrum == null)
        {
            throw new SourceException("The tuner isn't sending samples");
        }

        double floor = GainSelector.noiseFloor(spectrum);
        double[] snr = new double[targets.size()];

        for(int target = 0; target < snr.length; target++)
        {
            TunerChannel channel = targets.get(target);
            snr[target] = steady ? GainSelector.snr(spectrum, floor, channel.getFrequency() - frequency,
                    channel.getBandwidth(), mDongle.sampleRate()) : Double.NaN;
        }

        return new GainMeasurement(gain, floor, meter.clipFraction(), snr);
    }
}
