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

import com.fasterxml.jackson.core.JsonGenerator;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.math.BigDecimal;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Posts decoded pages to the call transcriber's log as JSON, one at a time on a background thread so a slow or
 * missing transcriber never holds up decoding.
 */
public class TwoTonePageSender
{
    private static final Logger mLog = LoggerFactory.getLogger(TwoTonePageSender.class);
    private static final long FAILURE_LOG_INTERVAL_MILLIS = 10 * 60 * 1000;
    private static final ObjectMapper MAPPER = new ObjectMapper().enable(JsonGenerator.Feature.WRITE_BIGDECIMAL_AS_PLAIN);
    private static final ExecutorService EXECUTOR = Executors.newSingleThreadExecutor(runnable -> {
        Thread thread = new Thread(runnable, "sdrtrunk two-tone page sender");
        thread.setDaemon(true);
        return thread;
    });
    private static final HttpClient HTTP_CLIENT = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(3))
            .build();
    private static volatile long sLastFailureLog = 0;

    private TwoTonePageSender()
    {
    }

    /**
     * Builds the JSON body for a page.
     * @param page detected
     * @param timestamp of the page in milliseconds
     * @param entry from the station list, or null when the tones are not on the list
     * @param channel name, or null
     * @param system name, or null
     * @param site name, or null
     * @param frequency of the channel in Hz, or 0 when unknown
     * @param apiKey for the transcriber, or empty
     */
    public static String toJson(TwoToneDetector.Page page, long timestamp, TwoToneTable.Entry entry, String channel,
                                String system, String site, long frequency, String apiKey)
    {
        Map<String,Object> body = new LinkedHashMap<>();
        body.put("key", apiKey == null ? "" : apiKey);
        //Unix seconds with milliseconds, written plainly (1792656843.25, not 1.79265684325E9)
        body.put("dateTime", BigDecimal.valueOf(timestamp, 3).stripTrailingZeros());
        body.put("toneA", round(page.toneA()));
        body.put("toneB", page.isLongTone() ? null : round(page.toneB()));
        body.put("toneASeconds", round(page.toneASeconds()));
        body.put("label", entry != null ? entry.name() : null);
        body.put("system", system);
        body.put("site", site);
        body.put("channel", channel);
        body.put("frequency", frequency > 0 ? frequency : null);

        try
        {
            return MAPPER.writeValueAsString(body);
        }
        catch(Exception e)
        {
            throw new IllegalStateException("Couldn't build page JSON", e);
        }
    }

    private static double round(double value)
    {
        return Math.round(value * 10.0) / 10.0;
    }

    /**
     * Sends the JSON body to the transcriber in the background.
     */
    public static void send(String url, String json)
    {
        EXECUTOR.execute(() -> {
            try
            {
                HttpRequest request = HttpRequest.newBuilder(URI.create(url))
                        .timeout(Duration.ofSeconds(5))
                        .header("Content-Type", "application/json")
                        .POST(HttpRequest.BodyPublishers.ofString(json))
                        .build();
                HttpResponse<String> response = HTTP_CLIENT.send(request, HttpResponse.BodyHandlers.ofString());

                if(response.statusCode() / 100 != 2)
                {
                    logFailure("transcriber at " + url + " answered " + response.statusCode() + " " +
                            response.body());
                }
            }
            catch(Exception e)
            {
                logFailure("couldn't reach the transcriber at " + url + " (" + e.getClass().getSimpleName() +
                        (e.getMessage() != null ? ": " + e.getMessage() : "") + ")");
            }
        });
    }

    /**
     * Logs failures at most every ten minutes, since the transcriber is often simply not running.
     */
    private static void logFailure(String message)
    {
        long now = System.currentTimeMillis();

        if(now - sLastFailureLog > FAILURE_LOG_INTERVAL_MILLIS)
        {
            sLastFailureLog = now;
            mLog.info("Two-tone page not logged: " + message + ".  Further failures are logged every 10 minutes.");
        }
    }
}
