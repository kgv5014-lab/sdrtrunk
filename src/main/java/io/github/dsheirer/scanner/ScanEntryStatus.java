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

/**
 * Live status for one frequency in a running scan list.  Updated from processing threads and read by the GUI.
 */
public class ScanEntryStatus
{
    /**
     * How the frequency is being monitored
     */
    public enum Mode
    {
        SCAN("Scanning"),
        PARALLEL("Monitored"),
        LOCKOUT("Locked out");

        private final String mLabel;

        Mode(String label)
        {
            mLabel = label;
        }

        @Override
        public String toString()
        {
            return mLabel;
        }
    }

    /**
     * How long after the last activity notification that the frequency is still shown as receiving.
     */
    public static final long RECEIVING_TIMEOUT_MS = 750;

    private final ScanGroup mScanGroup;
    private final long mFrequency;
    private final String mLabel;
    private final boolean mPriority;
    private volatile Mode mMode;
    private volatile String mTunerName;
    private volatile Channel mChildChannel;
    private volatile boolean mChildRunning;
    private volatile long mLastActivity;

    /**
     * Constructs an instance
     * @param scanGroup that owns this entry
     * @param frequency in hertz
     * @param label optional user label
     * @param priority flag
     * @param lockout true if the user locked out this frequency
     */
    public ScanEntryStatus(ScanGroup scanGroup, long frequency, String label, boolean priority, boolean lockout)
    {
        mScanGroup = scanGroup;
        mFrequency = frequency;
        mLabel = label;
        mPriority = priority;
        mMode = lockout ? Mode.LOCKOUT : Mode.SCAN;
    }

    public ScanGroup getScanGroup()
    {
        return mScanGroup;
    }

    public long getFrequency()
    {
        return mFrequency;
    }

    public String getLabel()
    {
        return mLabel;
    }

    /**
     * Label when one is defined, otherwise the frequency in MHz.
     */
    public String getDisplayName()
    {
        return mLabel != null ? mLabel : String.format("%.5f MHz", mFrequency / 1E6);
    }

    public boolean isPriority()
    {
        return mPriority;
    }

    public Mode getMode()
    {
        return mMode;
    }

    public void setMode(Mode mode)
    {
        mMode = mode;
    }

    public boolean isLockedOut()
    {
        return mMode == Mode.LOCKOUT;
    }

    /**
     * Name of the tuner monitoring this frequency in parallel, or null.
     */
    public String getTunerName()
    {
        return mTunerName;
    }

    public void setTunerName(String tunerName)
    {
        mTunerName = tunerName;
    }

    /**
     * Channel that monitors this frequency in parallel, or null when the frequency is scanned by the rover.
     */
    public Channel getChildChannel()
    {
        return mChildChannel;
    }

    public void setChildChannel(Channel childChannel)
    {
        mChildChannel = childChannel;
    }

    /**
     * Indicates if the parallel channel for this frequency has started processing
     */
    public boolean isChildRunning()
    {
        return mChildRunning;
    }

    public void setChildRunning(boolean running)
    {
        mChildRunning = running;
    }

    /**
     * Records activity (a call) on this frequency.
     */
    public void activity()
    {
        mLastActivity = System.currentTimeMillis();
    }

    /**
     * Timestamp of the last activity on this frequency, or 0 if none heard yet.
     */
    public long getLastActivity()
    {
        return mLastActivity;
    }

    /**
     * Indicates if a call is currently being received on this frequency.
     */
    public boolean isReceiving()
    {
        return mMode != Mode.LOCKOUT && (System.currentTimeMillis() - mLastActivity) < RECEIVING_TIMEOUT_MS;
    }

    /**
     * Indicates if the scanning (rover) channel is currently tuned to this frequency.
     */
    public boolean isTuned()
    {
        return mMode == Mode.SCAN && mScanGroup.getRoverFrequency() == mFrequency;
    }
}
