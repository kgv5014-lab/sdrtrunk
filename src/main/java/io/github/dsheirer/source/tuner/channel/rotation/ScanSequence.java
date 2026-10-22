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

package io.github.dsheirer.source.tuner.channel.rotation;

import java.util.ArrayList;
import java.util.Collection;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

/**
 * Determines the frequency visit order for a rotating multiple frequency channel source.
 *
 * Frequencies are visited in list order, skipping any frequencies that are locked.  When priority frequencies are
 * specified, a priority frequency is visited between each of the other frequencies (e.g. P1, A, P1, B, P1, C) so
 * that the priority frequencies are checked often.  Not thread safe.
 */
public class ScanSequence
{
    private final List<Long> mFrequencies;
    private final List<Long> mPriorityFrequencies = new ArrayList<>();
    private final Set<Long> mLockedFrequencies = new HashSet<>();
    private int mPointer = 0;
    private int mPriorityPointer = -1;
    private boolean mLastWasPriority = false;

    /**
     * Constructs an instance
     * @param frequencies to rotate through, in order
     * @param priorityFrequencies (optional) frequencies to revisit between each of the other frequencies.  Priority
     * frequencies that are not also in the frequency list are ignored.
     */
    public ScanSequence(List<Long> frequencies, Collection<Long> priorityFrequencies)
    {
        mFrequencies = new ArrayList<>(frequencies);

        if(priorityFrequencies != null)
        {
            for(Long frequency: priorityFrequencies)
            {
                if(mFrequencies.contains(frequency) && !mPriorityFrequencies.contains(frequency))
                {
                    mPriorityFrequencies.add(frequency);
                }
            }
        }
    }

    /**
     * Sets the current position in the sequence to the specified frequency, for example when the source starts on
     * a frequency other than the first frequency in the list.
     */
    public void setCurrent(long frequency)
    {
        int index = mFrequencies.indexOf(frequency);

        if(index >= 0)
        {
            mPointer = index;
        }

        mLastWasPriority = mPriorityFrequencies.contains(frequency);
    }

    /**
     * Locks the frequency so that it is skipped in the sequence.
     */
    public void lock(long frequency)
    {
        mLockedFrequencies.add(frequency);
    }

    /**
     * Unlocks a previously locked frequency.
     */
    public void unlock(long frequency)
    {
        mLockedFrequencies.remove(frequency);
    }

    /**
     * Indicates if the frequency is locked
     */
    public boolean isLocked(long frequency)
    {
        return mLockedFrequencies.contains(frequency);
    }

    /**
     * Identifies the next frequency in the sequence.
     *
     * @param current frequency in use
     * @return next frequency or 0 if there are no other (unlocked) frequencies than the current frequency.
     */
    public long next(long current)
    {
        if(mFrequencies.size() <= 1)
        {
            return 0;
        }

        boolean priorityMode = hasUnlockedPriority();

        if(priorityMode && !mLastWasPriority)
        {
            long priority = nextPriority(current);

            if(priority != 0)
            {
                mLastWasPriority = true;
                return priority;
            }
        }

        //Visit the next non-priority frequency in list order
        for(int attempts = 0; attempts < mFrequencies.size(); attempts++)
        {
            mPointer = (mPointer + 1) % mFrequencies.size();
            long frequency = mFrequencies.get(mPointer);

            if(frequency == current && !priorityMode)
            {
                //We've looped back to the current frequency, so there are no others available
                return 0;
            }

            if(frequency != current && !isLocked(frequency) && !(priorityMode && mPriorityFrequencies.contains(frequency)))
            {
                mLastWasPriority = false;
                return frequency;
            }
        }

        //Only priority frequencies remain - rotate between them
        if(priorityMode)
        {
            long priority = nextPriority(current);

            if(priority != 0)
            {
                mLastWasPriority = true;
                return priority;
            }
        }

        return 0;
    }

    /**
     * Indicates if there is at least one priority frequency that is not locked
     */
    private boolean hasUnlockedPriority()
    {
        for(Long frequency: mPriorityFrequencies)
        {
            if(!isLocked(frequency))
            {
                return true;
            }
        }

        return false;
    }

    /**
     * Next unlocked priority frequency (round-robin) that is not the current frequency, or 0.
     */
    private long nextPriority(long current)
    {
        for(int attempts = 0; attempts < mPriorityFrequencies.size(); attempts++)
        {
            mPriorityPointer = (mPriorityPointer + 1) % mPriorityFrequencies.size();
            long frequency = mPriorityFrequencies.get(mPriorityPointer);

            if(frequency != current && !isLocked(frequency))
            {
                return frequency;
            }
        }

        return 0;
    }
}
