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

import org.junit.jupiter.api.Test;

import java.time.ZoneId;
import java.time.ZonedDateTime;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

public class IncidentsTest
{
    //The shape /api/incidents returns from transcriber/sdrtrunk_transcriber/server.py
    private static final String INCIDENTS_JSON = """
        [{"id": 7, "first_time": 1792656843, "last_time": 1792657143, "lat": 40.6915, "lon": -75.21,
          "label": "North 3rd Street & Northampton Street, Easton", "category": "fire", "nature": "Structure fire",
          "hidden": 0, "type": "fire",
          "calls": [{"kind": "intersection", "heard": "n 3rd and northampton", "label": "x", "lat": 40.6915,
                     "lon": -75.21, "precision": "intersection", "id": 11, "call_time": 1792656843,
                     "talkgroup": "1203", "talkgroup_label": "Fire Dispatch", "talkgroup_group": "Fire",
                     "talker_alias": null, "source": "1", "transcript": "Station 4 Engine, respond North Third and Northampton",
                     "duration_s": 4.2, "codes": [], "highlights": [[40, 54]]},
                    {"kind": "follow-up", "heard": "engine 41", "label": "Engine 41", "lat": null, "lon": null,
                     "precision": "follow-up", "id": 12, "call_time": 1792657143, "talkgroup": "1301",
                     "talkgroup_label": null, "talker_alias": "ENGINE 41", "transcript": "Engine 41 on location"}],
          "pages": [{"id": 3, "page_time": 1792656813.25, "tone_a": 349.0, "tone_b": 832.5, "label": "Station 4 Engine"}],
          "summary": "Engine 41 sent to a structure fire at North 3rd and Northampton; on location.",
          "summary_tags": ["working fire"], "summary_status": "active", "summary_current": true},
         {"id": 6, "first_time": 1792650000, "last_time": 1792650000, "lat": 40.68, "lon": -75.25,
          "label": "Route 22 & Freemansburg Avenue, Palmer Township", "category": null, "nature": "MVA",
          "hidden": 0, "type": "mva", "calls": [], "pages": []}]
        """;

    @Test
    public void parsesIncidents()
    {
        List<TranscriptIncident> incidents = TranscriptionClient.parseIncidents(INCIDENTS_JSON);

        assertEquals(2, incidents.size());
        TranscriptIncident fire = incidents.get(0);
        assertEquals(7, fire.id());
        assertEquals(40.6915, fire.latitude(), 1e-9);
        assertEquals("Fire", fire.typeDisplay());
        assertEquals(2, fire.calls().size());
        assertEquals("Fire Dispatch", fire.calls().get(0).talkgroupLabel());
        assertEquals("1301", fire.calls().get(1).talkgroupLabel());
        assertEquals("follow-up", fire.calls().get(1).precision());
        assertEquals(1792656813L, fire.pages().get(0).pageTime());
        assertEquals("Station 4 Engine", fire.pages().get(0).label());
        assertTrue(fire.summary().startsWith("Engine 41 sent"));
        assertEquals(List.of("working fire"), fire.summaryTags());
        assertEquals("active", fire.summaryStatus());

        TranscriptIncident crash = incidents.get(1);
        assertEquals("MVA", crash.typeDisplay());
        assertEquals("mva", crash.typeKey());
        assertEquals(null, crash.summary());
        assertEquals(List.of(), crash.summaryTags());
        assertEquals("other", new TranscriptIncident(1, 0, 0, 0, 0, null, null, null, null, List.of(), List.of()).typeKey());
    }

    @Test
    public void describesAnIncident()
    {
        String text = IncidentsPanel.describe(TranscriptionClient.parseIncidents(INCIDENTS_JSON).get(0));

        assertTrue(text.startsWith("North 3rd Street & Northampton Street, Easton   Fire · Structure fire"), text);
        assertTrue(text.contains("Tone out "), text);
        assertTrue(text.contains("Station 4 Engine"), text);
        assertTrue(text.contains("\nSummary (AI): Engine 41 sent to a structure fire"), text);
        assertTrue(text.contains("\nTags: active, working fire"), text);
        assertTrue(text.contains("ENGINE 41  (follow-up, same unit)\nEngine 41 on location"), text);
    }

    @Test
    public void timeRanges()
    {
        ZoneId zone = ZoneId.of("America/New_York");
        //Thursday 2026-10-22 15:30 local
        long now = ZonedDateTime.of(2026, 10, 22, 15, 30, 0, 0, zone).toEpochSecond();

        assertEquals(now - 3600, IncidentsPanel.Range.HOUR.start(now, zone));
        assertEquals(ZonedDateTime.of(2026, 10, 22, 0, 0, 0, 0, zone).toEpochSecond(),
            IncidentsPanel.Range.TODAY.start(now, zone));
        assertEquals(ZonedDateTime.of(2026, 10, 18, 0, 0, 0, 0, zone).toEpochSecond(),
            IncidentsPanel.Range.WEEK.start(now, zone));
    }
}
