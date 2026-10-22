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

import io.github.dsheirer.sample.Listener;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Tracks detected carriers across successive spectrum updates and turns them into transmission start and end events.
 *
 * A new detection is a candidate.  It becomes active (STARTED) once it has been continuously present for the attack
 * time, which rejects impulse noise and brief splatter.  An active carrier ends (ENDED) once it has been absent for the
 * release time, which bridges short fades.  A carrier that stays narrow and on for the birdie time is flagged (BIRDIE)
 * and its frequency is ignored from then on.
 *
 * Not thread safe, except for the ignore list: update() is expected to be called from a single sample processing thread.
 */
public class CarrierTracker
{
    private final DiscoveryConfiguration mConfiguration;
    private final List<TrackedCarrier> mCarriers = new ArrayList<>();
    private final Map<Long,DiscoveredFrequency> mHistory = new TreeMap<>();
    private final Set<Long> mIgnored = ConcurrentHashMap.newKeySet();
    private Listener<DiscoveryEvent> mListener;

    /**
     * Constructs an instance
     * @param configuration settings
     */
    public CarrierTracker(DiscoveryConfiguration configuration)
    {
        mConfiguration = configuration;
    }

    public void setListener(Listener<DiscoveryEvent> listener)
    {
        mListener = listener;
    }

    /**
     * Ignores any future carrier on this channel frequency.
     */
    public void ignore(long channelFrequency)
    {
        mIgnored.add(channelFrequency);
    }

    public void unignore(long channelFrequency)
    {
        mIgnored.remove(channelFrequency);
    }

    public Set<Long> getIgnored()
    {
        return Collections.unmodifiableSet(mIgnored);
    }

    /**
     * Carriers currently being tracked, including candidates that are not yet active.
     */
    public List<TrackedCarrier> getCarriers()
    {
        return Collections.unmodifiableList(mCarriers);
    }

    /**
     * Activity history for every frequency that has had at least one completed transmission, ordered by frequency.
     */
    public Collection<DiscoveredFrequency> getHistory()
    {
        return Collections.unmodifiableCollection(mHistory.values());
    }

    /**
     * Ends all active carriers (for example on a tuner frequency change or shutdown) and clears all candidates.
     * @param timestamp in milliseconds
     */
    public void clear(long timestamp)
    {
        for(TrackedCarrier carrier : mCarriers)
        {
            if(carrier.isActive())
            {
                end(carrier, timestamp);
            }
        }

        mCarriers.clear();
    }

    /**
     * Processes the carriers detected in one spectrum update.
     * @param peaks detected in the spectrum
     * @param timestamp of the spectrum in milliseconds
     */
    public void update(List<SpectralPeak> peaks, long timestamp)
    {
        //Match each detection to the closest tracked carrier, keeping only the strongest detection per carrier
        Map<TrackedCarrier,SpectralPeak> matches = new IdentityHashMap<>();

        for(SpectralPeak peak : peaks)
        {
            TrackedCarrier match = findMatch(peak.frequency());

            if(match != null)
            {
                SpectralPeak existing = matches.get(match);

                if(existing == null || peak.snrDb() > existing.snrDb())
                {
                    matches.put(match, peak);
                }
            }
            else
            {
                ChannelRaster raster = mConfiguration.getChannelRaster((long)peak.frequency());
                boolean offRaster = raster.error(peak.frequency()) > mConfiguration.getMaximumRasterErrorHz();
                long channelFrequency = offRaster ? Math.round(peak.frequency() / 100.0) * 100 :
                        raster.snap(peak.frequency());

                if(!mIgnored.contains(channelFrequency))
                {
                    TrackedCarrier carrier = new TrackedCarrier(channelFrequency, offRaster, peak, timestamp);
                    mCarriers.add(carrier);
                    matches.put(carrier, peak);
                }
            }
        }

        for(Map.Entry<TrackedCarrier,SpectralPeak> entry : matches.entrySet())
        {
            if(entry.getKey().getLastSeen() != timestamp)
            {
                entry.getKey().update(entry.getValue(), timestamp);
            }
        }

        //Promote, release, and flag carriers
        Iterator<TrackedCarrier> it = mCarriers.iterator();

        while(it.hasNext())
        {
            TrackedCarrier carrier = it.next();
            long absent = timestamp - carrier.getLastSeen();

            if(carrier.isActive())
            {
                if(absent > mConfiguration.getReleaseTimeMs())
                {
                    end(carrier, timestamp);
                    it.remove();
                }
                else if(carrier.getDuration() >= mConfiguration.getBirdieTimeMs() &&
                        carrier.getMaximumWidthHz() <= mConfiguration.getBirdieMaximumWidthHz())
                {
                    DiscoveredFrequency history = getOrCreateHistory(carrier.getChannelFrequency());
                    history.add(carrier);
                    history.setBirdie();
                    mIgnored.add(carrier.getChannelFrequency());
                    it.remove();
                    broadcast(new DiscoveryEvent(DiscoveryEvent.Type.BIRDIE, carrier, timestamp));
                }
            }
            else
            {
                //Candidates must be continuously present; a gap of more than half the attack time drops them
                if(absent > mConfiguration.getAttackTimeMs() / 2)
                {
                    it.remove();
                }
                else if(carrier.getDuration() >= mConfiguration.getAttackTimeMs())
                {
                    carrier.setActive();
                    broadcast(new DiscoveryEvent(DiscoveryEvent.Type.STARTED, carrier, timestamp));
                }
            }
        }
    }

    /**
     * Finds the tracked carrier closest to the frequency, within the match tolerance.
     */
    private TrackedCarrier findMatch(double frequency)
    {
        TrackedCarrier best = null;
        double bestDistance = mConfiguration.getMatchToleranceHz();

        for(TrackedCarrier carrier : mCarriers)
        {
            double distance = Math.abs(carrier.getMeasuredFrequency() - frequency);

            if(distance <= bestDistance)
            {
                best = carrier;
                bestDistance = distance;
            }
        }

        return best;
    }

    private void end(TrackedCarrier carrier, long timestamp)
    {
        getOrCreateHistory(carrier.getChannelFrequency()).add(carrier);
        broadcast(new DiscoveryEvent(DiscoveryEvent.Type.ENDED, carrier, timestamp));
    }

    private DiscoveredFrequency getOrCreateHistory(long frequency)
    {
        return mHistory.computeIfAbsent(frequency, DiscoveredFrequency::new);
    }

    private void broadcast(DiscoveryEvent event)
    {
        if(mListener != null)
        {
            mListener.receive(event);
        }
    }
}
