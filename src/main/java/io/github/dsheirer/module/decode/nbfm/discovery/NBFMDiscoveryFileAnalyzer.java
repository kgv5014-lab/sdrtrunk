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

import io.github.dsheirer.sample.Listener;
import java.io.BufferedInputStream;
import java.io.File;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import javax.sound.sampled.AudioFormat;
import javax.sound.sampled.AudioInputStream;
import javax.sound.sampled.AudioSystem;
import javax.sound.sampled.UnsupportedAudioFileException;

/**
 * Runs NBFM carrier discovery against a baseband I/Q recording made with sdrtrunk's tuner recording feature
 * (16-bit stereo WAV, I = left, Q = right) and prints each transmission and a per-frequency summary.
 *
 * Usage: NBFMDiscoveryFileAnalyzer recording.wav centerFrequencyHz [detectionThresholdDb]
 */
public class NBFMDiscoveryFileAnalyzer
{
    private static final int SAMPLES_PER_READ = 65536;

    private static final Pattern RECORDING_FREQUENCY = Pattern.compile("_(\\d{7,10})_baseband");

    /**
     * Analyzes the recording.
     * @param file baseband recording
     * @param centerFrequency tuner centre frequency of the recording in Hertz
     * @param processor configured discovery processor
     * @return events in the order they occurred
     */
    public static List<DiscoveryEvent> analyze(File file, long centerFrequency, NBFMDiscoveryProcessor processor)
            throws IOException, UnsupportedAudioFileException
    {
        List<DiscoveryEvent> events = new ArrayList<>();
        analyze(file, centerFrequency, processor, events::add);
        return events;
    }

    /**
     * Analyzes the recording, delivering each event to the listener as it occurs.  Stops early if the calling thread
     * is interrupted.
     * @param file baseband recording
     * @param centerFrequency tuner centre frequency of the recording in Hertz
     * @param processor configured discovery processor
     * @param listener to receive events
     */
    public static void analyze(File file, long centerFrequency, NBFMDiscoveryProcessor processor,
                               Listener<DiscoveryEvent> listener) throws IOException, UnsupportedAudioFileException
    {
        processor.setListener(listener);

        try(AudioInputStream stream = AudioSystem.getAudioInputStream(new BufferedInputStream(
                Files.newInputStream(file.toPath()))))
        {
            AudioFormat format = stream.getFormat();

            if(format.getChannels() != 2 || format.getSampleSizeInBits() != 16 ||
                    format.getEncoding() != AudioFormat.Encoding.PCM_SIGNED)
            {
                throw new UnsupportedAudioFileException("Expected 16-bit signed stereo I/Q recording but found: " +
                        format);
            }

            processor.setTuning(centerFrequency, format.getSampleRate());

            byte[] bytes = new byte[SAMPLES_PER_READ * 4];
            boolean bigEndian = format.isBigEndian();
            int read;

            while((read = stream.readNBytes(bytes, 0, bytes.length)) > 0 && !Thread.currentThread().isInterrupted())
            {
                int count = read / 4;
                float[] i = new float[count];
                float[] q = new float[count];

                for(int x = 0; x < count; x++)
                {
                    i[x] = sample(bytes, x * 4, bigEndian);
                    q[x] = sample(bytes, x * 4 + 2, bigEndian);
                }

                processor.process(i, q);
            }
        }

        processor.getTracker().clear(processor.getTimestamp());
    }

    /**
     * Tuner centre frequency from the name of a recording made by sdrtrunk's tuner recorder, which is named
     * {tuner}_{frequency}_baseband_{timestamp}.wav
     * @param filename of the recording
     * @return centre frequency in Hertz, or null if the name doesn't contain one
     */
    public static Long getCenterFrequency(String filename)
    {
        Matcher matcher = RECORDING_FREQUENCY.matcher(filename);
        return matcher.find() ? Long.parseLong(matcher.group(1)) : null;
    }

    private static float sample(byte[] bytes, int offset, boolean bigEndian)
    {
        int high = bigEndian ? bytes[offset] : bytes[offset + 1];
        int low = bigEndian ? bytes[offset + 1] : bytes[offset];
        return (short)((high << 8) | (low & 0xFF)) / 32768.0f;
    }

    public static void main(String[] args) throws Exception
    {
        if(args.length < 2)
        {
            System.out.println("Usage: NBFMDiscoveryFileAnalyzer recording.wav centerFrequencyHz [detectionThresholdDb]");
            return;
        }

        DiscoveryConfiguration configuration = new DiscoveryConfiguration();

        if(args.length > 2)
        {
            configuration.setDetectionThresholdDb(Double.parseDouble(args[2]));
        }

        NBFMDiscoveryProcessor processor = new NBFMDiscoveryProcessor(configuration);
        List<DiscoveryEvent> events = analyze(Path.of(args[0]).toFile(), Long.parseLong(args[1]), processor);

        System.out.println("Transmissions:");

        for(DiscoveryEvent event : events)
        {
            System.out.printf("  %8.1f s  %s%n", event.timestamp() / 1000.0, event);
        }

        System.out.println("Frequencies heard:");

        for(DiscoveredFrequency frequency : processor.getTracker().getHistory())
        {
            System.out.println("  " + frequency);
        }
    }
}
