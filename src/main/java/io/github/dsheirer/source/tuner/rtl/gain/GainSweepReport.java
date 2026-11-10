/*
 * *****************************************************************************
 * Copyright (C) 2014-2022 Dennis Sheirer
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
package io.github.dsheirer.source.tuner.rtl.gain;

import io.github.dsheirer.source.tuner.channel.TunerChannel;
import java.util.List;

/**
 * Words and table shown after a gain sweep.
 */
public final class GainSweepReport
{
    private GainSweepReport()
    {
    }

    private static String db(int gain)
    {
        return String.format("%.1f dB", gain / 10.0);
    }

    /**
     * Why the gain was chosen, in a sentence.
     */
    public static String reason(GainOptimizer.Result result)
    {
        GainSelector.Choice choice = result.choice();

        return switch(choice.rule())
        {
            case SIGNAL -> "It is the lowest gain where " + names(result) + " within " +
                    (int)GainSelector.SNR_TOLERANCE_DB + " dB of the furthest above the noise it gets.";
            case NOISE_FLOOR -> "No channel had a steady signal to measure, so this is the lowest gain where noise " +
                    "from the antenna, not the tuner itself, sets the noise floor.  More gain than this can't make " +
                    "weak signals any clearer.";
            case HIGHEST_CLEAN -> "The noise floor never rose much, so the tuner's own noise is the limit: this is " +
                    "the highest gain that didn't overload.";
            case ALL_OVERLOADED -> "Every gain overloaded the tuner.  A very strong signal is nearby: this is the " +
                    "lowest gain, and a filter or attenuator would help.";
        };
    }

    private static String names(GainOptimizer.Result result)
    {
        List<Integer> steady = result.choice().steadyTargets();

        if(steady.size() == 1)
        {
            return mhz(result.targets().get(steady.getFirst())) + " MHz stands";
        }

        return "the weakest of the " + steady.size() + " steady signals stands";
    }

    private static String mhz(TunerChannel channel)
    {
        return String.format("%.4f", channel.getFrequency() / 1E6);
    }

    /**
     * The result as HTML for a dialog.
     * @param originalGain the tuner had, in tenths of a dB
     */
    public static String html(GainOptimizer.Result result, int originalGain)
    {
        GainSelector.Choice choice = result.choice();
        List<Integer> steady = choice.steadyTargets();
        StringBuilder sb = new StringBuilder("<html><b>Best gain: ").append(db(choice.gain())).append("</b>");
        sb.append(choice.gain() == originalGain ? " (what it is set to now)" : " (it is set to " + db(originalGain) + ")");
        sb.append("<br><div style='width:420px'>").append(reason(result)).append("</div><br>");
        sb.append("<table cellpadding='1' cellspacing='0'><tr><th align='left'>Gain&nbsp;&nbsp;</th>")
                .append("<th align='right'>Noise floor&nbsp;&nbsp;</th>");

        for(int target : steady)
        {
            sb.append("<th align='right'>").append(mhz(result.targets().get(target))).append("&nbsp;&nbsp;</th>");
        }

        sb.append("<th align='left'>Overload</th></tr>");

        for(GainMeasurement m : choice.measurements())
        {
            boolean chosen = m.gain() == choice.gain();
            sb.append(chosen ? "<tr bgcolor='#B6E3B6'>" : "<tr>");
            sb.append("<td>").append(db(m.gain())).append(m.gain() == originalGain ? " (now)" : "").append("&nbsp;&nbsp;</td>");
            sb.append("<td align='right'>").append(String.format("%.1f", m.floorDb())).append("&nbsp;&nbsp;</td>");

            for(int target : steady)
            {
                double snr = m.snrDb()[target];
                sb.append("<td align='right'>").append(Double.isNaN(snr) ? "-" : String.format("+%.1f", snr))
                        .append("&nbsp;&nbsp;</td>");
            }

            sb.append("<td>").append(m.overloaded() ? String.format("yes (%.1f%%)", m.clipFraction() * 100) : "")
                    .append("</td></tr>");
        }

        sb.append("</table>");

        if(!steady.isEmpty())
        {
            sb.append("<br>The frequency columns show how far the signal stands above the noise, in dB.");
        }

        return sb.append("</html>").toString();
    }
}
