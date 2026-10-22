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
import java.util.Arrays;
import java.util.List;

/**
 * Finds carriers in an averaged power spectrum.
 *
 * Steps:
 * 1. Estimate the local noise floor for each bin with a sliding low percentile, which follows tuner passband ripple and
 *    roll-off but ignores the carriers themselves.
 * 2. Seed a detection at each bin that exceeds the floor by the detection threshold, and grow it outward while bins
 *    exceed the floor by the (lower) edge threshold.
 * 3. Merge detections separated by small gaps, since an FM carrier's spectrum can have nulls.
 * 4. Measure each carrier's centre (power-weighted over bins within 20 dB of the peak), width, peak level and SNR.
 *
 * Bins in the tuner's DC spike region or outside the usable bandwidth never seed or extend a detection.
 */
public class CarrierDetector
{
    /**
     * Width and centre are measured over the bins within this many dB of the carrier's peak
     */
    private static final double OCCUPIED_WIDTH_DB = 20.0;

    private final DiscoveryConfiguration mConfiguration;

    /**
     * Constructs an instance
     * @param configuration settings
     */
    public CarrierDetector(DiscoveryConfiguration configuration)
    {
        mConfiguration = configuration;
    }

    /**
     * Detects carriers in the spectrum.
     * @param spectrum in dB, index 0 = centre - sampleRate/2, index length/2 = centre frequency
     * @param centerFrequency of the tuner in Hertz
     * @param sampleRate of the tuner in Hertz
     * @return detected carriers, ordered by frequency
     */
    public List<SpectralPeak> detect(float[] spectrum, long centerFrequency, double sampleRate)
    {
        int size = spectrum.length;
        double binHz = sampleRate / size;
        int windowBins = Math.max(8, (int)Math.round(mConfiguration.getNoiseFloorWindowHz() / binHz));
        float[] floor = estimateNoiseFloor(spectrum, windowBins, mConfiguration.getNoiseFloorPercentile());
        boolean[] usable = getUsableBins(size, binHz, centerFrequency);

        //Find regions: seeded above the detection threshold and grown while above the edge threshold
        List<int[]> regions = new ArrayList<>();
        int bin = 0;

        while(bin < size)
        {
            if(usable[bin] && spectrum[bin] - floor[bin] >= mConfiguration.getDetectionThresholdDb())
            {
                int low = bin;
                int high = bin;

                while(low > 0 && usable[low - 1] &&
                        spectrum[low - 1] - floor[low - 1] >= mConfiguration.getEdgeThresholdDb())
                {
                    low--;
                }

                while(high < size - 1 && usable[high + 1] &&
                        spectrum[high + 1] - floor[high + 1] >= mConfiguration.getEdgeThresholdDb())
                {
                    high++;
                }

                //If this region overlaps the previous one (grown back over it) extend the previous one instead
                if(!regions.isEmpty() && low <= regions.getLast()[1])
                {
                    regions.getLast()[1] = Math.max(regions.getLast()[1], high);
                }
                else
                {
                    regions.add(new int[]{low, high});
                }

                bin = high + 1;
            }
            else
            {
                bin++;
            }
        }

        //Merge regions separated by small gaps
        int mergeGapBins = (int)Math.floor(mConfiguration.getMergeGapHz() / binHz);
        List<int[]> merged = new ArrayList<>();

        for(int[] region : regions)
        {
            if(!merged.isEmpty() && region[0] - merged.getLast()[1] - 1 <= mergeGapBins &&
                    allUsable(usable, merged.getLast()[1], region[0]))
            {
                merged.getLast()[1] = region[1];
            }
            else
            {
                merged.add(region);
            }
        }

        List<SpectralPeak> peaks = new ArrayList<>();

        for(int[] region : merged)
        {
            SpectralPeak peak = measure(spectrum, floor, region[0], region[1], centerFrequency, binHz);

            if((region[1] - region[0] + 1) * binHz <= mConfiguration.getMaximumWidthHz() &&
                    !mConfiguration.isExcluded(peak.frequency()))
            {
                peaks.add(peak);
            }
        }

        return peaks;
    }

    /**
     * Measures centre, width, peak and SNR of the carrier within the region.
     */
    private static SpectralPeak measure(float[] spectrum, float[] floor, int low, int high, long centerFrequency,
                                        double binHz)
    {
        int peakBin = low;

        for(int x = low; x <= high; x++)
        {
            if(spectrum[x] > spectrum[peakBin])
            {
                peakBin = x;
            }
        }

        double peakDb = spectrum[peakBin];
        double cutoff = peakDb - OCCUPIED_WIDTH_DB;

        //Occupied edges: outermost bins within the region that are within 20 dB of the peak
        int left = low;
        while(left < peakBin && spectrum[left] < cutoff)
        {
            left++;
        }

        int right = high;
        while(right > peakBin && spectrum[right] < cutoff)
        {
            right--;
        }

        //Centre of mass of the power above the noise floor across the occupied bins
        double weightSum = 0.0;
        double weightedBin = 0.0;

        for(int x = left; x <= right; x++)
        {
            double excess = Math.pow(10.0, spectrum[x] / 10.0) - Math.pow(10.0, floor[x] / 10.0);

            if(excess > 0)
            {
                weightSum += excess;
                weightedBin += excess * x;
            }
        }

        double centreBin = weightSum > 0 ? weightedBin / weightSum : peakBin;
        double frequency = centerFrequency + (centreBin - spectrum.length / 2.0) * binHz;
        double width = (right - left + 1) * binHz;

        return new SpectralPeak(frequency, width, peakDb, peakDb - floor[peakBin]);
    }

    /**
     * Flags the bins that may be part of a detection: within the usable bandwidth and outside of the DC spike region.
     */
    private boolean[] getUsableBins(int size, double binHz, long centerFrequency)
    {
        boolean[] usable = new boolean[size];
        double usableHalfWidth = binHz * size / 2.0 * mConfiguration.getUsableBandwidthFraction();
        int dcHalfWidth = mConfiguration.getDcExclusionHalfWidthHz();

        for(int x = 0; x < size; x++)
        {
            double offset = Math.abs((x - size / 2) * binHz);
            usable[x] = offset <= usableHalfWidth && offset >= dcHalfWidth;
        }

        return usable;
    }

    private static boolean allUsable(boolean[] usable, int from, int to)
    {
        for(int x = from; x <= to; x++)
        {
            if(!usable[x])
            {
                return false;
            }
        }

        return true;
    }

    /**
     * Estimates the noise floor for each bin as a low percentile of the bins in a sliding window.  The percentile is
     * calculated at block centres spaced a quarter window apart and linearly interpolated between them.
     *
     * @param spectrum in dB
     * @param windowBins width of the sliding window in bins
     * @param percentile 0.0 - 1.0
     * @return noise floor in dB for each bin
     */
    public static float[] estimateNoiseFloor(float[] spectrum, int windowBins, double percentile)
    {
        int size = spectrum.length;
        int window = Math.min(windowBins, size);
        int step = Math.max(1, window / 4);
        int points = (size + step - 1) / step;
        int[] centres = new int[points];
        float[] values = new float[points];
        float[] scratch = new float[window];

        for(int p = 0; p < points; p++)
        {
            int centre = Math.min(size - 1, p * step + step / 2);
            int start = Math.clamp(centre - window / 2, 0, size - window);
            System.arraycopy(spectrum, start, scratch, 0, window);
            Arrays.sort(scratch);
            centres[p] = centre;
            values[p] = scratch[(int)Math.min(window - 1, Math.floor(percentile * (window - 1)))];
        }

        float[] floor = new float[size];
        int p = 0;

        for(int x = 0; x < size; x++)
        {
            while(p < points - 1 && centres[p + 1] <= x)
            {
                p++;
            }

            if(x <= centres[0])
            {
                floor[x] = values[0];
            }
            else if(p >= points - 1)
            {
                floor[x] = values[points - 1];
            }
            else
            {
                float fraction = (float)(x - centres[p]) / (centres[p + 1] - centres[p]);
                floor[x] = values[p] + fraction * (values[p + 1] - values[p]);
            }
        }

        return floor;
    }
}
