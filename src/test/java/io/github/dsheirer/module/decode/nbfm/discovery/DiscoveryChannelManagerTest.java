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

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import io.github.dsheirer.controller.channel.Channel;
import io.github.dsheirer.controller.channel.ChannelException;
import io.github.dsheirer.module.decode.analog.DecodeConfigAnalog;
import io.github.dsheirer.module.decode.nbfm.DecodeConfigNBFM;
import io.github.dsheirer.module.decode.nbfm.discovery.DiscoveryChannelManager.ChannelStatus;
import io.github.dsheirer.module.decode.nbfm.discovery.DiscoveryChannelManager.Detection;
import io.github.dsheirer.module.decode.nbfm.discovery.DiscoveryEvent.Type;
import io.github.dsheirer.source.config.SourceConfigTuner;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import org.junit.jupiter.api.Test;

/**
 * Temporary channel lifecycle: open on start, hang time, replies, channel limit, band fit and bandwidth choice.
 */
public class DiscoveryChannelManagerTest
{
    private final DiscoveryConfiguration mConfiguration = new DiscoveryConfiguration();
    private final FakeHost mHost = new FakeHost();
    private final List<ChannelStatus> mStatus = new ArrayList<>();
    private long mNow = 0;
    private final DiscoveryChannelManager mManager = new DiscoveryChannelManager(mConfiguration, mHost, () -> mNow);

    public DiscoveryChannelManagerTest()
    {
        mConfiguration.setAutoChannels(true);
        mManager.setStatusListener(mStatus::add);
    }

    private void event(Type type, long frequency, double width)
    {
        mManager.handle(new Detection(type, frequency, width));
    }

    private void advance(long ms)
    {
        mNow += ms;
        mManager.poll();
    }

    @Test
    void opensChannelPinnedToTunerAndClosesAfterHangTime()
    {
        event(Type.STARTED, 155_752_500, 5_000);

        assertEquals(1, mHost.mRunning.size());
        Channel channel = mHost.mRunning.iterator().next();
        assertEquals("AUTO 155.7525", channel.getName());
        assertEquals(DiscoveryChannelManager.SYSTEM_NAME, channel.getSystem());
        SourceConfigTuner source = (SourceConfigTuner)channel.getSourceConfiguration();
        assertEquals(155_752_500, source.getFrequency());
        assertEquals("RTL-SDR V4", source.getPreferredTuner());
        assertEquals(DecodeConfigAnalog.Bandwidth.BW_12_5,
                ((DecodeConfigNBFM)channel.getDecodeConfiguration()).getBandwidth());

        event(Type.ENDED, 155_752_500, 5_000);
        advance(4_900);
        assertTrue(mManager.isRunning(155_752_500), "Should stay open during the hang time");

        advance(200);
        assertFalse(mManager.isRunning(155_752_500));
        assertTrue(mHost.mRunning.isEmpty());
        assertEquals(ChannelStatus.State.CLOSED, mStatus.getLast().state());
    }

    @Test
    void replyDuringHangTimeKeepsTheSameChannel()
    {
        event(Type.STARTED, 155_752_500, 5_000);
        Channel first = mHost.mRunning.iterator().next();
        event(Type.ENDED, 155_752_500, 5_000);
        advance(3_000);
        event(Type.STARTED, 155_752_500, 5_000);
        advance(10_000);

        assertEquals(1, mHost.mStarts);
        assertTrue(mHost.mRunning.contains(first), "Reply should keep the channel open");
    }

    @Test
    void doesNothingWhenAutoChannelsIsOff()
    {
        mConfiguration.setAutoChannels(false);
        event(Type.STARTED, 155_752_500, 5_000);

        assertTrue(mHost.mRunning.isEmpty());
    }

    @Test
    void respectsChannelLimit()
    {
        mConfiguration.setMaximumChannels(2);
        event(Type.STARTED, 155_752_500, 5_000);
        event(Type.STARTED, 155_122_500, 5_000);
        event(Type.STARTED, 154_430_000, 5_000);

        assertEquals(2, mHost.mRunning.size());
        assertEquals(new ChannelStatus(154_430_000, ChannelStatus.State.NO_ROOM,
                "All 2 discovery channels are in use"), mStatus.getLast());
    }

    @Test
    void skipsChannelsThatDontFitTheTuner()
    {
        mHost.mFits = false;
        event(Type.STARTED, 155_752_500, 5_000);

        assertTrue(mHost.mRunning.isEmpty());
        assertEquals(ChannelStatus.State.OUT_OF_BAND, mStatus.getLast().state());
    }

    @Test
    void reportsStartFailure()
    {
        mHost.mFailStart = true;
        event(Type.STARTED, 155_752_500, 5_000);

        assertFalse(mManager.isRunning(155_752_500));
        assertEquals(new ChannelStatus(155_752_500, ChannelStatus.State.FAILED, "No Tuner Available"),
                mStatus.getLast());
    }

    @Test
    void choosesWideBandwidthForWideSignals()
    {
        event(Type.STARTED, 155_752_500, 11_000);
        Channel channel = mHost.mRunning.iterator().next();

        assertEquals(DecodeConfigAnalog.Bandwidth.BW_25_0,
                ((DecodeConfigNBFM)channel.getDecodeConfiguration()).getBandwidth());

        //Width is remembered for the next transmission even if it starts narrow (e.g. tone only)
        event(Type.ENDED, 155_752_500, 11_000);
        advance(10_000);
        event(Type.STARTED, 155_752_500, 1_500);
        channel = mHost.mRunning.iterator().next();
        assertEquals(DecodeConfigAnalog.Bandwidth.BW_25_0,
                ((DecodeConfigNBFM)channel.getDecodeConfiguration()).getBandwidth());

        mConfiguration.setChannelBandwidth(DiscoveryConfiguration.ChannelBandwidth.NARROW);
        assertEquals(DecodeConfigAnalog.Bandwidth.BW_12_5, mManager.getBandwidth(155_752_500));
    }

    @Test
    void birdieClosesItsChannel()
    {
        event(Type.STARTED, 156_000_000, 1_200);
        event(Type.BIRDIE, 156_000_000, 1_200);

        assertTrue(mHost.mRunning.isEmpty());
    }

    @Test
    void channelStoppedByUserIsForgottenAndReopenedOnNextTransmission()
    {
        event(Type.STARTED, 155_752_500, 5_000);
        mHost.mRunning.clear();
        advance(100);

        assertFalse(mManager.isRunning(155_752_500));
        event(Type.STARTED, 155_752_500, 5_000);
        assertEquals(2, mHost.mStarts);
    }

    @Test
    void closeAndStopAllStopChannels()
    {
        event(Type.STARTED, 155_752_500, 5_000);
        event(Type.STARTED, 155_122_500, 5_000);

        mManager.close(155_752_500);
        assertEquals(1, mHost.mRunning.size());

        mManager.stopAll();
        assertTrue(mHost.mRunning.isEmpty());
        assertEquals(0, mManager.getRunningCount());
    }

    private static class FakeHost implements DiscoveryChannelHost
    {
        private final Set<Channel> mRunning = new HashSet<>();
        private boolean mFits = true;
        private boolean mFailStart = false;
        private int mStarts = 0;

        @Override
        public String getTunerName()
        {
            return "RTL-SDR V4";
        }

        @Override
        public boolean fits(long frequency, double bandwidth)
        {
            return mFits;
        }

        @Override
        public void start(Channel channel) throws ChannelException
        {
            if(mFailStart)
            {
                throw new ChannelException("No Tuner Available");
            }

            mStarts++;
            mRunning.add(channel);
        }

        @Override
        public void stop(Channel channel)
        {
            mRunning.remove(channel);
        }

        @Override
        public boolean isRunning(Channel channel)
        {
            return mRunning.contains(channel);
        }
    }
}
