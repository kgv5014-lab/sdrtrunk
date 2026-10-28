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

import io.github.dsheirer.audio.squelch.ISquelchStateListener;
import io.github.dsheirer.audio.squelch.SquelchState;
import io.github.dsheirer.audio.squelch.SquelchStateEvent;
import io.github.dsheirer.message.DroppedSamplesMessage;
import io.github.dsheirer.message.IMessage;
import io.github.dsheirer.message.IMessageListener;
import io.github.dsheirer.message.SyncLossMessage;
import io.github.dsheirer.module.Module;
import io.github.dsheirer.sample.Listener;
import io.github.dsheirer.sample.complex.ComplexSamples;
import io.github.dsheirer.sample.complex.IComplexSamplesListener;
import io.github.dsheirer.source.Source;

/**
 * Processing chain module that counts, for one channel, the messages that decode cleanly or fail, the bits lost to
 * sync loss and the channel power with and without a signal.  The SignalQualityMonitor collects the counts every
 * minute and adds them up per dongle.
 */
public class SignalQualityModule extends Module implements IMessageListener, IComplexSamplesListener,
    ISquelchStateListener
{
    private final SignalQualityMonitor mMonitor;
    private final Source mSource;
    private final String mChannelName;
    private final String mDecoder;
    private final boolean mTrafficChannel;
    private final boolean mSquelchedAtStart;
    private SignalTally mTally = new SignalTally();
    private volatile boolean mSignalPresent;

    /**
     * Constructs an instance
     * @param monitor that collects the counts
     * @param source of the channel's samples
     * @param channelName to show in the log
     * @param decoder short name, like P25 or NBFM
     * @param trafficChannel true for a voice channel started for one call
     * @param squelchedAtStart true for analog channels that only carry a signal while someone talks
     */
    public SignalQualityModule(SignalQualityMonitor monitor, Source source, String channelName, String decoder,
                               boolean trafficChannel, boolean squelchedAtStart)
    {
        mMonitor = monitor;
        mSource = source;
        mChannelName = channelName;
        mDecoder = decoder;
        mTrafficChannel = trafficChannel;
        mSquelchedAtStart = squelchedAtStart;
        mSignalPresent = !squelchedAtStart;
    }

    public Source getSource()
    {
        return mSource;
    }

    public String getChannelName()
    {
        return mChannelName;
    }

    public String getDecoder()
    {
        return mDecoder;
    }

    /**
     * Current channel frequency in hertz, or 0 when the source can't say.
     */
    public long getFrequency()
    {
        try
        {
            return mSource.getFrequency();
        }
        catch(Exception e)
        {
            return 0;
        }
    }

    /**
     * Hands over the counts since the last call and starts a fresh tally.
     */
    public synchronized SignalTally drain()
    {
        SignalTally tally = mTally;
        mTally = new SignalTally();
        return tally;
    }

    @Override
    public void reset()
    {
        mSignalPresent = !mSquelchedAtStart;
    }

    @Override
    public void start()
    {
        mMonitor.add(this);
    }

    @Override
    public void stop()
    {
        mMonitor.remove(this);
    }

    @Override
    public Listener<IMessage> getMessageListener()
    {
        return this::receiveMessage;
    }

    private synchronized void receiveMessage(IMessage message)
    {
        if(message instanceof SyncLossMessage syncLoss)
        {
            //Voice channels lose sync at the end of every call; only a control channel losing sync means trouble
            if(!mTrafficChannel)
            {
                mTally.addSyncLoss(syncLoss.getBitsProcessed());
            }
        }
        else if(message instanceof DroppedSamplesMessage dropped)
        {
            mTally.addDroppedBits(dropped.getBitsDropped());
        }
        else
        {
            mTally.addMessage(message.isValid());
        }
    }

    @Override
    public Listener<ComplexSamples> getComplexSamplesListener()
    {
        return this::receiveSamples;
    }

    private void receiveSamples(ComplexSamples samples)
    {
        float[] i = samples.i();
        float[] q = samples.q();

        if(i.length == 0)
        {
            return;
        }

        double power = 0.0;

        for(int x = 0; x < i.length; x++)
        {
            power += i[x] * i[x] + q[x] * q[x];
        }

        power /= i.length;

        if(power > 0)
        {
            double db = 10.0 * Math.log10(power);

            synchronized(this)
            {
                mTally.addPower(db, mSignalPresent);
            }
        }
    }

    @Override
    public Listener<SquelchStateEvent> getSquelchStateListener()
    {
        //Digital channels carry a signal the whole time they run, whatever their audio squelch is doing
        return event -> {
            if(mSquelchedAtStart)
            {
                mSignalPresent = event.getSquelchState() == SquelchState.UNSQUELCH;
            }
        };
    }
}
