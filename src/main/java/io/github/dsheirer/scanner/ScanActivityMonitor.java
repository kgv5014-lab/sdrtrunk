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

import io.github.dsheirer.channel.state.DecoderStateEvent;
import io.github.dsheirer.channel.state.IDecoderStateEventListener;
import io.github.dsheirer.channel.state.State;
import io.github.dsheirer.module.Module;
import io.github.dsheirer.sample.Listener;

/**
 * Processing chain module for a channel that monitors one scan list frequency in parallel.  Reports when the
 * channel is running and when calls are heard so the scanner panel can show them.
 */
public class ScanActivityMonitor extends Module implements IDecoderStateEventListener, Listener<DecoderStateEvent>
{
    private final ScanEntryStatus mEntry;

    /**
     * Constructs an instance
     * @param entry to update
     */
    public ScanActivityMonitor(ScanEntryStatus entry)
    {
        mEntry = entry;
    }

    @Override
    public void start()
    {
        mEntry.setChildRunning(true);
    }

    @Override
    public void stop()
    {
        mEntry.setChildRunning(false);
        mEntry.getScanGroup().parallelChannelStopped(mEntry);
    }

    @Override
    public void reset()
    {
    }

    @Override
    public Listener<DecoderStateEvent> getDecoderStateListener()
    {
        return this;
    }

    @Override
    public void receive(DecoderStateEvent event)
    {
        if(event.getEvent() == DecoderStateEvent.Event.NOTIFICATION_CHANNEL_STATE && event.getState() == State.CALL)
        {
            mEntry.activity();
        }
    }
}
