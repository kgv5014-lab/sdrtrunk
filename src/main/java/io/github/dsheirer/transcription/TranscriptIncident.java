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
 * @param summary short summary written by the service's local AI model (Ollama), null when there is none
 * @param summaryTags tags picked by the AI model, e.g. "injuries", "road blocked"
 * @param summaryStatus latest status the AI model read from the calls: active, under control, cleared, canceled
 *                      or unknown, may be null
 * @param services police, fire and/or ems: the services working it, in that order
 * @param helicopters medevac and police helicopters that came down near it, e.g. "Medevac N812LF landed 2:14 PM"
 * @param updates what changed along the way, written by the AI model, oldest first
 */
public record TranscriptIncident(long id, long firstTime, long lastTime, double latitude, double longitude,
                                 String label, String category, String nature, String type,
                                 List<Call> calls, List<Page> pages, String summary, List<String> summaryTags,
                                 String summaryStatus, List<String> services, List<String> helicopters,
                                 List<Update> updates)
{
    /**
     * An incident without update notes.
     */
    public TranscriptIncident(long id, long firstTime, long lastTime, double latitude, double longitude,
                              String label, String category, String nature, String type,
                              List<Call> calls, List<Page> pages, String summary, List<String> summaryTags,
                              String summaryStatus, List<String> services, List<String> helicopters)
    {
        this(id, firstTime, lastTime, latitude, longitude, label, category, nature, type, calls, pages, summary,
            summaryTags, summaryStatus, services, helicopters, List.of());
    }

    /**
     * An incident without helicopters.
     */
    public TranscriptIncident(long id, long firstTime, long lastTime, double latitude, double longitude,
                              String label, String category, String nature, String type,
                              List<Call> calls, List<Page> pages, String summary, List<String> summaryTags,
                              String summaryStatus, List<String> services)
    {
        this(id, firstTime, lastTime, latitude, longitude, label, category, nature, type, calls, pages, summary,
            summaryTags, summaryStatus, services, List.of());
    }

    /**
     * An incident without the services working it.
     */
    public TranscriptIncident(long id, long firstTime, long lastTime, double latitude, double longitude,
                              String label, String category, String nature, String type,
                              List<Call> calls, List<Page> pages, String summary, List<String> summaryTags,
                              String summaryStatus)
    {
        this(id, firstTime, lastTime, latitude, longitude, label, category, nature, type, calls, pages, summary,
            summaryTags, summaryStatus, List.of());
    }

    /**
     * An incident without an AI summary.
     */
    public TranscriptIncident(long id, long firstTime, long lastTime, double latitude, double longitude,
                              String label, String category, String nature, String type,
                              List<Call> calls, List<Page> pages)
    {
        this(id, firstTime, lastTime, latitude, longitude, label, category, nature, type, calls, pages, null,
            List.of(), null);
    }

    /**
     * One call in an incident.
     *
     * @param id call log id
     * @param callTime seconds since epoch
     * @param talkgroupLabel talkgroup alias, or the talkgroup value when there is no alias
     * @param talkerAlias talker alias, else who the transcriber says is talking ("Dispatch", "1781"); may be null
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
     * A change in the incident, such as "Second alarm called" or "Patient transported".
     *
     * @param time the newest call it is about, seconds since epoch
     * @param note what changed
     */
    public record Update(long time, String note) {}

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
     * The services working it for display: "Police · Fire · EMS", empty when none are known.
     */
    public String servicesDisplay()
    {
        return String.join(" · ", services.stream().map(TranscriptIncident::typeName).toList());
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
