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
package io.github.dsheirer.source.tuner.quality;

import io.github.dsheirer.controller.channel.Channel;
import io.github.dsheirer.module.decode.DecoderType;
import io.github.dsheirer.preference.UserPreferences;
import io.github.dsheirer.preference.paging.PagingPreference;
import io.github.dsheirer.source.Source;
import io.github.dsheirer.source.tuner.Tuner;
import io.github.dsheirer.source.tuner.TunerController;
import io.github.dsheirer.source.tuner.channel.MultiFrequencyTunerChannelSource;
import io.github.dsheirer.source.tuner.manager.DiscoveredTuner;
import io.github.dsheirer.source.tuner.ui.DiscoveredTunerModel;
import io.github.dsheirer.util.ThreadPool;
import java.awt.EventQueue;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.WeakHashMap;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Adds up signal quality for each dongle every hour: how many messages decoded cleanly, how long the control channel
 * lost sync, channel signal and noise levels, frequency drift (PPM) and USB errors.  At the top of each hour it writes
 * the numbers to the application log and to signal_quality.csv in the logs folder, shows a short summary in the
 * Tuners tab and sends them to the call transcriber, which sends a phone alert when a dongle gets worse than its own
 * normal.
 */
public class SignalQualityMonitor
{
    private static final Logger mLog = LoggerFactory.getLogger(SignalQualityMonitor.class);
    public static final String CSV_FILE_NAME = "signal_quality.csv";
    private static final String UNKNOWN_TUNER = "Unknown dongle";
    private static final long HOUR_SECONDS = 3600;
    private static final HttpClient HTTP_CLIENT = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(3)).build();

    private final UserPreferences mUserPreferences;
    private final DiscoveredTunerModel mDiscoveredTunerModel;
    private final Map<Source,Tuner> mSourceTuners = Collections.synchronizedMap(new WeakHashMap<>());
    private final Set<SignalQualityModule> mModules = ConcurrentHashMap.newKeySet();
    private final Map<String,ChannelHour> mChannelHours = new LinkedHashMap<>();
    private final Map<String,TunerHour> mTunerHours = new LinkedHashMap<>();
    private final Map<String,String> mSummaries = new ConcurrentHashMap<>();
    private long mHourStart;
    private ScheduledFuture<?> mTimer;

    /**
     * Constructs an instance
     * @param userPreferences for the logs folder and the transcriber address
     * @param discoveredTunerModel listing the dongles
     */
    public SignalQualityMonitor(UserPreferences userPreferences, DiscoveredTunerModel discoveredTunerModel)
    {
        mUserPreferences = userPreferences;
        mDiscoveredTunerModel = discoveredTunerModel;
    }

    /**
     * Starts the once-a-minute collection.
     */
    public void start()
    {
        if(mTimer == null)
        {
            mHourStart = hourStart(System.currentTimeMillis() / 1000);
            long delay = 60_000 - System.currentTimeMillis() % 60_000 + 1_000;
            mTimer = ThreadPool.SCHEDULED.scheduleAtFixedRate(this::tick, delay, 60_000, TimeUnit.MILLISECONDS);
        }
    }

    public void stop()
    {
        if(mTimer != null)
        {
            mTimer.cancel(false);
            mTimer = null;
        }
    }

    /**
     * Records which dongle a channel source came from.
     */
    public void sourceCreated(Source source, Tuner tuner)
    {
        if(source != null && tuner != null)
        {
            mSourceTuners.put(source, tuner);
        }
    }

    /**
     * Creates the counting module for a channel's processing chain.
     */
    public SignalQualityModule createModule(Channel channel, Source source)
    {
        DecoderType decoderType = channel.getDecodeConfiguration().getDecoderType();
        boolean analog = decoderType == DecoderType.NBFM || decoderType == DecoderType.AM;
        String name = channel.getName() == null || channel.getName().isBlank() ? "Channel" : channel.getName();

        if(channel.isTrafficChannel())
        {
            name += " voice";
        }

        return new SignalQualityModule(this, source, name, decoderType.getShortDisplayString(),
            channel.isTrafficChannel(), analog);
    }

    void add(SignalQualityModule module)
    {
        mModules.add(module);
    }

    /**
     * Keeps the counts from a channel that stopped.
     */
    void remove(SignalQualityModule module)
    {
        if(mModules.remove(module))
        {
            synchronized(this)
            {
                collect(module, false);
            }
        }
    }

    /**
     * Short text for the dongle's row in the Tuners tab, or "collecting" before the first hour is done.
     */
    public String getSummary(Tuner tuner)
    {
        String summary = mSummaries.get(name(tuner));
        return summary != null ? summary : "collecting";
    }

    private synchronized void tick()
    {
        try
        {
            for(SignalQualityModule module: new ArrayList<>(mModules))
            {
                collect(module, true);
            }

            sampleTuners();

            long now = System.currentTimeMillis() / 1000;

            if(hourStart(now) != mHourStart)
            {
                SignalQualityReport report = buildReport();
                mHourStart = hourStart(now);
                mChannelHours.clear();
                mTunerHours.clear();
                publish(report);
            }
        }
        catch(Throwable t)
        {
            mLog.error("Error while collecting signal quality", t);
        }
    }

    private static long hourStart(long seconds)
    {
        return seconds - Math.floorMod(seconds, HOUR_SECONDS);
    }

    private void collect(SignalQualityModule module, boolean running)
    {
        SignalTally tally = module.drain();
        String tuner = name(tunerFor(module.getSource()));
        String key = tuner + "\u0000" + module.getChannelName() + "\u0000" + module.getDecoder();
        ChannelHour hour = mChannelHours.computeIfAbsent(key,
            k -> new ChannelHour(tuner, module.getChannelName(), module.getDecoder()));
        hour.tally.add(tally);

        long frequency = module.getFrequency();

        if(frequency > 0)
        {
            hour.frequency = frequency;
        }

        if(running)
        {
            hour.minutes.add(System.currentTimeMillis() / 60_000);
        }
    }

    private Tuner tunerFor(Source source)
    {
        if(source instanceof MultiFrequencyTunerChannelSource multi)
        {
            source = multi.getCurrentSource();
        }

        return source == null ? null : mSourceTuners.get(source);
    }

    private static String name(Tuner tuner)
    {
        if(tuner == null)
        {
            return UNKNOWN_TUNER;
        }

        try
        {
            return tuner.getTunerType().getLabel() + " " + tuner.getPreferredName();
        }
        catch(Exception e)
        {
            return tuner.getPreferredName();
        }
    }

    private void sampleTuners()
    {
        for(DiscoveredTuner discoveredTuner: mDiscoveredTunerModel.getAvailableTuners())
        {
            Tuner tuner = discoveredTuner.getTuner();

            if(tuner == null)
            {
                continue;
            }

            TunerController controller = tuner.getTunerController();
            TunerHour hour = mTunerHours.computeIfAbsent(name(tuner), k -> new TunerHour());
            double ppm = controller.getFrequencyCorrection();
            long usbErrors = controller.getUsbErrorCount();

            if(hour.samples == 0)
            {
                hour.ppmStart = ppm;
                hour.usbStart = usbErrors;
            }

            hour.samples++;
            hour.ppmEnd = ppm;
            hour.usbEnd = usbErrors;

            if(controller.hasMeasuredFrequencyError())
            {
                hour.errorSum += Math.abs(controller.getMeasuredFrequencyError());
                hour.errorCount++;
            }
        }
    }

    private SignalQualityReport buildReport()
    {
        List<SignalQualityReport.Row> rows = new ArrayList<>();
        Set<String> listed = new HashSet<>();

        for(int x = 0; x < mDiscoveredTunerModel.getRowCount(); x++)
        {
            DiscoveredTuner discoveredTuner = mDiscoveredTunerModel.getDiscoveredTuner(x);

            if(discoveredTuner == null)
            {
                continue;
            }

            String tuner = discoveredTuner.hasTuner() ? name(discoveredTuner.getTuner()) : discoveredTuner.getId();

            if(listed.add(tuner))
            {
                addRows(rows, tuner, discoveredTuner.getTunerStatus().toString());
            }
        }

        //Channels whose dongle has since disappeared, or couldn't be told
        for(ChannelHour hour: mChannelHours.values())
        {
            if(listed.add(hour.tuner))
            {
                addRows(rows, hour.tuner, UNKNOWN_TUNER.equals(hour.tuner) ? "Unknown" : "Removed");
            }
        }

        return new SignalQualityReport(mHourStart, rows);
    }

    private void addRows(List<SignalQualityReport.Row> rows, String tuner, String status)
    {
        TunerHour tunerHour = mTunerHours.get(tuner);
        Double ppm = tunerHour != null ? tunerHour.ppmEnd : null;
        double ppmChange = tunerHour != null ? tunerHour.ppmEnd - tunerHour.ppmStart : 0.0;
        Integer error = tunerHour != null && tunerHour.errorCount > 0 ?
            (int)Math.round((double)tunerHour.errorSum / tunerHour.errorCount) : null;
        long usb = tunerHour != null ? tunerHour.usbEnd - tunerHour.usbStart : 0;
        boolean any = false;

        for(ChannelHour hour: mChannelHours.values())
        {
            if(hour.tuner.equals(tuner))
            {
                any = true;
                rows.add(new SignalQualityReport.Row(tuner, status, hour.channel, hour.decoder, hour.frequency,
                    Math.min(hour.minutes.size(), 60), hour.tally, ppm, ppmChange, error, usb));
            }
        }

        if(!any)
        {
            rows.add(new SignalQualityReport.Row(tuner, status, "", "", 0, 0, new SignalTally(), ppm, ppmChange,
                error, usb));
        }
    }

    private void publish(SignalQualityReport report)
    {
        if(report.rows().isEmpty())
        {
            return;
        }

        for(String line: report.logLines())
        {
            mLog.info(line);
        }

        mSummaries.clear();

        for(Map.Entry<String,List<SignalQualityReport.Row>> entry: report.byTuner().entrySet())
        {
            mSummaries.put(entry.getKey(), SignalQualityReport.summarize(entry.getValue()));
        }

        //Rows updated rather than data changed, so the tuner selected in the Tuners tab stays selected
        EventQueue.invokeLater(() -> {
            int rows = mDiscoveredTunerModel.getRowCount();

            if(rows > 0)
            {
                mDiscoveredTunerModel.fireTableRowsUpdated(0, rows - 1);
            }
        });

        writeCsv(report);
        send(report);
    }

    private void writeCsv(SignalQualityReport report)
    {
        try
        {
            Path folder = mUserPreferences.getDirectoryPreference().getDirectoryApplicationLog();
            Files.createDirectories(folder);
            Path file = folder.resolve(CSV_FILE_NAME);
            List<String> lines = new ArrayList<>();

            if(!Files.exists(file))
            {
                lines.add(SignalQualityReport.CSV_HEADER);
            }

            lines.addAll(report.csvLines());
            Files.write(file, lines, StandardCharsets.UTF_8, StandardOpenOption.CREATE, StandardOpenOption.APPEND);
        }
        catch(Exception e)
        {
            mLog.warn("Couldn't write " + CSV_FILE_NAME + ": " + e.getMessage());
        }
    }

    /**
     * Transcriber address for signal reports, next to the one two-tone pages go to.
     */
    public static String signalUrl(String pageUrl)
    {
        String url = pageUrl == null || pageUrl.isBlank() ? PagingPreference.DEFAULT_TRANSCRIBER_URL : pageUrl.strip();

        if(url.endsWith("/api/page"))
        {
            return url.substring(0, url.length() - "/api/page".length()) + "/api/signal";
        }

        while(url.endsWith("/"))
        {
            url = url.substring(0, url.length() - 1);
        }

        return url + "/api/signal";
    }

    private void send(SignalQualityReport report)
    {
        PagingPreference preference = mUserPreferences.getPagingPreference();

        if(!preference.isSendToTranscriber())
        {
            return;
        }

        String url = signalUrl(preference.getTranscriberUrl());
        String json = report.toJson(preference.getTranscriberApiKey());

        ThreadPool.CACHED.execute(() -> {
            try
            {
                HttpRequest request = HttpRequest.newBuilder(URI.create(url))
                    .timeout(Duration.ofSeconds(10))
                    .header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofString(json))
                    .build();
                HttpResponse<String> response = HTTP_CLIENT.send(request, HttpResponse.BodyHandlers.ofString());

                if(response.statusCode() / 100 != 2)
                {
                    mLog.info("Signal quality not sent: transcriber at " + url + " answered " +
                        response.statusCode() + " " + response.body());
                }
            }
            catch(Exception e)
            {
                mLog.info("Signal quality not sent: couldn't reach the transcriber at " + url + " (" +
                    e.getClass().getSimpleName() + ")");
            }
        });
    }

    private static class ChannelHour
    {
        private final String tuner;
        private final String channel;
        private final String decoder;
        private final SignalTally tally = new SignalTally();
        private final Set<Long> minutes = new HashSet<>();
        private long frequency;

        ChannelHour(String tuner, String channel, String decoder)
        {
            this.tuner = tuner;
            this.channel = channel;
            this.decoder = decoder;
        }
    }

    private static class TunerHour
    {
        private int samples;
        private double ppmStart;
        private double ppmEnd;
        private long usbStart;
        private long usbEnd;
        private long errorSum;
        private int errorCount;
    }
}
