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

import java.util.List;
import java.util.Map;
import java.util.Set;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Smart Fire Scan: reading the transcriber's toned-out departments.
 */
public class TonedOutFocusTest
{
    @Test
    void addressIsNextToThePageAddress()
    {
        assertEquals("http://localhost:3000/api/scan-focus", TonedOutFocus.focusUrl("http://localhost:3000/api/page"));
        assertEquals("http://localhost:3000/api/scan-focus", TonedOutFocus.focusUrl(null));
        assertEquals("http://pc:3000/api/scan-focus", TonedOutFocus.focusUrl("http://pc:3000/"));
    }

    @Test
    void readsFrequenciesAndHoldTimes()
    {
        String json = "{\"hold_minutes\": 15, \"frequencies\": [" +
            "{\"frequency\": 453300000, \"name\": \"Forks Fire\", \"station\": 25, \"until\": 1800000900.5}," +
            "{\"frequency\": 460375000, \"until\": 1800000100}]}";
        assertEquals(Map.of(453_300_000L, 1_800_000_900_500L, 460_375_000L, 1_800_000_100_000L),
            TonedOutFocus.parse(json));
        assertTrue(TonedOutFocus.parse("{\"hold_minutes\": 15, \"frequencies\": []}").isEmpty());
        assertTrue(TonedOutFocus.parse("not json").isEmpty());
    }

    @Test
    void matchesWithinOneKilohertzAndExpires()
    {
        Map<Long,Long> matched = TonedOutFocus.match(Map.of(453_300_400L, 5000L, 155_000_000L, 9000L),
            List.of(502_425_000L, 453_300_000L, 460_375_000L));
        assertEquals(Map.of(453_300_000L, 5000L), matched);
        assertEquals(Set.of(453_300_000L), TonedOutFocus.active(matched, 4999));
        assertTrue(TonedOutFocus.active(matched, 5000).isEmpty());
    }
}
