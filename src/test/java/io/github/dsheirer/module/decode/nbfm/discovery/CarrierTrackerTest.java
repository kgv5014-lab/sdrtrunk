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

import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.Test;

/**
 * Carrier tracking: attack, release, raster snapping, birdies and history, driven by synthetic detections every 25 ms.
 */
public class CarrierTrackerTest
{
    private static final int UPDATE_MS = 25;

    private final DiscoveryConfiguration mConfiguration = new DiscoveryConfiguration();
    private final CarrierTracker mTracker = new CarrierTracker(mConfiguration);
    private final List<DiscoveryEvent> mEvents = new ArrayList<>();
    private long mTime = 0;

    public CarrierTrackerTest()
    {
        mTracker.setListener(mEvents::add);
    }

    /**
     * Runs updates for the duration with the given detections present in every update.
     */
    private void run(int durationMs, SpectralPeak... peaks)
    {
        for(int elapsed = 0; elapsed < durationMs; elapsed += UPDATE_MS)
        {
            mTime += UPDATE_MS;
            mTracker.update(List.of(peaks), mTime);
        }
    }

    private static SpectralPeak voice(double frequency)
    {
        return new SpectralPeak(frequency, 5_000, -40, 20);
    }

    private List<DiscoveryEvent.Type> types()
    {
        return mEvents.stream().map(DiscoveryEvent::type).toList();
    }

    @Test
    void transmissionStartsAfterAttackAndEndsAfterRelease()
    {
        run(200, voice(155_752_700));
        assertTrue(mEvents.isEmpty(), "Should not start before the attack time");

        run(100, voice(155_752_700));
        assertEquals(List.of(DiscoveryEvent.Type.STARTED), types());
        assertEquals(155_752_500, mEvents.getFirst().carrier().getChannelFrequency());
        assertFalse(mEvents.getFirst().carrier().isOffRaster());

        run(5_000, voice(155_752_700));
        run(500);
        assertEquals(1, mEvents.size(), "Should not end before the release time");

        run(500);
        assertEquals(List.of(DiscoveryEvent.Type.STARTED, DiscoveryEvent.Type.ENDED), types());

        DiscoveredFrequency history = mTracker.getHistory().iterator().next();
        assertEquals(155_752_500, history.getFrequency());
        assertEquals(1, history.getTransmissionCount());
        assertEquals(5_300, history.getTotalAirtimeMs(), UPDATE_MS);
    }

    @Test
    void shortBurstsAreIgnored()
    {
        for(int x = 0; x < 10; x++)
        {
            run(100, voice(154_430_000));
            run(200);
        }

        assertTrue(mEvents.isEmpty(), "Bursts shorter than the attack time should be ignored: " + mEvents);
        assertTrue(mTracker.getCarriers().isEmpty());
    }

    @Test
    void fadesShorterThanReleaseTimeDoNotSplitATransmission()
    {
        run(1_000, voice(155_122_500));
        run(400);
        run(1_000, voice(155_122_500));
        run(1_000);

        assertEquals(List.of(DiscoveryEvent.Type.STARTED, DiscoveryEvent.Type.ENDED), types());
    }

    @Test
    void simultaneousCarriersAreTrackedSeparately()
    {
        run(1_000, voice(155_745_000), voice(155_760_000), voice(154_430_000));

        assertEquals(3, mEvents.size());
        assertEquals(3, mTracker.getCarriers().size());
    }

    @Test
    void wanderingDetectionsStayOnOneCarrier()
    {
        //FM modulation moves the measured centre around by several hundred Hertz
        for(int x = 0; x < 40; x++)
        {
            run(UPDATE_MS, voice(155_752_500 + (x % 2 == 0 ? 700 : -700)));
        }

        assertEquals(1, mTracker.getCarriers().size());
        assertEquals(155_752_500, mTracker.getCarriers().getFirst().getMeasuredFrequency(), 100);
    }

    @Test
    void offRasterCarriersAreFlagged()
    {
        //UHF 6.25 kHz raster: 460.1405 MHz is 3 kHz from the nearest channel
        run(500, voice(460_140_520));

        TrackedCarrier carrier = mEvents.getFirst().carrier();
        assertTrue(carrier.isOffRaster());
        assertEquals(460_140_500, carrier.getChannelFrequency());
    }

    @Test
    void narrowAlwaysOnCarrierIsFlaggedAsBirdieAndIgnored()
    {
        mConfiguration.setBirdieTimeMs(10_000);
        SpectralPeak birdie = new SpectralPeak(156_000_000, 1_200, -30, 30);

        run(11_000, birdie);
        assertEquals(List.of(DiscoveryEvent.Type.STARTED, DiscoveryEvent.Type.BIRDIE), types());
        assertTrue(mTracker.getIgnored().contains(156_000_000L));

        run(2_000, birdie);
        assertEquals(2, mEvents.size(), "Birdie frequency should be ignored from now on");
        assertTrue(mTracker.getHistory().iterator().next().isBirdie());
    }

    @Test
    void longVoiceTransmissionIsNotABirdie()
    {
        mConfiguration.setBirdieTimeMs(10_000);
        run(11_000, voice(156_000_000));

        assertEquals(List.of(DiscoveryEvent.Type.STARTED), types());
    }

    @Test
    void ignoredFrequenciesAreNotTracked()
    {
        mTracker.ignore(155_752_500);
        run(1_000, voice(155_752_500));

        assertTrue(mEvents.isEmpty());
    }

    @Test
    void clearEndsActiveCarriers()
    {
        run(1_000, voice(155_752_500));
        mTracker.clear(mTime);

        assertEquals(List.of(DiscoveryEvent.Type.STARTED, DiscoveryEvent.Type.ENDED), types());
        assertTrue(mTracker.getCarriers().isEmpty());
    }

    @Test
    void channelRasterSnapsToBandDefaults()
    {
        assertEquals(155_752_500, ChannelRaster.forFrequency(155_752_400).snap(155_752_400));
        assertEquals(151_122_500, ChannelRaster.forFrequency(151_123_000).snap(151_123_000));
        assertEquals(460_137_500, ChannelRaster.forFrequency(460_138_000).snap(460_138_000));
        assertEquals(39_500_000, ChannelRaster.forFrequency(39_501_000).snap(39_501_000));
        assertEquals(500, ChannelRaster.VHF_HIGH.error(155_753_000), 0.001);
    }
}
