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

package io.github.dsheirer.source.config;

import com.fasterxml.jackson.annotation.JsonIgnore;
import com.fasterxml.jackson.dataformat.xml.annotation.JacksonXmlProperty;

/**
 * Per-frequency scanner settings for a multiple frequency source configuration that is used as a conventional
 * scan list (NBFM/AM).  Entries are keyed by frequency.
 */
public class ScanEntry
{
    private long mFrequency;
    private String mLabel;
    private boolean mLockout;
    private boolean mPriority;

    /**
     * Default constructor for Jackson XML deserialization
     */
    public ScanEntry()
    {
    }

    /**
     * Constructs an instance
     * @param frequency in hertz
     * @param label optional user label
     * @param lockout true to skip this frequency while scanning
     * @param priority true to revisit this frequency between each of the other frequencies
     */
    public ScanEntry(long frequency, String label, boolean lockout, boolean priority)
    {
        mFrequency = frequency;
        setLabel(label);
        mLockout = lockout;
        mPriority = priority;
    }

    @JacksonXmlProperty(isAttribute = true, localName = "frequency")
    public long getFrequency()
    {
        return mFrequency;
    }

    public void setFrequency(long frequency)
    {
        mFrequency = frequency;
    }

    @JacksonXmlProperty(isAttribute = true, localName = "label")
    public String getLabel()
    {
        return mLabel;
    }

    public void setLabel(String label)
    {
        mLabel = (label == null || label.isBlank()) ? null : label.trim();
    }

    @JsonIgnore
    public boolean hasLabel()
    {
        return mLabel != null;
    }

    @JacksonXmlProperty(isAttribute = true, localName = "lockout")
    public boolean isLockout()
    {
        return mLockout;
    }

    public void setLockout(boolean lockout)
    {
        mLockout = lockout;
    }

    @JacksonXmlProperty(isAttribute = true, localName = "priority")
    public boolean isPriority()
    {
        return mPriority;
    }

    public void setPriority(boolean priority)
    {
        mPriority = priority;
    }

    /**
     * Indicates if this entry holds anything other than default settings and is worth persisting.
     */
    @JsonIgnore
    public boolean hasSettings()
    {
        return hasLabel() || mLockout || mPriority;
    }

    /**
     * Creates a copy of this entry
     */
    public ScanEntry copyOf()
    {
        return new ScanEntry(mFrequency, mLabel, mLockout, mPriority);
    }
}
