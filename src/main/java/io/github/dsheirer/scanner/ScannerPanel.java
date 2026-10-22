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

import net.miginfocom.swing.MigLayout;

import javax.swing.JButton;
import javax.swing.JLabel;
import javax.swing.JPanel;
import javax.swing.JScrollPane;
import javax.swing.JTable;
import javax.swing.ListSelectionModel;
import javax.swing.Timer;
import javax.swing.table.AbstractTableModel;
import javax.swing.table.DefaultTableCellRenderer;
import java.awt.Component;
import java.awt.Font;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;

/**
 * Scanner panel that shows each running scan list (NBFM/AM channels with more than one frequency), which
 * frequencies are being scanned or monitored in parallel, and which are receiving a call.  Provides hold, skip and
 * lockout controls.
 */
public class ScannerPanel extends JPanel
{
    private static final int REFRESH_INTERVAL_MS = 250;
    private static final String HELP_TEXT = "<html>No scan lists are running.  To create one, add an NBFM or AM " +
        "channel in the Playlist Editor, give it more than one frequency, and start it.</html>";

    private final ScannerTableModel mTableModel = new ScannerTableModel();
    private final JTable mTable = new JTable(mTableModel);
    private final JLabel mStatusLabel = new JLabel(HELP_TEXT);
    private final JButton mHoldButton = new JButton("Hold");
    private final JButton mSkipButton = new JButton("Skip");
    private final JButton mLockoutButton = new JButton("Lock Out");
    private final Timer mRefreshTimer = new Timer(REFRESH_INTERVAL_MS, e -> refresh());

    /**
     * Constructs an instance
     */
    public ScannerPanel()
    {
        setLayout(new MigLayout("insets 2 2 2 2", "[][][][grow,fill]", "[]2[grow,fill]"));

        mHoldButton.setToolTipText("Stop scanning and stay on the current frequency of the selected scan list");
        mHoldButton.addActionListener(e -> {
            ScanEntryStatus selected = getSelectedEntry();

            if(selected != null)
            {
                ScanGroup group = selected.getScanGroup();
                group.setHold(!group.isHold());
                updateControls();
            }
        });

        mSkipButton.setToolTipText("Move the selected scan list to its next frequency now");
        mSkipButton.addActionListener(e -> {
            ScanEntryStatus selected = getSelectedEntry();

            if(selected != null)
            {
                selected.getScanGroup().skip();
                updateControls();
            }
        });

        mLockoutButton.setToolTipText("Skip the selected frequency until it is restored or the scan list is " +
            "restarted.  Use the Playlist Editor to lock out a frequency permanently.");
        mLockoutButton.addActionListener(e -> {
            ScanEntryStatus selected = getSelectedEntry();

            if(selected != null)
            {
                selected.getScanGroup().setLockout(selected, !selected.isLockedOut());
                updateControls();
            }
        });

        add(mHoldButton);
        add(mSkipButton);
        add(mLockoutButton);
        add(mStatusLabel, "wrap");

        mTable.setSelectionMode(ListSelectionModel.SINGLE_SELECTION);
        mTable.setAutoCreateRowSorter(false);
        mTable.getSelectionModel().addListSelectionListener(e -> updateControls());
        mTable.setDefaultRenderer(Object.class, new StatusRenderer());

        JScrollPane scrollPane = new JScrollPane(mTable);
        add(scrollPane, "span,grow");

        //Only refresh while the panel is visible
        addHierarchyListener(e -> {
            if(isShowing())
            {
                refresh();
                mRefreshTimer.start();
            }
            else
            {
                mRefreshTimer.stop();
            }
        });

        updateControls();
    }

    /**
     * Currently selected scan list entry or null
     */
    private ScanEntryStatus getSelectedEntry()
    {
        int row = mTable.getSelectedRow();
        return row >= 0 ? mTableModel.getEntry(row) : null;
    }

    /**
     * Reloads the running scan groups and repaints the table, preserving the selection.
     */
    private void refresh()
    {
        ScanEntryStatus selected = getSelectedEntry();
        mTableModel.update(ScannerRegistry.getInstance().getScanGroups());

        if(selected != null)
        {
            int row = mTableModel.indexOf(selected);

            if(row >= 0 && mTable.getSelectedRow() != row)
            {
                mTable.getSelectionModel().setSelectionInterval(row, row);
            }
        }

        updateControls();
    }

    /**
     * Updates the button states and the status summary for the selected scan list.
     */
    private void updateControls()
    {
        ScanEntryStatus selected = getSelectedEntry();
        boolean hasSelection = selected != null;
        mHoldButton.setEnabled(hasSelection);
        mSkipButton.setEnabled(hasSelection);
        mLockoutButton.setEnabled(hasSelection);

        if(hasSelection)
        {
            ScanGroup group = selected.getScanGroup();
            mHoldButton.setText(group.isHold() ? "Resume" : "Hold");
            mLockoutButton.setText(selected.isLockedOut() ? "Restore" : "Lock Out");
            mStatusLabel.setText(getSummary(group));
        }
        else if(mTableModel.getRowCount() == 0)
        {
            mHoldButton.setText("Hold");
            mLockoutButton.setText("Lock Out");
            mStatusLabel.setText(HELP_TEXT);
        }
        else
        {
            mStatusLabel.setText("Select a frequency to control its scan list");
        }
    }

    /**
     * Summary of what the scan group is doing.
     */
    private static String getSummary(ScanGroup group)
    {
        int scanned = 0;
        int parallel = 0;

        for(ScanEntryStatus entry: group.getEntries())
        {
            if(entry.getMode() == ScanEntryStatus.Mode.SCAN)
            {
                scanned++;
            }
            else if(entry.getMode() == ScanEntryStatus.Mode.PARALLEL)
            {
                parallel++;
            }
        }

        StringBuilder sb = new StringBuilder();
        sb.append(group.getName()).append(": ");

        ScanEntryStatus tuned = group.getEntry(group.getRoverFrequency());
        String tunedName = tuned != null ? tuned.getDisplayName() : "-";

        if(group.isParked())
        {
            sb.append("monitoring ").append(parallel + scanned).append(" frequencies at the same time");
            return sb.toString();
        }

        sb.append(group.isHold() ? "holding on " : "scanning " + scanned + " frequencies, now on ").append(tunedName);

        if(parallel > 0)
        {
            sb.append(", plus ").append(parallel).append(" monitored at the same time");
        }

        return sb.toString();
    }

    /**
     * Table model with one row per scan list frequency.
     */
    private static class ScannerTableModel extends AbstractTableModel
    {
        private static final String[] COLUMNS = {"Scan List", "Frequency", "Label", "Status", "Mode", "Tuner",
            "Last Heard"};
        private final SimpleDateFormat mTimeFormat = new SimpleDateFormat("HH:mm:ss");
        private List<ScanEntryStatus> mEntries = new ArrayList<>();

        public void update(List<ScanGroup> groups)
        {
            List<ScanEntryStatus> entries = new ArrayList<>();

            for(ScanGroup group: groups)
            {
                entries.addAll(group.getEntries());
            }

            boolean structureChanged = !entries.equals(mEntries);
            mEntries = entries;

            if(structureChanged)
            {
                fireTableDataChanged();
            }
            else if(!mEntries.isEmpty())
            {
                fireTableRowsUpdated(0, mEntries.size() - 1);
            }
        }

        public ScanEntryStatus getEntry(int row)
        {
            return row < mEntries.size() ? mEntries.get(row) : null;
        }

        public int indexOf(ScanEntryStatus entry)
        {
            return mEntries.indexOf(entry);
        }

        @Override
        public int getRowCount()
        {
            return mEntries.size();
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
        public Object getValueAt(int row, int column)
        {
            ScanEntryStatus entry = getEntry(row);

            if(entry == null)
            {
                return null;
            }

            return switch(column)
            {
                case 0 -> entry.getScanGroup().getName();
                case 1 -> String.format("%.5f", entry.getFrequency() / 1E6);
                case 2 -> entry.getLabel() != null ? entry.getLabel() : "";
                case 3 -> getStatus(entry);
                case 4 -> getMode(entry) + (entry.isPriority() ? " (priority)" : "");
                case 5 -> getTuner(entry);
                case 6 -> entry.getLastActivity() > 0 ? mTimeFormat.format(new Date(entry.getLastActivity())) : "";
                default -> null;
            };
        }

        private static String getMode(ScanEntryStatus entry)
        {
            //A parked rover monitors its frequency full time, just like the parallel channels
            if(entry.getMode() == ScanEntryStatus.Mode.SCAN && entry.getScanGroup().isParked())
            {
                return ScanEntryStatus.Mode.PARALLEL.toString();
            }

            return entry.getMode().toString();
        }

        private static String getTuner(ScanEntryStatus entry)
        {
            if(entry.getTunerName() != null)
            {
                return entry.getTunerName();
            }
            else if(entry.isTuned() && entry.getScanGroup().getRoverTunerName() != null)
            {
                return entry.getScanGroup().getRoverTunerName();
            }

            return "";
        }

        private static String getStatus(ScanEntryStatus entry)
        {
            if(entry.isLockedOut())
            {
                return "";
            }
            else if(entry.isReceiving())
            {
                return "Receiving";
            }
            else if(entry.isTuned())
            {
                ScanGroup group = entry.getScanGroup();
                return group.isParked() ? "Idle" : group.isHold() ? "Holding" : "Tuned";
            }
            else if(entry.getMode() == ScanEntryStatus.Mode.PARALLEL)
            {
                return entry.isChildRunning() ? "Idle" : "Starting";
            }

            return "";
        }
    }

    /**
     * Renders receiving rows in bold so active calls stand out in any color theme.
     */
    private class StatusRenderer extends DefaultTableCellRenderer
    {
        @Override
        public Component getTableCellRendererComponent(JTable table, Object value, boolean isSelected,
                                                       boolean hasFocus, int row, int column)
        {
            Component component = super.getTableCellRendererComponent(table, value, isSelected, hasFocus, row, column);
            ScanEntryStatus entry = mTableModel.getEntry(row);
            boolean receiving = entry != null && entry.isReceiving();
            component.setFont(table.getFont().deriveFont(receiving ? Font.BOLD : Font.PLAIN));
            component.setEnabled(entry == null || !entry.isLockedOut());
            return component;
        }
    }
}
