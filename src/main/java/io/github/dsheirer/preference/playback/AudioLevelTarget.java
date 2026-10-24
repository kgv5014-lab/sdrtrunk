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

package io.github.dsheirer.preference.playback;

/**
 * Loudness that the audio leveler aims for, as an average (RMS) level in dB relative to full scale.
 */
public enum AudioLevelTarget
{
    QUIETER(-24.0f, "Quieter"),
    NORMAL(-20.0f, "Normal"),
    LOUDER(-16.0f, "Louder");

    private final float mTargetDb;
    private final String mLabel;

    AudioLevelTarget(float targetDb, String label)
    {
        mTargetDb = targetDb;
        mLabel = label;
    }

    public float getTargetDb()
    {
        return mTargetDb;
    }

    /**
     * Looks up the entry by name, or returns NORMAL if the name isn't recognized.
     */
    public static AudioLevelTarget fromName(String name)
    {
        for(AudioLevelTarget target : values())
        {
            if(target.name().equals(name))
            {
                return target;
            }
        }

        return NORMAL;
    }

    @Override
    public String toString()
    {
        return mLabel;
    }
}
