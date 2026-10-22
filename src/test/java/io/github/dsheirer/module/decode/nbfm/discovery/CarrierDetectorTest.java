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

import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.Test;

/**
 * Carrier detection against synthetic VHF high band spectra at an RTL-SDR sample rate.
 */
public class CarrierDetectorTest
{
    private static final double SAMPLE_RATE = 2_400_000;
    private static final long CENTER = 155_400_000;
    private static final double NOISE_SIGMA = 0.01;

    /**
     * Averages one spectrum (25 ms) of the synthetic signal and runs the detector over it.
     */
    private static List<SpectralPeak> detect(SyntheticBaseband baseband, DiscoveryConfiguration configuration)
    {
        int fftSize = configuration.getFftSize();
        int frames = AveragedPowerSpectrum.framesFor(fftSize, SAMPLE_RATE, configuration.getAveragingTimeMs());
        AveragedPowerSpectrum spectrum = new AveragedPowerSpectrum(fftSize, frames);
        List<float[]> spectra = new ArrayList<>();
        spectrum.setListener(spectra::add);

        while(spectra.isEmpty())
        {
            float[][] samples = baseband.next(fftSize);
            spectrum.process(samples[0], samples[1]);
        }

        return new CarrierDetector(configuration).detect(spectra.getFirst(), CENTER, SAMPLE_RATE);
    }

    private static SpectralPeak nearest(List<SpectralPeak> peaks, long frequency)
    {
        return peaks.stream().min((a, b) -> Double.compare(Math.abs(a.frequency() - frequency),
                Math.abs(b.frequency() - frequency))).orElseThrow();
    }

    @Test
    void detectsStrongAndWeakVoiceCarriers()
    {
        //Channel SNR in 12.5 kHz: strong ~ 30 dB, medium ~ 20 dB, weak ~ 12 dB
        SyntheticBaseband baseband = new SyntheticBaseband(SAMPLE_RATE, NOISE_SIGMA)
                .addFmCarrier(155_752_500 - CENTER, 0.032, 2500, 0, 10)
                .addFmCarrier(155_122_500 - CENTER, 0.01, 2500, 0, 10)
                .addFmCarrier(154_430_000 - CENTER, 0.004, 2500, 0, 10);

        DiscoveryConfiguration configuration = new DiscoveryConfiguration();
        List<SpectralPeak> peaks = detect(baseband, configuration);

        assertEquals(3, peaks.size(), "Expected 3 carriers: " + peaks);

        for(long frequency : new long[]{155_752_500, 155_122_500, 154_430_000})
        {
            SpectralPeak peak = nearest(peaks, frequency);
            assertEquals(frequency, peak.frequency(), 600, "Centre frequency error for " + peak);
            assertEquals(frequency, ChannelRaster.VHF_HIGH.snap(peak.frequency()), "Snapped frequency for " + peak);
            assertTrue(peak.widthHz() >= 3_000 && peak.widthHz() <= 14_000, "Occupied width for " + peak);
        }
    }

    @Test
    void noiseOnlyProducesNoDetections()
    {
        SyntheticBaseband baseband = new SyntheticBaseband(SAMPLE_RATE, NOISE_SIGMA);
        DiscoveryConfiguration configuration = new DiscoveryConfiguration();

        for(int x = 0; x < 20; x++)
        {
            List<SpectralPeak> peaks = detect(baseband, configuration);
            assertTrue(peaks.isEmpty(), "False detections in noise: " + peaks);
        }
    }

    @Test
    void ignoresDcSpikeAndBandEdges()
    {
        SyntheticBaseband baseband = new SyntheticBaseband(SAMPLE_RATE, NOISE_SIGMA)
                .addTone(0, 0.05, 0, 10)                        //DC spike
                .addFmCarrier(1_150_000, 0.03, 2500, 0, 10)     //beyond 90% of the band
                .addFmCarrier(-500_000, 0.03, 2500, 0, 10);

        List<SpectralPeak> peaks = detect(baseband, new DiscoveryConfiguration());

        assertEquals(1, peaks.size(), "Expected only the in-band carrier: " + peaks);
        assertEquals(CENTER - 500_000, peaks.getFirst().frequency(), 600);
    }

    @Test
    void rejectsWidebandSignals()
    {
        //Broadcast-style FM with 75 kHz deviation
        SyntheticBaseband baseband = new SyntheticBaseband(SAMPLE_RATE, NOISE_SIGMA)
                .addFmCarrier(300_000, 0.05, 75_000, 0, 10)
                .addFmCarrier(-300_000, 0.03, 2500, 0, 10);

        List<SpectralPeak> peaks = detect(baseband, new DiscoveryConfiguration());

        assertEquals(1, peaks.size(), "Expected only the narrowband carrier: " + peaks);
        assertEquals(CENTER - 300_000, peaks.getFirst().frequency(), 600);
    }

    @Test
    void skipsExcludedFrequencies()
    {
        SyntheticBaseband baseband = new SyntheticBaseband(SAMPLE_RATE, NOISE_SIGMA)
                .addFmCarrier(155_752_500 - CENTER, 0.03, 2500, 0, 10)
                .addFmCarrier(155_122_500 - CENTER, 0.03, 2500, 0, 10);

        DiscoveryConfiguration configuration = new DiscoveryConfiguration();
        configuration.addExcludedRange(DiscoveryConfiguration.FrequencyRange.centeredOn(155_752_500, 6_250));

        List<SpectralPeak> peaks = detect(baseband, configuration);

        assertEquals(1, peaks.size(), "Expected the configured channel to be excluded: " + peaks);
        assertEquals(155_122_500, peaks.getFirst().frequency(), 600);
    }

    @Test
    void separatesAdjacentChannels()
    {
        //Two 12.5 kHz channels 15 kHz apart (VHF high narrowband channels on adjacent 7.5 kHz offsets)
        SyntheticBaseband baseband = new SyntheticBaseband(SAMPLE_RATE, NOISE_SIGMA)
                .addFmCarrier(155_745_000 - CENTER, 0.02, 2500, 0, 10)
                .addFmCarrier(155_760_000 - CENTER, 0.02, 2500, 0, 10);

        List<SpectralPeak> peaks = detect(baseband, new DiscoveryConfiguration());

        assertEquals(2, peaks.size(), "Expected two separate carriers: " + peaks);
    }

    @Test
    void noiseFloorFollowsPassbandSlopeAndIgnoresCarriers()
    {
        //Floor rises linearly by 10 dB across the band, with a block of 10 strong 10-bin carriers
        int size = 2048;
        float[] spectrum = new float[size];

        for(int x = 0; x < size; x++)
        {
            spectrum[x] = -60.0f + 10.0f * x / size;
        }

        for(int carrier = 0; carrier < 10; carrier++)
        {
            for(int x = 0; x < 10; x++)
            {
                spectrum[1000 + carrier * 15 + x] += 30.0f;
            }
        }

        float[] floor = CarrierDetector.estimateNoiseFloor(spectrum, 170, 0.2);

        for(int x = 0; x < size; x++)
        {
            float expected = -60.0f + 10.0f * x / size;
            assertEquals(expected, floor[x], 1.0, "Noise floor at bin " + x);
        }
    }
}
