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

package io.github.dsheirer.dsp.squelch;

import java.util.Random;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

public class AdaptiveSquelchTest
{
    //1 kHz sample rate with the clock moving 1 ms per sample: power is reported every 0.5 seconds
    private static final int SAMPLE_RATE = 1000;

    private static float fromDb(double db)
    {
        return (float)Math.pow(10.0, db / 10.0);
    }

    private static void run(AdaptiveSquelch squelch, long[] clock, double powerDb, double seconds, Random random)
    {
        for(int x = 0; x < seconds * SAMPLE_RATE; x++)
        {
            clock[0]++;
            squelch.process(fromDb(powerDb) * (0.5f + random.nextFloat()));
        }
    }

    private static AdaptiveSquelch squelch(long[] clock, boolean autoTrack)
    {
        AdaptiveSquelch squelch = new AdaptiveSquelch(0.1f, -78.0f, autoTrack, () -> clock[0]);
        squelch.setSampleRate(SAMPLE_RATE);
        return squelch;
    }

    @Test
    public void thresholdBelowTheNoiseIsRaisedAboveIt()
    {
        long[] clock = {0};
        Random random = new Random(1);
        AdaptiveSquelch squelch = squelch(clock, true);

        //Noise at -68 dB, 10 dB above the threshold: the squelch is open on noise
        run(squelch, clock, -68, 10, random);
        assertTrue(squelch.isUnmuted());

        run(squelch, clock, -68, 25, random);
        assertTrue(squelch.isMuted(), "squelch should close once it has been open on noise for 30 seconds");
        assertTrue(squelch.getSquelchThreshold() > -68.0f);

        //A transmission well above the noise still opens it
        run(squelch, clock, -50, 2, random);
        assertTrue(squelch.isUnmuted());
        run(squelch, clock, -68, 2, random);
        assertTrue(squelch.isMuted());
    }

    @Test
    public void thresholdIsLeftAloneWithoutAutoTrack()
    {
        long[] clock = {0};
        AdaptiveSquelch squelch = squelch(clock, false);
        run(squelch, clock, -68, 60, new Random(2));
        assertTrue(squelch.isUnmuted());
        assertFalse(squelch.getSquelchThreshold() > -77.0f);
    }
}
