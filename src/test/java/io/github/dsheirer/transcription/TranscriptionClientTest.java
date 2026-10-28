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

import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.Test;

import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

public class TranscriptionClientTest
{
    //The shape /api/calls returns from transcriber/sdrtrunk_transcriber/server.py
    private static final String CALLS_JSON = """
        [{"id": 2, "call_time": 1792656843, "received_at": 1792656850, "system_id": "1",
          "system_label": "County P25", "talkgroup": "1203", "talkgroup_label": "Fire Dispatch",
          "talkgroup_group": "Fire", "source": "4521004", "talker_alias": "ENGINE 12", "frequency": 851262500,
          "patches": "[]", "audio_path": "calls/2026/10/22/x.mp3", "duration_s": 3.0,
          "transcript": "2081 10-23 at 1201 Main Street. Received 08:14, 10-4.",
          "transcript_raw": "2081 ten twenty three", "confidence": -0.21, "status": "done", "error": null,
          "backend": "faster-whisper", "model": "large-v3-turbo", "transcribe_ms": 310,
          "codes": [{"code": "2081", "meaning": "Easton Fire/EMS, Ambulance", "kind": "unit"},
                    {"code": "10-23", "meaning": "Arrived on scene", "kind": "code"}]},
         {"id": 1, "call_time": 1792656800, "talkgroup": "2001", "talkgroup_label": null, "source": null,
          "talker_alias": null, "transcript": null, "duration_s": null, "status": "queued", "error": null,
          "codes": []},
         {"id": 3, "call_time": 1792656900, "talkgroup": "11007", "source": "0", "talker_alias": "",
          "speaker": "Dispatch", "speaker_kind": "dispatch", "transcript": "County 1781.", "status": "done",
          "codes": []},
         {"id": 4, "call_time": 1792656901, "talkgroup": "11007", "source": "1117081", "talker_alias": null,
          "speaker": "1781", "speaker_kind": "unit", "transcript": "1781 County, on scene.", "status": "done",
          "codes": []}]
        """;

    @Test
    public void parsesTheServiceCallLog()
    {
        List<TranscriptCall> calls = TranscriptionClient.parseCalls(CALLS_JSON);

        assertEquals(4, calls.size());
        TranscriptCall call = calls.get(0);
        assertEquals(2, call.id());
        assertEquals(1792656843L, call.callTime());
        assertEquals("Fire Dispatch (1203)", call.talkgroupDisplay());
        assertEquals("ENGINE 12 (4521004)", call.sourceDisplay());
        assertEquals("2081 10-23 at 1201 Main Street. Received 08:14, 10-4.", call.transcriptDisplay());
        assertEquals(3.0, call.durationSeconds());
        assertEquals("2081 Easton Fire/EMS, Ambulance  ·  10-23 Arrived on scene", call.codesDisplay());

        TranscriptCall queued = calls.get(1);
        assertEquals("2001", queued.talkgroupDisplay());
        assertEquals("", queued.sourceDisplay());
        assertEquals("(queued...)", queued.transcriptDisplay());
        assertNull(queued.durationSeconds());
        assertEquals("", queued.codesDisplay());

        //Who is talking when SDRTrunk has no talker alias (radio ID 0 means it had none)
        assertEquals("Dispatch", calls.get(2).sourceDisplay());
        assertEquals("1781 (1117081)", calls.get(3).sourceDisplay());
    }

    @Test
    public void describesCallsWithoutTranscripts()
    {
        TranscriptCall skipped = new TranscriptCall(1, 0, "1", null, null, null, null, null, "skipped", "too short",
            null, List.of());
        TranscriptCall failed = new TranscriptCall(1, 0, "1", null, null, null, null, null, "failed", "GPU error",
            null, List.of());
        TranscriptCall silent = new TranscriptCall(1, 0, "1", null, null, null, null, "", "done", null, null, List.of());

        assertEquals("(not transcribed: too short)", skipped.transcriptDisplay());
        assertEquals("(transcription failed: GPU error)", failed.transcriptDisplay());
        assertEquals("(no speech)", silent.transcriptDisplay());
    }

    @Test
    public void rejectsNonJsonReplies()
    {
        assertThrows(IllegalStateException.class, () -> TranscriptionClient.parseCalls("<html>nope</html>"));
        assertThrows(IllegalStateException.class, () -> TranscriptionClient.parseCalls("{\"detail\": \"x\"}"));
    }

    @Test
    public void buildsTheRequestAddress()
    {
        assertEquals("http://localhost:3000/api/calls?limit=200&q=10-23+Main&talkgroup=fire",
            TranscriptionClient.callsUri("http://localhost:3000/", " 10-23 Main ", "fire", 200).toString());
        assertEquals("http://localhost:3000/api/calls?limit=5&q=&talkgroup=",
            TranscriptionClient.callsUri("", null, null, 5).toString());
    }

    @Test
    public void fetchesFromARunningService() throws Exception
    {
        AtomicReference<String> query = new AtomicReference<>();
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/api/calls", exchange -> {
            query.set(exchange.getRequestURI().getRawQuery());
            byte[] body = CALLS_JSON.getBytes(StandardCharsets.UTF_8);
            exchange.sendResponseHeaders(200, body.length);
            try(OutputStream out = exchange.getResponseBody())
            {
                out.write(body);
            }
        });
        server.start();

        try
        {
            String url = "http://127.0.0.1:" + server.getAddress().getPort();
            List<TranscriptCall> calls = new TranscriptionClient().fetchCalls(url, "ambulance", "", 50)
                .get(10, TimeUnit.SECONDS);
            assertEquals(4, calls.size());
            assertEquals("limit=50&q=ambulance&talkgroup=", query.get());
        }
        finally
        {
            server.stop(0);
        }
    }

    @Test
    public void reportsAServiceThatIsNotRunning() throws Exception
    {
        //Grab a free port, then close it so nothing is listening there
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        int port = server.getAddress().getPort();
        server.stop(0);

        ExecutionException error = assertThrows(ExecutionException.class, () ->
            new TranscriptionClient().fetchCalls("http://127.0.0.1:" + port, "", "", 10).get(10, TimeUnit.SECONDS));
        assertTrue(error.getCause() != null);
    }
}
