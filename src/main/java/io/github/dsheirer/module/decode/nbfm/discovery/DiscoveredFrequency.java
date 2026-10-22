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
 * Activity history for one channel frequency across all of its transmissions.
 */
public class DiscoveredFrequency
{
    private final long mFrequency;
    private long mFirstHeard;
    private long mLastHeard;
    private int mTransmissionCount;
    private long mTotalAirtimeMs;
    private double mPeakSnrDb;
    private double mMaximumWidthHz;
    private boolean mBirdie;

    /**
     * Constructs an instance
     * @param frequency channel frequency in Hertz
     */
    public DiscoveredFrequency(long frequency)
    {
        mFrequency = frequency;
    }

    /**
     * Adds a completed (or birdie-flagged) transmission to the history.
     */
    void add(TrackedCarrier carrier)
    {
        if(mTransmissionCount == 0)
        {
            mFirstHeard = carrier.getFirstSeen();
        }

        mTransmissionCount++;
        mLastHeard = carrier.getLastSeen();
        mTotalAirtimeMs += carrier.getDuration();
        mPeakSnrDb = Math.max(mPeakSnrDb, carrier.getPeakSnrDb());
        mMaximumWidthHz = Math.max(mMaximumWidthHz, carrier.getMaximumWidthHz());
    }

    void setBirdie()
    {
        mBirdie = true;
    }

    public long getFrequency()
    {
        return mFrequency;
    }

    public long getFirstHeard()
    {
        return mFirstHeard;
    }

    public long getLastHeard()
    {
        return mLastHeard;
    }

    public int getTransmissionCount()
    {
        return mTransmissionCount;
    }

    public long getTotalAirtimeMs()
    {
        return mTotalAirtimeMs;
    }

    public double getPeakSnrDb()
    {
        return mPeakSnrDb;
    }

    public double getMaximumWidthHz()
    {
        return mMaximumWidthHz;
    }

    public boolean isBirdie()
    {
        return mBirdie;
    }

    @Override
    public String toString()
    {
        return String.format("%.4f MHz transmissions:%d airtime:%.1f s width:%.1f kHz snr:%.1f dB%s",
                mFrequency / 1e6, mTransmissionCount, mTotalAirtimeMs / 1e3, mMaximumWidthHz / 1e3, mPeakSnrDb,
                mBirdie ? " BIRDIE" : "");
    }
}
