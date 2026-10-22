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
package io.github.dsheirer.gui.discovery;

import io.github.dsheirer.module.decode.nbfm.discovery.DiscoveryChannelManager;
import io.github.dsheirer.module.decode.nbfm.discovery.DiscoveryEvent;
import io.github.dsheirer.module.decode.nbfm.discovery.TrackedCarrier;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.function.LongPredicate;
import javax.swing.table.AbstractTableModel;

/**
 * Table of discovered frequencies, one row per channel frequency, updated from discovery events.  Must only be
 * accessed on the Swing event dispatch thread.
 */
public class DiscoveredFrequencyTableModel extends AbstractTableModel
{
    public static final int COLUMN_FREQUENCY = 0;
    public static final int COLUMN_STATUS = 1;
    public static final int COLUMN_LAST_HEARD = 2;
    public static final int COLUMN_TRANSMISSIONS = 3;
    public static final int COLUMN_AIRTIME = 4;
    public static final int COLUMN_SNR = 5;
    public static final int COLUMN_WIDTH = 6;
    public static final int COLUMN_PLAYLIST = 7;
    public static final int COLUMN_CHANNEL = 8;

    private static final String[] COLUMNS = {"Frequency (MHz)", "Status", "Last Heard", "Transmissions",
            "Airtime (s)", "Peak SNR (dB)", "Width (kHz)", "In Playlist", "Channel"};

    private final SimpleDateFormat mTimeFormat = new SimpleDateFormat("HH:mm:ss");
    private final List<Row> mRows = new ArrayList<>();
    private LongPredicate mPlaylistLookup = frequency -> false;
    private boolean mRecordingTime = false;

    /**
     * Sets the test used to flag frequencies that already have a channel in the playlist.
     */
    public void setPlaylistLookup(LongPredicate playlistLookup)
    {
        mPlaylistLookup = playlistLookup;
    }

    /**
     * Sets how timestamps are shown: wall clock time for a live tuner, or seconds from the start for a recording.
     */
    public void setRecordingTime(boolean recordingTime)
    {
        mRecordingTime = recordingTime;
        fireTableDataChanged();
    }

    /**
     * Marks every row idle, for example when discovery is stopped mid-transmission.
     */
    public void setAllIdle()
    {
        for(Row row : mRows)
        {
            row.mActive = false;
        }

        fireTableDataChanged();
    }

    public void clear()
    {
        mRows.clear();
        fireTableDataChanged();
    }

    /**
     * Channel frequency of the row, in Hertz.
     */
    public long getFrequency(int row)
    {
        return mRows.get(row).mFrequency;
    }

    /**
     * Removes the row for the frequency.
     */
    public void remove(long frequency)
    {
        for(int x = 0; x < mRows.size(); x++)
        {
            if(mRows.get(x).mFrequency == frequency)
            {
                mRows.remove(x);
                fireTableRowsDeleted(x, x);
                return;
            }
        }
    }

    /**
     * Applies a discovery event.
     */
    public void update(Snapshot snapshot)
    {
        int index = 0;

        while(index < mRows.size() && mRows.get(index).mFrequency < snapshot.frequency())
        {
            index++;
        }

        Row row;

        if(index < mRows.size() && mRows.get(index).mFrequency == snapshot.frequency())
        {
            row = mRows.get(index);
        }
        else
        {
            row = new Row(snapshot.frequency(), mPlaylistLookup.test(snapshot.frequency()));
            mRows.add(index, row);
            fireTableRowsInserted(index, index);
        }

        row.mOffRaster = snapshot.offRaster();
        row.mLastHeard = snapshot.lastSeen();
        row.mPeakSnr = Math.max(row.mPeakSnr, snapshot.peakSnrDb());
        row.mWidth = Math.max(row.mWidth, snapshot.maximumWidthHz());

        switch(snapshot.type())
        {
            case STARTED ->
            {
                row.mActive = true;
                row.mTransmissions++;
            }
            case ENDED ->
            {
                row.mActive = false;
                row.mAirtimeMs += snapshot.durationMs();
            }
            case BIRDIE ->
            {
                row.mActive = false;
                row.mBirdie = true;
                row.mAirtimeMs += snapshot.durationMs();
            }
        }

        fireTableRowsUpdated(index, index);
    }

    /**
     * Applies a temporary channel status change to the frequency's row, if it is listed.
     */
    public void update(DiscoveryChannelManager.ChannelStatus status)
    {
        for(int x = 0; x < mRows.size(); x++)
        {
            Row row = mRows.get(x);

            if(row.mFrequency == status.frequency())
            {
                row.mChannel = switch(status.state())
                {
                    case OPEN -> "Open (" + status.detail() + ")";
                    case CLOSED -> "";
                    case NO_ROOM -> "No free channel";
                    case OUT_OF_BAND -> "Too near band edge";
                    case FAILED -> "Failed: " + status.detail();
                };
                fireTableRowsUpdated(x, x);
                return;
            }
        }
    }

    @Override
    public int getRowCount()
    {
        return mRows.size();
    }

    @Override
    public int getColumnCount()
    {
        return COLUMNS.length;
    }

    @Override
    public String getColumnName(int column)
    {
        return COLUMNS[column];
    }

    @Override
    public Class<?> getColumnClass(int column)
    {
        return switch(column)
        {
            case COLUMN_TRANSMISSIONS -> Integer.class;
            case COLUMN_AIRTIME, COLUMN_SNR, COLUMN_WIDTH -> Double.class;
            default -> String.class;
        };
    }

    @Override
    public Object getValueAt(int rowIndex, int column)
    {
        Row row = mRows.get(rowIndex);

        return switch(column)
        {
            case COLUMN_FREQUENCY -> String.format("%.5f", row.mFrequency / 1e6) + (row.mOffRaster ? " *" : "");
            case COLUMN_STATUS -> row.mBirdie ? "Birdie (ignored)" : row.mActive ? "ON AIR" : "Idle";
            case COLUMN_LAST_HEARD -> mRecordingTime ? String.format("+%.1f s", row.mLastHeard / 1000.0) :
                    mTimeFormat.format(new Date(row.mLastHeard));
            case COLUMN_TRANSMISSIONS -> row.mTransmissions;
            case COLUMN_AIRTIME -> Math.round(row.mAirtimeMs / 100.0) / 10.0;
            case COLUMN_SNR -> Math.round(row.mPeakSnr * 10.0) / 10.0;
            case COLUMN_WIDTH -> Math.round(row.mWidth / 100.0) / 10.0;
            case COLUMN_PLAYLIST -> row.mInPlaylist ? "Yes" : "";
            case COLUMN_CHANNEL -> row.mChannel;
            default -> null;
        };
    }

    /**
     * Immutable copy of a discovery event, taken on the sample processing thread so that it can be safely handed to
     * the Swing thread.
     */
    public record Snapshot(DiscoveryEvent.Type type, long frequency, boolean offRaster, long lastSeen,
                           long durationMs, double peakSnrDb, double maximumWidthHz)
    {
        public static Snapshot of(DiscoveryEvent event)
        {
            TrackedCarrier carrier = event.carrier();
            return new Snapshot(event.type(), carrier.getChannelFrequency(), carrier.isOffRaster(),
                    carrier.getLastSeen(), carrier.getDuration(), carrier.getPeakSnrDb(), carrier.getMaximumWidthHz());
        }
    }

    private static class Row
    {
        private final long mFrequency;
        private final boolean mInPlaylist;
        private boolean mOffRaster;
        private boolean mActive;
        private boolean mBirdie;
        private long mLastHeard;
        private int mTransmissions;
        private long mAirtimeMs;
        private double mPeakSnr;
        private double mWidth;
        private String mChannel = "";

        Row(long frequency, boolean inPlaylist)
        {
            mFrequency = frequency;
            mInPlaylist = inPlaylist;
        }
    }
}
