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
package io.github.dsheirer.module.decode.nbfm.discovery;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.ByteArrayInputStream;
import java.io.File;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import javax.sound.sampled.AudioFileFormat;
import javax.sound.sampled.AudioFormat;
import javax.sound.sampled.AudioInputStream;
import javax.sound.sampled.AudioSystem;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/**
 * End-to-end discovery from complex samples to transmission events.
 */
public class NBFMDiscoveryProcessorTest
{
    private static final double SAMPLE_RATE = 2_400_000;
    private static final long CENTER = 155_400_000;
    private static final int TIMING_TOLERANCE_MS = 60;

    private static DiscoveryEvent find(List<DiscoveryEvent> events, DiscoveryEvent.Type type, long frequency)
    {
        return events.stream().filter(e -> e.type() == type && e.carrier().getChannelFrequency() == frequency)
                .findFirst().orElseThrow(() -> new AssertionError("No " + type + " for " + frequency + " in " + events));
    }

    @Test
    void reportsEachTransmissionWithCorrectTiming()
    {
        DiscoveryConfiguration configuration = new DiscoveryConfiguration();
        NBFMDiscoveryProcessor processor = new NBFMDiscoveryProcessor(configuration);
        List<DiscoveryEvent> events = new ArrayList<>();
        processor.setListener(events::add);
        processor.setTuning(CENTER, SAMPLE_RATE);

        new SyntheticBaseband(SAMPLE_RATE, 0.01)
                .addFmCarrier(155_752_500 - CENTER, 0.02, 2500, 0.2, 1.2)
                .addFmCarrier(154_430_000 - CENTER, 0.005, 2500, 0.5, 1.0)
                .feed(processor, 2.2);

        assertEquals(4, events.size(), "Expected start and end of two transmissions: " + events);

        long attack = configuration.getAttackTimeMs();
        long release = configuration.getReleaseTimeMs();
        assertEquals(200 + attack, find(events, DiscoveryEvent.Type.STARTED, 155_752_500).timestamp(),
                TIMING_TOLERANCE_MS);
        assertEquals(1200 + release, find(events, DiscoveryEvent.Type.ENDED, 155_752_500).timestamp(),
                TIMING_TOLERANCE_MS);
        assertEquals(500 + attack, find(events, DiscoveryEvent.Type.STARTED, 154_430_000).timestamp(),
                TIMING_TOLERANCE_MS);
        assertEquals(1000 + release, find(events, DiscoveryEvent.Type.ENDED, 154_430_000).timestamp(),
                TIMING_TOLERANCE_MS);

        TrackedCarrier strong = find(events, DiscoveryEvent.Type.ENDED, 155_752_500).carrier();
        assertEquals(1000, strong.getDuration(), TIMING_TOLERANCE_MS);
        assertEquals(0, strong.getFrequencyError(), 300, "Measured frequency error: " + strong);
    }

    @Test
    void retuneEndsActiveCarriers()
    {
        NBFMDiscoveryProcessor processor = new NBFMDiscoveryProcessor(new DiscoveryConfiguration());
        List<DiscoveryEvent> events = new ArrayList<>();
        processor.setListener(events::add);
        processor.setTuning(CENTER, SAMPLE_RATE);

        SyntheticBaseband baseband = new SyntheticBaseband(SAMPLE_RATE, 0.01)
                .addFmCarrier(155_752_500 - CENTER, 0.02, 2500, 0, 10);

        baseband.feed(processor, 0.5);
        assertEquals(1, events.size());

        processor.setTuning(CENTER + 1_000_000, SAMPLE_RATE);
        baseband.feed(processor, 0.05);

        assertEquals(DiscoveryEvent.Type.ENDED, events.getLast().type());
    }

    @Test
    void analyzesBasebandRecording(@TempDir Path directory) throws Exception
    {
        //sdrtrunk tuner recordings are 16-bit little endian stereo WAV files: I = left, Q = right
        double sampleRate = 1_200_000;
        long center = 154_330_000;
        float[][] samples = new SyntheticBaseband(sampleRate, 0.01)
                .addFmCarrier(100_000, 0.02, 2500, 0.2, 1.0)
                .next((int)(sampleRate * 1.5));

        byte[] bytes = new byte[samples[0].length * 4];

        for(int x = 0; x < samples[0].length; x++)
        {
            short i = (short)Math.round(samples[0][x] * 32767);
            short q = (short)Math.round(samples[1][x] * 32767);
            bytes[4 * x] = (byte)i;
            bytes[4 * x + 1] = (byte)(i >> 8);
            bytes[4 * x + 2] = (byte)q;
            bytes[4 * x + 3] = (byte)(q >> 8);
        }

        AudioFormat format = new AudioFormat((float)sampleRate, 16, 2, true, false);
        File file = directory.resolve("baseband.wav").toFile();
        AudioSystem.write(new AudioInputStream(new ByteArrayInputStream(bytes), format, samples[0].length),
                AudioFileFormat.Type.WAVE, file);

        NBFMDiscoveryProcessor processor = new NBFMDiscoveryProcessor(new DiscoveryConfiguration());
        List<DiscoveryEvent> events = NBFMDiscoveryFileAnalyzer.analyze(file, center, processor);

        assertEquals(List.of(DiscoveryEvent.Type.STARTED, DiscoveryEvent.Type.ENDED),
                events.stream().map(DiscoveryEvent::type).toList());
        assertEquals(1, processor.getTracker().getHistory().size());

        DiscoveredFrequency heard = processor.getTracker().getHistory().iterator().next();
        assertEquals(154_430_000, heard.getFrequency());
        assertTrue(heard.getTotalAirtimeMs() > 700, "Airtime: " + heard);
    }
}
