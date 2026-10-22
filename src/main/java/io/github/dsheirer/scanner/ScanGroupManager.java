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
import io.github.dsheirer.controller.channel.Channel;
import io.github.dsheirer.controller.channel.ChannelEvent;
import io.github.dsheirer.controller.channel.IChannelEventProvider;
import io.github.dsheirer.controller.channel.event.ChannelStartProcessingRequest;
import io.github.dsheirer.module.Module;
import io.github.dsheirer.sample.Listener;
import io.github.dsheirer.source.ISourceEventListener;
import io.github.dsheirer.source.SourceEvent;
import io.github.dsheirer.source.config.ScanEntry;
import io.github.dsheirer.source.config.SourceConfigTuner;
import io.github.dsheirer.source.config.SourceConfigTunerMultipleFrequency;
import io.github.dsheirer.source.tuner.TunerController;
import io.github.dsheirer.source.tuner.channel.TunerChannel;
import io.github.dsheirer.source.tuner.channel.rotation.FrequencyLockChangeRequest;
import io.github.dsheirer.source.tuner.channel.rotation.ScanHoldRequest;
import io.github.dsheirer.source.tuner.channel.rotation.ScanSkipRequest;
import io.github.dsheirer.source.tuner.manager.DiscoveredTuner;
import io.github.dsheirer.source.tuner.manager.TunerManager;
import io.github.dsheirer.util.ThreadPool;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;

/**
 * Processing chain module for a conventional scan list (a NBFM or AM channel with multiple frequencies).
 *
 * The channel's own source (the rover) rotates through the frequencies under control of the channel rotation monitor.
 * At startup, this manager checks for free tuners and, when parallel monitoring is enabled, starts a separate channel
 * for each frequency that fits inside a free tuner's bandwidth.  Those frequencies are locked out of the rover's
 * rotation so the rover only scans what is left over.  The manager also publishes live status to the scanner registry
 * for the scanner panel and relays the panel's hold, skip and lockout controls to the processing chain.
 */
public class ScanGroupManager extends Module implements IDecoderStateEventListener, ISourceEventListener,
    IChannelEventProvider
{
    private static final Logger mLog = LoggerFactory.getLogger(ScanGroupManager.class);

    /**
     * Delay after startup before planning parallel channels, so that the rover's source is settled.
     */
    private static final long PLANNING_DELAY_MS = 500;

    private final Channel mChannel;
    private final SourceConfigTunerMultipleFrequency mSourceConfig;
    private final int mChannelBandwidth;
    private final ScanGroup mScanGroup;
    private final List<ScanEntryStatus> mParallelEntries = new ArrayList<>();
    private final DecoderStateListener mDecoderStateListener = new DecoderStateListener();
    private final SourceEventListener mSourceEventListener = new SourceEventListener();
    private Listener<ChannelEvent> mChannelEventListener;
    private ScheduledFuture<?> mPlanningFuture;
    private volatile boolean mRunning;

    /**
     * Constructs an instance
     * @param channel for the scan list
     * @param sourceConfig with the scan list frequencies and settings
     * @param channelBandwidth of the decoder in hertz
     */
    public ScanGroupManager(Channel channel, SourceConfigTunerMultipleFrequency sourceConfig, int channelBandwidth)
    {
        mChannel = channel;
        mSourceConfig = sourceConfig;
        mChannelBandwidth = channelBandwidth;
        mScanGroup = new ScanGroup(channel, this);

        for(ScanEntry scanEntry: sourceConfig.getAllScanEntries())
        {
            mScanGroup.addEntry(new ScanEntryStatus(mScanGroup, scanEntry.getFrequency(), scanEntry.getLabel(),
                scanEntry.isPriority(), scanEntry.isLockout()));
        }
    }

    /**
     * Live scan group state for this channel
     */
    public ScanGroup getScanGroup()
    {
        return mScanGroup;
    }

    @Override
    public void start()
    {
        mRunning = true;
        ScannerRegistry.getInstance().register(mScanGroup);

        if(mSourceConfig.isScanParallel())
        {
            mPlanningFuture = ThreadPool.SCHEDULED.schedule(() -> {
                try
                {
                    planParallelChannels();
                }
                catch(Throwable t)
                {
                    mLog.error("Error while planning parallel scan channels for [" + mChannel.getName() + "]", t);
                }
            }, PLANNING_DELAY_MS, TimeUnit.MILLISECONDS);
        }
    }

    @Override
    public void stop()
    {
        mRunning = false;

        if(mPlanningFuture != null)
        {
            mPlanningFuture.cancel(false);
            mPlanningFuture = null;
        }

        synchronized(mParallelEntries)
        {
            for(ScanEntryStatus entry: mParallelEntries)
            {
                stopParallelChannel(entry);
            }

            mParallelEntries.clear();
        }

        ScannerRegistry.getInstance().unregister(mScanGroup);
    }

    @Override
    public void reset()
    {
    }

    /**
     * Determines which frequencies can be monitored in parallel on free tuners and starts a channel for each.
     */
    private void planParallelChannels()
    {
        TunerManager tunerManager = ScannerRegistry.getInstance().getTunerManager();

        if(!mRunning || tunerManager == null)
        {
            return;
        }

        List<Long> frequencies = new ArrayList<>();
        List<Long> priorities = new ArrayList<>();

        for(ScanEntryStatus entry: mScanGroup.getEntries())
        {
            if(!entry.isLockedOut())
            {
                frequencies.add(entry.getFrequency());

                if(entry.isPriority())
                {
                    priorities.add(entry.getFrequency());
                }
            }
        }

        long roverFrequency = mScanGroup.getRoverFrequency();
        ScanPlanner.TunerSlot roverSlot = null;
        List<ScanPlanner.TunerSlot> freeSlots = new ArrayList<>();

        for(DiscoveredTuner discoveredTuner: tunerManager.getAvailableTuners())
        {
            if(!discoveredTuner.hasTuner())
            {
                continue;
            }

            TunerController controller = discoveredTuner.getTuner().getTunerController();
            ScanPlanner.TunerSlot slot = new ScanPlanner.TunerSlot(discoveredTuner.getTuner().getPreferredName(),
                controller.getUsableBandwidth(), controller::canTune);

            if(discoveredTuner.getTuner().getChannelSourceManager().getTunerChannelCount() == 0)
            {
                freeSlots.add(slot);
            }
            else if(roverSlot == null && isSourcing(discoveredTuner, roverFrequency))
            {
                roverSlot = slot;
            }
        }

        if(roverSlot != null)
        {
            mScanGroup.setRoverTunerName(roverSlot.name());
        }

        ScanPlanner.Plan plan = ScanPlanner.plan(frequencies, priorities, roverFrequency, roverSlot, freeSlots,
            mChannelBandwidth);

        if(plan.parallel().isEmpty())
        {
            mLog.info("Scan list [" + mChannel.getName() + "] scanning " + frequencies.size() +
                " frequencies - no free tuner bandwidth for parallel monitoring");
            return;
        }

        for(Map.Entry<Long,String> assignment: plan.parallel().entrySet())
        {
            ScanEntryStatus entry = mScanGroup.getEntry(assignment.getKey());

            if(entry != null && mRunning)
            {
                startParallelChannel(entry, assignment.getValue());
            }
        }

        mLog.info("Scan list [" + mChannel.getName() + "] monitoring " + mParallelEntries.size() +
            " frequencies in parallel and scanning " + mScanGroup.getScannedCount());
    }

    /**
     * Indicates if the discovered tuner is sourcing a channel at the frequency
     */
    private static boolean isSourcing(DiscoveredTuner discoveredTuner, long frequency)
    {
        for(TunerChannel tunerChannel: discoveredTuner.getTuner().getChannelSourceManager().getTunerChannels())
        {
            if(tunerChannel.getFrequency() == frequency)
            {
                return true;
            }
        }

        return false;
    }

    /**
     * Starts a channel to monitor the scan entry frequency in parallel.  When the channel starts, the frequency is
     * locked out of the rover's rotation.
     */
    private void startParallelChannel(ScanEntryStatus entry, String tunerName)
    {
        Channel parallel = mChannel.copyOf();
        parallel.setName(mChannel.getName() + " - " + entry.getDisplayName());
        parallel.setAutoStart(false);
        SourceConfigTuner sourceConfigTuner = new SourceConfigTuner();
        sourceConfigTuner.setFrequency(entry.getFrequency());
        sourceConfigTuner.setPreferredTuner(tunerName);
        parallel.setSourceConfiguration(sourceConfigTuner);

        entry.setChildChannel(parallel);
        entry.setTunerName(tunerName);
        ScannerRegistry.getInstance().registerParallelChannel(parallel, entry);

        //The processing chain event bus delivers this request to the channel processing manager on this thread
        getInterModuleEventBus().post(new ChannelStartProcessingRequest(parallel));

        if(entry.isChildRunning())
        {
            getInterModuleEventBus().post(FrequencyLockChangeRequest.lock(entry.getFrequency()));
            entry.setMode(ScanEntryStatus.Mode.PARALLEL);

            synchronized(mParallelEntries)
            {
                mParallelEntries.add(entry);
            }

            //If the rover was sitting on this frequency, move it along
            if(mScanGroup.getRoverFrequency() == entry.getFrequency())
            {
                getInterModuleEventBus().post(new ScanSkipRequest());
            }
        }
        else
        {
            mLog.info("Unable to start parallel channel for [" + entry.getDisplayName() + "] on tuner [" +
                tunerName + "] - frequency will be scanned");
            ScannerRegistry.getInstance().unregisterParallelChannel(parallel);
            entry.setChildChannel(null);
            entry.setTunerName(null);
        }
    }

    /**
     * Stops the parallel channel for the entry, if there is one.
     */
    private void stopParallelChannel(ScanEntryStatus entry)
    {
        Channel parallel = entry.getChildChannel();

        if(parallel != null)
        {
            //Clear the channel first so that the stop notification isn't treated as an unexpected stop
            entry.setChildChannel(null);
            entry.setTunerName(null);
            broadcast(new ChannelEvent(parallel, ChannelEvent.Event.REQUEST_DISABLE));
            ScannerRegistry.getInstance().unregisterParallelChannel(parallel);
            entry.setChildRunning(false);
        }
    }

    /**
     * Notification that a parallel channel stopped.  If we didn't stop it (e.g. the tuner was removed), return the
     * frequency to the rover's scan so it is still monitored.
     */
    void parallelChannelStopped(ScanEntryStatus entry)
    {
        Channel parallel = entry.getChildChannel();

        if(parallel == null || !mRunning)
        {
            return;
        }

        synchronized(mParallelEntries)
        {
            mParallelEntries.remove(entry);
        }

        entry.setChildChannel(null);
        entry.setTunerName(null);
        ScannerRegistry.getInstance().unregisterParallelChannel(parallel);

        if(entry.getMode() == ScanEntryStatus.Mode.PARALLEL)
        {
            entry.setMode(ScanEntryStatus.Mode.SCAN);

            if(hasInterModuleEventBus())
            {
                getInterModuleEventBus().post(FrequencyLockChangeRequest.unlock(entry.getFrequency()));
            }
        }

        mLog.info("Parallel channel for [" + entry.getDisplayName() + "] stopped - frequency returned to the scan");
    }

    /**
     * Holds the rover on its current frequency, or resumes scanning.
     */
    void hold(boolean hold)
    {
        if(hasInterModuleEventBus())
        {
            getInterModuleEventBus().post(new ScanHoldRequest(hold));
        }
    }

    /**
     * Skips the rover to the next frequency.
     */
    void skip()
    {
        if(hasInterModuleEventBus())
        {
            getInterModuleEventBus().post(new ScanSkipRequest());
        }
    }

    /**
     * Locks out a frequency, or restores a locked out frequency to the scan, for this session.  A frequency that
     * was monitored in parallel has its channel stopped when locked out, and joins the scan when restored.
     */
    void setLockout(ScanEntryStatus entry, boolean lockout)
    {
        if(!hasInterModuleEventBus() || entry.isLockedOut() == lockout)
        {
            return;
        }

        if(lockout)
        {
            synchronized(mParallelEntries)
            {
                if(mParallelEntries.remove(entry))
                {
                    stopParallelChannel(entry);
                }
            }

            entry.setMode(ScanEntryStatus.Mode.LOCKOUT);
            getInterModuleEventBus().post(FrequencyLockChangeRequest.lock(entry.getFrequency()));

            if(mScanGroup.getRoverFrequency() == entry.getFrequency())
            {
                getInterModuleEventBus().post(new ScanSkipRequest());
            }
        }
        else
        {
            entry.setMode(ScanEntryStatus.Mode.SCAN);
            getInterModuleEventBus().post(FrequencyLockChangeRequest.unlock(entry.getFrequency()));
        }
    }

    private void broadcast(ChannelEvent channelEvent)
    {
        if(mChannelEventListener != null)
        {
            mChannelEventListener.receive(channelEvent);
        }
    }

    @Override
    public void setChannelEventListener(Listener<ChannelEvent> listener)
    {
        mChannelEventListener = listener;
    }

    @Override
    public void removeChannelEventListener()
    {
        mChannelEventListener = null;
    }

    @Override
    public Listener<DecoderStateEvent> getDecoderStateListener()
    {
        return mDecoderStateListener;
    }

    @Override
    public Listener<SourceEvent> getSourceEventListener()
    {
        return mSourceEventListener;
    }

    /**
     * Tracks call activity on the rover's current frequency.
     */
    private class DecoderStateListener implements Listener<DecoderStateEvent>
    {
        @Override
        public void receive(DecoderStateEvent event)
        {
            if(event.getEvent() == DecoderStateEvent.Event.NOTIFICATION_CHANNEL_STATE && event.getState() == State.CALL)
            {
                ScanEntryStatus entry = mScanGroup.getEntry(mScanGroup.getRoverFrequency());

                if(entry != null)
                {
                    entry.activity();
                }
            }
        }
    }

    /**
     * Tracks the rover's current frequency.
     */
    private class SourceEventListener implements Listener<SourceEvent>
    {
        @Override
        public void receive(SourceEvent event)
        {
            switch(event.getEvent())
            {
                case NOTIFICATION_FREQUENCY_CHANGE:
                case NOTIFICATION_FREQUENCY_ROTATION_SUCCESS:
                    if(event.getValue() != null)
                    {
                        long frequency = event.getValue().longValue();

                        //Ignore tuner center frequency changes - only track frequencies that are in the scan list
                        if(mScanGroup.getEntry(frequency) != null)
                        {
                            mScanGroup.setRoverFrequency(frequency);
                        }
                    }
                    break;
                default:
                    break;
            }
        }
    }
}
