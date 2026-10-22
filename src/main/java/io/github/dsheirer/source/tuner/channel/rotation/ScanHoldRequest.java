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

/**
 * Request to a scanning channel rotation monitor to hold (pause) on the current frequency, or to resume scanning.
 */
public class ScanHoldRequest extends ModuleEventBusMessage
{
    private final boolean mHold;

    /**
     * Constructs an instance
     * @param hold true to hold on the current frequency or false to resume scanning
     */
    public ScanHoldRequest(boolean hold)
    {
        mHold = hold;
    }

    /**
     * Indicates if this is a hold (true) or resume (false) request.
     */
    public boolean isHold()
    {
        return mHold;
    }
}
