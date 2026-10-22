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

package io.github.dsheirer.scanner;

import io.github.dsheirer.controller.channel.Channel;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * Live state of a running conventional scan list (a NBFM or AM channel that has more than one frequency).  Shared
 * between the scan group manager in the channel's processing chain and the scanner panel in the GUI.
 */
public class ScanGroup
{
    private final Channel mChannel;
    private final List<ScanEntryStatus> mEntries = new ArrayList<>();
    private final ScanGroupManager mManager;
    private volatile long mRoverFrequency;
    private volatile boolean mHold;
    private volatile String mRoverTunerName;

    /**
     * Constructs an instance
     * @param channel for the scan list
     * @param manager that controls the scan list
     */
    public ScanGroup(Channel channel, ScanGroupManager manager)
    {
        mChannel = channel;
        mManager = manager;
    }

    /**
     * Adds an entry.  Only invoked during setup, before the group is registered.
     */
    void addEntry(ScanEntryStatus entry)
    {
        mEntries.add(entry);
    }

    public Channel getChannel()
    {
        return mChannel;
    }

    public String getName()
    {
        return mChannel.getName();
    }

    /**
     * Entries in scan list order.
     */
    public List<ScanEntryStatus> getEntries()
    {
        return Collections.unmodifiableList(mEntries);
    }

    /**
     * Entry for the frequency or null.
     */
    public ScanEntryStatus getEntry(long frequency)
    {
        for(ScanEntryStatus entry: mEntries)
        {
            if(entry.getFrequency() == frequency)
            {
                return entry;
            }
        }

        return null;
    }

    /**
     * Frequency that the scanning (rover) channel is tuned to.
     */
    public long getRoverFrequency()
    {
        return mRoverFrequency;
    }

    void setRoverFrequency(long frequency)
    {
        mRoverFrequency = frequency;
    }

    /**
     * Name of the tuner sourcing the scanning (rover) channel, when known.
     */
    public String getRoverTunerName()
    {
        return mRoverTunerName;
    }

    void setRoverTunerName(String tunerName)
    {
        mRoverTunerName = tunerName;
    }

    /**
     * Indicates if the scanning (rover) channel is parked on one frequency because every other frequency is
     * monitored in parallel or locked out.
     */
    public boolean isParked()
    {
        return getScannedCount() <= 1;
    }

    public boolean isHold()
    {
        return mHold;
    }

    /**
     * Holds on the current frequency, or resumes scanning.
     */
    public void setHold(boolean hold)
    {
        mHold = hold;
        mManager.hold(hold);
    }

    /**
     * Skips the scanning channel to the next frequency.
     */
    public void skip()
    {
        mHold = false;
        mManager.skip();
    }

    /**
     * Locks out, or restores, a frequency for the rest of this session.
     */
    public void setLockout(ScanEntryStatus entry, boolean lockout)
    {
        mManager.setLockout(entry, lockout);
    }

    /**
     * Notification that the parallel channel for an entry has stopped.
     */
    void parallelChannelStopped(ScanEntryStatus entry)
    {
        mManager.parallelChannelStopped(entry);
    }

    /**
     * Number of frequencies that are being scanned by the rover channel.
     */
    public int getScannedCount()
    {
        int count = 0;

        for(ScanEntryStatus entry: mEntries)
        {
            if(entry.getMode() == ScanEntryStatus.Mode.SCAN)
            {
                count++;
            }
        }

        return count;
    }
}
