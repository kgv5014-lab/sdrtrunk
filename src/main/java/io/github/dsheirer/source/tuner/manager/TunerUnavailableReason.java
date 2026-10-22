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

package io.github.dsheirer.source.tuner.manager;

import io.github.dsheirer.source.tuner.Tuner;
import io.github.dsheirer.source.tuner.TunerController;
import io.github.dsheirer.source.tuner.channel.TunerChannel;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.SortedSet;

/**
 * Explains, in plain words, why none of the discovered tuners could provide a channel source, so that the user sees
 * more than "No Tuner Available".
 */
public class TunerUnavailableReason
{
    /**
     * Snapshot of a discovered tuner's state, used to explain why it can't source a channel.
     *
     * @param name of the tuner as shown to the user
     * @param status when the tuner isn't usable (disabled, error, removed), otherwise null
     * @param minimum tunable frequency in hertz
     * @param maximum tunable frequency in hertz
     * @param center frequency the tuner is currently tuned to, in hertz
     * @param usableBandwidth of the tuner in hertz
     * @param channelCount number of channels the tuner is currently providing
     * @param channelsLow lowest frequency in use by the current channels, in hertz
     * @param channelsHigh highest frequency in use by the current channels, in hertz
     */
    public record TunerSnapshot(String name, String status, long minimum, long maximum, long center,
                                int usableBandwidth, int channelCount, long channelsLow, long channelsHigh)
    {
        /**
         * Creates a snapshot of a tuner that isn't usable.
         */
        public static TunerSnapshot unusable(String name, String status)
        {
            return new TunerSnapshot(name, status, 0, 0, 0, 0, 0, 0, 0);
        }

        /**
         * Creates a snapshot from a discovered tuner.
         */
        public static TunerSnapshot of(DiscoveredTuner discoveredTuner)
        {
            if(!discoveredTuner.hasTuner())
            {
                String status = discoveredTuner.getTunerStatus().toString();

                if(discoveredTuner.hasErrorMessage())
                {
                    status = status + " - " + discoveredTuner.getErrorMessage();
                }

                return unusable(discoveredTuner.toString(), status);
            }

            Tuner tuner = discoveredTuner.getTuner();
            TunerController controller = tuner.getTunerController();
            SortedSet<TunerChannel> channels = tuner.getChannelSourceManager().getTunerChannels();
            long low = channels.isEmpty() ? 0 : channels.first().getMinFrequency();
            long high = channels.isEmpty() ? 0 : channels.last().getMaxFrequency();

            return new TunerSnapshot(tuner.getPreferredName(), null, controller.getMinimumFrequency(),
                controller.getMaximumFrequency(), controller.getFrequency(), controller.getUsableBandwidth(),
                channels.size(), low, high);
        }
    }

    /**
     * Describes why none of the tuners could provide the requested channel.
     *
     * @param tunerChannel that was requested
     * @param preferredTuner name, or null when the channel has no preferred tuner
     * @param tuners that were tried
     * @return a multi-line explanation
     */
    public static String describe(TunerChannel tunerChannel, String preferredTuner, List<TunerSnapshot> tuners)
    {
        List<String> lines = new ArrayList<>();
        lines.add("No tuner can receive " + mhz(tunerChannel.getFrequency()) + " MHz.");

        if(tuners.isEmpty())
        {
            lines.add("No tuners were found. Plug in an SDR, or check the Tuners tab in User Preferences.");
            return String.join("\n", lines);
        }

        if(preferredTuner != null && tuners.stream().noneMatch(tuner -> preferredTuner.equalsIgnoreCase(tuner.name())))
        {
            lines.add("The preferred tuner [" + preferredTuner + "] isn't connected.");
        }

        for(TunerSnapshot tuner: tuners)
        {
            lines.add(tuner.name() + ": " + describe(tunerChannel, tuner));
        }

        return String.join("\n", lines);
    }

    /**
     * Describes why a single tuner can't provide the requested channel.
     */
    private static String describe(TunerChannel tunerChannel, TunerSnapshot tuner)
    {
        if(tuner.status() != null)
        {
            return "not usable (" + tuner.status() + ")";
        }

        if(tunerChannel.getMinFrequency() < tuner.minimum() || tunerChannel.getMaxFrequency() > tuner.maximum())
        {
            return "out of range, it tunes " + mhz(tuner.minimum()) + " to " + mhz(tuner.maximum()) + " MHz";
        }

        if(tuner.channelCount() > 0)
        {
            long low = Math.min(tuner.channelsLow(), tunerChannel.getMinFrequency());
            long high = Math.max(tuner.channelsHigh(), tunerChannel.getMaxFrequency());

            return "busy, tuned to " + mhz(tuner.center()) + " MHz for " + tuner.channelCount() + " channel" +
                (tuner.channelCount() == 1 ? "" : "s") + " (" + mhz(tuner.channelsLow()) + " to " +
                mhz(tuner.channelsHigh()) + " MHz). Adding this channel needs " + mhz(high - low) +
                " MHz of bandwidth but the tuner has " + mhz(tuner.usableBandwidth()) + " MHz usable.";
        }

        return "couldn't tune to the channel, see the application log for details";
    }

    /**
     * Describes in one line how each tuner could receive the frequency, for display beside a channel's frequency.
     *
     * @param frequency in hertz
     * @param tuners to evaluate
     * @return coverage description
     */
    public static Coverage coverage(long frequency, List<TunerSnapshot> tuners)
    {
        if(tuners.isEmpty())
        {
            return new Coverage(Coverage.Level.NONE, "No tuners found");
        }

        Coverage.Level level = Coverage.Level.NONE;
        List<String> parts = new ArrayList<>();

        for(TunerSnapshot tuner: tuners)
        {
            if(tuner.status() != null)
            {
                parts.add(tuner.name() + " not usable (" + tuner.status() + ")");
            }
            else if(frequency < tuner.minimum() || frequency > tuner.maximum())
            {
                parts.add(tuner.name() + " out of range");
            }
            else if(tuner.channelCount() == 0 || (Math.max(tuner.channelsHigh(), frequency) -
                Math.min(tuner.channelsLow(), frequency)) <= tuner.usableBandwidth())
            {
                parts.add(tuner.name() + " can receive it now");
                level = Coverage.Level.NOW;
            }
            else
            {
                parts.add(tuner.name() + " in range, busy at " + mhz(tuner.center()) + " MHz");

                if(level == Coverage.Level.NONE)
                {
                    level = Coverage.Level.WHEN_FREE;
                }
            }
        }

        return new Coverage(level, String.join(" | ", parts));
    }

    /**
     * Tuner coverage for a frequency
     * @param level best coverage across all tuners
     * @param description of each tuner's coverage
     */
    public record Coverage(Level level, String description)
    {
        public enum Level {NOW, WHEN_FREE, NONE}
    }

    /**
     * Formats a frequency in hertz as megahertz, dropping unneeded trailing zeros (e.g. 154.43 or 851.0125)
     */
    private static String mhz(long hertz)
    {
        String formatted = String.format(Locale.US, "%.4f", hertz / 1E6);
        formatted = formatted.replaceAll("0+$", "");
        return formatted.endsWith(".") ? formatted + "0" : formatted;
    }
}
