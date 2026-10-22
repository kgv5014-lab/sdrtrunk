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

package io.github.dsheirer.source.tuner.channel.rotation;

import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Tests for the scan frequency sequencing and the scan mode rotation timing.
 */
public class ScanSequenceTest
{
    private static final long A = 155_000_000L;
    private static final long B = 155_500_000L;
    private static final long C = 156_000_000L;
    private static final long D = 460_000_000L;

    /**
     * Runs the sequence the specified number of steps, starting from the current frequency, and returns the visits.
     */
    private static List<Long> run(ScanSequence sequence, long current, int steps)
    {
        List<Long> visits = new ArrayList<>();

        for(int x = 0; x < steps; x++)
        {
            long next = sequence.next(current);
            visits.add(next);

            if(next != 0)
            {
                current = next;
            }
        }

        return visits;
    }

    @Test
    void rotatesInListOrder()
    {
        ScanSequence sequence = new ScanSequence(List.of(A, B, C), null);
        sequence.setCurrent(A);
        assertEquals(List.of(B, C, A, B), run(sequence, A, 4));
    }

    @Test
    void startsFromCurrentFrequency()
    {
        ScanSequence sequence = new ScanSequence(List.of(A, B, C), null);
        sequence.setCurrent(B);
        assertEquals(List.of(C, A, B), run(sequence, B, 3));
    }

    @Test
    void skipsLockedFrequencies()
    {
        ScanSequence sequence = new ScanSequence(List.of(A, B, C, D), null);
        sequence.setCurrent(A);
        sequence.lock(C);
        assertEquals(List.of(B, D, A, B), run(sequence, A, 4));

        sequence.unlock(C);
        assertEquals(List.of(C, D), run(sequence, B, 2));
    }

    @Test
    void returnsZeroWhenNoOtherFrequencyIsAvailable()
    {
        ScanSequence single = new ScanSequence(List.of(A), null);
        assertEquals(0, single.next(A));

        ScanSequence sequence = new ScanSequence(List.of(A, B, C), null);
        sequence.setCurrent(A);
        sequence.lock(B);
        sequence.lock(C);
        assertEquals(0, sequence.next(A));
    }

    @Test
    void interleavesPriorityFrequency()
    {
        ScanSequence sequence = new ScanSequence(List.of(A, B, C, D), List.of(A));
        sequence.setCurrent(A);
        assertEquals(List.of(B, A, C, A, D, A, B), run(sequence, A, 7));
    }

    @Test
    void rotatesBetweenMultiplePriorityFrequencies()
    {
        ScanSequence sequence = new ScanSequence(List.of(A, B, C, D), List.of(A, C));
        sequence.setCurrent(B);
        assertEquals(List.of(A, D, C, B, A, D, C), run(sequence, B, 7));
    }

    @Test
    void lockedPriorityFrequencyIsSkipped()
    {
        ScanSequence sequence = new ScanSequence(List.of(A, B, C), List.of(A));
        sequence.setCurrent(B);
        sequence.lock(A);
        assertEquals(List.of(C, B, C), run(sequence, B, 3));
    }

    @Test
    void onlyPriorityFrequenciesRemain()
    {
        ScanSequence sequence = new ScanSequence(List.of(A, B, C), List.of(A, B));
        sequence.setCurrent(A);
        sequence.lock(C);
        assertEquals(List.of(B, A, B), run(sequence, A, 3));
    }

    @Test
    void priorityNotInListIsIgnored()
    {
        ScanSequence sequence = new ScanSequence(List.of(A, B), List.of(D));
        sequence.setCurrent(A);
        assertEquals(List.of(B, A), run(sequence, A, 2));
    }

    @Test
    void rotationTimingUsesDwellWhenIdleAndHangTimeAfterActivity()
    {
        long dwell = 500;
        long hang = 3000;

        //No activity: rotate after the dwell time
        assertFalse(ChannelRotationMonitor.isRotationDue(1400, 1000, false, dwell, hang, true));
        assertTrue(ChannelRotationMonitor.isRotationDue(1600, 1000, false, dwell, hang, true));

        //Activity: wait for the hang time after the last activity
        assertFalse(ChannelRotationMonitor.isRotationDue(1600, 1000, true, dwell, hang, true));
        assertFalse(ChannelRotationMonitor.isRotationDue(3900, 1000, true, dwell, hang, true));
        assertTrue(ChannelRotationMonitor.isRotationDue(4100, 1000, true, dwell, hang, true));

        //Hang time shorter than the dwell time never rotates sooner than the dwell time
        assertFalse(ChannelRotationMonitor.isRotationDue(1300, 1000, true, dwell, 100, true));

        //Control channel (non-scan) monitors ignore the hang time
        assertTrue(ChannelRotationMonitor.isRotationDue(1600, 1000, true, dwell, hang, false));
    }
}
