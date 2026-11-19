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

package io.github.dsheirer.audio.playback;

import io.github.dsheirer.alias.AliasList;
import io.github.dsheirer.audio.AudioSegment;
import io.github.dsheirer.module.decode.am.AMTalkgroup;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Aviation mute: reading the transcriber's switch and matching calls to it.
 */
public class AviationMuteTest
{
    @AfterEach
    void unmute()
    {
        AviationMute.apply("{\"muted\": false, \"talkgroups\": []}");
    }

    @Test
    void addressIsNextToThePageAddress()
    {
        assertEquals("http://localhost:3000/listen/aviation-mute", AviationMute.muteUrl("http://localhost:3000/api/page"));
        assertEquals("http://localhost:3000/listen/aviation-mute", AviationMute.muteUrl(null));
        assertEquals("http://pc:3000/listen/aviation-mute", AviationMute.muteUrl("http://pc:3000/"));
    }

    @Test
    void mutesOnlyTheListedTalkgroupsWhileOn()
    {
        AudioSegment tower = segment(9102);
        AudioSegment guard = segment(9103);
        AudioSegment fire = segment(9000);
        assertFalse(AviationMute.isMuted(tower));

        assertTrue(AviationMute.apply("{\"muted\": true, \"talkgroups\": [\"9101\", \"9102\", \"9104\"]}"));
        assertTrue(AviationMute.isMuted());
        assertTrue(AviationMute.isMuted(tower));
        assertFalse(AviationMute.isMuted(guard), "121.5 is never muted");
        assertFalse(AviationMute.isMuted(fire));

        assertTrue(AviationMute.apply("{\"muted\": false, \"talkgroups\": [\"9101\", \"9102\"]}"));
        assertFalse(AviationMute.isMuted());
        assertFalse(AviationMute.isMuted(tower));
    }

    @Test
    void aBadAnswerChangesNothing()
    {
        AviationMute.apply("{\"muted\": true, \"talkgroups\": [\"9102\"]}");
        assertFalse(AviationMute.apply("not json"));
        assertFalse(AviationMute.apply("{\"detail\": \"Not Found\"}"));
        assertTrue(AviationMute.isMuted(segment(9102)));
    }

    private static AudioSegment segment(int talkgroup)
    {
        AudioSegment segment = new AudioSegment(new AliasList("test"), 0);
        segment.addIdentifier(new AMTalkgroup(talkgroup));
        return segment;
    }
}
