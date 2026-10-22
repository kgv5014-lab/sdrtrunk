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
import java.util.Collections;
import java.util.List;

/**
 * Settings for NBFM carrier discovery.  Defaults are tuned for VHF high band public safety (fire, police, EMS) on an
 * RTL-SDR at 2.4 MHz sample rate.
 */
public class DiscoveryConfiguration
{
    private int mFftSize = 8192;
    private int mAveragingTimeMs = 25;
    private double mDetectionThresholdDb = 10.0;
    private double mEdgeThresholdDb = 4.0;
    private int mNoiseFloorWindowHz = 200_000;
    private double mNoiseFloorPercentile = 0.2;
    private int mMergeGapHz = 2_500;
    private int mMaximumWidthHz = 20_000;
    private int mDcExclusionHalfWidthHz = 6_000;
    private double mUsableBandwidthFraction = 0.9;
    private int mAttackTimeMs = 250;
    private int mReleaseTimeMs = 750;
    private int mMatchToleranceHz = 3_000;
    private int mBirdieMaximumWidthHz = 2_000;
    private int mBirdieTimeMs = 120_000;
    private ChannelRaster mChannelRaster = null;
    private double mMaximumRasterErrorHz = 2_000;
    private List<FrequencyRange> mExcludedRanges = new ArrayList<>();

    /**
     * Constructs an instance with default settings
     */
    public DiscoveryConfiguration()
    {
    }

    /**
     * FFT size in bins.  At 2.4 MHz an 8192 point FFT gives ~293 Hz bin resolution, fine enough to measure carrier
     * width (12.5 vs 25 kHz channels, narrow birdies) through the window's main lobe.
     */
    public int getFftSize()
    {
        return mFftSize;
    }

    public void setFftSize(int fftSize)
    {
        if(fftSize < 64 || Integer.bitCount(fftSize) != 1)
        {
            throw new IllegalArgumentException("FFT size must be a power of 2 and at least 64: " + fftSize);
        }

        mFftSize = fftSize;
    }

    /**
     * Duration of samples averaged into each spectrum update.  Longer averaging lowers noise variance (finds weaker
     * carriers) but responds more slowly.
     */
    public int getAveragingTimeMs()
    {
        return mAveragingTimeMs;
    }

    public void setAveragingTimeMs(int averagingTimeMs)
    {
        mAveragingTimeMs = Math.max(1, averagingTimeMs);
    }

    /**
     * Signal level above the local noise floor (dB) required for a bin to start a carrier detection.
     */
    public double getDetectionThresholdDb()
    {
        return mDetectionThresholdDb;
    }

    public void setDetectionThresholdDb(double detectionThresholdDb)
    {
        mDetectionThresholdDb = detectionThresholdDb;
    }

    /**
     * Signal level above the local noise floor (dB) used to find the edges of a detected carrier.  This is lower than
     * the detection threshold so that the measured width and centre include the carrier's skirts.
     */
    public double getEdgeThresholdDb()
    {
        return mEdgeThresholdDb;
    }

    public void setEdgeThresholdDb(double edgeThresholdDb)
    {
        mEdgeThresholdDb = edgeThresholdDb;
    }

    /**
     * Width of the sliding window used to estimate the local noise floor.  Must be much wider than a channel so that
     * carriers don't raise their own floor, yet narrow enough to follow the tuner's passband ripple and roll-off.
     */
    public int getNoiseFloorWindowHz()
    {
        return mNoiseFloorWindowHz;
    }

    public void setNoiseFloorWindowHz(int noiseFloorWindowHz)
    {
        mNoiseFloorWindowHz = noiseFloorWindowHz;
    }

    /**
     * Percentile (0.0 - 1.0) of the bins within the noise floor window that is taken as the noise floor.
     */
    public double getNoiseFloorPercentile()
    {
        return mNoiseFloorPercentile;
    }

    public void setNoiseFloorPercentile(double noiseFloorPercentile)
    {
        mNoiseFloorPercentile = Math.clamp(noiseFloorPercentile, 0.0, 1.0);
    }

    /**
     * Maximum gap between two detected regions that are merged into one carrier.  FM spectra have nulls, especially
     * with a CTCSS tone and no voice, so one carrier can show as several narrow regions.
     */
    public int getMergeGapHz()
    {
        return mMergeGapHz;
    }

    public void setMergeGapHz(int mergeGapHz)
    {
        mMergeGapHz = mergeGapHz;
    }

    /**
     * Carriers wider than this (e.g. broadcast FM, wideband data) are rejected.
     */
    public int getMaximumWidthHz()
    {
        return mMaximumWidthHz;
    }

    public void setMaximumWidthHz(int maximumWidthHz)
    {
        mMaximumWidthHz = maximumWidthHz;
    }

    /**
     * Half-width of the region around the tuner's centre frequency that is ignored because of the DC spike.
     */
    public int getDcExclusionHalfWidthHz()
    {
        return mDcExclusionHalfWidthHz;
    }

    public void setDcExclusionHalfWidthHz(int dcExclusionHalfWidthHz)
    {
        mDcExclusionHalfWidthHz = dcExclusionHalfWidthHz;
    }

    /**
     * Fraction (0.0 - 1.0) of the sample rate that is usable.  Carriers in the band edges outside of this fraction are
     * ignored because of anti-aliasing filter roll-off.
     */
    public double getUsableBandwidthFraction()
    {
        return mUsableBandwidthFraction;
    }

    public void setUsableBandwidthFraction(double usableBandwidthFraction)
    {
        mUsableBandwidthFraction = Math.clamp(usableBandwidthFraction, 0.1, 1.0);
    }

    /**
     * Time a carrier must be continuously present before it is reported as active.
     */
    public int getAttackTimeMs()
    {
        return mAttackTimeMs;
    }

    public void setAttackTimeMs(int attackTimeMs)
    {
        mAttackTimeMs = attackTimeMs;
    }

    /**
     * Time a carrier must be absent before it is reported as ended.  Bridges short fades and drop-outs.
     */
    public int getReleaseTimeMs()
    {
        return mReleaseTimeMs;
    }

    public void setReleaseTimeMs(int releaseTimeMs)
    {
        mReleaseTimeMs = releaseTimeMs;
    }

    /**
     * Maximum difference between a detection and a tracked carrier for the detection to update that carrier.
     */
    public int getMatchToleranceHz()
    {
        return mMatchToleranceHz;
    }

    public void setMatchToleranceHz(int matchToleranceHz)
    {
        mMatchToleranceHz = matchToleranceHz;
    }

    /**
     * Carriers that never get wider than this and stay on longer than the birdie time are flagged as birdies (spurs
     * generated by the tuner or nearby electronics) and ignored from then on.
     */
    public int getBirdieMaximumWidthHz()
    {
        return mBirdieMaximumWidthHz;
    }

    public void setBirdieMaximumWidthHz(int birdieMaximumWidthHz)
    {
        mBirdieMaximumWidthHz = birdieMaximumWidthHz;
    }

    /**
     * Continuous on-time after which a narrow, always-on carrier is flagged as a birdie.
     */
    public int getBirdieTimeMs()
    {
        return mBirdieTimeMs;
    }

    public void setBirdieTimeMs(int birdieTimeMs)
    {
        mBirdieTimeMs = birdieTimeMs;
    }

    /**
     * Channel raster override.  When null, the raster is chosen from the band of each carrier.
     */
    public ChannelRaster getChannelRaster()
    {
        return mChannelRaster;
    }

    public void setChannelRaster(ChannelRaster channelRaster)
    {
        mChannelRaster = channelRaster;
    }

    /**
     * Raster to use for the frequency, either the override or the default for the band.
     */
    public ChannelRaster getChannelRaster(long frequency)
    {
        return mChannelRaster != null ? mChannelRaster : ChannelRaster.forFrequency(frequency);
    }

    /**
     * Carriers measured further than this from the nearest raster frequency are reported as off-raster.
     */
    public double getMaximumRasterErrorHz()
    {
        return mMaximumRasterErrorHz;
    }

    public void setMaximumRasterErrorHz(double maximumRasterErrorHz)
    {
        mMaximumRasterErrorHz = maximumRasterErrorHz;
    }

    /**
     * Frequency ranges to ignore, for example the frequencies of channels that are already configured.
     */
    public List<FrequencyRange> getExcludedRanges()
    {
        return Collections.unmodifiableList(mExcludedRanges);
    }

    public void setExcludedRanges(List<FrequencyRange> excludedRanges)
    {
        mExcludedRanges = new ArrayList<>(excludedRanges);
    }

    public void addExcludedRange(FrequencyRange range)
    {
        mExcludedRanges.add(range);
    }

    /**
     * Indicates if the frequency falls within any of the excluded ranges.
     */
    public boolean isExcluded(double frequency)
    {
        for(FrequencyRange range : mExcludedRanges)
        {
            if(range.contains(frequency))
            {
                return true;
            }
        }

        return false;
    }

    /**
     * Inclusive frequency range in Hertz.
     */
    public record FrequencyRange(long minimum, long maximum)
    {
        public FrequencyRange
        {
            if(maximum < minimum)
            {
                throw new IllegalArgumentException("Maximum frequency is below minimum: " + minimum + " > " + maximum);
            }
        }

        /**
         * Creates a range centred on the frequency.
         * @param frequency centre in Hertz
         * @param halfWidth either side of centre, in Hertz
         */
        public static FrequencyRange centeredOn(long frequency, long halfWidth)
        {
            return new FrequencyRange(frequency - halfWidth, frequency + halfWidth);
        }

        public boolean contains(double frequency)
        {
            return minimum <= frequency && frequency <= maximum;
        }
    }
}
