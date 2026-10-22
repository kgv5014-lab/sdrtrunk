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
package io.github.dsheirer.module.decode.twotone;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.ArrayList;
import java.util.List;
import java.util.Random;
import org.junit.jupiter.api.Test;

/**
 * Two-tone paging detection against synthetic 8 kHz demodulated audio.
 */
public class TwoToneDetectorTest
{
    private static final int RATE = TwoToneDetector.SAMPLE_RATE;

    /**
     * Builds audio piece by piece: tones, silence, noise and voice-like sound, with optional CTCSS and noise mixed in.
     */
    private static class Audio
    {
        private final List<Float> mSamples = new ArrayList<>();
        private final Random mRandom = new Random(42);
        private double mPhase = 0;

        Audio tone(double hz, double seconds)
        {
            for(int x = 0; x < (int)(seconds * RATE); x++)
            {
                mPhase += 2 * Math.PI * hz / RATE;
                mSamples.add((float)(0.5 * Math.sin(mPhase)));
            }
            return this;
        }

        Audio silence(double seconds)
        {
            for(int x = 0; x < (int)(seconds * RATE); x++)
            {
                mSamples.add(0f);
            }
            return this;
        }

        /**
         * Voice-like sound: a gliding pitch with many harmonics and a syllable rhythm.
         */
        Audio voice(double seconds)
        {
            double phase = 0;
            for(int x = 0; x < (int)(seconds * RATE); x++)
            {
                double t = x / (double)RATE;
                double pitch = 120 + 40 * Math.sin(2 * Math.PI * 0.7 * t) + 15 * Math.sin(2 * Math.PI * 3.1 * t);
                phase += 2 * Math.PI * pitch / RATE;
                double sample = 0;
                for(int harmonic = 1; harmonic <= 20; harmonic++)
                {
                    //Formant-ish weighting that moves with the syllables
                    double hz = harmonic * pitch;
                    double formant = 500 + 300 * Math.sin(2 * Math.PI * 4 * t);
                    double weight = Math.exp(-Math.pow((hz - formant) / 400, 2)) + 0.3 * Math.exp(-Math.pow((hz - 1800) / 500, 2));
                    sample += weight * Math.sin(harmonic * phase);
                }
                double envelope = 0.5 + 0.5 * Math.sin(2 * Math.PI * 4 * t);
                mSamples.add((float)(0.15 * envelope * sample));
            }
            return this;
        }

        Audio noise(double seconds, double sigma)
        {
            for(int x = 0; x < (int)(seconds * RATE); x++)
            {
                mSamples.add((float)(sigma * mRandom.nextGaussian()));
            }
            return this;
        }

        /**
         * Adds a CTCSS tone and white noise across everything built so far.
         */
        Audio mix(double ctcssHz, double noiseSigma)
        {
            for(int x = 0; x < mSamples.size(); x++)
            {
                double extra = 0.1 * Math.sin(2 * Math.PI * ctcssHz * x / RATE) + noiseSigma * mRandom.nextGaussian();
                mSamples.set(x, (float)(mSamples.get(x) + extra));
            }
            return this;
        }

        /**
         * Runs the audio through a detector in buffers of uneven size, as the decoder receives it.
         */
        List<TwoToneDetector.Page> detect()
        {
            List<TwoToneDetector.Page> pages = new ArrayList<>();
            TwoToneDetector detector = new TwoToneDetector(pages::add);
            int offset = 0;
            int size = 173;
            while(offset < mSamples.size())
            {
                int count = Math.min(size, mSamples.size() - offset);
                float[] buffer = new float[count];
                for(int x = 0; x < count; x++)
                {
                    buffer[x] = mSamples.get(offset + x);
                }
                detector.receive(buffer);
                offset += count;
                size = size == 173 ? 811 : 173;
            }
            detector.flush();
            return pages;
        }
    }

    private static void assertPage(TwoToneDetector.Page page, double toneA, double toneB)
    {
        assertEquals(toneA, page.toneA(), 1.0, "tone A of " + page);
        if(Double.isNaN(toneB))
        {
            assertTrue(page.isLongTone(), "expected long tone " + page);
        }
        else
        {
            assertEquals(toneB, page.toneB(), 1.0, "tone B of " + page);
        }
    }

    @Test
    public void standardQuickCallPage()
    {
        List<TwoToneDetector.Page> pages = new Audio().silence(0.5).tone(349.0, 1.0).tone(832.5, 3.0).silence(1)
                .detect();
        assertEquals(1, pages.size(), pages.toString());
        assertPage(pages.get(0), 349.0, 832.5);
        assertEquals(1.0, pages.get(0).toneASeconds(), 0.15);
    }

    @Test
    public void pageWithCtcssAndNoiseThenVoice()
    {
        List<TwoToneDetector.Page> pages = new Audio().noise(0.3, 0.05).tone(1153.4, 1.0).tone(1981.5, 3.0)
                .voice(6).mix(127.3, 0.05).detect();
        assertEquals(1, pages.size(), pages.toString());
        assertPage(pages.get(0), 1153.4, 1981.5);
    }

    @Test
    public void heavyNoiseStillDecodes()
    {
        //Noise power about the same as the tone power (0 dB SNR across the whole 4 kHz audio band)
        List<TwoToneDetector.Page> pages = new Audio().tone(433.7, 1.0).tone(569.1, 3.0).mix(0, 0.35).detect();
        assertEquals(1, pages.size(), pages.toString());
        assertPage(pages.get(0), 433.7, 569.1);
    }

    @Test
    public void stackedPagesForSeveralStations()
    {
        List<TwoToneDetector.Page> pages = new Audio().tone(349.0, 1.0).tone(832.5, 3.0)
                .tone(368.5, 1.0).tone(707.3, 3.0)
                .silence(0.1).tone(389.0, 1.0).tone(1006.9, 3.0).silence(1).detect();
        assertEquals(3, pages.size(), pages.toString());
        assertPage(pages.get(0), 349.0, 832.5);
        assertPage(pages.get(1), 368.5, 707.3);
        assertPage(pages.get(2), 389.0, 1006.9);
    }

    @Test
    public void shortTonesStillPair()
    {
        //Some agencies use half-second A and B tones
        List<TwoToneDetector.Page> pages = new Audio().tone(600.9, 0.5).tone(1255.4, 0.6).silence(1).detect();
        assertEquals(1, pages.size(), pages.toString());
        assertPage(pages.get(0), 600.9, 1255.4);
    }

    @Test
    public void longToneIsReportedAlone()
    {
        List<TwoToneDetector.Page> pages = new Audio().silence(0.5).tone(1084.0, 8.0).silence(1).detect();
        assertEquals(1, pages.size(), pages.toString());
        assertPage(pages.get(0), 1084.0, Double.NaN);
    }

    @Test
    public void toneThatStopsBetweenPairsIsNotAPage()
    {
        //A one second gap is too long for tone A and B to belong together
        List<TwoToneDetector.Page> pages = new Audio().tone(349.0, 1.0).silence(1.0).tone(832.5, 3.0).silence(1)
                .detect();
        assertEquals(0, pages.size(), pages.toString());
    }

    @Test
    public void voiceNoiseAndCtcssAloneAreNotPages()
    {
        List<TwoToneDetector.Page> pages = new Audio().voice(20).noise(10, 0.3).voice(10).mix(100.0, 0.02).detect();
        assertEquals(0, pages.size(), pages.toString());
    }

    @Test
    public void briefBeepsAreNotPages()
    {
        //Roger beeps and short alert tones
        Audio audio = new Audio();
        for(int x = 0; x < 10; x++)
        {
            audio.voice(2).tone(1000 + x * 100, 0.15).tone(1500, 0.1).silence(0.5);
        }
        List<TwoToneDetector.Page> pages = audio.detect();
        assertEquals(0, pages.size(), pages.toString());
    }
}
