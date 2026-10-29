package io.github.dsheirer.dsp.am;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;

public class SquelchingAMDemodulatorTest
{
    private static final int RATE = 25000;
    private static final int BUFFER = 1024;

    /**
     * AM signal: a carrier of the given strength, 50% modulated by a 1 kHz tone, as I/Q samples.
     */
    private static float[][] signal(double carrierDb, double seconds)
    {
        int length = (int)(seconds * RATE) / BUFFER * BUFFER;
        float[] i = new float[length];
        float[] q = new float[length];
        double carrier = Math.pow(10.0, carrierDb / 20.0);
        for(int x = 0; x < length; x++)
        {
            double envelope = carrier * (1.0 + 0.5 * Math.sin(2 * Math.PI * 1000.0 * x / RATE));
            double phase = 2 * Math.PI * 30.0 * x / RATE;
            i[x] = (float)(envelope * Math.cos(phase));
            q[x] = (float)(envelope * Math.sin(phase));
        }
        return new float[][]{i, q};
    }

    /**
     * Demodulates in buffers, like the AM decoder does, and returns the RMS level in dB of the last half second.
     */
    private static double demodulatedDb(SquelchingAMDemodulator demodulator, float[][] iq)
    {
        double sum = 0.0;
        int count = 0;
        int start = iq[0].length - RATE / 2;
        for(int offset = 0; offset < iq[0].length; offset += BUFFER)
        {
            float[] i = java.util.Arrays.copyOfRange(iq[0], offset, offset + BUFFER);
            float[] q = java.util.Arrays.copyOfRange(iq[1], offset, offset + BUFFER);
            float[] out = demodulator.demodulate(i, q);
            for(int x = 0; x < out.length; x++)
            {
                if(offset + x >= start)
                {
                    sum += out[x] * out[x];
                    count++;
                }
            }
        }
        return 10.0 * Math.log10(sum / count);
    }

    private static SquelchingAMDemodulator demodulator()
    {
        SquelchingAMDemodulator demodulator = new SquelchingAMDemodulator(150.0f, 0.0004f, -78.0f, false);
        demodulator.setSampleRate(RATE);
        return demodulator;
    }

    @Test
    void strongAndWeakStationsComeOutEquallyLoud()
    {
        //An aircraft overhead at -30 dB and a distant tower at -60 dB: 30 dB apart on the air
        double strong = demodulatedDb(demodulator(), signal(-30.0, 1.0));
        double weak = demodulatedDb(demodulator(), signal(-60.0, 1.0));
        assertEquals(strong, weak, 1.0, "strong " + strong + " dB, weak " + weak + " dB");
        //50% modulation at 0.5 gain is a 0.25 amplitude sine
        assertEquals(20.0 * Math.log10(0.25 / Math.sqrt(2.0)), strong, 1.0);
    }

    @Test
    void sameChannelEvensOutWhenTheStationChanges()
    {
        //One demodulator hears the loud aircraft, then the quiet tower right after
        SquelchingAMDemodulator demodulator = demodulator();
        double strong = demodulatedDb(demodulator, signal(-30.0, 1.0));
        double weak = demodulatedDb(demodulator, signal(-60.0, 1.0));
        assertEquals(strong, weak, 1.0, "strong " + strong + " dB, weak " + weak + " dB");
    }

    @Test
    void noiseBelowTheSquelchIsNotTurnedUp()
    {
        //A carrier 20 dB under the squelch threshold is muted, so the output stays silent
        double db = demodulatedDb(demodulator(), signal(-98.0, 1.0));
        assertTrue(db < -100.0, "level " + db);
    }
}
