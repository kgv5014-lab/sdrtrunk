package io.github.dsheirer.source.tuner.rtl.gain;

import io.github.dsheirer.buffer.INativeBuffer;
import io.github.dsheirer.sample.Listener;
import io.github.dsheirer.sample.complex.ComplexSamples;
import io.github.dsheirer.source.tuner.channel.TunerChannel;
import java.util.ArrayList;
import java.util.List;
import java.util.Random;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Gain sweep: picking the gain from measurements, measuring a simulated dongle, and the whole sweep.
 */
public class GainOptimizerTest
{
    private static final double SAMPLE_RATE = 2_400_000;
    private static final long CENTRE = 852_900_000;

    private static GainMeasurement m(int gain, double floor, double clip, double... snr)
    {
        return new GainMeasurement(gain, floor, clip, snr);
    }

    @Test
    void steadySignalPicksLowestGainNearTheBest()
    {
        //Signal above noise climbs while the converter noise dominates, levels off, then overload eats into it
        GainSelector.Choice choice = GainSelector.choose(List.of(m(0, -80, 0, 8), m(100, -80, 0, 17),
                m(200, -76, 0, 24.5), m(300, -68, 0, 25.2), m(400, -58, 0, 25.0), m(500, -48, 0.02, 14)));
        assertEquals(200, choice.gain());
        assertEquals(GainSelector.Rule.SIGNAL, choice.rule());
        assertEquals(List.of(0), choice.steadyTargets());
    }

    @Test
    void weakestSteadySignalDecidesAndQuietChannelsAreIgnored()
    {
        //Target 0 is strong everywhere, target 1 needs more gain, target 2 keyed up once
        GainSelector.Choice choice = GainSelector.choose(List.of(m(0, -80, 0, 30, 6, 0.2), m(100, -80, 0, 34, 12, 0.1),
                m(200, -78, 0, 36, 18, 22), m(300, -70, 0, 36, 21, 0.3), m(400, -60, 0, 36, 21.4, 0)));
        assertEquals(300, choice.gain());
        assertEquals(List.of(0, 1), choice.steadyTargets());
    }

    @Test
    void quietChannelsUseTheNoiseFloorRise()
    {
        GainSelector.Choice choice = GainSelector.choose(List.of(m(0, -80.2, 0, 0.1), m(100, -80, 0, 0.3),
                m(200, -77, 0, 0), m(300, -71.5, 0, 0.2), m(400, -62, 0, 0), m(500, -52, 0.004, 0)));
        assertEquals(300, choice.gain());
        assertEquals(GainSelector.Rule.NOISE_FLOOR, choice.rule());
    }

    @Test
    void flatNoiseFloorUsesTheHighestCleanGainAndOverloadIsNeverChosen()
    {
        GainSelector.Choice flat = GainSelector.choose(List.of(m(0, -80, 0), m(100, -79, 0), m(200, -77, 0),
                m(300, -60, 0.05)));
        assertEquals(200, flat.gain());
        assertEquals(GainSelector.Rule.HIGHEST_CLEAN, flat.rule());

        GainSelector.Choice overloaded = GainSelector.choose(List.of(m(100, -40, 0.3), m(0, -45, 0.2)));
        assertEquals(0, overloaded.gain());
        assertEquals(GainSelector.Rule.ALL_OVERLOADED, overloaded.rule());
    }

    /**
     * A dongle with an 8-bit converter: antenna noise and one steady carrier are amplified by the gain, the
     * converter adds its own fixed noise and clips at full scale.
     */
    private static class FakeDongle implements GainOptimizer.Dongle
    {
        private final Random mRandom = new Random(1);
        private final double mCarrier;
        private final long mCarrierOffset;
        private Listener<INativeBuffer> mListener;
        private int mGain;
        private double mPhase;
        final List<Integer> mGainsSet = new ArrayList<>();

        FakeDongle(double carrier, long carrierOffset)
        {
            mCarrier = carrier;
            mCarrierOffset = carrierOffset;
        }

        @Override
        public int[] gains()
        {
            return new int[]{0, 90, 197, 297, 402, 496};
        }

        @Override
        public void setGain(int gain)
        {
            mGain = gain;
            mGainsSet.add(gain);
        }

        @Override
        public long frequency()
        {
            return CENTRE;
        }

        @Override
        public double sampleRate()
        {
            return SAMPLE_RATE;
        }

        @Override
        public List<TunerChannel> channels()
        {
            return List.of(new TunerChannel(CENTRE + mCarrierOffset, 12500), new TunerChannel(CENTRE - 600_000, 12500));
        }

        @Override
        public void addListener(Listener<INativeBuffer> listener)
        {
            mListener = listener;
        }

        @Override
        public void removeListener(Listener<INativeBuffer> listener)
        {
            mListener = null;
        }

        ComplexSamples samples(int count)
        {
            double gain = Math.pow(10, mGain / 200.0);
            double step = 2 * Math.PI * mCarrierOffset / SAMPLE_RATE;
            float[] i = new float[count];
            float[] q = new float[count];

            for(int x = 0; x < count; x++)
            {
                mPhase += step;
                i[x] = convert(gain * (mCarrier * Math.cos(mPhase) + 0.0005 * mRandom.nextGaussian()));
                q[x] = convert(gain * (mCarrier * Math.sin(mPhase) + 0.0005 * mRandom.nextGaussian()));
            }

            return new ComplexSamples(i, q, 0);
        }

        private float convert(double volts)
        {
            double noisy = volts + 0.003 * mRandom.nextGaussian();
            return (float)Math.clamp(Math.round(noisy * 128) / 128.0, -0.996, 0.996);
        }
    }

    private static GainMeasurement measure(FakeDongle dongle, int gain)
    {
        dongle.setGain(gain);
        GainMeter meter = new GainMeter();
        meter.process(dongle.samples(GainMeter.FFT_SIZE * 40));
        float[] spectrum = meter.spectrum();
        double floor = GainSelector.noiseFloor(spectrum);
        return new GainMeasurement(gain, floor, meter.clipFraction(),
                new double[]{GainSelector.snr(spectrum, floor, dongle.mCarrierOffset, 12500, SAMPLE_RATE)});
    }

    @Test
    void meterSeesTheFloorRiseTheSignalAndTheOverload()
    {
        FakeDongle dongle = new FakeDongle(0.004, 300_000);
        GainMeasurement low = measure(dongle, 0);
        GainMeasurement mid = measure(dongle, 297);
        GainMeasurement high = measure(dongle, 496);

        assertTrue(mid.floorDb() > low.floorDb() + 8, "antenna noise lifts the floor: " + low + " " + mid);
        assertTrue(mid.snrDb()[0] > low.snrDb()[0] + 10, "more gain lifts the signal out of the converter noise");
        assertFalse(mid.overloaded());
        assertTrue(high.overloaded(), "0.004 x 300 is past full scale");
        assertTrue(Double.isNaN(GainSelector.snr(new float[4096], 0, 1_300_000, 12500, SAMPLE_RATE)));
    }

    @Test
    void sweepTriesEveryGainThenPutsTheOriginalBack() throws Exception
    {
        FakeDongle dongle = new FakeDongle(0.004, 300_000);
        GainOptimizer optimizer = new GainOptimizer(dongle, 0, 0);
        //Feed samples from another thread the way the USB thread does
        Thread usb = new Thread(() -> {
            while(!Thread.currentThread().isInterrupted())
            {
                Listener<INativeBuffer> listener = dongle.mListener;

                if(listener instanceof GainMeter meter)
                {
                    meter.process(dongle.samples(GainMeter.FFT_SIZE));
                }
            }
        });
        usb.setDaemon(true);
        usb.start();

        try
        {
            GainOptimizer.Result result = new GainOptimizer(dongle, 30, 150).run(197, null, null);
            assertEquals(6, result.choice().measurements().size());
            assertEquals(List.of(0, 90, 197, 297, 402, 496, 197), dongle.mGainsSet);
            assertEquals(GainSelector.Rule.SIGNAL, result.choice().rule());
            assertEquals(List.of(0), result.choice().steadyTargets());
            assertTrue(result.choice().gain() >= 197 && result.choice().gain() <= 402, "chose " + result.choice().gain());
            assertTrue(GainSweepReport.html(result, 197).contains("Best gain"));

            dongle.mGainsSet.clear();
            assertNull(optimizer.run(90, null, () -> true));
            assertEquals(List.of(90), dongle.mGainsSet);
        }
        finally
        {
            usb.interrupt();
        }
    }
}
