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

import io.github.dsheirer.buffer.INativeBuffer;
import io.github.dsheirer.sample.Listener;
import io.github.dsheirer.sample.complex.ComplexSamples;
import io.github.dsheirer.source.ISourceEventProcessor;
import io.github.dsheirer.source.SourceEvent;
import io.github.dsheirer.source.tuner.TunerController;
import java.util.Iterator;
import java.util.List;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Discovers NBFM carriers in a tuner's full bandwidth.
 *
 * Receives the tuner's wideband sample buffers, computes an averaged power spectrum, detects carriers in it, and tracks
 * them over time.  Transmission start and end events are delivered to the registered listener and logged.
 *
 * Use attach() to run against a live tuner, or setTuning() and process() to run against recorded samples.
 */
public class NBFMDiscoveryProcessor implements Listener<INativeBuffer>, ISourceEventProcessor
{
    private static final Logger mLog = LoggerFactory.getLogger(NBFMDiscoveryProcessor.class);

    private final DiscoveryConfiguration mConfiguration;
    private final CarrierDetector mDetector;
    private final CarrierTracker mTracker;
    private AveragedPowerSpectrum mSpectrum;
    private long mCenterFrequency;
    private double mSampleRate;
    private volatile long mPendingCenterFrequency;
    private volatile double mPendingSampleRate;
    private long mTimeOrigin = -1;
    private long mSpectrumCount;
    private long mSamplesPerSpectrum;
    private Listener<DiscoveryEvent> mListener;
    private TunerController mTunerController;

    /**
     * Constructs an instance
     * @param configuration settings
     */
    public NBFMDiscoveryProcessor(DiscoveryConfiguration configuration)
    {
        mConfiguration = configuration;
        mDetector = new CarrierDetector(configuration);
        mTracker = new CarrierTracker(configuration);
        mTracker.setListener(this::dispatch);
    }

    public CarrierTracker getTracker()
    {
        return mTracker;
    }

    /**
     * Registers a listener for carrier start, end and birdie events.  Events are delivered on the sample processing
     * thread.
     */
    public void setListener(Listener<DiscoveryEvent> listener)
    {
        mListener = listener;
    }

    /**
     * Starts discovery on the tuner, using its current frequency and sample rate and following any changes.
     */
    public void attach(TunerController tunerController)
    {
        detach();
        mTunerController = tunerController;
        setTuning(tunerController.getFrequency(), tunerController.getSampleRate());
        tunerController.addListener(this);
        tunerController.addBufferListener(this);
    }

    /**
     * Stops discovery on the currently attached tuner, if any.
     */
    public void detach()
    {
        if(mTunerController != null)
        {
            mTunerController.removeBufferListener(this);
            mTunerController.removeListener(this);
            mTunerController = null;
        }
    }

    /**
     * Sets the tuner centre frequency and sample rate.  Takes effect at the start of the next sample buffer, which
     * ends any active carriers and restarts spectrum averaging.
     */
    public void setTuning(long centerFrequency, double sampleRate)
    {
        mPendingCenterFrequency = centerFrequency;
        mPendingSampleRate = sampleRate;
    }

    /**
     * Tuner frequency and sample rate change notifications.
     */
    @Override
    public void process(SourceEvent event)
    {
        switch(event.getEvent())
        {
            case NOTIFICATION_FREQUENCY_CHANGE -> mPendingCenterFrequency = event.getValue().longValue();
            case NOTIFICATION_SAMPLE_RATE_CHANGE -> mPendingSampleRate = event.getValue().doubleValue();
            default -> {}
        }
    }

    /**
     * Live tuner sample buffers.  Timestamps are anchored to the first buffer's timestamp and then advanced by sample
     * count, so they are not affected by buffer delivery jitter.
     */
    @Override
    public void receive(INativeBuffer buffer)
    {
        if(mTimeOrigin < 0)
        {
            mTimeOrigin = buffer.getTimestamp();
        }

        Iterator<ComplexSamples> it = buffer.iterator();

        while(it.hasNext())
        {
            ComplexSamples samples = it.next();
            process(samples.i(), samples.q());
        }
    }

    /**
     * Processes complex samples.  When used without a live tuner, timestamps start at zero.
     */
    public void process(float[] i, float[] q)
    {
        if(mTimeOrigin < 0)
        {
            mTimeOrigin = 0;
        }

        applyPendingTuning();

        if(mSpectrum != null)
        {
            mSpectrum.process(i, q);
        }
    }

    /**
     * Current timestamp in milliseconds, based on the number of samples processed.
     */
    public long getTimestamp()
    {
        return mTimeOrigin + (mSampleRate > 0 ? Math.round(mSpectrumCount * mSamplesPerSpectrum * 1000.0 /
                mSampleRate) : 0);
    }

    private void applyPendingTuning()
    {
        long frequency = mPendingCenterFrequency;
        double sampleRate = mPendingSampleRate;

        if(frequency != mCenterFrequency || sampleRate != mSampleRate)
        {
            long timestamp = getTimestamp();
            mTracker.clear(timestamp);
            mTimeOrigin = timestamp;
            mSpectrumCount = 0;
            mCenterFrequency = frequency;
            mSampleRate = sampleRate;

            if(frequency > 0 && sampleRate > 0)
            {
                int fftSize = mConfiguration.getFftSize();
                int frames = AveragedPowerSpectrum.framesFor(fftSize, sampleRate, mConfiguration.getAveragingTimeMs());
                mSamplesPerSpectrum = (long)fftSize * frames;
                mSpectrum = new AveragedPowerSpectrum(fftSize, frames);
                mSpectrum.setListener(this::processSpectrum);
                mLog.info("NBFM discovery tuned to {} MHz, sample rate {} MHz, {} Hz resolution, {} ms averaging",
                        frequency / 1e6, sampleRate / 1e6, Math.round(sampleRate / fftSize),
                        Math.round(mSamplesPerSpectrum * 1000.0 / sampleRate));
            }
            else
            {
                mSpectrum = null;
            }
        }
    }

    private void processSpectrum(float[] spectrum)
    {
        mSpectrumCount++;
        List<SpectralPeak> peaks = mDetector.detect(spectrum, mCenterFrequency, mSampleRate);
        mTracker.update(peaks, getTimestamp());
    }

    private void dispatch(DiscoveryEvent event)
    {
        mLog.info("NBFM discovery: {}", event);

        if(mListener != null)
        {
            mListener.receive(event);
        }
    }
}
