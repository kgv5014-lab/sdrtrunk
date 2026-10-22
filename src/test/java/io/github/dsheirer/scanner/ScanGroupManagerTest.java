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

import com.google.common.eventbus.EventBus;
import com.google.common.eventbus.Subscribe;
import io.github.dsheirer.channel.state.DecoderStateEvent;
import io.github.dsheirer.channel.state.State;
import io.github.dsheirer.controller.channel.Channel;
import io.github.dsheirer.module.decode.nbfm.DecodeConfigNBFM;
import io.github.dsheirer.source.SourceEvent;
import io.github.dsheirer.source.config.ScanEntry;
import io.github.dsheirer.source.config.SourceConfigTunerMultipleFrequency;
import io.github.dsheirer.source.tuner.channel.rotation.ChannelRotationMonitor;
import io.github.dsheirer.source.tuner.channel.rotation.FrequencyLockChangeRequest;
import io.github.dsheirer.source.tuner.channel.rotation.ScanHoldRequest;
import io.github.dsheirer.source.tuner.channel.rotation.ScanSkipRequest;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Tests the scan group controls used by the scanner panel, without tuners.
 */
public class ScanGroupManagerTest
{
    private static final long A = 154_430_000L;
    private static final long B = 154_190_000L;
    private static final long C = 155_475_000L;

    /**
     * Captures the requests that the scan group manager posts to the processing chain event bus
     */
    public static class Capture
    {
        final List<Object> mRequests = new ArrayList<>();

        @Subscribe
        public void lock(FrequencyLockChangeRequest request)
        {
            mRequests.add((request.isLockRequest() ? "lock " : "unlock ") + request.getFrequency());
        }

        @Subscribe
        public void skip(ScanSkipRequest request)
        {
            mRequests.add("skip");
        }

        @Subscribe
        public void hold(ScanHoldRequest request)
        {
            mRequests.add(request.isHold() ? "hold" : "resume");
        }
    }

    private static ScanGroupManager manager(EventBus eventBus)
    {
        Channel channel = new Channel("Fire");
        channel.setDecodeConfiguration(new DecodeConfigNBFM());
        SourceConfigTunerMultipleFrequency config = new SourceConfigTunerMultipleFrequency();
        config.setFrequencies(List.of(A, B, C));
        config.setScanParallel(false);
        config.setScanEntries(List.of(new ScanEntry(A, "Dispatch", false, true), new ScanEntry(C, null, true, false)));
        channel.setSourceConfiguration(config);

        ScanGroupManager manager = new ScanGroupManager(channel, config, 12_500);
        manager.setInterModuleEventBus(eventBus);
        return manager;
    }

    @Test
    void entriesReflectConfiguration()
    {
        ScanGroup group = manager(new EventBus()).getScanGroup();

        assertEquals(3, group.getEntries().size());
        assertEquals("Dispatch", group.getEntry(A).getDisplayName());
        assertTrue(group.getEntry(A).isPriority());
        assertEquals(ScanEntryStatus.Mode.SCAN, group.getEntry(B).getMode());
        assertEquals(ScanEntryStatus.Mode.LOCKOUT, group.getEntry(C).getMode());
        assertEquals(2, group.getScannedCount());
    }

    @Test
    void registersWhileRunning()
    {
        ScanGroupManager manager = manager(new EventBus());
        manager.start();
        assertTrue(ScannerRegistry.getInstance().getScanGroups().contains(manager.getScanGroup()));
        manager.stop();
        assertFalse(ScannerRegistry.getInstance().getScanGroups().contains(manager.getScanGroup()));
    }

    @Test
    void tracksRoverFrequencyAndActivity()
    {
        ScanGroupManager manager = manager(new EventBus());
        ScanGroup group = manager.getScanGroup();

        manager.getSourceEventListener().receive(SourceEvent.frequencyChange(null, B));
        assertEquals(B, group.getRoverFrequency());
        assertTrue(group.getEntry(B).isTuned());

        //Frequencies that aren't in the scan list (e.g. tuner center frequency) are ignored
        manager.getSourceEventListener().receive(SourceEvent.frequencyChange(null, 155_000_000L));
        assertEquals(B, group.getRoverFrequency());

        assertFalse(group.getEntry(B).isReceiving());
        manager.getDecoderStateListener().receive(new DecoderStateEvent(this,
            DecoderStateEvent.Event.NOTIFICATION_CHANNEL_STATE, State.CALL));
        assertTrue(group.getEntry(B).isReceiving());
        assertTrue(group.getEntry(B).getLastActivity() > 0);
        assertFalse(group.getEntry(A).isReceiving());
    }

    @Test
    void lockoutLocksAndSkipsTheCurrentFrequency()
    {
        EventBus eventBus = new EventBus();
        Capture capture = new Capture();
        eventBus.register(capture);
        ScanGroupManager manager = manager(eventBus);
        ScanGroup group = manager.getScanGroup();
        manager.getSourceEventListener().receive(SourceEvent.frequencyChange(null, B));

        group.setLockout(group.getEntry(B), true);
        assertEquals(List.of("lock " + B, "skip"), capture.mRequests);
        assertTrue(group.getEntry(B).isLockedOut());

        capture.mRequests.clear();
        group.setLockout(group.getEntry(C), false);
        assertEquals(List.of("unlock " + C), capture.mRequests);
        assertEquals(ScanEntryStatus.Mode.SCAN, group.getEntry(C).getMode());

        //Locking out a frequency the rover isn't on doesn't skip
        capture.mRequests.clear();
        group.setLockout(group.getEntry(A), true);
        assertEquals(List.of("lock " + A), capture.mRequests);
    }

    @Test
    void holdAndSkip()
    {
        EventBus eventBus = new EventBus();
        Capture capture = new Capture();
        eventBus.register(capture);
        ScanGroup group = manager(eventBus).getScanGroup();

        group.setHold(true);
        assertTrue(group.isHold());
        group.skip();
        assertFalse(group.isHold());
        assertEquals(List.of("hold", "skip"), capture.mRequests);
    }

    @Test
    void rotationMonitorHoldsAndSkips() throws Exception
    {
        EventBus eventBus = new EventBus();
        List<State> activeStates = new ArrayList<>(List.of(State.CALL));
        ChannelRotationMonitor monitor = new ChannelRotationMonitor(activeStates, 200, 1000, null);
        monitor.setInterModuleEventBus(eventBus);

        CountDownLatch rotated = new CountDownLatch(1);
        monitor.setSourceEventListener(event -> {
            if(event.getEvent() == SourceEvent.Event.REQUEST_FREQUENCY_ROTATION)
            {
                rotated.countDown();
            }
        });

        assertTrue(monitor.isScanMode());
        eventBus.post(new ScanHoldRequest(true));
        assertTrue(monitor.isHold());

        //Skip releases the hold and rotates right away
        eventBus.post(new ScanSkipRequest());
        assertFalse(monitor.isHold());
        assertTrue(rotated.await(2, TimeUnit.SECONDS));
    }
}
