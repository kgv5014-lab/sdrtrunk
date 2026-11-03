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

import io.github.dsheirer.module.ModuleEventBusMessage;
import java.util.Set;

/**
 * Request to limit a scan list to a few of its frequencies (departments that were just toned out), or to return
 * to the full list when the set is empty.
 */
public class ScanFocusRequest extends ModuleEventBusMessage
{
    private final Set<Long> mFrequencies;

    public ScanFocusRequest(Set<Long> frequencies)
    {
        mFrequencies = Set.copyOf(frequencies);
    }

    /**
     * Frequencies to scan, or empty for all of them.
     */
    public Set<Long> getFrequencies()
    {
        return mFrequencies;
    }
}
