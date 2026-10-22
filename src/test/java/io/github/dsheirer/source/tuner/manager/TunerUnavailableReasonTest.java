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

package io.github.dsheirer.source.tuner.manager;

import io.github.dsheirer.source.tuner.channel.TunerChannel;
import io.github.dsheirer.source.tuner.manager.TunerUnavailableReason.TunerSnapshot;
import java.util.List;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Tests the explanation shown when no tuner can source a channel
 */
public class TunerUnavailableReasonTest
{
    private static final TunerChannel CHANNEL = new TunerChannel(155_475_000L, 12_500);

    @Test
    void noTuners()
    {
        String reason = TunerUnavailableReason.describe(CHANNEL, null, List.of());
        assertEquals("No tuner can receive 155.475 MHz.\nNo tuners were found. Plug in an SDR, or check the " +
            "Tuners tab in User Preferences.", reason);
    }

    @Test
    void outOfRange()
    {
        TunerSnapshot tuner = new TunerSnapshot("Airspy", null, 24_000_000L, 150_000_000L, 0, 2_400_000, 0, 0, 0);
        String reason = TunerUnavailableReason.describe(CHANNEL, null, List.of(tuner));
        assertTrue(reason.contains("Airspy: out of range, it tunes 24.0 to 150.0 MHz"), reason);
    }

    @Test
    void busyWithOtherChannels()
    {
        TunerSnapshot tuner = new TunerSnapshot("RTL-2832 #1", null, 24_000_000L, 1_766_000_000L, 851_000_000L,
            2_000_000, 3, 850_100_000L, 851_900_000L);
        TunerChannel channel = new TunerChannel(853_500_000L, 12_500);
        String reason = TunerUnavailableReason.describe(channel, null, List.of(tuner));
        assertTrue(reason.contains("RTL-2832 #1: busy, tuned to 851.0 MHz for 3 channels (850.1 to 851.9 MHz). " +
            "Adding this channel needs 3.4063 MHz of bandwidth but the tuner has 2.0 MHz usable."), reason);
    }

    @Test
    void disabledAndMissingPreferredTuner()
    {
        TunerSnapshot tuner = TunerSnapshot.unusable("USB Tuner - 1-2", "Disabled");
        String reason = TunerUnavailableReason.describe(CHANNEL, "SMArt #2", List.of(tuner));
        assertTrue(reason.contains("The preferred tuner [SMArt #2] isn't connected."), reason);
        assertTrue(reason.contains("USB Tuner - 1-2: not usable (Disabled)"), reason);
    }
    @Test
    void coverageLevels()
    {
        TunerSnapshot idle = new TunerSnapshot("RTL #1", null, 24_000_000L, 1_766_000_000L, 100_000_000L, 2_000_000,
            0, 0, 0);
        TunerSnapshot busy = new TunerSnapshot("RTL #2", null, 24_000_000L, 1_766_000_000L, 851_000_000L, 2_000_000,
            1, 851_000_000L, 851_012_500L);
        TunerSnapshot small = new TunerSnapshot("HF", null, 500_000L, 30_000_000L, 0, 2_000_000, 0, 0, 0);

        TunerUnavailableReason.Coverage now = TunerUnavailableReason.coverage(155_475_000L, List.of(idle, busy));
        assertEquals(TunerUnavailableReason.Coverage.Level.NOW, now.level());
        assertEquals("RTL #1 can receive it now | RTL #2 in range, busy at 851.0 MHz", now.description());

        assertEquals(TunerUnavailableReason.Coverage.Level.WHEN_FREE,
            TunerUnavailableReason.coverage(155_475_000L, List.of(busy, small)).level());
        assertEquals(TunerUnavailableReason.Coverage.Level.NOW,
            TunerUnavailableReason.coverage(851_500_000L, List.of(busy)).level());
        assertEquals(TunerUnavailableReason.Coverage.Level.NONE,
            TunerUnavailableReason.coverage(155_475_000L, List.of(small)).level());
        assertEquals(TunerUnavailableReason.Coverage.Level.NONE,
            TunerUnavailableReason.coverage(155_475_000L, List.of()).level());
    }
}
