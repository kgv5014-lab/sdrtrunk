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

/**
 * A carrier that is being tracked across successive spectrum updates for the duration of one transmission.
 */
public class TrackedCarrier
{
    private final long mChannelFrequency;
    private final boolean mOffRaster;
    private final long mFirstSeen;
    private long mLastSeen;
    private boolean mActive = false;
    private double mMeasuredFrequency;
    private double mPeakSnrDb;
    private double mMaximumWidthHz;
    private int mDetectionCount = 0;

    /**
     * Constructs an instance
     * @param channelFrequency frequency snapped to the channel raster (or the rounded measured frequency if off-raster)
     * @param offRaster true if the measured frequency was too far from any raster frequency
     * @param peak first detection
     * @param timestamp of the first detection in milliseconds
     */
    public TrackedCarrier(long channelFrequency, boolean offRaster, SpectralPeak peak, long timestamp)
    {
        mChannelFrequency = channelFrequency;
        mOffRaster = offRaster;
        mFirstSeen = timestamp;
        mMeasuredFrequency = peak.frequency();
        update(peak, timestamp);
    }

    /**
     * Updates the carrier with a new detection.
     */
    void update(SpectralPeak peak, long timestamp)
    {
        mLastSeen = timestamp;
        mDetectionCount++;
        //Smooth the measured frequency, since FM modulation moves the instantaneous centre of mass around
        mMeasuredFrequency += (peak.frequency() - mMeasuredFrequency) / Math.min(mDetectionCount, 16);
        mPeakSnrDb = Math.max(mPeakSnrDb, peak.snrDb());
        mMaximumWidthHz = Math.max(mMaximumWidthHz, peak.widthHz());
    }

    void setActive()
    {
        mActive = true;
    }

    /**
     * Channel frequency in Hertz, snapped to the channel raster.
     */
    public long getChannelFrequency()
    {
        return mChannelFrequency;
    }

    /**
     * Indicates the carrier's measured frequency was too far from the channel raster to snap.
     */
    public boolean isOffRaster()
    {
        return mOffRaster;
    }

    /**
     * Average measured centre frequency in Hertz.
     */
    public double getMeasuredFrequency()
    {
        return mMeasuredFrequency;
    }

    /**
     * Measured offset of the carrier from its channel frequency in Hertz.  A consistent offset across many carriers
     * indicates a tuner PPM error.
     */
    public double getFrequencyError()
    {
        return mMeasuredFrequency - mChannelFrequency;
    }

    public long getFirstSeen()
    {
        return mFirstSeen;
    }

    public long getLastSeen()
    {
        return mLastSeen;
    }

    /**
     * Duration from the first to the last detection in milliseconds.
     */
    public long getDuration()
    {
        return mLastSeen - mFirstSeen;
    }

    /**
     * Indicates the carrier has been present for at least the attack time and was reported as started.
     */
    public boolean isActive()
    {
        return mActive;
    }

    public double getPeakSnrDb()
    {
        return mPeakSnrDb;
    }

    /**
     * Widest occupied bandwidth measured during this transmission, in Hertz.
     */
    public double getMaximumWidthHz()
    {
        return mMaximumWidthHz;
    }

    @Override
    public String toString()
    {
        return String.format("%.4f MHz%s error:%+.0f Hz width:%.1f kHz snr:%.1f dB duration:%.1f s",
                mChannelFrequency / 1e6, mOffRaster ? " (off-raster)" : "", getFrequencyError(),
                mMaximumWidthHz / 1e3, mPeakSnrDb, getDuration() / 1e3);
    }
}
