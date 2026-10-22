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

import java.util.Collections;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Tests for planning parallel monitoring of a scan list across tuners.
 */
public class ScanPlannerTest
{
    private static final int USABLE = 2_000_000; //Roughly an RTL-SDR at 2.4 MSps
    private static final int BW = 12_500;

    private static ScanPlanner.TunerSlot slot(String name)
    {
        return new ScanPlanner.TunerSlot(name, USABLE, frequency -> frequency >= 24_000_000L && frequency <= 1_766_000_000L);
    }

    private static long mhz(double mhz)
    {
        return Math.round(mhz * 1E6);
    }

    @Test
    void clustersByUsableBandwidth()
    {
        List<List<Long>> clusters = ScanPlanner.cluster(List.of(mhz(155.0), mhz(460.1), mhz(156.9), mhz(155.5),
            mhz(460.5)), USABLE, BW);

        assertEquals(List.of(List.of(mhz(155.0), mhz(155.5), mhz(156.9)), List.of(mhz(460.1), mhz(460.5))), clusters);
    }

    @Test
    void clusterSplitsWhenSpanExceedsBandwidth()
    {
        //2.0 MHz apart plus channel bandwidth does not fit inside 2.0 MHz usable
        List<List<Long>> clusters = ScanPlanner.cluster(List.of(mhz(155.0), mhz(157.0)), USABLE, BW);
        assertEquals(2, clusters.size());
    }

    @Test
    void singleTunerAllInWindowParksRover()
    {
        List<Long> frequencies = List.of(mhz(155.0), mhz(155.5), mhz(156.0));
        ScanPlanner.Plan plan = ScanPlanner.plan(frequencies, Collections.emptyList(), mhz(155.0), slot("rover"),
            Collections.emptyList(), BW);

        assertEquals(Map.of(mhz(155.5), "rover", mhz(156.0), "rover"), plan.parallel());
        assertEquals(List.of(mhz(155.0)), plan.scanned());
        assertTrue(plan.isFullyParallel());
    }

    @Test
    void singleTunerSpreadOutListScans()
    {
        List<Long> frequencies = List.of(mhz(155.0), mhz(460.0), mhz(855.0));
        ScanPlanner.Plan plan = ScanPlanner.plan(frequencies, Collections.emptyList(), mhz(155.0), slot("rover"),
            Collections.emptyList(), BW);

        assertTrue(plan.parallel().isEmpty());
        assertEquals(frequencies, plan.scanned());
        assertFalse(plan.isFullyParallel());
    }

    @Test
    void enoughTunersCoversEverything()
    {
        List<Long> frequencies = List.of(mhz(155.0), mhz(155.5), mhz(460.0), mhz(460.2), mhz(855.0));
        ScanPlanner.Plan plan = ScanPlanner.plan(frequencies, Collections.emptyList(), mhz(155.0), slot("rover"),
            List.of(slot("t2"), slot("t3")), BW);

        assertEquals("rover", plan.parallel().get(mhz(155.5)));
        assertEquals("t2", plan.parallel().get(mhz(460.0)));
        assertEquals("t2", plan.parallel().get(mhz(460.2)));
        assertEquals("t3", plan.parallel().get(mhz(855.0)));
        assertEquals(List.of(mhz(155.0)), plan.scanned());
    }

    @Test
    void notEnoughTunersScansTheRest()
    {
        //Four clusters, rover plus one free tuner: the largest other cluster goes parallel, the rest are scanned
        List<Long> frequencies = List.of(mhz(155.0), mhz(460.0), mhz(460.1), mhz(460.2), mhz(855.0), mhz(162.55));
        ScanPlanner.Plan plan = ScanPlanner.plan(frequencies, Collections.emptyList(), mhz(155.0), slot("rover"),
            List.of(slot("t2")), BW);

        assertEquals(Map.of(mhz(460.0), "t2", mhz(460.1), "t2", mhz(460.2), "t2"), plan.parallel());
        assertEquals(List.of(mhz(155.0), mhz(855.0), mhz(162.55)), plan.scanned());
    }

    @Test
    void priorityClusterGoesParallelFirst()
    {
        List<Long> frequencies = List.of(mhz(155.0), mhz(460.0), mhz(460.1), mhz(460.2), mhz(855.0));
        ScanPlanner.Plan plan = ScanPlanner.plan(frequencies, List.of(mhz(855.0)), mhz(155.0), slot("rover"),
            List.of(slot("t2")), BW);

        assertEquals(Map.of(mhz(855.0), "t2"), plan.parallel());
        assertEquals(List.of(mhz(155.0), mhz(460.0), mhz(460.1), mhz(460.2)), plan.scanned());
    }

    @Test
    void tunerThatCannotTuneIsNotUsed()
    {
        ScanPlanner.TunerSlot vhfOnly = new ScanPlanner.TunerSlot("vhf", USABLE, frequency -> frequency < 300_000_000L);
        List<Long> frequencies = List.of(mhz(155.0), mhz(460.0));
        ScanPlanner.Plan plan = ScanPlanner.plan(frequencies, Collections.emptyList(), mhz(155.0), slot("rover"),
            List.of(vhfOnly), BW);

        assertTrue(plan.parallel().isEmpty());
        assertEquals(frequencies, plan.scanned());
    }

    @Test
    void unknownRoverTunerStillUsesFreeTuners()
    {
        List<Long> frequencies = List.of(mhz(155.0), mhz(155.5), mhz(460.0));
        ScanPlanner.Plan plan = ScanPlanner.plan(frequencies, Collections.emptyList(), mhz(155.0), null,
            List.of(slot("t2")), BW);

        assertEquals(Map.of(mhz(460.0), "t2"), plan.parallel());
        assertEquals(List.of(mhz(155.0), mhz(155.5)), plan.scanned());
    }

    @Test
    void noTunersMeansScanEverything()
    {
        List<Long> frequencies = List.of(mhz(155.0), mhz(155.5));
        ScanPlanner.Plan plan = ScanPlanner.plan(frequencies, Collections.emptyList(), mhz(155.0), null,
            Collections.emptyList(), BW);

        assertTrue(plan.parallel().isEmpty());
        assertEquals(frequencies, plan.scanned());
    }
}
