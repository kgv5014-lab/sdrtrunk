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

package io.github.dsheirer.scanner;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.github.dsheirer.preference.paging.PagingPreference;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.CompletableFuture;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Smart Fire Scan: asks the transcriber which departments on a scan list were just toned out, so the scan list
 * stays on them (rotating among only those when there are several) until their hold time runs out.
 *
 * The transcriber answers GET /api/scan-focus with {"frequencies": [{"frequency": Hz, "until": unix seconds}]}.
 */
public class TonedOutFocus
{
    private static final Logger mLog = LoggerFactory.getLogger(TonedOutFocus.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final HttpClient HTTP_CLIENT = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(3)).build();
    private static final long FAILURE_LOG_INTERVAL_MS = 10 * 60 * 1000;
    /**
     * A transcriber frequency matches a scan list frequency this close (Hz).
     */
    public static final long MATCH_HZ = 1000;
    private static volatile long sLastFailureLog = 0;

    private TonedOutFocus()
    {
    }

    /**
     * Transcriber address for the toned-out departments, next to the one two-tone pages go to.
     */
    public static String focusUrl(String pageUrl)
    {
        String url = pageUrl == null || pageUrl.isBlank() ? PagingPreference.DEFAULT_TRANSCRIBER_URL : pageUrl.strip();

        if(url.endsWith("/api/page"))
        {
            return url.substring(0, url.length() - "/api/page".length()) + "/api/scan-focus";
        }

        while(url.endsWith("/"))
        {
            url = url.substring(0, url.length() - 1);
        }

        return url + "/api/scan-focus";
    }

    /**
     * Reads the transcriber's answer.
     * @param json body
     * @return hold end time (milliseconds) by frequency (Hz); empty when nothing was toned out or the body is bad
     */
    public static Map<Long,Long> parse(String json)
    {
        Map<Long,Long> untilByFrequency = new HashMap<>();

        try
        {
            JsonNode frequencies = MAPPER.readTree(json).path("frequencies");

            for(JsonNode item: frequencies)
            {
                long frequency = item.path("frequency").asLong(0);
                long until = Math.round(item.path("until").asDouble(0) * 1000);

                if(frequency > 0 && until > 0)
                {
                    untilByFrequency.merge(frequency, until, Math::max);
                }
            }
        }
        catch(Exception e)
        {
            untilByFrequency.clear();
        }

        return untilByFrequency;
    }

    /**
     * Matches the transcriber's frequencies to the scan list's entries (within 1 kHz).
     * @return hold end time by scan entry frequency
     */
    public static Map<Long,Long> match(Map<Long,Long> untilByFrequency, Iterable<Long> scanFrequencies)
    {
        Map<Long,Long> matched = new HashMap<>();

        for(Map.Entry<Long,Long> toned: untilByFrequency.entrySet())
        {
            for(Long frequency: scanFrequencies)
            {
                if(Math.abs(frequency - toned.getKey()) <= MATCH_HZ)
                {
                    matched.merge(frequency, toned.getValue(), Math::max);
                }
            }
        }

        return matched;
    }

    /**
     * Frequencies to stay on now: entries still within their hold time.
     */
    public static Set<Long> active(Map<Long,Long> untilByFrequency, long now)
    {
        Set<Long> active = new HashSet<>();

        for(Map.Entry<Long,Long> entry: untilByFrequency.entrySet())
        {
            if(entry.getValue() > now)
            {
                active.add(entry.getKey());
            }
        }

        return active;
    }

    /**
     * Asks the transcriber in the background.
     * @return the answer's hold end times by frequency, or null when the transcriber can't be reached
     */
    public static CompletableFuture<Map<Long,Long>> fetch(String url)
    {
        HttpRequest request;

        try
        {
            request = HttpRequest.newBuilder(URI.create(url)).timeout(Duration.ofSeconds(4)).GET().build();
        }
        catch(Exception e)
        {
            logFailure("bad transcriber address " + url);
            return CompletableFuture.completedFuture(null);
        }

        return HTTP_CLIENT.sendAsync(request, HttpResponse.BodyHandlers.ofString())
            .thenApply(response -> {
                if(response.statusCode() / 100 == 2)
                {
                    return parse(response.body());
                }

                //An older transcriber without Smart Fire Scan answers 404: scan normally
                logFailure("transcriber at " + url + " answered " + response.statusCode());
                return (Map<Long,Long>)null;
            })
            .exceptionally(t -> {
                logFailure("couldn't reach the transcriber at " + url + " - " + t.getMessage());
                return null;
            });
    }

    private static void logFailure(String message)
    {
        long now = System.currentTimeMillis();

        if(now - sLastFailureLog > FAILURE_LOG_INTERVAL_MS)
        {
            sLastFailureLog = now;
            mLog.info("Smart Fire Scan: " + message + " (the scan list keeps rotating normally)");
        }
    }
}
