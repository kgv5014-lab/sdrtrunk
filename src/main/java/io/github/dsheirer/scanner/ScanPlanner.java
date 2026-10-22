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

package io.github.dsheirer.scanner;

import java.util.ArrayList;
import java.util.Collection;
import java.util.Collections;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.function.LongPredicate;

/**
 * Plans how a conventional scan list is spread across the available tuners.
 *
 * Each RTL-SDR style tuner can decode every channel inside its usable bandwidth at the same time.  The planner groups
 * the scan list frequencies into clusters that each fit inside one tuner's usable bandwidth.  Clusters are assigned
 * to free tuners and monitored in parallel (no missed calls), while the scanning (rover) channel rotates through any
 * frequencies that are left over.
 *
 * This class has no dependencies on live tuners so that it can be unit tested.
 */
public class ScanPlanner
{
    /**
     * A tuner that has no channels allocated and can host a cluster of parallel channels.
     * @param name preferred tuner name, used to pin channels to the tuner
     * @param usableBandwidth of the tuner in hertz
     * @param canTune predicate to test if the tuner can tune a frequency
     */
    public record TunerSlot(String name, int usableBandwidth, LongPredicate canTune)
    {
        /**
         * Indicates if every frequency in the cluster can be tuned by this tuner and the cluster fits inside the
         * usable bandwidth.
         */
        public boolean canHost(List<Long> cluster, int channelBandwidth)
        {
            if(cluster.isEmpty() || span(cluster, channelBandwidth) > usableBandwidth)
            {
                return false;
            }

            for(Long frequency: cluster)
            {
                if(!canTune.test(frequency))
                {
                    return false;
                }
            }

            return true;
        }
    }

    /**
     * Planning result.
     * @param parallel map of frequency to the tuner name that should monitor it in parallel, in assignment order
     * @param scanned frequencies that the rover channel should continue to scan
     */
    public record Plan(Map<Long,String> parallel, List<Long> scanned)
    {
        /**
         * Indicates if every frequency is covered in parallel or by the rover's current frequency, meaning the rover
         * will remain parked on its current frequency.
         */
        public boolean isFullyParallel()
        {
            return scanned.size() <= 1;
        }
    }

    /**
     * Bandwidth spanned by a sorted cluster of channel frequencies, including half a channel on each edge.
     */
    public static long span(List<Long> sortedCluster, int channelBandwidth)
    {
        if(sortedCluster.isEmpty())
        {
            return 0;
        }

        return sortedCluster.get(sortedCluster.size() - 1) - sortedCluster.get(0) + channelBandwidth;
    }

    /**
     * Groups frequencies into clusters, lowest frequency first, where each cluster fits inside the maximum span.
     *
     * @param frequencies to cluster (any order, duplicates are removed)
     * @param maxSpan in hertz (tuner usable bandwidth)
     * @param channelBandwidth of each channel in hertz
     * @return clusters, each sorted by frequency
     */
    public static List<List<Long>> cluster(Collection<Long> frequencies, int maxSpan, int channelBandwidth)
    {
        List<Long> sorted = new ArrayList<>(frequencies.stream().distinct().toList());
        Collections.sort(sorted);

        List<List<Long>> clusters = new ArrayList<>();
        List<Long> current = new ArrayList<>();

        for(Long frequency: sorted)
        {
            if(!current.isEmpty() && (frequency - current.get(0) + channelBandwidth) > maxSpan)
            {
                clusters.add(current);
                current = new ArrayList<>();
            }

            current.add(frequency);
        }

        if(!current.isEmpty())
        {
            clusters.add(current);
        }

        return clusters;
    }

    /**
     * Creates a plan for the scan list.
     *
     * @param frequencies to monitor, in scan list order, excluding any locked out frequencies.
     * @param priorityFrequencies that the user marked as priority.  Clusters holding a priority frequency are assigned
     * to free tuners first.
     * @param roverFrequency the frequency the rover (scanning) channel is currently tuned to.
     * @param roverTuner the tuner that is sourcing the rover channel, or null if unknown.  When every cluster can be
     * covered, the other frequencies in the rover's cluster are placed on this tuner and the rover stays parked.
     * @param freeTuners tuners that currently have no channels allocated.
     * @param channelBandwidth of each channel in hertz.
     * @return plan
     */
    public static Plan plan(List<Long> frequencies, Collection<Long> priorityFrequencies, long roverFrequency,
                            TunerSlot roverTuner, List<TunerSlot> freeTuners, int channelBandwidth)
    {
        Map<Long,String> parallel = new LinkedHashMap<>();

        if(frequencies.size() <= 1 || (roverTuner == null && freeTuners.isEmpty()))
        {
            return new Plan(parallel, new ArrayList<>(frequencies));
        }

        //Cluster using the narrowest usable bandwidth so that any cluster fits on any of the tuners.
        int usableBandwidth = Integer.MAX_VALUE;

        if(roverTuner != null)
        {
            usableBandwidth = roverTuner.usableBandwidth();
        }

        for(TunerSlot slot: freeTuners)
        {
            usableBandwidth = Math.min(usableBandwidth, slot.usableBandwidth());
        }

        List<List<Long>> clusters = cluster(frequencies, usableBandwidth, channelBandwidth);

        List<Long> roverCluster = null;

        for(List<Long> cluster: clusters)
        {
            if(cluster.contains(roverFrequency))
            {
                roverCluster = cluster;
                break;
            }
        }

        List<List<Long>> others = new ArrayList<>(clusters);

        if(roverCluster != null)
        {
            others.remove(roverCluster);
        }

        //Rank the other clusters: clusters with a priority frequency first, then the largest clusters.
        others.sort(Comparator.<List<Long>>comparingInt(c -> containsAny(c, priorityFrequencies) ? 0 : 1)
            .thenComparing(Comparator.<List<Long>>comparingInt(List::size).reversed()));

        //Assign clusters to free tuners
        List<TunerSlot> available = new ArrayList<>(freeTuners);
        List<List<Long>> unassigned = new ArrayList<>();

        for(List<Long> cluster: others)
        {
            TunerSlot slot = findSlot(available, cluster, channelBandwidth);

            if(slot != null)
            {
                available.remove(slot);

                for(Long frequency: cluster)
                {
                    parallel.put(frequency, slot.name());
                }
            }
            else
            {
                unassigned.add(cluster);
            }
        }

        //When everything outside the rover's cluster is covered, park the rover and cover the rest of its cluster
        //on the rover's tuner.
        if(unassigned.isEmpty() && roverCluster != null && roverTuner != null &&
            roverTuner.canHost(roverCluster, channelBandwidth))
        {
            for(Long frequency: roverCluster)
            {
                if(frequency != roverFrequency)
                {
                    parallel.put(frequency, roverTuner.name());
                }
            }
        }

        List<Long> scanned = new ArrayList<>();

        for(Long frequency: frequencies)
        {
            if(!parallel.containsKey(frequency))
            {
                scanned.add(frequency);
            }
        }

        return new Plan(parallel, scanned);
    }

    /**
     * Finds the first tuner slot that can host the cluster
     */
    private static TunerSlot findSlot(List<TunerSlot> slots, List<Long> cluster, int channelBandwidth)
    {
        for(TunerSlot slot: slots)
        {
            if(slot.canHost(cluster, channelBandwidth))
            {
                return slot;
            }
        }

        return null;
    }

    /**
     * Indicates if the cluster contains any of the frequencies
     */
    private static boolean containsAny(List<Long> cluster, Collection<Long> frequencies)
    {
        for(Long frequency: frequencies)
        {
            if(cluster.contains(frequency))
            {
                return true;
            }
        }

        return false;
    }
}
