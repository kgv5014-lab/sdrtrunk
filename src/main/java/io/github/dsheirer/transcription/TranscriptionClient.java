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

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParseException;
import com.google.gson.JsonParser;

import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CompletableFuture;

/**
 * Reads the call log from the local transcription service (transcriber/ in this repository) over its
 * HTTP API.  All transcription work happens in that service; this only fetches finished text.
 */
public class TranscriptionClient
{
    public static final String DEFAULT_URL = "http://localhost:3000";
    private static final Duration TIMEOUT = Duration.ofSeconds(3);

    private final HttpClient mHttpClient = HttpClient.newBuilder()
        .version(HttpClient.Version.HTTP_1_1)
        .connectTimeout(TIMEOUT)
        .build();

    /**
     * Fetches the newest calls, optionally filtered.
     * @param baseUrl service address, e.g. http://localhost:3000
     * @param search transcript search text, may be empty
     * @param talkgroup talkgroup value or label filter, may be empty
     * @param limit maximum number of calls
     * @return calls, newest first
     */
    public CompletableFuture<List<TranscriptCall>> fetchCalls(String baseUrl, String search, String talkgroup,
                                                               int limit)
    {
        HttpRequest request;

        try
        {
            request = HttpRequest.newBuilder(callsUri(baseUrl, search, talkgroup, limit))
                .timeout(TIMEOUT)
                .header("Accept", "application/json")
                .GET()
                .build();
        }
        catch(IllegalArgumentException e)
        {
            return CompletableFuture.failedFuture(e);
        }

        return mHttpClient.sendAsync(request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8))
            .thenApply(response -> {
                if(response.statusCode() != 200)
                {
                    throw new IllegalStateException("HTTP " + response.statusCode());
                }

                return parseCalls(response.body());
            });
    }

    /**
     * Fetches the incident map's incidents active since a time.
     * @param baseUrl service address, e.g. http://localhost:3000
     * @param since seconds since epoch
     * @return incidents, most recent first
     */
    public CompletableFuture<List<TranscriptIncident>> fetchIncidents(String baseUrl, long since)
    {
        return get(URI.create(base(baseUrl) + "/api/incidents?since=" + since)).thenApply(TranscriptionClient::parseIncidents);
    }

    private CompletableFuture<String> get(URI uri)
    {
        HttpRequest request;

        try
        {
            request = HttpRequest.newBuilder(uri)
                .timeout(TIMEOUT)
                .header("Accept", "application/json")
                .GET()
                .build();
        }
        catch(IllegalArgumentException e)
        {
            return CompletableFuture.failedFuture(e);
        }

        return mHttpClient.sendAsync(request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8))
            .thenApply(response -> {
                if(response.statusCode() != 200)
                {
                    throw new IllegalStateException("HTTP " + response.statusCode());
                }

                return response.body();
            });
    }

    /**
     * Service address without a trailing slash, the default when empty.
     */
    static String base(String baseUrl)
    {
        String base = baseUrl == null || baseUrl.isBlank() ? DEFAULT_URL : baseUrl.trim();

        while(base.endsWith("/"))
        {
            base = base.substring(0, base.length() - 1);
        }

        return base;
    }

    /**
     * Builds the /api/calls request address.
     */
    static URI callsUri(String baseUrl, String search, String talkgroup, int limit)
    {
        return URI.create(base(baseUrl) + "/api/calls?limit=" + limit + "&q=" + encode(search) +
            "&talkgroup=" + encode(talkgroup));
    }

    /**
     * Parses the JSON array returned by /api/incidents.
     */
    static List<TranscriptIncident> parseIncidents(String json)
    {
        List<TranscriptIncident> incidents = new ArrayList<>();

        for(JsonElement element : array(json))
        {
            if(!element.isJsonObject())
            {
                continue;
            }

            JsonObject incident = element.getAsJsonObject();
            List<TranscriptIncident.Call> calls = new ArrayList<>();
            List<TranscriptIncident.Page> pages = new ArrayList<>();

            if(incident.has("calls") && incident.get("calls").isJsonArray())
            {
                for(JsonElement item : incident.getAsJsonArray("calls"))
                {
                    if(item.isJsonObject())
                    {
                        JsonObject call = item.getAsJsonObject();
                        String talkgroup = string(call, "talkgroup_label");
                        calls.add(new TranscriptIncident.Call(number(call, "id", 0L), number(call, "call_time", 0L),
                            talkgroup != null ? talkgroup : string(call, "talkgroup"), string(call, "talker_alias"),
                            string(call, "transcript"), string(call, "precision")));
                    }
                }
            }

            if(incident.has("pages") && incident.get("pages").isJsonArray())
            {
                for(JsonElement item : incident.getAsJsonArray("pages"))
                {
                    if(item.isJsonObject())
                    {
                        pages.add(new TranscriptIncident.Page(number(item.getAsJsonObject(), "page_time", 0L),
                            string(item.getAsJsonObject(), "label")));
                    }
                }
            }

            List<String> summaryTags = new ArrayList<>();

            if(incident.has("summary_tags") && incident.get("summary_tags").isJsonArray())
            {
                for(JsonElement item : incident.getAsJsonArray("summary_tags"))
                {
                    if(item.isJsonPrimitive())
                    {
                        summaryTags.add(item.getAsString());
                    }
                }
            }

            List<String> services = new ArrayList<>();

            if(incident.has("services") && incident.get("services").isJsonArray())
            {
                for(JsonElement item : incident.getAsJsonArray("services"))
                {
                    if(item.isJsonPrimitive())
                    {
                        services.add(item.getAsString());
                    }
                }
            }

            incidents.add(new TranscriptIncident(number(incident, "id", 0L), number(incident, "first_time", 0L),
                number(incident, "last_time", 0L), decimal(incident, "lat"), decimal(incident, "lon"),
                string(incident, "label"), string(incident, "category"), string(incident, "nature"),
                string(incident, "type"), calls, pages, string(incident, "summary"), summaryTags,
                string(incident, "summary_status"), services));
        }

        return incidents;
    }

    private static JsonArray array(String json)
    {
        JsonElement root;

        try
        {
            root = JsonParser.parseString(json);
        }
        catch(JsonParseException e)
        {
            throw new IllegalStateException("Unexpected reply from the transcription service", e);
        }

        if(!root.isJsonArray())
        {
            throw new IllegalStateException("Unexpected reply from the transcription service");
        }

        return root.getAsJsonArray();
    }

    private static double decimal(JsonObject object, String name)
    {
        JsonElement element = object.get(name);

        try
        {
            return element == null || element.isJsonNull() ? 0 : element.getAsDouble();
        }
        catch(NumberFormatException | UnsupportedOperationException e)
        {
            return 0;
        }
    }

    private static String encode(String value)
    {
        return URLEncoder.encode(value == null ? "" : value.trim(), StandardCharsets.UTF_8);
    }

    /**
     * Parses the JSON array returned by /api/calls.
     */
    static List<TranscriptCall> parseCalls(String json)
    {
        List<TranscriptCall> calls = new ArrayList<>();
        JsonElement root;

        try
        {
            root = JsonParser.parseString(json);
        }
        catch(JsonParseException e)
        {
            throw new IllegalStateException("Unexpected reply from the transcription service", e);
        }

        if(!root.isJsonArray())
        {
            throw new IllegalStateException("Unexpected reply from the transcription service");
        }

        for(JsonElement element : root.getAsJsonArray())
        {
            if(!element.isJsonObject())
            {
                continue;
            }

            JsonObject call = element.getAsJsonObject();
            List<TranscriptCall.CodeLabel> codes = new ArrayList<>();

            if(call.has("codes") && call.get("codes").isJsonArray())
            {
                JsonArray array = call.getAsJsonArray("codes");

                for(JsonElement code : array)
                {
                    if(code.isJsonObject())
                    {
                        String value = string(code.getAsJsonObject(), "code");
                        String meaning = string(code.getAsJsonObject(), "meaning");

                        if(value != null && meaning != null)
                        {
                            codes.add(new TranscriptCall.CodeLabel(value, meaning));
                        }
                    }
                }
            }

            calls.add(new TranscriptCall(
                number(call, "id", 0L),
                number(call, "call_time", 0L),
                string(call, "talkgroup"),
                string(call, "talkgroup_label"),
                string(call, "source"),
                string(call, "talker_alias"),
                string(call, "transcript"),
                string(call, "status"),
                string(call, "error"),
                call.has("duration_s") && !call.get("duration_s").isJsonNull() ? call.get("duration_s").getAsDouble() : null,
                codes));
        }

        return calls;
    }

    private static String string(JsonObject object, String name)
    {
        JsonElement element = object.get(name);
        return element == null || element.isJsonNull() ? null : element.getAsString();
    }

    private static long number(JsonObject object, String name, long defaultValue)
    {
        JsonElement element = object.get(name);

        try
        {
            return element == null || element.isJsonNull() ? defaultValue : element.getAsLong();
        }
        catch(NumberFormatException | UnsupportedOperationException e)
        {
            try
            {
                return (long)element.getAsDouble();     //page times have fractions of a second
            }
            catch(NumberFormatException | UnsupportedOperationException e2)
            {
                return defaultValue;
            }
        }
    }
}
