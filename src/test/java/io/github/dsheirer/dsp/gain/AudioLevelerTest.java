package io.github.dsheirer.dsp.gain;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.Random;
import org.junit.jupiter.api.Test;

public class AudioLevelerTest
{
    private static final int RATE = 8000;

    /**
     * Speech-like test signal: noise bursts with gaps, which has a high peak to average ratio like real speech.
     */
    private static float[] speech(float amplitude, double seconds, long seed)
    {
        Random random = new Random(seed);
        float[] samples = new float[(int)(seconds * RATE)];
        for(int x = 0; x < samples.length; x++)
        {
            boolean talking = (x / 1600) % 4 != 3; //200 ms words with a pause every 4th
            double envelope = Math.abs(Math.sin(Math.PI * (x % 1600) / 1600.0));
            samples[x] = talking ? (float)(random.nextGaussian() * amplitude * envelope) : 0.0f;
        }
        return samples;
    }

    private static float[] tone(float amplitude, double frequency, double seconds)
    {
        float[] samples = new float[(int)(seconds * RATE)];
        for(int x = 0; x < samples.length; x++)
        {
            samples[x] = (float)(amplitude * Math.sin(2 * Math.PI * frequency * x / RATE));
        }
        return samples;
    }

    /**
     * Runs the leveler in 160 sample buffers, like SDRTrunk does, and returns the output.
     */
    private static float[] run(AudioLeveler leveler, float[] input)
    {
        float[] output = new float[input.length];
        for(int x = 0; x < input.length; x += 160)
        {
            int length = Math.min(160, input.length - x);
            float[] buffer = new float[length];
            System.arraycopy(input, x, buffer, 0, length);
            System.arraycopy(leveler.process(buffer), 0, output, x, length);
        }
        return output;
    }

    /**
     * RMS level in dB of the last part of the signal, skipping the first second while the leveler settles.
     */
    private static double rmsDb(float[] samples)
    {
        double sum = 0;
        int count = 0;
        for(int x = RATE; x < samples.length; x++)
        {
            sum += samples[x] * samples[x];
            count++;
        }
        return 10 * Math.log10(sum / count);
    }

    private static float peak(float[] samples)
    {
        float max = 0;
        for(float sample : samples)
        {
            max = Math.max(max, Math.abs(sample));
        }
        return max;
    }

    @Test
    void quietAndLoudSpeechEndUpClose()
    {
        float[] quiet = run(new AudioLeveler(), speech(0.01f, 5, 1));
        float[] loud = run(new AudioLeveler(), speech(0.5f, 5, 2));
        double difference = Math.abs(rmsDb(quiet) - rmsDb(loud));
        assertTrue(difference < 3.0, "quiet and loud speech differ by " + difference + " dB");
    }

    @Test
    void pagingToneEndsUpCloseToSpeech()
    {
        //Near full scale tone versus moderate speech: before leveling the tone is about 20 dB louder.  Speech
        //includes pauses, so a few dB of difference in this average is expected.
        float[] tone = run(new AudioLeveler(), tone(0.95f, 1000, 5));
        float[] voice = run(new AudioLeveler(), speech(0.1f, 5, 3));
        double toneDb = rmsDb(tone);
        double voiceDb = rmsDb(voice);
        assertTrue(toneDb - voiceDb < 6.0, "tone is " + (toneDb - voiceDb) + " dB louder than speech");
    }

    @Test
    void neverClips()
    {
        AudioLeveler leveler = new AudioLeveler(-14.0f);
        //Quiet speech pushes the gain up, then a sudden full scale burst arrives
        float[] input = new float[RATE * 6];
        System.arraycopy(speech(0.005f, 5, 4), 0, input, 0, RATE * 5);
        System.arraycopy(tone(1.0f, 800, 1), 0, input, RATE * 5, RATE);
        assertTrue(peak(run(leveler, input)) <= 0.8913f, "output went past -1 dBFS");
    }

    @Test
    void silenceIsNotBoosted()
    {
        AudioLeveler leveler = new AudioLeveler();
        Random random = new Random(5);
        float[] hiss = new float[RATE * 3];
        for(int x = 0; x < hiss.length; x++)
        {
            hiss[x] = (float)(random.nextGaussian() * 0.0005); // about -66 dBFS
        }
        run(leveler, hiss);
        assertEquals(0.0, leveler.getGainDb(), 0.01, "gain moved during background hiss");
    }
}
