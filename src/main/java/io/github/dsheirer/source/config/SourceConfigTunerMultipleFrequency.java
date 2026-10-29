/*
 * ******************************************************************************
 * sdrtrunk
 * Copyright (C) 2014-2019 Dennis Sheirer
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
 * *****************************************************************************
 */
package io.github.dsheirer.source.config;

import com.fasterxml.jackson.annotation.JsonIgnore;
import com.fasterxml.jackson.annotation.JsonSubTypes;
import com.fasterxml.jackson.dataformat.xml.annotation.JacksonXmlProperty;
import io.github.dsheirer.source.SourceType;
import io.github.dsheirer.source.tuner.channel.TunerChannel;
import io.github.dsheirer.source.tuner.channel.rotation.ChannelRotationMonitor;

import java.util.ArrayList;
import java.util.List;

/**
 * Multiple frequency tuner source configuration is used for a system with a rolling/rotating control
 * channel where the control channel rotates across a pre-defined set of channel frequencies.
 */
@JsonSubTypes.Type(value = SourceConfigTunerMultipleFrequency.class, name = "sourceConfigTunerMultipleFrequency")
public class SourceConfigTunerMultipleFrequency extends SourceConfiguration
{
    private List<Long> mFrequencies = new ArrayList<>();
    private String mPreferredTuner;
    private Long mPreferredFrequency;
    private int mFrequencyRotationDelay = ChannelRotationMonitor.CHANNEL_ROTATION_DELAY_DEFAULT;
    private int mScanHangTime = ChannelRotationMonitor.SCAN_HANG_TIME_DEFAULT;
    private boolean mScanParallel = true;
    private List<ScanEntry> mScanEntries = new ArrayList<>();

    public SourceConfigTunerMultipleFrequency()
    {
        super(SourceType.TUNER_MULTIPLE_FREQUENCIES);
    }

    /**
     * List of frequencies for this configuration
     */
    @JacksonXmlProperty(isAttribute = false, localName = "frequency")
    public List<Long> getFrequencies()
    {
        return mFrequencies;
    }

    /**
     * Sets the list of frequencies for this configuration
     */
    public void setFrequencies(List<Long> frequencies)
    {
        mFrequencies = frequencies;
    }

    /**
     * Indicates if this configuration has more than one frequency specified
     */
    @JsonIgnore
    public boolean hasMultipleFrequencies()
    {
        return mFrequencies.size() > 1;
    }

    /**
     * Preferred frequency to start first for this configuration
     */
    @JsonIgnore
    public long getPreferredFrequency()
    {
        if(mPreferredFrequency != null)
        {
            return mPreferredFrequency;
        }
        else if(mFrequencies.size() > 0)
        {
            return mFrequencies.get(0);
        }

        return 0l;
    }

    /**
     * Sets which is the preferred frequency to use for this config when it's started
     * @param frequency to use first
     */
    public void setPreferredFrequency(long frequency)
    {
        //Only set the frequency if it is one of the frequencies in the list
        if(mFrequencies.contains(frequency))
        {
            mPreferredFrequency = frequency;
        }
    }

    /**
     * Adds a frequency to the list
     */
    public void addFrequency(long frequency)
    {
        mFrequencies.add(frequency);
    }

    /**
     * Preferred tuner to use for this configuration.
     * @return tuner name or null
     */
    @JacksonXmlProperty(isAttribute = true, localName = "preferred_tuner")
    public String getPreferredTuner()
    {
        return mPreferredTuner;
    }

    /**
     * Indicates if this configuration has a specified preferred tuner
     */
    @JsonIgnore
    public boolean hasPreferredTuner()
    {
        return mPreferredTuner != null;
    }

    /**
     * Specifies the preferred tuner to use for this configuration
     * @param preferredTuner to use, if available
     */
    public void setPreferredTuner(String preferredTuner)
    {
        mPreferredTuner = preferredTuner;
    }

    @JsonIgnore
    @Override
    public String getDescription()
    {
        return "Frequencies [" + mFrequencies.size() + "]";
    }

    @JsonIgnore
    public TunerChannel getTunerChannel(int bandwidth)
    {
        return new TunerChannel(getPreferredFrequency(), bandwidth);
    }

    /**
     * Channel rotation delay.  This setting is used when multiple channel frequencies are defined in the source
     * config and controls how long the decoder will remaining on each frequency until the channel is identified as
     * active or the channel is identified as inactive and a change frequency request is issued.
     *
     * Note: this value is ignored when the source config contains a single frequency.
     *
     * @return channel rotation delay in milliseconds.
     */
    @JacksonXmlProperty(isAttribute = true, localName = "frequency_rotation_delay")
    public int getFrequencyRotationDelay()
    {
        return mFrequencyRotationDelay;
    }

    /**
     * Sets the channel rotation delay.
     * @param frequencyRotationDelay in milliseconds.
     */
    public void setFrequencyRotationDelay(int frequencyRotationDelay)
    {
        if(frequencyRotationDelay < ChannelRotationMonitor.CHANNEL_ROTATION_DELAY_MINIMUM)
        {
            mFrequencyRotationDelay = ChannelRotationMonitor.CHANNEL_ROTATION_DELAY_MINIMUM;
        }
        else if(frequencyRotationDelay > ChannelRotationMonitor.CHANNEL_ROTATION_DELAY_MAXIMUM)
        {
            mFrequencyRotationDelay = ChannelRotationMonitor.CHANNEL_ROTATION_DELAY_MAXIMUM;
        }
        else
        {
            mFrequencyRotationDelay = frequencyRotationDelay;
        }
    }

    /**
     * Scanner hang time.  Used by conventional (NBFM/AM) scan lists to control how long the scanner remains on a
     * frequency after a call ends before resuming the scan.
     *
     * @return hang time in milliseconds.
     */
    @JacksonXmlProperty(isAttribute = true, localName = "scan_hang_time")
    public int getScanHangTime()
    {
        return mScanHangTime;
    }

    /**
     * Sets the scanner hang time
     * @param hangTime in milliseconds, constrained to the min/max values in the channel rotation monitor.
     */
    public void setScanHangTime(int hangTime)
    {
        mScanHangTime = Math.max(ChannelRotationMonitor.SCAN_HANG_TIME_MINIMUM,
            Math.min(ChannelRotationMonitor.SCAN_HANG_TIME_MAXIMUM, hangTime));
    }

    /**
     * Indicates if a conventional (NBFM/AM) scan list should monitor frequencies at the same time, instead of
     * scanning them, when they fit within the bandwidth of an available tuner.
     */
    @JacksonXmlProperty(isAttribute = true, localName = "scan_parallel")
    public boolean isScanParallel()
    {
        return mScanParallel;
    }

    /**
     * Sets the parallel monitoring option for conventional scan lists.
     */
    public void setScanParallel(boolean scanParallel)
    {
        mScanParallel = scanParallel;
    }

    /**
     * Per-frequency scan settings (label, lockout, priority).  Only frequencies that have non-default settings
     * are included.
     */
    @JacksonXmlProperty(isAttribute = false, localName = "scan_entry")
    public List<ScanEntry> getScanEntries()
    {
        return mScanEntries;
    }

    /**
     * Sets the per-frequency scan settings.  Entries with only default values are discarded.
     */
    public void setScanEntries(List<ScanEntry> scanEntries)
    {
        mScanEntries = new ArrayList<>();

        if(scanEntries != null)
        {
            for(ScanEntry scanEntry: scanEntries)
            {
                if(scanEntry != null && scanEntry.hasSettings())
                {
                    mScanEntries.add(scanEntry);
                }
            }
        }
    }

    /**
     * Scan entry for the specified frequency.
     * @param frequency to lookup
     * @return the matching entry or a new entry with default settings when there isn't a matching entry.
     */
    public ScanEntry getScanEntry(long frequency)
    {
        for(ScanEntry scanEntry: mScanEntries)
        {
            if(scanEntry.getFrequency() == frequency)
            {
                return scanEntry;
            }
        }

        return new ScanEntry(frequency, null, false, false);
    }

    /**
     * Scan entries for each of the frequencies in this configuration, in frequency list order, including default
     * entries for frequencies that don't have custom settings.
     */
    @JsonIgnore
    public List<ScanEntry> getAllScanEntries()
    {
        List<ScanEntry> entries = new ArrayList<>();

        for(Long frequency: mFrequencies)
        {
            entries.add(getScanEntry(frequency));
        }

        return entries;
    }

    /**
     * Frequencies that the user has locked out of the scan.
     */
    @JsonIgnore
    public List<Long> getLockoutFrequencies()
    {
        List<Long> frequencies = new ArrayList<>();

        for(ScanEntry scanEntry: getAllScanEntries())
        {
            if(scanEntry.isLockout())
            {
                frequencies.add(scanEntry.getFrequency());
            }
        }

        return frequencies;
    }

    /**
     * Frequencies that the user has marked as priority.
     */
    @JsonIgnore
    public List<Long> getPriorityFrequencies()
    {
        List<Long> frequencies = new ArrayList<>();

        for(ScanEntry scanEntry: getAllScanEntries())
        {
            if(scanEntry.isPriority())
            {
                frequencies.add(scanEntry.getFrequency());
            }
        }

        return frequencies;
    }
}
