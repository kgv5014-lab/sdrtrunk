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
import io.github.dsheirer.controller.channel.ChannelProcessingManager;
import io.github.dsheirer.source.tuner.Tuner;
import io.github.dsheirer.source.tuner.TunerController;
import io.github.dsheirer.source.tuner.channel.TunerChannel;

/**
 * Runs discovery's temporary channels on a tuner through the channel processing manager.
 */
public class TunerDiscoveryChannelHost implements DiscoveryChannelHost
{
    private final Tuner mTuner;
    private final ChannelProcessingManager mChannelProcessingManager;

    /**
     * Constructs an instance
     * @param tuner that discovery is watching
     * @param channelProcessingManager to start and stop channels
     */
    public TunerDiscoveryChannelHost(Tuner tuner, ChannelProcessingManager channelProcessingManager)
    {
        mTuner = tuner;
        mChannelProcessingManager = channelProcessingManager;
    }

    @Override
    public String getTunerName()
    {
        return mTuner.getPreferredName();
    }

    /**
     * Same test that the polyphase channel source manager uses to decide that it can keep its current centre
     * frequency, so a channel that fits here is sourced without retuning.
     */
    @Override
    public boolean fits(long frequency, double bandwidth)
    {
        TunerController controller = mTuner.getTunerController();
        TunerChannel channel = new TunerChannel(frequency, (int)bandwidth);
        long center = controller.getFrequency();
        int usableHalf = controller.getUsableHalfBandwidth();

        if(channel.getMinFrequency() < center - usableHalf || channel.getMaxFrequency() > center + usableHalf)
        {
            return false;
        }

        return !controller.hasMiddleUnusableBandwidth() ||
                !channel.overlaps(center - controller.getMiddleUnusableHalfBandwidth(),
                        center + controller.getMiddleUnusableHalfBandwidth());
    }

    @Override
    public void start(Channel channel) throws ChannelException
    {
        mChannelProcessingManager.start(channel);
    }

    @Override
    public void stop(Channel channel) throws ChannelException
    {
        mChannelProcessingManager.stop(channel);
    }

    @Override
    public boolean isRunning(Channel channel)
    {
        return mChannelProcessingManager.getProcessingChain(channel) != null;
    }
}
