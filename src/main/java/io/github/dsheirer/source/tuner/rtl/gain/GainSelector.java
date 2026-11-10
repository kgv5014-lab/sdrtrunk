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

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * Picks the best gain from the measurements of a gain sweep, and the spectrum math the sweep uses.
 *
 * The best gain is where signals stand furthest above the noise.  Too little gain and the dongle's own converter
 * noise buries weak signals; more gain helps until the noise arriving from the antenna is what limits reception,
 * and beyond that it only brings the dongle closer to overload.  So the lowest gain that is as good as the best is
 * chosen, by one of two rules:
 *
 * Signal rule: when a target frequency carries a steady signal (a P25 control channel never stops), the signal above
 * noise is measured directly at every gain and the lowest gain within {@link #SNR_TOLERANCE_DB} of the best is chosen.
 *
 * Noise floor rule: when the targets are quiet most of the time (aircraft, fire ground), there is no signal to
 * measure.  The noise floor is flat at low gains, where the converter's own noise sets it, and starts to climb once
 * noise from the antenna and amplifier takes over.  The lowest gain that lifts the floor {@link #FLOOR_RISE_DB} above
 * the flat part is chosen: the converter then costs under 1 dB of sensitivity and more gain can't improve on that.
 *
 * Gains where the converter clips are never chosen.
 */
public final class GainSelector
{
    /** Fraction of clipped samples above which a gain counts as overloaded. */
    public static final double CLIP_LIMIT = 0.001;
    /** Sample magnitude treated as clipped.  The 8-bit converter spans about -0.996 to 0.996. */
    public static final float CLIP_LEVEL = 0.96f;
    /** A gain this close to the best signal above noise is as good as the best. */
    public static final double SNR_TOLERANCE_DB = 1.0;
    /** A target counts as a steady signal when it is at least this far above the noise ... */
    public static final double STEADY_SNR_DB = 10.0;
    /** ... at this share of the usable gains. */
    public static final double STEADY_SHARE = 0.6;
    /** Noise floor rise over the converter's own floor that makes the converter's noise negligible. */
    public static final double FLOOR_RISE_DB = 8.0;
    /** Share of the pass band, around the centre, used for the noise floor: the edges roll off. */
    private static final double FLOOR_SPAN = 0.8;
    /** Bins each side of the centre left out of the noise floor: the dongle's DC spike sits there. */
    private static final int DC_BINS = 4;

    public enum Rule
    {
        /** Chosen from the measured signal above noise of a steady target. */
        SIGNAL,
        /** Chosen from the rise of the noise floor. */
        NOISE_FLOOR,
        /** The noise floor never rose enough, so the highest clean gain was chosen. */
        HIGHEST_CLEAN,
        /** The converter clipped at every gain, so the lowest gain was chosen. */
        ALL_OVERLOADED
    }

    /**
     * Result of a sweep.
     * @param gain chosen, in tenths of a dB
     * @param rule that chose it
     * @param steadyTargets indexes of the targets that carried a steady signal
     * @param measurements of the sweep, lowest gain first
     */
    public record Choice(int gain, Rule rule, List<Integer> steadyTargets, List<GainMeasurement> measurements)
    {
    }

    private GainSelector()
    {
    }

    /**
     * Chooses the best gain.
     * @param measurements one for each gain of the sweep, at least one
     */
    public static Choice choose(List<GainMeasurement> measurements)
    {
        if(measurements.isEmpty())
        {
            throw new IllegalArgumentException("No gain measurements");
        }

        List<GainMeasurement> sorted = new ArrayList<>(measurements);
        sorted.sort((a, b) -> Integer.compare(a.gain(), b.gain()));
        List<GainMeasurement> clean = sorted.stream().filter(m -> !m.overloaded()).toList();

        if(clean.isEmpty())
        {
            return new Choice(sorted.getFirst().gain(), Rule.ALL_OVERLOADED, List.of(), sorted);
        }

        List<Integer> steady = steadyTargets(clean);

        if(!steady.isEmpty())
        {
            //The weakest steady target decides: every one of them has to be received
            double best = Double.NEGATIVE_INFINITY;

            for(GainMeasurement m : clean)
            {
                best = Math.max(best, weakest(m, steady));
            }

            for(GainMeasurement m : clean)
            {
                if(weakest(m, steady) >= best - SNR_TOLERANCE_DB)
                {
                    return new Choice(m.gain(), Rule.SIGNAL, steady, sorted);
                }
            }
        }

        double lowest = clean.stream().mapToDouble(GainMeasurement::floorDb).min().orElse(0);

        for(GainMeasurement m : clean)
        {
            if(m.floorDb() >= lowest + FLOOR_RISE_DB)
            {
                return new Choice(m.gain(), Rule.NOISE_FLOOR, steady, sorted);
            }
        }

        return new Choice(clean.getLast().gain(), Rule.HIGHEST_CLEAN, steady, sorted);
    }

    private static double weakest(GainMeasurement m, List<Integer> targets)
    {
        double weakest = Double.POSITIVE_INFINITY;

        for(int target : targets)
        {
            double snr = target < m.snrDb().length ? m.snrDb()[target] : Double.NaN;
            weakest = Math.min(weakest, Double.isNaN(snr) ? Double.NEGATIVE_INFINITY : snr);
        }

        return weakest;
    }

    /**
     * Targets that were well above the noise at most of the gains: signals that are always on the air.
     */
    private static List<Integer> steadyTargets(List<GainMeasurement> clean)
    {
        int targets = clean.stream().mapToInt(m -> m.snrDb().length).min().orElse(0);
        List<Integer> steady = new ArrayList<>();

        for(int target = 0; target < targets; target++)
        {
            int strong = 0;

            for(GainMeasurement m : clean)
            {
                if(m.snrDb()[target] >= STEADY_SNR_DB)
                {
                    strong++;
                }
            }

            if(strong >= Math.max(1, Math.ceil(clean.size() * STEADY_SHARE)))
            {
                steady.add(target);
            }
        }

        return steady;
    }

    /**
     * Noise floor of a power spectrum: the level that a fifth of the bins fall below, which signals in the pass band
     * don't move.
     * @param spectrum bin powers in dB, lowest frequency first with the centre frequency in the middle
     * @return floor in dB
     */
    public static double noiseFloor(float[] spectrum)
    {
        int centre = spectrum.length / 2;
        int half = (int)(spectrum.length * FLOOR_SPAN / 2);
        float[] bins = new float[2 * half];
        int count = 0;

        for(int bin = centre - half; bin < centre + half; bin++)
        {
            if(Math.abs(bin - centre) > DC_BINS)
            {
                bins[count++] = spectrum[bin];
            }
        }

        Arrays.sort(bins, 0, count);
        return bins[count / 5];
    }

    /**
     * Signal above the noise floor at a frequency: the average power across the channel against the floor.
     * @param spectrum bin powers in dB, lowest frequency first with the centre frequency in the middle
     * @param floorDb from {@link #noiseFloor(float[])}
     * @param offsetHz of the channel from the centre frequency
     * @param bandwidthHz of the channel
     * @param sampleRate of the spectrum
     * @return dB above the floor, or NaN when the channel is outside the spectrum
     */
    public static double snr(float[] spectrum, double floorDb, double offsetHz, double bandwidthHz, double sampleRate)
    {
        double binHz = sampleRate / spectrum.length;
        int centre = spectrum.length / 2 + (int)Math.round(offsetHz / binHz);
        int half = Math.max(1, (int)Math.round(bandwidthHz / binHz / 2));

        if(centre - half < 0 || centre + half >= spectrum.length)
        {
            return Double.NaN;
        }

        double power = 0;

        for(int bin = centre - half; bin <= centre + half; bin++)
        {
            power += Math.pow(10, spectrum[bin] / 10.0);
        }

        return 10 * Math.log10(power / (2 * half + 1)) - floorDb;
    }
}
