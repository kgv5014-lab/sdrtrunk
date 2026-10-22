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
import io.github.dsheirer.source.tuner.manager.TunerManager;

import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CopyOnWriteArrayList;

/**
 * Registry of running scan lists.  Scan group managers register here when their channel starts, the decoder factory
 * uses it to recognize the parallel channels that a scan group creates, and the scanner panel reads it to show
 * live status.
 */
public class ScannerRegistry
{
    private static final ScannerRegistry INSTANCE = new ScannerRegistry();
    private final List<ScanGroup> mScanGroups = new CopyOnWriteArrayList<>();
    private final Map<Channel,ScanEntryStatus> mParallelChannels = new ConcurrentHashMap<>();
    private volatile TunerManager mTunerManager;

    private ScannerRegistry()
    {
    }

    public static ScannerRegistry getInstance()
    {
        return INSTANCE;
    }

    /**
     * Tuner manager used to plan parallel monitoring across tuners.
     */
    public TunerManager getTunerManager()
    {
        return mTunerManager;
    }

    public void setTunerManager(TunerManager tunerManager)
    {
        mTunerManager = tunerManager;
    }

    /**
     * Running scan groups
     */
    public List<ScanGroup> getScanGroups()
    {
        return mScanGroups;
    }

    public void register(ScanGroup scanGroup)
    {
        if(!mScanGroups.contains(scanGroup))
        {
            mScanGroups.add(scanGroup);
        }
    }

    public void unregister(ScanGroup scanGroup)
    {
        mScanGroups.remove(scanGroup);
    }

    /**
     * Registers a channel that monitors a scan list frequency in parallel.
     */
    public void registerParallelChannel(Channel channel, ScanEntryStatus entry)
    {
        mParallelChannels.put(channel, entry);
    }

    public void unregisterParallelChannel(Channel channel)
    {
        mParallelChannels.remove(channel);
    }

    /**
     * Scan list entry for a parallel monitoring channel, or null if the channel isn't one.
     */
    public ScanEntryStatus getParallelChannelEntry(Channel channel)
    {
        return mParallelChannels.get(channel);
    }
}
