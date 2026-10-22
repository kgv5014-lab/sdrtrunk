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

import java.util.Map;
import java.util.concurrent.ConcurrentSkipListMap;

/**
 * Offline index that maps a frequency to a human readable label (e.g. "County Fire - Dispatch"), so that a frequency
 * found by the NBFM discovery window can be named without making a network call.
 */
public class FrequencyLabelIndex
{
    /** Frequencies within this many hertz of an entry are treated as the same channel */
    public static final long DEFAULT_TOLERANCE_HZ = 1_000;

    private final ConcurrentSkipListMap<Long,String> mLabels = new ConcurrentSkipListMap<>();
    private final long mToleranceHz;

    public FrequencyLabelIndex()
    {
        this(DEFAULT_TOLERANCE_HZ);
    }

    public FrequencyLabelIndex(long toleranceHz)
    {
        mToleranceHz = toleranceHz;
    }

    /**
     * Adds a label for the frequency.  The first label added for a frequency is kept.
     * @param hertz frequency
     * @param label to show the user
     */
    public void add(long hertz, String label)
    {
        if(hertz > 0 && label != null && !label.isBlank())
        {
            mLabels.putIfAbsent(hertz, label);
        }
    }

    /**
     * Finds the label of the entry closest to the frequency, within the tolerance.
     * @param hertz to look up
     * @return label, or null when nothing is close enough
     */
    public String describe(long hertz)
    {
        Map.Entry<Long,String> best = null;
        long bestDistance = Long.MAX_VALUE;

        for(Map.Entry<Long,String> entry: mLabels.subMap(hertz - mToleranceHz, true, hertz + mToleranceHz, true)
            .entrySet())
        {
            long distance = Math.abs(entry.getKey() - hertz);

            if(distance < bestDistance)
            {
                best = entry;
                bestDistance = distance;
            }
        }

        return best != null ? best.getValue() : null;
    }

    /**
     * Adds every entry from the other index.
     */
    public void addAll(FrequencyLabelIndex other)
    {
        other.mLabels.forEach(this::add);
    }

    /**
     * Number of frequencies in the index
     */
    public int size()
    {
        return mLabels.size();
    }

    /**
     * Removes all entries
     */
    public void clear()
    {
        mLabels.clear();
    }
}
