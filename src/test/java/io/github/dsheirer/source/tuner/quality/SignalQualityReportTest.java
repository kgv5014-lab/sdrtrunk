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

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.util.List;
import org.junit.jupiter.api.Test;

public class SignalQualityReportTest
{
    private static SignalTally controlChannel()
    {
        SignalTally tally = new SignalTally();

        for(int x = 0; x < 970; x++)
        {
            tally.addMessage(true);
        }

        for(int x = 0; x < 30; x++)
        {
            tally.addMessage(false);
        }

        tally.addSyncLoss(9600 * 3);

        for(int x = 0; x < 100; x++)
        {
            tally.addPower(-42.0, true);
        }

        return tally;
    }

    private static SignalTally fireChannel()
    {
        SignalTally tally = new SignalTally();

        for(int x = 0; x < 50; x++)
        {
            tally.addPower(-61.0, true);
        }

        for(int x = 0; x < 500; x++)
        {
            tally.addPower(-98.0 + (x % 3) * 0.5, false);
        }

        return tally;
    }

    private static SignalQualityReport report()
    {
        return new SignalQualityReport(1_793_000_000L - 1_793_000_000L % 3600, List.of(
            new SignalQualityReport.Row("R820T 00000011", "Enabled", "Control South", "P25-1", 852_550_000, 60,
                controlChannel(), 0.42, 0.05, 12, 0),
            new SignalQualityReport.Row("R828D 00000003", "Enabled", "Nancy FD", "NBFM", 502_425_000, 60,
                fireChannel(), -1.5, 0.0, null, 2),
            new SignalQualityReport.Row("R828D 00000004", "Enabled", "", "", 0, 0, new SignalTally(), 0.0, 0.0,
                null, 0)));
    }

    @Test
    public void tallyMedians()
    {
        SignalTally tally = fireChannel();
        assertEquals(-61.0, tally.getSignalDb(), 0.01);
        assertEquals(-97.5, tally.getNoiseDb(), 0.51);
        assertEquals(97.0, controlChannel().getGoodPercent(), 0.01);
        assertNull(new SignalTally().getGoodPercent());

        SignalTally few = new SignalTally();
        few.addPower(-50, true);
        assertNull(few.getSignalDb(), "too few readings to report a level");

        SignalTally sum = new SignalTally();
        sum.add(controlChannel());
        sum.add(controlChannel());
        assertEquals(1940, sum.getGoodMessages());
        assertEquals(60, sum.getBadMessages());
    }

    @Test
    public void logLines()
    {
        List<String> lines = report().logLines();
        assertTrue(lines.get(0).contains("R820T 00000011: drift +0.42 ppm (moved +0.05), channels off by 12 Hz, USB errors 0"),
            lines.get(0));
        assertTrue(lines.get(1).contains("Control South 852.5500 MHz P25-1: 97.0% of 1,000 messages decoded, lost sync 3 s, signal -42.0 dB"),
            lines.get(1));
        assertTrue(lines.get(3).contains("Nancy FD 502.4250 MHz NBFM: signal -61.0 dB on calls, noise -97.5 dB, 36.5 dB over noise")
            || lines.get(3).contains("noise -98.0 dB"), lines.get(3));
        assertTrue(lines.get(5).contains("no channels this hour"), lines.get(5));
    }

    @Test
    public void summaries()
    {
        var byTuner = report().byTuner();
        assertEquals("97% decoded, +0.42 ppm", SignalQualityReport.summarize(byTuner.get("R820T 00000011")));
        assertTrue(SignalQualityReport.summarize(byTuner.get("R828D 00000003")).matches("3[67] dB over noise, -1.50 ppm, 2 USB errors"));
        assertEquals("no channels, +0.00 ppm", SignalQualityReport.summarize(byTuner.get("R828D 00000004")));
    }

    @Test
    public void csvAndJson() throws Exception
    {
        SignalQualityReport report = report();
        List<String> csv = report.csvLines();
        assertEquals(3, csv.size());
        assertEquals(SignalQualityReport.CSV_HEADER.split(",").length, csv.get(0).split(",", -1).length);
        assertTrue(csv.get(0).contains(",Control South,P25-1,852.55000,60,970,30,97.0,3.0,-42.0,,0.420,0.050,12,0"),
            csv.get(0));

        JsonNode json = new ObjectMapper().readTree(report.toJson("abc"));
        assertEquals("abc", json.get("key").asText());
        assertEquals(3, json.get("rows").size());
        JsonNode control = json.get("rows").get(0);
        assertEquals(97.0, control.get("goodPercent").asDouble(), 0.01);
        assertEquals(3.0, control.get("syncLostSeconds").asDouble(), 0.01);
        assertTrue(json.get("rows").get(2).get("channel").isNull());
    }

    @Test
    public void signalUrl()
    {
        assertEquals("http://localhost:3000/api/signal", SignalQualityMonitor.signalUrl(null));
        assertEquals("http://192.168.1.5:3000/api/signal", SignalQualityMonitor.signalUrl("http://192.168.1.5:3000/api/page"));
        assertEquals("http://host:3000/api/signal", SignalQualityMonitor.signalUrl("http://host:3000/"));
    }
}
