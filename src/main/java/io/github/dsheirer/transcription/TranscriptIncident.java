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

package io.github.dsheirer.transcription;

import java.util.List;

/**
 * A possible incident from the transcription service's incident map (/api/incidents): calls that named the same
 * place, or follow-up calls from a unit sent there, grouped together.
 *
 * @param id incident id
 * @param firstTime first call, seconds since epoch
 * @param lastTime latest call, seconds since epoch
 * @param latitude where it is
 * @param longitude where it is
 * @param label place, e.g. "1234 Northampton Street, Easton"
 * @param category fire, ems, police or null
 * @param nature what it is about, e.g. "Structure fire" or "MVA", may be null
 * @param type what the map colors and filters by: fire, ems, police, mva or other
 * @param calls the calls in this incident, oldest first
 * @param pages tone-outs just before the first call
 */
public record TranscriptIncident(long id, long firstTime, long lastTime, double latitude, double longitude,
                                 String label, String category, String nature, String type,
                                 List<Call> calls, List<Page> pages)
{
    /**
     * One call in an incident.
     *
     * @param id call log id
     * @param callTime seconds since epoch
     * @param talkgroupLabel talkgroup alias, or the talkgroup value when there is no alias
     * @param talkerAlias talker alias, may be null
     * @param transcript transcript text
     * @param precision how it was placed: address, intersection, landmark, street or follow-up
     */
    public record Call(long id, long callTime, String talkgroupLabel, String talkerAlias, String transcript,
                       String precision) {}

    /**
     * A tone-out page.
     *
     * @param pageTime seconds since epoch
     * @param label station name, null when not on the station list
     */
    public record Page(long pageTime, String label) {}

    /**
     * Display name of the type: Fire, EMS, Police, MVA or Other.
     */
    public String typeDisplay()
    {
        return typeName(type);
    }

    /**
     * Display name of a type value.
     */
    public static String typeName(String type)
    {
        if(type == null)
        {
            return "Other";
        }

        return switch(type)
        {
            case "fire" -> "Fire";
            case "ems" -> "EMS";
            case "police" -> "Police";
            case "mva" -> "MVA";
            default -> "Other";
        };
    }

    /**
     * Normalized type value, "other" when missing or unknown.
     */
    public String typeKey()
    {
        return switch(type == null ? "" : type)
        {
            case "fire", "ems", "police", "mva" -> type;
            default -> "other";
        };
    }
}
