/*
 * *****************************************************************************
 * Copyright (C) 2014-2024 Dennis Sheirer
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

package io.github.dsheirer.source.tuner.channel.rotation;

import com.google.common.eventbus.Subscribe;
import io.github.dsheirer.channel.state.DecoderStateEvent;
import io.github.dsheirer.channel.state.IDecoderStateEventListener;
import io.github.dsheirer.channel.state.State;
import io.github.dsheirer.module.Module;
import io.github.dsheirer.preference.PreferenceType;
import io.github.dsheirer.preference.UserPreferences;
import io.github.dsheirer.sample.Listener;
import io.github.dsheirer.source.ISourceEventProvider;
import io.github.dsheirer.source.SourceEvent;
import io.github.dsheirer.util.ThreadPool;
import java.util.Collection;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Monitors channel state to detect when a channel is not in an identified active state and issues a request to rotate
 * to the next channel frequency in the list.  This class depends on the ChannelState providing a continuous
 * stream of channel state notification events in the form of DecoderStateEvents.
 */
public class ChannelRotationMonitor extends Module implements ISourceEventProvider, IDecoderStateEventListener,
        Listener<DecoderStateEvent>
{
    public static final int CHANNEL_ROTATION_DELAY_MINIMUM = 200;
    public static final int CHANNEL_ROTATION_DELAY_DEFAULT = 1000;
    public static final int CHANNEL_ROTATION_DELAY_MAXIMUM = 2000;
    public static final int SCAN_HANG_TIME_MINIMUM = 0;
    public static final int SCAN_HANG_TIME_DEFAULT = 2000;
    public static final int SCAN_HANG_TIME_MAXIMUM = 10000;

    private final static Logger mLog = LoggerFactory.getLogger(ChannelRotationMonitor.class);
    private UserPreferences mUserPreferences;
    private Collection<State> mActiveStates;
    private ScheduledFuture<?> mScheduledFuture;
    private Listener<SourceEvent> mSourceEventListener;
    private long mRotationDelay;
    private long mLastActiveTimestamp = System.currentTimeMillis();
    private boolean mEnabled = true;
    private boolean mScanMode = false;
    private long mHangTime;
    private boolean mActivityDetected = false;
    private boolean mHold = false;

    /**
     * Constructs a channel rotation monitor that uses the specified rotation delay.
     * @param activeStates to monitor
     * @param rotationDelay specifies how long to remain on each frequency before rotating (in milliseconds).
     */
    public ChannelRotationMonitor(Collection<State> activeStates, long rotationDelay, UserPreferences userPreferences)
    {
        mActiveStates = activeStates;
        mRotationDelay = rotationDelay;
        mUserPreferences = userPreferences;

        if(mRotationDelay < CHANNEL_ROTATION_DELAY_MINIMUM)
        {
            mRotationDelay = CHANNEL_ROTATION_DELAY_MINIMUM;
        }
        else if(mRotationDelay > CHANNEL_ROTATION_DELAY_MAXIMUM)
        {
            mRotationDelay = CHANNEL_ROTATION_DELAY_MAXIMUM;
        }
    }

    /**
     * Constructs a channel rotation monitor for a conventional scan list.  The monitor rotates to the next frequency
     * when no activity is detected for the dwell time, or when the hang time has elapsed after the last detected
     * activity.  Scan mode monitors ignore the global multi-frequency rotation delay preference.
     *
     * @param activeStates to monitor
     * @param dwellTime to remain on each inactive frequency before rotating (in milliseconds).
     * @param hangTime to remain on a frequency after activity ends before rotating (in milliseconds).
     * @param userPreferences reference
     */
    public ChannelRotationMonitor(Collection<State> activeStates, long dwellTime, long hangTime,
                                  UserPreferences userPreferences)
    {
        this(activeStates, dwellTime, userPreferences);
        mScanMode = true;
        mHangTime = Math.max(SCAN_HANG_TIME_MINIMUM, Math.min(SCAN_HANG_TIME_MAXIMUM, hangTime));
    }

    /**
     * Indicates if this monitor is configured for conventional scanning
     */
    public boolean isScanMode()
    {
        return mScanMode;
    }

    /**
     * Registers the external listener to receive frequency rotation requests from this module
     */
    @Override
    public void setSourceEventListener(Listener<SourceEvent> listener)
    {
        mSourceEventListener = listener;
    }

    /**
     * Unregisters the external listener from receiving frequency rotation requests.
     */
    @Override
    public void removeSourceEventListener()
    {
        mSourceEventListener = null;
    }

    @Override
    public Listener<DecoderStateEvent> getDecoderStateListener()
    {
        return this;
    }

    @Override
    public void receive(DecoderStateEvent event)
    {
        switch(event.getEvent())
        {
            case NOTIFICATION_CHANNEL_STATE:
                if(mActiveStates.contains(event.getState()))
                {
                    mLastActiveTimestamp = System.currentTimeMillis();
                    mActivityDetected = true;
                }
                break;
        }
    }

    /**
     * Processes a request to disable this monitor instance.
     * @param request to disable
     */
    @Subscribe
    public void disable(DisableChannelRotationMonitorRequest request)
    {
        mEnabled = false;
        stop();
    }

    /**
     * Processes a request to add an active state to the list of monitored active states.
     * @param request to add
     */
    @Subscribe
    public void addActiveState(AddChannelRotationActiveStateRequest request)
    {
        if(!mActiveStates.contains(request.getState()))
        {
            mActiveStates.add(request.getState());
        }
    }

    /**
     * Processes a request to hold (pause) or resume scanning on the current frequency.
     * @param request to hold or resume
     */
    @Subscribe
    public void hold(ScanHoldRequest request)
    {
        mHold = request.isHold();

        if(!mHold)
        {
            //Restart the dwell timer so that we don't immediately rotate away from the frequency the user was holding
            mLastActiveTimestamp = System.currentTimeMillis();
        }
    }

    /**
     * Processes a request to skip to the next frequency immediately.  This also releases a hold.
     * @param request to skip
     */
    @Subscribe
    public void skip(ScanSkipRequest request)
    {
        mHold = false;

        if(mEnabled)
        {
            //Requests can arrive on the GUI thread, so dispatch the rotation to the thread pool
            ThreadPool.SCHEDULED.submit(() -> {
                try
                {
                    rotate();
                }
                catch(Throwable t)
                {
                    mLog.warn("Error while skipping to next frequency", t);
                }
            });
        }
    }

    /**
     * Indicates if the monitor is holding on the current frequency
     */
    public boolean isHold()
    {
        return mHold;
    }

    /**
     * Indicates if a frequency rotation should occur.
     *
     * @param now current timestamp
     * @param lastActiveTimestamp timestamp of either the last activity or the last rotation, whichever is later
     * @param activityDetected true if activity was detected since the last rotation
     * @param dwellTime to remain on an inactive frequency
     * @param hangTime to remain on a frequency after activity ends (scan mode only)
     * @param scanMode true for conventional scanning
     * @return true if rotation is due
     */
    static boolean isRotationDue(long now, long lastActiveTimestamp, boolean activityDetected, long dwellTime,
                                 long hangTime, boolean scanMode)
    {
        long delay = (scanMode && activityDetected) ? Math.max(hangTime, dwellTime) : dwellTime;
        return (lastActiveTimestamp + delay) < now;
    }

    /**
     * Checks the current active state and when inactive for longer than the specified delay, issues a
     * channel frequency rotation request
     */
    private void checkState()
    {
        if(mEnabled && !mHold && isRotationDue(System.currentTimeMillis(), mLastActiveTimestamp, mActivityDetected,
            mRotationDelay, mHangTime, mScanMode))
        {
            rotate();
        }
    }

    /**
     * Issues a frequency rotation request and resets the dwell timer.
     */
    private void rotate()
    {
        if(mSourceEventListener != null)
        {
            mSourceEventListener.receive(SourceEvent.frequencyRotationRequest());
            mLastActiveTimestamp = System.currentTimeMillis();
            mActivityDetected = false;
        }
    }

    /**
     * Guava event bus method to receive notifications when user preferences are updated.  This allows us to
     * dynamically adjust rotation delay if/when the user changes the rotation delay value.
     */
    @Subscribe
    public void preferenceUpdated(PreferenceType preferenceType)
    {
        if(preferenceType == PreferenceType.MULTI_FREQUENCY && !mScanMode)
        {
            mRotationDelay = mUserPreferences.getChannelMultiFrequencyPreference().getRotationDelay();
        }
    }

    @Override
    public void reset()
    {
    }

    @Override
    public void start()
    {
        if(mScheduledFuture == null)
        {
            Runnable runnable = () -> {
                try
                {
                    checkState();
                }
                catch(Throwable t)
                {
                    mLog.warn("Error while checking state", t);
                }
            };

            mScheduledFuture = ThreadPool.SCHEDULED.scheduleAtFixedRate(runnable, mRotationDelay * 2,
                mRotationDelay / 2, TimeUnit.MILLISECONDS);
        }
    }

    @Override
    public void stop()
    {
        if(mScheduledFuture != null)
        {
            mScheduledFuture.cancel(true);
            mScheduledFuture = null;
        }
    }
}

