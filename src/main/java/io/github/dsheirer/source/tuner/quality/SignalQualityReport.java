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

import com.fasterxml.jackson.core.JsonGenerator;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.time.Instant;
import java.time.LocalDateTime;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * One hour of signal quality for every dongle, written as log lines, spreadsheet rows, a short summary for the Tuners
 * tab and the JSON sent to the call transcriber.
 *
 * @param hourStart unix seconds at the start of the hour
 * @param rows one per channel that ran on a dongle, plus one with a blank channel for a dongle with no channels
 */
public record SignalQualityReport(long hourStart, List<Row> rows)
{
    /**
     * P25 phase 1 sends 9600 bits a second.
     */
    private static final double P25_BITS_PER_SECOND = 9600.0;
    /**
     * A channel with fewer messages than this doesn't give a decode rate worth showing.
     */
    public static final long MINIMUM_MESSAGES = 100;
    public static final String CSV_HEADER = "hour_start,tuner,status,channel,decoder,frequency_mhz,minutes," +
        "messages_good,messages_bad,good_percent,sync_lost_seconds,signal_db,noise_db,ppm,ppm_change," +
        "channel_error_hz,usb_errors";
    private static final DateTimeFormatter HOUR_FORMAT = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm");
    private static final DateTimeFormatter CLOCK_FORMAT = DateTimeFormatter.ofPattern("h:mm a", Locale.US);
    private static final ObjectMapper MAPPER = new ObjectMapper().enable(JsonGenerator.Feature.WRITE_BIGDECIMAL_AS_PLAIN);

    /**
     * One channel's hour on one dongle.  Dongle fields repeat on each of its channels.
     *
     * @param tuner dongle name, like R820T 00000011
     * @param status Enabled, Error or Disabled
     * @param channel name, or blank for a dongle with no channels this hour
     * @param decoder short decoder name, like P25 or NBFM
     * @param frequency of the channel in hertz, or 0
     * @param minutes the channel ran this hour
     * @param tally counts for the channel
     * @param ppm dongle frequency correction at the end of the hour, or null when unknown
     * @param ppmChange how much the correction moved during the hour
     * @param channelErrorHz average frequency error the decoders measured, or null
     * @param usbErrors USB transfer errors during the hour
     */
    public record Row(String tuner, String status, String channel, String decoder, long frequency, int minutes,
                      SignalTally tally, Double ppm, double ppmChange, Integer channelErrorHz, long usbErrors)
    {
        public boolean hasChannel()
        {
            return channel != null && !channel.isEmpty();
        }

        /**
         * Seconds of control channel lost to sync loss.  Only P25 reports sync loss, at 9600 bits a second.
         */
        public double syncLostSeconds()
        {
            return tally.getSyncLossBits() / P25_BITS_PER_SECOND;
        }

        /**
         * Decode rate when the channel had enough messages to judge, else null.
         */
        public Double goodPercent()
        {
            return tally.getGoodMessages() + tally.getBadMessages() >= MINIMUM_MESSAGES ? tally.getGoodPercent() : null;
        }
    }

    /**
     * Log lines: one per dongle, then one per channel on it.
     */
    public List<String> logLines()
    {
        List<String> lines = new ArrayList<>();
        String hour = describeHour();

        for(Map.Entry<String,List<Row>> entry: byTuner().entrySet())
        {
            Row first = entry.getValue().get(0);
            StringBuilder sb = new StringBuilder();
            sb.append("Signal quality ").append(hour).append(" - ").append(entry.getKey());

            if(!"Enabled".equalsIgnoreCase(first.status()))
            {
                sb.append(" [").append(first.status()).append("]");
            }

            if(first.ppm() != null)
            {
                sb.append(": drift ").append(String.format(Locale.US, "%+.2f", first.ppm())).append(" ppm");

                if(Math.abs(first.ppmChange()) >= 0.005)
                {
                    sb.append(" (moved ").append(String.format(Locale.US, "%+.2f", first.ppmChange())).append(")");
                }

                if(first.channelErrorHz() != null)
                {
                    sb.append(", channels off by ").append(first.channelErrorHz()).append(" Hz");
                }

                sb.append(", USB errors ").append(first.usbErrors());
            }

            lines.add(sb.toString());

            for(Row row: entry.getValue())
            {
                if(row.hasChannel())
                {
                    lines.add("    " + describeChannel(row));
                }
            }

            if(entry.getValue().stream().noneMatch(Row::hasChannel))
            {
                lines.add("    no channels this hour");
            }
        }

        return lines;
    }

    private String describeHour()
    {
        LocalDateTime start = LocalDateTime.ofInstant(Instant.ofEpochSecond(hourStart), ZoneId.systemDefault());
        return CLOCK_FORMAT.format(start) + "-" + CLOCK_FORMAT.format(start.plusHours(1));
    }

    /**
     * Plain description of one channel's hour, like "Control South 852.550 MHz P25: 97.1% of 41,203 messages decoded,
     * lost sync 3 s, signal -42.5 dB".
     */
    public static String describeChannel(Row row)
    {
        StringBuilder sb = new StringBuilder();
        sb.append(row.channel());

        if(row.frequency() > 0)
        {
            sb.append(" ").append(String.format(Locale.US, "%.4f", row.frequency() / 1E6)).append(" MHz");
        }

        if(row.decoder() != null && !row.decoder().isEmpty())
        {
            sb.append(" ").append(row.decoder());
        }

        sb.append(":");
        List<String> parts = new ArrayList<>();
        SignalTally tally = row.tally();
        long messages = tally.getGoodMessages() + tally.getBadMessages();

        if(messages > 0)
        {
            parts.add(String.format(Locale.US, "%.1f%% of %,d messages decoded", tally.getGoodPercent(), messages));
        }

        if(tally.getSyncLossBits() > 0)
        {
            parts.add(String.format(Locale.US, "lost sync %.0f s", row.syncLostSeconds()));
        }

        if(tally.getDroppedBits() > 0)
        {
            parts.add(String.format(Locale.US, "%,d bits dropped", tally.getDroppedBits()));
        }

        Double signal = tally.getSignalDb();
        Double noise = tally.getNoiseDb();

        if(signal != null)
        {
            parts.add(String.format(Locale.US, "signal %.1f dB", signal) + (noise != null ? " on calls" : ""));
        }

        if(noise != null)
        {
            parts.add(String.format(Locale.US, "noise %.1f dB", noise));
        }

        if(signal != null && noise != null)
        {
            parts.add(String.format(Locale.US, "%.1f dB over noise", signal - noise));
        }

        if(parts.isEmpty())
        {
            parts.add("nothing heard");
        }

        sb.append(" ").append(String.join(", ", parts));

        if(row.minutes() < 60)
        {
            sb.append(" (").append(row.minutes()).append(" min)");
        }

        return sb.toString();
    }

    /**
     * Short text for the Tuners tab, like "97% decoded, +0.42 ppm" or "37 dB over noise".
     */
    public static String summarize(List<Row> rows)
    {
        if(rows == null || rows.isEmpty())
        {
            return "";
        }

        SignalTally combined = new SignalTally();
        Double bestMargin = null;

        for(Row row: rows)
        {
            if(!row.hasChannel())
            {
                continue;
            }

            if(row.decoder() == null || !row.decoder().contains("FM") && !row.decoder().equals("AM"))
            {
                combined.addMessages(row.tally().getGoodMessages(), row.tally().getBadMessages());
            }

            Double signal = row.tally().getSignalDb();
            Double noise = row.tally().getNoiseDb();

            if(signal != null && noise != null && (bestMargin == null || signal - noise > bestMargin))
            {
                bestMargin = signal - noise;
            }
        }

        List<String> parts = new ArrayList<>();
        Row first = rows.get(0);

        if(combined.getGoodMessages() + combined.getBadMessages() >= MINIMUM_MESSAGES)
        {
            parts.add(String.format(Locale.US, "%.0f%% decoded", Math.floor(combined.getGoodPercent())));
        }
        else if(bestMargin != null)
        {
            parts.add(String.format(Locale.US, "%.0f dB over noise", bestMargin));
        }
        else if(rows.stream().noneMatch(Row::hasChannel))
        {
            parts.add("no channels");
        }

        if(first.ppm() != null)
        {
            parts.add(String.format(Locale.US, "%+.2f ppm", first.ppm()));
        }

        if(first.usbErrors() > 0)
        {
            parts.add(first.usbErrors() + " USB errors");
        }

        return String.join(", ", parts);
    }

    /**
     * Rows grouped by dongle, keeping report order.
     */
    public Map<String,List<Row>> byTuner()
    {
        Map<String,List<Row>> map = new LinkedHashMap<>();

        for(Row row: rows)
        {
            map.computeIfAbsent(row.tuner(), key -> new ArrayList<>()).add(row);
        }

        return map;
    }

    /**
     * Spreadsheet rows, matching CSV_HEADER.
     */
    public List<String> csvLines()
    {
        List<String> lines = new ArrayList<>();
        String hour = HOUR_FORMAT.format(LocalDateTime.ofInstant(Instant.ofEpochSecond(hourStart), ZoneId.systemDefault()));

        for(Row row: rows)
        {
            SignalTally tally = row.tally();
            List<String> cells = new ArrayList<>();
            cells.add(hour);
            cells.add(csv(row.tuner()));
            cells.add(csv(row.status()));
            cells.add(csv(row.channel()));
            cells.add(csv(row.decoder()));
            cells.add(row.frequency() > 0 ? String.format(Locale.US, "%.5f", row.frequency() / 1E6) : "");
            cells.add(row.hasChannel() ? String.valueOf(row.minutes()) : "");
            cells.add(row.hasChannel() ? String.valueOf(tally.getGoodMessages()) : "");
            cells.add(row.hasChannel() ? String.valueOf(tally.getBadMessages()) : "");
            cells.add(number(row.goodPercent(), "%.1f"));
            cells.add(row.hasChannel() ? String.format(Locale.US, "%.1f", row.syncLostSeconds()) : "");
            cells.add(number(tally.getSignalDb(), "%.1f"));
            cells.add(number(tally.getNoiseDb(), "%.1f"));
            cells.add(number(row.ppm(), "%.3f"));
            cells.add(row.ppm() != null ? String.format(Locale.US, "%.3f", row.ppmChange()) : "");
            cells.add(row.channelErrorHz() != null ? String.valueOf(row.channelErrorHz()) : "");
            cells.add(row.ppm() != null ? String.valueOf(row.usbErrors()) : "");
            lines.add(String.join(",", cells));
        }

        return lines;
    }

    private static String number(Double value, String format)
    {
        return value == null ? "" : String.format(Locale.US, format, value);
    }

    private static String csv(String value)
    {
        if(value == null)
        {
            return "";
        }

        if(value.contains(",") || value.contains("\"") || value.contains("\n"))
        {
            return "\"" + value.replace("\"", "\"\"") + "\"";
        }

        return value;
    }

    /**
     * JSON body for the call transcriber's /api/signal address.
     * @param apiKey for the transcriber, or empty
     */
    public String toJson(String apiKey)
    {
        Map<String,Object> body = new LinkedHashMap<>();
        body.put("key", apiKey == null ? "" : apiKey);
        body.put("hourStart", hourStart);
        List<Map<String,Object>> list = new ArrayList<>();

        for(Row row: rows)
        {
            Map<String,Object> map = new LinkedHashMap<>();
            map.put("tuner", row.tuner());
            map.put("status", row.status());
            map.put("channel", row.hasChannel() ? row.channel() : null);
            map.put("decoder", row.decoder());
            map.put("frequency", row.frequency() > 0 ? row.frequency() : null);
            map.put("minutes", row.minutes());
            map.put("messagesGood", row.tally().getGoodMessages());
            map.put("messagesBad", row.tally().getBadMessages());
            map.put("goodPercent", round(row.goodPercent()));
            map.put("syncLostSeconds", round(row.syncLostSeconds()));
            map.put("signalDb", round(row.tally().getSignalDb()));
            map.put("noiseDb", round(row.tally().getNoiseDb()));
            map.put("ppm", row.ppm() == null ? null : Math.round(row.ppm() * 1000.0) / 1000.0);
            map.put("ppmChange", Math.round(row.ppmChange() * 1000.0) / 1000.0);
            map.put("channelErrorHz", row.channelErrorHz());
            map.put("usbErrors", row.usbErrors());
            list.add(map);
        }

        body.put("rows", list);

        try
        {
            return MAPPER.writeValueAsString(body);
        }
        catch(Exception e)
        {
            throw new IllegalStateException("Couldn't build signal quality JSON", e);
        }
    }

    private static Double round(Double value)
    {
        return value == null ? null : Math.round(value * 10.0) / 10.0;
    }
}
