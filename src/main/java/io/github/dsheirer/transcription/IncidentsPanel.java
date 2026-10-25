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

package io.github.dsheirer.transcription;

import net.miginfocom.swing.MigLayout;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import javax.swing.JButton;
import javax.swing.JCheckBox;
import javax.swing.JComboBox;
import javax.swing.JLabel;
import javax.swing.JPanel;
import javax.swing.JScrollPane;
import javax.swing.JSplitPane;
import javax.swing.JTable;
import javax.swing.JTextArea;
import javax.swing.ListSelectionModel;
import javax.swing.SwingUtilities;
import javax.swing.Timer;
import javax.swing.table.AbstractTableModel;
import javax.swing.table.DefaultTableCellRenderer;
import java.awt.Color;
import java.awt.Component;
import java.awt.Desktop;
import java.awt.Font;
import java.awt.event.ComponentAdapter;
import java.awt.event.ComponentEvent;
import java.net.URI;
import java.text.SimpleDateFormat;
import java.time.DayOfWeek;
import java.time.LocalDate;
import java.time.ZoneId;
import java.time.temporal.TemporalAdjusters;
import java.util.ArrayList;
import java.util.Date;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.prefs.Preferences;

/**
 * Lists the possible incidents from the transcription service's incident map (transcriber/ in this repository):
 * calls that named the same address or intersection, plus follow-up calls from units sent there.  Filters by
 * time and type (Fire, EMS, Police, MVA, Other) and refreshes every few seconds while the tab is visible.
 * Incidents that appear after the tab was opened are shown in bold until selected.
 */
public class IncidentsPanel extends JPanel
{
    private static final Logger mLog = LoggerFactory.getLogger(IncidentsPanel.class);
    private static final int REFRESH_INTERVAL_MS = 5000;
    private static final String PREFERENCE_URL = "transcription.service.url";
    private static final String PREFERENCE_RANGE = "transcription.incidents.range";
    private static final String PREFERENCE_TYPES = "transcription.incidents.types";
    static final String[] TYPES = {"fire", "ems", "police", "mva", "other"};
    private static final Map<String, Color> TYPE_COLORS = Map.of(
        "fire", new Color(0xD3, 0x3C, 0x2F), "ems", new Color(0x1F, 0x8A, 0x5B),
        "police", new Color(0x2A, 0x6F, 0xDB), "mva", new Color(0xE0, 0x7B, 0x00), "other", new Color(0x7A, 0x7A, 0x82));

    /**
     * Time ranges offered in the time filter.
     */
    enum Range
    {
        HOUR("Last hour"), SIX_HOURS("Last 6 hours"), TODAY("Today"), DAY("Last 24 hours"), WEEK("This week"),
        MONTH("Last 30 days");

        private final String mLabel;

        Range(String label)
        {
            mLabel = label;
        }

        /**
         * Start of the range in seconds since epoch.  This week starts Sunday at midnight.
         */
        long start(long nowSeconds, ZoneId zone)
        {
            LocalDate today = LocalDate.ofInstant(java.time.Instant.ofEpochSecond(nowSeconds), zone);

            return switch(this)
            {
                case HOUR -> nowSeconds - 3600;
                case SIX_HOURS -> nowSeconds - 6 * 3600;
                case TODAY -> today.atStartOfDay(zone).toEpochSecond();
                case DAY -> nowSeconds - 24 * 3600;
                case WEEK -> today.with(TemporalAdjusters.previousOrSame(DayOfWeek.SUNDAY)).atStartOfDay(zone).toEpochSecond();
                case MONTH -> nowSeconds - 30L * 24 * 3600;
            };
        }

        @Override
        public String toString()
        {
            return mLabel;
        }
    }

    private final Preferences mPreferences = Preferences.userNodeForPackage(TranscriptsPanel.class);
    private final TranscriptionClient mClient = new TranscriptionClient();
    private final IncidentTableModel mTableModel = new IncidentTableModel();
    private final JTable mTable = new JTable(mTableModel);
    private final JComboBox<Range> mRangeCombo = new JComboBox<>(Range.values());
    private final Map<String, JCheckBox> mTypeBoxes = new LinkedHashMap<>();
    private final JButton mMapButton = new JButton("Open Map");
    private final JTextArea mDetailArea = new JTextArea(6, 40);
    private final JLabel mStatusLabel = new JLabel(" ");
    private final Timer mRefreshTimer = new Timer(REFRESH_INTERVAL_MS, e -> refresh());
    private final Set<Long> mSeen = new HashSet<>();
    private List<TranscriptIncident> mAllIncidents = new ArrayList<>();
    private boolean mFirstLoad = true;
    private boolean mRefreshing = false;

    /**
     * Constructs an instance
     */
    public IncidentsPanel()
    {
        setLayout(new MigLayout("insets 2 2 2 2", "[][][][][][][][grow][]", "[]2[grow,fill]2[]"));

        mRangeCombo.setSelectedItem(rangeFromPreference());
        mRangeCombo.addActionListener(e -> {
            mPreferences.put(PREFERENCE_RANGE, ((Range)mRangeCombo.getSelectedItem()).name());
            refresh();
        });
        add(new JLabel("Show:"));
        add(mRangeCombo);

        Set<String> shown = new HashSet<>(List.of(mPreferences.get(PREFERENCE_TYPES, String.join(",", TYPES)).split(",")));

        for(String type : TYPES)
        {
            JCheckBox box = new JCheckBox(TranscriptIncident.typeName(type), shown.contains(type));
            box.setForeground(TYPE_COLORS.get(type));
            box.addActionListener(e -> {
                saveTypes();
                applyFilter();
            });
            mTypeBoxes.put(type, box);
            add(box);
        }

        mMapButton.setToolTipText("Open the incident map in a browser");
        mMapButton.addActionListener(e -> openMap());
        add(new JLabel(""), "growx");
        add(mMapButton, "wrap");

        mTable.setSelectionMode(ListSelectionModel.SINGLE_SELECTION);
        mTable.setDefaultRenderer(Object.class, new IncidentRenderer());
        mTable.getColumnModel().getColumn(IncidentTableModel.TIME).setPreferredWidth(140);
        mTable.getColumnModel().getColumn(IncidentTableModel.TIME).setMaxWidth(200);
        mTable.getColumnModel().getColumn(IncidentTableModel.TYPE).setPreferredWidth(70);
        mTable.getColumnModel().getColumn(IncidentTableModel.TYPE).setMaxWidth(90);
        mTable.getColumnModel().getColumn(IncidentTableModel.WHAT).setPreferredWidth(140);
        mTable.getColumnModel().getColumn(IncidentTableModel.PLACE).setPreferredWidth(280);
        mTable.getColumnModel().getColumn(IncidentTableModel.SUMMARY).setPreferredWidth(420);
        mTable.getColumnModel().getColumn(IncidentTableModel.CALLS).setPreferredWidth(50);
        mTable.getColumnModel().getColumn(IncidentTableModel.CALLS).setMaxWidth(70);
        mTable.getSelectionModel().addListSelectionListener(e -> {
            if(!e.getValueIsAdjusting())
            {
                TranscriptIncident incident = getSelectedIncident();

                if(incident != null && mSeen.add(incident.id()))
                {
                    mTable.repaint();
                }

                updateDetail();
            }
        });

        mDetailArea.setEditable(false);
        mDetailArea.setLineWrap(true);
        mDetailArea.setWrapStyleWord(true);

        JSplitPane split = new JSplitPane(JSplitPane.VERTICAL_SPLIT, new JScrollPane(mTable),
            new JScrollPane(mDetailArea));
        split.setResizeWeight(0.6);
        add(split, "span, grow, wrap");
        add(mStatusLabel, "span");

        addComponentListener(new ComponentAdapter()
        {
            @Override
            public void componentShown(ComponentEvent e)
            {
                refresh();
            }
        });

        mRefreshTimer.setInitialDelay(0);
        mRefreshTimer.start();
    }

    private Range rangeFromPreference()
    {
        try
        {
            return Range.valueOf(mPreferences.get(PREFERENCE_RANGE, Range.DAY.name()));
        }
        catch(IllegalArgumentException e)
        {
            return Range.DAY;
        }
    }

    private void saveTypes()
    {
        mPreferences.put(PREFERENCE_TYPES, String.join(",", shownTypes()));
    }

    private Set<String> shownTypes()
    {
        Set<String> shown = new HashSet<>();
        mTypeBoxes.forEach((type, box) -> {
            if(box.isSelected())
            {
                shown.add(type);
            }
        });
        return shown;
    }

    private String getUrl()
    {
        return TranscriptionClient.base(mPreferences.get(PREFERENCE_URL, TranscriptionClient.DEFAULT_URL));
    }

    private void openMap()
    {
        try
        {
            Desktop.getDesktop().browse(URI.create(getUrl() + "/map"));
        }
        catch(Exception e)
        {
            mLog.error("Unable to open the incident map in a browser", e);
            mStatusLabel.setText("Couldn't open a browser for " + getUrl() + "/map");
        }
    }

    /**
     * Fetches incidents in the background.  Skipped while the tab isn't on screen.
     */
    private void refresh()
    {
        if(!isShowing() || mRefreshing)
        {
            return;
        }

        mRefreshing = true;
        String url = getUrl();
        long since = ((Range)mRangeCombo.getSelectedItem()).start(System.currentTimeMillis() / 1000, ZoneId.systemDefault());

        mClient.fetchIncidents(url, since).whenComplete((incidents, error) -> SwingUtilities.invokeLater(() -> {
            mRefreshing = false;

            if(error != null)
            {
                mStatusLabel.setText("Incident map isn't reachable at " + url +
                    ". Start the transcriber with start-transcriber.bat, and run fetch-places.bat once.");
                return;
            }

            if(mFirstLoad)
            {
                //Everything already on the map when the tab first loads counts as seen
                incidents.forEach(incident -> mSeen.add(incident.id()));
                mFirstLoad = false;
            }

            mAllIncidents = incidents;
            applyFilter();
        }));
    }

    private void applyFilter()
    {
        Set<String> shown = shownTypes();
        List<TranscriptIncident> visible = mAllIncidents.stream().filter(i -> shown.contains(i.typeKey())).toList();
        TranscriptIncident selected = getSelectedIncident();

        if(mTableModel.setIncidents(visible) && selected != null)
        {
            int row = mTableModel.indexOf(selected.id());

            if(row >= 0)
            {
                mTable.getSelectionModel().setSelectionInterval(row, row);
            }
        }

        long fresh = mAllIncidents.stream().filter(i -> !mSeen.contains(i.id())).count();
        mStatusLabel.setText(visible.size() + " of " + mAllIncidents.size() + " incidents shown" +
            (fresh > 0 ? ", " + fresh + " new" : "") + " · from " + getUrl() + "/map");
        updateDetail();
    }

    private TranscriptIncident getSelectedIncident()
    {
        int row = mTable.getSelectedRow();
        return row >= 0 ? mTableModel.getIncident(row) : null;
    }

    private void updateDetail()
    {
        TranscriptIncident incident = getSelectedIncident();
        mDetailArea.setText(incident == null ? "" : describe(incident));
        mDetailArea.setCaretPosition(0);
    }

    /**
     * Incident details: place, type, tone-outs and each call's transcript.
     */
    static String describe(TranscriptIncident incident)
    {
        StringBuilder sb = new StringBuilder();
        sb.append(incident.label() != null ? incident.label() : "Unknown place");
        sb.append("   ").append(incident.typeDisplay());

        if(incident.nature() != null && !incident.nature().equals("MVA"))
        {
            sb.append(" · ").append(incident.nature());
        }

        sb.append(String.format("   (%.5f, %.5f)", incident.latitude(), incident.longitude()));

        if(incident.summary() != null && !incident.summary().isBlank())
        {
            sb.append("\nSummary (AI): ").append(incident.summary());

            List<String> tags = new ArrayList<>();

            if(incident.summaryStatus() != null && !incident.summaryStatus().equals("unknown"))
            {
                tags.add(incident.summaryStatus());
            }

            tags.addAll(incident.summaryTags());

            if(!tags.isEmpty())
            {
                sb.append("\nTags: ").append(String.join(", ", tags));
            }
        }

        for(TranscriptIncident.Page page : incident.pages())
        {
            sb.append("\nTone out ").append(formatClock(page.pageTime())).append(": ")
                .append(page.label() != null ? page.label() : "not on the station list");
        }

        for(TranscriptIncident.Call call : incident.calls())
        {
            sb.append("\n\n").append(formatClock(call.callTime())).append("  ").append(call.talkgroupLabel());

            if(call.talkerAlias() != null && !call.talkerAlias().isBlank())
            {
                sb.append("  ").append(call.talkerAlias());
            }

            if("follow-up".equals(call.precision()))
            {
                sb.append("  (follow-up, same unit)");
            }

            sb.append("\n").append(call.transcript() != null ? call.transcript() : "");
        }

        return sb.toString();
    }

    private static String formatTime(long epochSeconds)
    {
        return new SimpleDateFormat("MMM d HH:mm").format(new Date(epochSeconds * 1000));
    }

    private static String formatClock(long epochSeconds)
    {
        return new SimpleDateFormat("HH:mm:ss").format(new Date(epochSeconds * 1000));
    }

    /**
     * Colors the type column and shows incidents not yet looked at in bold.
     */
    private class IncidentRenderer extends DefaultTableCellRenderer
    {
        @Override
        public Component getTableCellRendererComponent(JTable table, Object value, boolean isSelected,
                                                       boolean hasFocus, int row, int column)
        {
            Component component = super.getTableCellRendererComponent(table, value, isSelected, hasFocus, row, column);
            TranscriptIncident incident = mTableModel.getIncident(row);

            if(incident != null)
            {
                component.setFont(component.getFont().deriveFont(mSeen.contains(incident.id()) ? Font.PLAIN : Font.BOLD));

                if(!isSelected)
                {
                    component.setForeground(column == IncidentTableModel.TYPE ? TYPE_COLORS.get(incident.typeKey()) :
                        table.getForeground());
                }
            }

            return component;
        }
    }

    /**
     * Table model for the incident list, most recent first.
     */
    static class IncidentTableModel extends AbstractTableModel
    {
        static final int TIME = 0;
        static final int TYPE = 1;
        static final int WHAT = 2;
        static final int PLACE = 3;
        static final int CALLS = 4;
        static final int SUMMARY = 5;
        private static final String[] COLUMNS = {"Latest", "Type", "What", "Place", "Calls", "Summary"};

        private List<TranscriptIncident> mIncidents = new ArrayList<>();

        /**
         * Replaces the incidents.
         * @return true if anything changed
         */
        boolean setIncidents(List<TranscriptIncident> incidents)
        {
            if(mIncidents.equals(incidents))
            {
                return false;
            }

            mIncidents = new ArrayList<>(incidents);
            fireTableDataChanged();
            return true;
        }

        TranscriptIncident getIncident(int row)
        {
            return row >= 0 && row < mIncidents.size() ? mIncidents.get(row) : null;
        }

        int indexOf(long id)
        {
            for(int i = 0; i < mIncidents.size(); i++)
            {
                if(mIncidents.get(i).id() == id)
                {
                    return i;
                }
            }

            return -1;
        }

        @Override
        public int getRowCount()
        {
            return mIncidents.size();
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
            TranscriptIncident incident = mIncidents.get(row);

            return switch(column)
            {
                case TIME -> formatTime(incident.lastTime());
                case TYPE -> incident.typeDisplay();
                case WHAT -> incident.nature() != null ? incident.nature() : "";
                case PLACE -> incident.label() != null ? incident.label() : "";
                case CALLS -> incident.calls().size();
                case SUMMARY -> incident.summary() != null ? incident.summary() : "";
                default -> "";
            };
        }
    }
}
