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

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.github.dsheirer.alias.Alias;
import io.github.dsheirer.alias.AliasList;
import io.github.dsheirer.audio.AudioSegment;
import io.github.dsheirer.identifier.Identifier;
import io.github.dsheirer.identifier.talkgroup.TalkgroupIdentifier;
import io.github.dsheirer.preference.paging.PagingPreference;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.Collections;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.function.Supplier;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Aviation mute: while it is on, calls on the airport and helicopter talkgroups are not played on this PC.  They
 * are still recorded and sent to the transcriber, so they are still transcribed and shown on the aviation page.
 *
 * The switch lives in the transcriber (the Listen page's "Mute aviation" button, and View > Mute Aviation Audio
 * here), so the Listen page, the PC speakers player and this window always agree.  This asks the transcriber every
 * few seconds: GET /listen/aviation-mute answers {"muted": true|false, "talkgroups": ["9101", ...]}.  The talkgroups
 * leave out 121.5, which is never muted.  When the transcriber can't be reached, the last answer is kept.
 */
public class AviationMute
{
    private static final Logger mLog = LoggerFactory.getLogger(AviationMute.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final HttpClient HTTP_CLIENT = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(3)).build();
    private static final long POLL_SECONDS = 3;
    private static final long FAILURE_LOG_INTERVAL_MS = 10 * 60 * 1000;

    private static volatile Set<String> sMutedTalkgroups = Collections.emptySet();
    private static volatile boolean sMuted = false;
    private static volatile long sLastFailureLog = 0;
    private static volatile ScheduledExecutorService sExecutor;
    private static final List<Runnable> sListeners = new CopyOnWriteArrayList<>();

    private AviationMute()
    {
    }

    /**
     * Transcriber address of the switch, next to the one two-tone pages go to.
     */
    public static String muteUrl(String pageUrl)
    {
        String url = pageUrl == null || pageUrl.isBlank() ? PagingPreference.DEFAULT_TRANSCRIBER_URL : pageUrl.strip();

        if(url.endsWith("/api/page"))
        {
            url = url.substring(0, url.length() - "/api/page".length());
        }

        while(url.endsWith("/"))
        {
            url = url.substring(0, url.length() - 1);
        }

        return url + "/listen/aviation-mute";
    }

    /**
     * Starts asking the transcriber in the background.  Calling it again does nothing.
     * @param pageUrl supplies the transcriber's two-tone page address (Preferences > Paging), read on every ask
     */
    public static synchronized void start(Supplier<String> pageUrl)
    {
        if(sExecutor != null)
        {
            return;
        }

        sExecutor = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread thread = new Thread(r, "sdrtrunk aviation mute");
            thread.setDaemon(true);
            return thread;
        });
        sExecutor.scheduleWithFixedDelay(() -> {
            try
            {
                refresh(muteUrl(pageUrl.get()));
            }
            catch(Throwable t)
            {
                logFailure(t.getMessage());
            }
        }, 1, POLL_SECONDS, TimeUnit.SECONDS);
    }

    /**
     * Called (on a background thread) whenever the switch changes.
     */
    public static void addListener(Runnable listener)
    {
        sListeners.add(listener);
    }

    /**
     * Whether aviation audio is muted now.
     */
    public static boolean isMuted()
    {
        return sMuted;
    }

    /**
     * Whether this call is on a muted aviation talkgroup, by talkgroup number or alias name.
     */
    public static boolean isMuted(AudioSegment audioSegment)
    {
        Set<String> muted = sMutedTalkgroups;

        if(muted.isEmpty() || audioSegment == null)
        {
            return false;
        }

        Identifier identifier = audioSegment.getIdentifierCollection().getToIdentifier();

        if(identifier == null)
        {
            return false;
        }

        if(identifier instanceof TalkgroupIdentifier talkgroup &&
            muted.contains(String.valueOf(talkgroup.getValue())))
        {
            return true;
        }

        AliasList aliasList = audioSegment.getAliasList();

        if(aliasList != null)
        {
            for(Alias alias: aliasList.getAliases(identifier))
            {
                if(alias.getName() != null && muted.contains(alias.getName().strip().toLowerCase(Locale.ROOT)))
                {
                    return true;
                }
            }
        }

        return false;
    }

    /**
     * Reads the transcriber's answer and applies it.
     * @param json body
     * @return true when the body could be read
     */
    public static boolean apply(String json)
    {
        try
        {
            JsonNode root = MAPPER.readTree(json);

            if(root == null || !root.has("muted"))
            {
                return false;
            }

            boolean muted = root.path("muted").asBoolean(false);
            Set<String> talkgroups = new HashSet<>();

            if(muted)
            {
                for(JsonNode item: root.path("talkgroups"))
                {
                    String value = item.asText("").strip().toLowerCase(Locale.ROOT);

                    if(!value.isEmpty())
                    {
                        talkgroups.add(value);
                    }
                }
            }

            boolean changed = muted != sMuted;
            sMutedTalkgroups = Collections.unmodifiableSet(talkgroups);
            sMuted = muted;

            if(changed)
            {
                mLog.info("Aviation audio " + (muted ? "muted (still recorded and transcribed): talkgroups " +
                    talkgroups : "playing again"));

                for(Runnable listener: sListeners)
                {
                    listener.run();
                }
            }

            return true;
        }
        catch(Exception e)
        {
            return false;
        }
    }

    private static void refresh(String url)
    {
        try
        {
            HttpRequest request = HttpRequest.newBuilder(URI.create(url)).timeout(Duration.ofSeconds(4)).GET().build();
            HttpResponse<String> response = HTTP_CLIENT.send(request, HttpResponse.BodyHandlers.ofString());

            if(response.statusCode() / 100 != 2 || !apply(response.body()))
            {
                //An older transcriber, or one with [listen] turned off, answers 404: the last answer is kept (none = not muted)
                logFailure("transcriber at " + url + " answered " + response.statusCode());
            }
        }
        catch(InterruptedException ie)
        {
            Thread.currentThread().interrupt();
        }
        catch(Exception e)
        {
            logFailure("couldn't reach the transcriber at " + url + " - " + e.getMessage());
        }
    }

    /**
     * Turns the mute on or off at the transcriber (View menu), in the background.
     * @param pageUrl the transcriber's two-tone page address (Preferences > Paging)
     */
    public static void set(String pageUrl, boolean muted)
    {
        String url = muteUrl(pageUrl);

        try
        {
            HttpRequest request = HttpRequest.newBuilder(URI.create(url)).timeout(Duration.ofSeconds(4))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString("{\"muted\": " + muted + "}")).build();
            HTTP_CLIENT.sendAsync(request, HttpResponse.BodyHandlers.ofString()).thenAccept(response -> {
                if(response.statusCode() / 100 != 2 || !apply(response.body()))
                {
                    mLog.warn("Aviation mute: the transcriber at " + url + " answered " + response.statusCode() +
                        " - is it running, with [listen] turned on?");
                    notifyListeners();
                }
            }).exceptionally(t -> {
                mLog.warn("Aviation mute: couldn't reach the transcriber at " + url + " - " + t.getMessage());
                notifyListeners();
                return null;
            });
        }
        catch(Exception e)
        {
            mLog.warn("Aviation mute: bad transcriber address " + url);
            notifyListeners();
        }
    }

    private static void notifyListeners()
    {
        for(Runnable listener: sListeners)
        {
            listener.run();
        }
    }

    private static void logFailure(String message)
    {
        long now = System.currentTimeMillis();

        if(now - sLastFailureLog > FAILURE_LOG_INTERVAL_MS)
        {
            sLastFailureLog = now;
            mLog.info("Aviation mute: " + message + " (aviation audio plays normally until it answers)");
        }
    }
}
