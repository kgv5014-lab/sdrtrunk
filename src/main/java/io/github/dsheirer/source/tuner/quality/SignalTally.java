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
package io.github.dsheirer.source.tuner.quality;

/**
 * Counts for one channel over a stretch of time: messages that decoded cleanly or failed their error check, time the
 * decoder lost sync, and channel power readings split into "a signal is present" and "only noise".
 *
 * Not thread safe; the owner synchronizes.
 */
public class SignalTally
{
    private static final double MINIMUM_DB = -150.0;
    private static final double MAXIMUM_DB = 10.0;
    private static final int BINS_PER_DB = 2;
    private static final int BIN_COUNT = (int)((MAXIMUM_DB - MINIMUM_DB) * BINS_PER_DB) + 1;
    /**
     * Fewer power readings than this don't give a level worth reporting.
     */
    public static final int MINIMUM_READINGS = 20;

    private long mGoodMessages;
    private long mBadMessages;
    private long mSyncLossBits;
    private long mDroppedBits;
    private final int[] mSignalBins = new int[BIN_COUNT];
    private final int[] mNoiseBins = new int[BIN_COUNT];
    private int mSignalReadings;
    private int mNoiseReadings;

    public void addMessage(boolean valid)
    {
        if(valid)
        {
            mGoodMessages++;
        }
        else
        {
            mBadMessages++;
        }
    }

    /**
     * Adds message counts gathered elsewhere.
     */
    public void addMessages(long good, long bad)
    {
        mGoodMessages += good;
        mBadMessages += bad;
    }

    public void addSyncLoss(int bits)
    {
        mSyncLossBits += Math.max(bits, 0);
    }

    public void addDroppedBits(int bits)
    {
        mDroppedBits += Math.max(bits, 0);
    }

    /**
     * Records one channel power reading.
     * @param db channel power in dB relative to full scale
     * @param signal true when a signal is present (squelch open, or a control channel), false for noise only
     */
    public void addPower(double db, boolean signal)
    {
        if(Double.isNaN(db) || Double.isInfinite(db))
        {
            return;
        }

        int bin = (int)Math.round((Math.min(Math.max(db, MINIMUM_DB), MAXIMUM_DB) - MINIMUM_DB) * BINS_PER_DB);

        if(signal)
        {
            mSignalBins[bin]++;
            mSignalReadings++;
        }
        else
        {
            mNoiseBins[bin]++;
            mNoiseReadings++;
        }
    }

    /**
     * Adds another tally's counts to this one.
     */
    public void add(SignalTally other)
    {
        mGoodMessages += other.mGoodMessages;
        mBadMessages += other.mBadMessages;
        mSyncLossBits += other.mSyncLossBits;
        mDroppedBits += other.mDroppedBits;
        mSignalReadings += other.mSignalReadings;
        mNoiseReadings += other.mNoiseReadings;

        for(int x = 0; x < BIN_COUNT; x++)
        {
            mSignalBins[x] += other.mSignalBins[x];
            mNoiseBins[x] += other.mNoiseBins[x];
        }
    }

    /**
     * Indicates if anything at all was counted.
     */
    public boolean isEmpty()
    {
        return mGoodMessages == 0 && mBadMessages == 0 && mSyncLossBits == 0 && mDroppedBits == 0 &&
            mSignalReadings == 0 && mNoiseReadings == 0;
    }

    public long getGoodMessages()
    {
        return mGoodMessages;
    }

    public long getBadMessages()
    {
        return mBadMessages;
    }

    public long getSyncLossBits()
    {
        return mSyncLossBits;
    }

    public long getDroppedBits()
    {
        return mDroppedBits;
    }

    /**
     * Share of messages that decoded cleanly, 0 to 100, or null when there were none.
     */
    public Double getGoodPercent()
    {
        long total = mGoodMessages + mBadMessages;
        return total == 0 ? null : 100.0 * mGoodMessages / total;
    }

    /**
     * Median channel power while a signal was present, or null with too few readings.
     */
    public Double getSignalDb()
    {
        return median(mSignalBins, mSignalReadings);
    }

    /**
     * Median channel power with only noise present, or null with too few readings.
     */
    public Double getNoiseDb()
    {
        return median(mNoiseBins, mNoiseReadings);
    }

    private static Double median(int[] bins, int readings)
    {
        if(readings < MINIMUM_READINGS)
        {
            return null;
        }

        int half = (readings + 1) / 2;
        int seen = 0;

        for(int x = 0; x < BIN_COUNT; x++)
        {
            seen += bins[x];

            if(seen >= half)
            {
                return MINIMUM_DB + (double)x / BINS_PER_DB;
            }
        }

        return MAXIMUM_DB;
    }
}
