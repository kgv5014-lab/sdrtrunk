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

import io.github.dsheirer.controller.channel.Channel;
import io.github.dsheirer.controller.channel.ChannelException;

/**
 * Where discovery's temporary channels run: the tuner that discovery is watching and the channel processing manager.
 */
public interface DiscoveryChannelHost
{
    /**
     * Name of the tuner, used as the preferred tuner for temporary channels.
     */
    String getTunerName();

    /**
     * Indicates if a channel of this bandwidth fits inside the tuner's usable bandwidth at its current centre
     * frequency, outside of any DC spike region, so that starting it won't move the tuner.
     * @param frequency of the channel in Hertz
     * @param bandwidth of the channel in Hertz
     */
    boolean fits(long frequency, double bandwidth);

    /**
     * Starts processing the channel.
     */
    void start(Channel channel) throws ChannelException;

    /**
     * Stops processing the channel.
     */
    void stop(Channel channel) throws ChannelException;

    /**
     * Indicates if the channel is still processing.  It may have been stopped by the user or by a tuner error.
     */
    boolean isRunning(Channel channel);
}
