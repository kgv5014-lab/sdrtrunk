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

package io.github.dsheirer.service.radioreference;

import io.github.dsheirer.rrapi.type.Frequency;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;

/**
 * Tests the offline frequency label lookup
 */
public class FrequencyLabelIndexTest
{
    @Test
    void findsExactAndNearbyFrequencies()
    {
        FrequencyLabelIndex index = new FrequencyLabelIndex();
        index.add(154_430_000L, "County Fire - Dispatch");
        assertEquals("County Fire - Dispatch", index.describe(154_430_000L));
        assertEquals("County Fire - Dispatch", index.describe(154_430_800L));
        assertEquals("County Fire - Dispatch", index.describe(154_429_500L));
        assertNull(index.describe(154_432_500L));
        assertNull(index.describe(155_475_000L));
    }

    @Test
    void picksTheClosestEntry()
    {
        FrequencyLabelIndex index = new FrequencyLabelIndex();
        index.add(154_430_000L, "A");
        index.add(154_431_000L, "B");
        assertEquals("B", index.describe(154_430_700L));
        assertEquals("A", index.describe(154_430_200L));
    }

    @Test
    void keepsFirstLabelAndIgnoresBlank()
    {
        FrequencyLabelIndex index = new FrequencyLabelIndex();
        index.add(154_430_000L, "First");
        index.add(154_430_000L, "Second");
        index.add(155_000_000L, " ");
        assertEquals("First", index.describe(154_430_000L));
        assertEquals(1, index.size());
    }

    @Test
    void buildsLabelFromBestAvailableText()
    {
        Frequency frequency = new Frequency();
        frequency.setDescription("Dispatch");
        assertEquals("County Fire - Dispatch", RadioReferenceFrequencyLookup.buildLabel("County Fire", "Fire", frequency));
        frequency.setDescription(null);
        frequency.setAlphaTag("FD TAC");
        assertEquals("County Fire - FD TAC", RadioReferenceFrequencyLookup.buildLabel("County Fire", "Fire", frequency));
        frequency.setAlphaTag("");
        assertEquals("County Fire - Fire", RadioReferenceFrequencyLookup.buildLabel("County Fire", "Fire", frequency));
    }
}
