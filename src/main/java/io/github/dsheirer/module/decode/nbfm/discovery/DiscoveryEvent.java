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

/**
 * Carrier discovery event.
 *
 * @param type of event
 * @param carrier that the event applies to
 * @param timestamp of the event in milliseconds
 */
public record DiscoveryEvent(Type type, TrackedCarrier carrier, long timestamp)
{
    public enum Type
    {
        /**
         * Carrier has been present for the attack time.
         */
        STARTED,
        /**
         * Carrier has been absent for the release time.
         */
        ENDED,
        /**
         * Carrier stayed narrow and continuously on for the birdie time and is ignored from now on.
         */
        BIRDIE
    }

    @Override
    public String toString()
    {
        return type + " " + carrier;
    }
}
