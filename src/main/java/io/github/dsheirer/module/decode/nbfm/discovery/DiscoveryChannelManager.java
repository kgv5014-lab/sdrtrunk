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
import io.github.dsheirer.module.decode.analog.DecodeConfigAnalog;
import io.github.dsheirer.module.decode.nbfm.DecodeConfigNBFM;
import io.github.dsheirer.sample.Listener;
import io.github.dsheirer.source.config.SourceConfigTuner;
import java.util.HashMap;
import java.util.Iterator;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.function.LongSupplier;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Starts a temporary NBFM channel for each discovered carrier so it can be heard, recorded and streamed like any other
 * channel, and stops the channel once the carrier has been gone for the hang time.
 *
 * Temporary channels are standard channels that are never added to the playlist.  They are pinned to the discovery
 * tuner and only started when they fit inside that tuner's current bandwidth, so they never cause it to retune.
 *
 * The decision logic (handle and poll) is synchronous and driven by an injectable clock.  start() runs it on a single
 * background thread, which keeps channel start and stop calls off the sample processing thread.
 */
public class DiscoveryChannelManager implements Listener<DiscoveryEvent>
{
    private static final Logger mLog = LoggerFactory.getLogger(DiscoveryChannelManager.class);
    public static final String SYSTEM_NAME = "NBFM Discovery";
    private static final long POLL_INTERVAL_MS = 250;

    private final DiscoveryConfiguration mConfiguration;
    private final DiscoveryChannelHost mHost;
    private final LongSupplier mClock;
    private final Map<Long,RunningChannel> mRunning = new HashMap<>();
    private final Map<Long,Double> mWidestSeen = new HashMap<>();
    private ScheduledExecutorService mExecutor;
    private Listener<ChannelStatus> mStatusListener;

    /**
     * Constructs an instance
     * @param configuration settings
     * @param host tuner and channel processing access
     * @param clock wall clock in milliseconds
     */
    public DiscoveryChannelManager(DiscoveryConfiguration configuration, DiscoveryChannelHost host, LongSupplier clock)
    {
        mConfiguration = configuration;
        mHost = host;
        mClock = clock;
    }

    /**
     * Registers a listener for channel open, close and failure notifications.
     */
    public void setStatusListener(Listener<ChannelStatus> listener)
    {
        mStatusListener = listener;
    }

    /**
     * Starts the background thread that handles events and stops expired channels.
     */
    public void start()
    {
        if(mExecutor == null)
        {
            mExecutor = Executors.newSingleThreadScheduledExecutor(runnable -> {
                Thread thread = new Thread(runnable, "sdrtrunk nbfm discovery channels");
                thread.setDaemon(true);
                return thread;
            });
            //An exception escaping a periodic task cancels it, so the poll task logs and carries on
            mExecutor.scheduleAtFixedRate(() -> {
                try
                {
                    poll();
                }
                catch(Exception e)
                {
                    mLog.error("Error checking NBFM discovery channels", e);
                }
            }, POLL_INTERVAL_MS, POLL_INTERVAL_MS, TimeUnit.MILLISECONDS);
        }
    }

    /**
     * Stops every temporary channel and the background thread.
     */
    public void stop()
    {
        if(mExecutor != null)
        {
            mExecutor.execute(this::stopAll);
            mExecutor.shutdown();

            try
            {
                mExecutor.awaitTermination(5, TimeUnit.SECONDS);
            }
            catch(InterruptedException ie)
            {
                Thread.currentThread().interrupt();
            }

            mExecutor = null;
        }
        else
        {
            stopAll();
        }
    }

    /**
     * Discovery events, received on the sample processing thread and handed to the background thread.
     */
    @Override
    public void receive(DiscoveryEvent event)
    {
        //Copy what's needed now, since the tracked carrier keeps changing on the sample thread
        Detection detection = new Detection(event.type(), event.carrier().getChannelFrequency(),
                event.carrier().getMaximumWidthHz());
        ScheduledExecutorService executor = mExecutor;

        if(executor != null)
        {
            executor.execute(() -> handle(detection));
        }
    }

    /**
     * Number of temporary channels currently running.
     */
    public synchronized int getRunningCount()
    {
        return mRunning.size();
    }

    /**
     * Indicates if a temporary channel is running on the frequency.
     */
    public synchronized boolean isRunning(long frequency)
    {
        return mRunning.containsKey(frequency);
    }

    /**
     * Applies a carrier event: opens a channel when a carrier starts, and sets the stop time when it ends.
     */
    public synchronized void handle(Detection detection)
    {
        long frequency = detection.frequency();
        mWidestSeen.merge(frequency, detection.widthHz(), Math::max);
        RunningChannel running = mRunning.get(frequency);

        if(running != null && !mHost.isRunning(running.mChannel))
        {
            //Stopped outside of discovery
            mRunning.remove(frequency);
            notify(frequency, ChannelStatus.State.CLOSED, null);
            running = null;
        }

        switch(detection.type())
        {
            case STARTED ->
            {
                if(running != null)
                {
                    //Reply on a channel that is still open: keep it open
                    running.mStopTime = Long.MAX_VALUE;
                }
                else if(mConfiguration.isAutoChannels())
                {
                    open(frequency);
                }
            }
            case ENDED ->
            {
                if(running != null)
                {
                    running.mStopTime = mClock.getAsLong() + mConfiguration.getChannelHangTimeMs();
                }
            }
            case BIRDIE ->
            {
                if(running != null)
                {
                    close(frequency, running);
                }
            }
        }
    }

    /**
     * Stops channels whose hang time has expired.
     */
    public synchronized void poll()
    {
        long now = mClock.getAsLong();
        Iterator<Map.Entry<Long,RunningChannel>> it = mRunning.entrySet().iterator();

        while(it.hasNext())
        {
            Map.Entry<Long,RunningChannel> entry = it.next();

            if(!mHost.isRunning(entry.getValue().mChannel))
            {
                //Stopped outside of discovery
                it.remove();
                notify(entry.getKey(), ChannelStatus.State.CLOSED, null);
            }
            else if(entry.getValue().mStopTime <= now)
            {
                it.remove();
                stopChannel(entry.getKey(), entry.getValue());
            }
        }
    }

    /**
     * Stops the temporary channel on the frequency now, if one is running.  For example, when the user ignores the
     * frequency.
     */
    public synchronized void close(long frequency)
    {
        RunningChannel running = mRunning.get(frequency);

        if(running != null)
        {
            close(frequency, running);
        }
    }

    /**
     * Stops all temporary channels now.
     */
    public synchronized void stopAll()
    {
        for(Map.Entry<Long,RunningChannel> entry : mRunning.entrySet())
        {
            stopChannel(entry.getKey(), entry.getValue());
        }

        mRunning.clear();
    }

    /**
     * Bandwidth for a temporary channel on the frequency: the configured bandwidth, or with AUTO, the bandwidth that
     * fits the widest signal measured on the frequency so far.
     */
    public synchronized DecodeConfigAnalog.Bandwidth getBandwidth(long frequency)
    {
        return switch(mConfiguration.getChannelBandwidth())
        {
            case NARROW -> DecodeConfigAnalog.Bandwidth.BW_12_5;
            case WIDE -> DecodeConfigAnalog.Bandwidth.BW_25_0;
            case AUTO -> mWidestSeen.getOrDefault(frequency, 0.0) > mConfiguration.getWideChannelWidthHz() ?
                    DecodeConfigAnalog.Bandwidth.BW_25_0 : DecodeConfigAnalog.Bandwidth.BW_12_5;
        };
    }

    private void open(long frequency)
    {
        if(mRunning.size() >= mConfiguration.getMaximumChannels())
        {
            notify(frequency, ChannelStatus.State.NO_ROOM, "All " + mConfiguration.getMaximumChannels() +
                    " discovery channels are in use");
            return;
        }

        DecodeConfigAnalog.Bandwidth bandwidth = getBandwidth(frequency);

        if(!mHost.fits(frequency, bandwidth.getValue()))
        {
            notify(frequency, ChannelStatus.State.OUT_OF_BAND, "Too close to the tuner's edge or centre");
            return;
        }

        Channel channel = createChannel(frequency, bandwidth, mHost.getTunerName());

        try
        {
            mHost.start(channel);
            mRunning.put(frequency, new RunningChannel(channel));
            notify(frequency, ChannelStatus.State.OPEN, bandwidth.toString());
            mLog.info("NBFM discovery opened channel {} ({})", channel.getName(), bandwidth);
        }
        catch(ChannelException ce)
        {
            notify(frequency, ChannelStatus.State.FAILED, ce.getMessage());
            mLog.info("NBFM discovery couldn't open channel {}: {}", channel.getName(), ce.getMessage());
        }
        catch(RuntimeException re)
        {
            notify(frequency, ChannelStatus.State.FAILED, "error, see the log");
            mLog.error("Error opening NBFM discovery channel " + channel.getName(), re);
        }
    }

    private void close(long frequency, RunningChannel running)
    {
        mRunning.remove(frequency);
        stopChannel(frequency, running);
    }

    private void stopChannel(long frequency, RunningChannel running)
    {
        try
        {
            mHost.stop(running.mChannel);
            mLog.info("NBFM discovery closed channel {}", running.mChannel.getName());
        }
        catch(ChannelException | RuntimeException e)
        {
            mLog.error("Error stopping NBFM discovery channel " + running.mChannel.getName(), e);
        }

        notify(frequency, ChannelStatus.State.CLOSED, null);
    }

    /**
     * Creates a temporary NBFM channel on the frequency, pinned to the tuner.
     */
    public static Channel createChannel(long frequency, DecodeConfigAnalog.Bandwidth bandwidth, String tunerName)
    {
        Channel channel = new Channel(String.format("AUTO %.4f", frequency / 1e6));
        channel.setSystem(SYSTEM_NAME);
        channel.setSite(tunerName);

        DecodeConfigNBFM decodeConfig = new DecodeConfigNBFM();
        decodeConfig.setBandwidth(bandwidth);
        channel.setDecodeConfiguration(decodeConfig);

        SourceConfigTuner sourceConfig = new SourceConfigTuner();
        sourceConfig.setFrequency(frequency);
        sourceConfig.setPreferredTuner(tunerName);
        channel.setSourceConfiguration(sourceConfig);

        return channel;
    }

    private void notify(long frequency, ChannelStatus.State state, String detail)
    {
        if(mStatusListener != null)
        {
            mStatusListener.receive(new ChannelStatus(frequency, state, detail));
        }
    }

    /**
     * The parts of a discovery event the manager needs, copied on the sample processing thread.
     */
    public record Detection(DiscoveryEvent.Type type, long frequency, double widthHz)
    {
    }

    /**
     * Temporary channel status for a frequency.
     */
    public record ChannelStatus(long frequency, State state, String detail)
    {
        public enum State
        {
            OPEN, CLOSED, NO_ROOM, OUT_OF_BAND, FAILED
        }
    }

    private static class RunningChannel
    {
        private final Channel mChannel;
        private long mStopTime = Long.MAX_VALUE;

        RunningChannel(Channel channel)
        {
            mChannel = channel;
        }
    }
}
