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
package io.github.dsheirer.module.decode.twotone;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.sun.net.httpserver.HttpServer;
import io.github.dsheirer.identifier.configuration.ChannelNameConfigurationIdentifier;
import io.github.dsheirer.identifier.configuration.FrequencyConfigurationIdentifier;
import io.github.dsheirer.module.decode.event.DecodeEventType;
import io.github.dsheirer.module.decode.event.IDecodeEvent;
import io.github.dsheirer.module.decode.nbfm.NBFMTalkgroup;
import io.github.dsheirer.preference.paging.PagingPreference;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/**
 * Audio in, through the decoder and decoder state, to an Events tab entry and a post to the transcriber.
 */
public class TwoToneDecoderChainTest
{
    /**
     * Paging preference that doesn't touch the user's stored preferences.
     */
    private static class TestPreference extends PagingPreference
    {
        private final Path mTable;
        private final String mUrl;

        TestPreference(Path table, String url)
        {
            super(type -> {}, null);
            mTable = table;
            mUrl = url;
        }

        @Override
        public Path getTablePath()
        {
            return mTable;
        }

        @Override
        public boolean isSendToTranscriber()
        {
            return true;
        }

        @Override
        public String getTranscriberUrl()
        {
            return mUrl;
        }

        @Override
        public String getTranscriberApiKey()
        {
            return "";
        }
    }

    private static float[] tones(double[] frequencies, double[] seconds)
    {
        int total = 0;
        for(double s : seconds)
        {
            total += (int)(s * TwoToneDetector.SAMPLE_RATE);
        }
        float[] audio = new float[total];
        int index = 0;
        double phase = 0;
        for(int x = 0; x < frequencies.length; x++)
        {
            for(int y = 0; y < (int)(seconds[x] * TwoToneDetector.SAMPLE_RATE); y++)
            {
                phase += 2 * Math.PI * frequencies[x] / TwoToneDetector.SAMPLE_RATE;
                audio[index++] = (float)(frequencies[x] > 0 ? 0.4 * Math.sin(phase) : 0);
            }
        }
        return audio;
    }

    @Test
    public void pageShowsInEventsAndReachesTheTranscriber(@TempDir Path folder) throws Exception
    {
        Path table = folder.resolve("paging_tones.csv");
        Files.writeString(table, "349.0,832.5,Station 4\n");

        BlockingQueue<String> posted = new ArrayBlockingQueue<>(10);
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/api/page", exchange -> {
            posted.add(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            exchange.sendResponseHeaders(200, -1);
            exchange.close();
        });
        server.start();

        try
        {
            String url = "http://127.0.0.1:" + server.getAddress().getPort() + "/api/page";
            TwoToneDecoder decoder = new TwoToneDecoder();
            TwoToneDecoderState state = new TwoToneDecoderState(new TestPreference(table, url));
            state.getIdentifierCollection().update(ChannelNameConfigurationIdentifier.create("Fire Dispatch"));
            state.getIdentifierCollection().update(FrequencyConfigurationIdentifier.create(154_190_000L));
            state.getIdentifierCollection().update(new NBFMTalkgroup(1));
            List<IDecodeEvent> events = new ArrayList<>();
            state.addDecodeEventListener(events::add);
            decoder.setMessageListener(state);

            //Station 4, an unlisted station, then Station 4 toned again
            float[] audio = tones(new double[]{0, 349.0, 832.5, 0, 368.5, 707.3, 0, 349.0, 832.5, 0},
                    new double[]{0.5, 1.0, 3.0, 0.5, 1.0, 3.0, 1.0, 1.0, 3.0, 1.0});
            for(int offset = 0; offset < audio.length; offset += 400)
            {
                float[] buffer = new float[Math.min(400, audio.length - offset)];
                System.arraycopy(audio, offset, buffer, 0, buffer.length);
                decoder.receive(buffer);
            }

            assertEquals(3, events.size(), events.toString());
            assertEquals(DecodeEventType.PAGE, events.get(0).getEventType());
            assertTrue(events.get(0).getDetails().startsWith("Page: Station 4 (349."), events.get(0).getDetails());
            assertTrue(events.get(1).getDetails().startsWith("Unknown page 368."), events.get(1).getDetails());
            assertTrue(events.get(2).getDetails().startsWith("Page: Station 4 (349."), events.get(2).getDetails());
            assertTrue(state.getActivitySummary().contains("Station 4"));

            String first = posted.poll(10, TimeUnit.SECONDS);
            String second = posted.poll(10, TimeUnit.SECONDS);
            assertTrue(first.contains("\"toneA\":349.0") && first.contains("\"label\":\"Station 4\"") &&
                    first.contains("\"channel\":\"Fire Dispatch\"") && first.contains("\"frequency\":154190000"), first);
            assertTrue(second.contains("\"toneA\":368.") && second.contains("\"label\":null"), second);
        }
        finally
        {
            server.stop(0);
        }
    }
}
