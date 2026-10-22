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
import javax.swing.JLabel;
import javax.swing.JPanel;
import javax.swing.JScrollPane;
import javax.swing.JSplitPane;
import javax.swing.JTable;
import javax.swing.JTextArea;
import javax.swing.JTextField;
import javax.swing.ListSelectionModel;
import javax.swing.SwingUtilities;
import javax.swing.Timer;
import javax.swing.event.DocumentEvent;
import javax.swing.event.DocumentListener;
import javax.swing.table.AbstractTableModel;
import java.awt.Desktop;
import java.awt.event.ComponentAdapter;
import java.awt.event.ComponentEvent;
import java.awt.event.FocusAdapter;
import java.awt.event.FocusEvent;
import java.net.URI;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.prefs.Preferences;

/**
 * Shows the call transcripts from the local transcription service (transcriber/ in this repository) in a
 * table that refreshes every few seconds while the tab is visible.  Transcription itself runs in that
 * separate service, so this panel only makes one small HTTP request per refresh.
 */
public class TranscriptsPanel extends JPanel
{
    private static final Logger mLog = LoggerFactory.getLogger(TranscriptsPanel.class);
    private static final int REFRESH_INTERVAL_MS = 3000;
    private static final int FILTER_DELAY_MS = 300;
    private static final int CALL_LIMIT = 200;
    private static final String PREFERENCE_URL = "transcription.service.url";

    private final Preferences mPreferences = Preferences.userNodeForPackage(TranscriptsPanel.class);
    private final TranscriptionClient mClient = new TranscriptionClient();
    private final TranscriptTableModel mTableModel = new TranscriptTableModel();
    private final JTable mTable = new JTable(mTableModel);
    private final JTextField mSearchField = new JTextField();
    private final JTextField mTalkgroupField = new JTextField();
    private final JTextField mUrlField = new JTextField();
    private final JButton mBrowserButton = new JButton("Open in Browser");
    private final JTextArea mDetailArea = new JTextArea(4, 40);
    private final JLabel mStatusLabel = new JLabel(" ");
    private final Timer mRefreshTimer = new Timer(REFRESH_INTERVAL_MS, e -> refresh());
    private final Timer mFilterTimer = new Timer(FILTER_DELAY_MS, e -> refresh());
    private boolean mRefreshing = false;
    private boolean mRefreshAgain = false;

    /**
     * Constructs an instance
     */
    public TranscriptsPanel()
    {
        setLayout(new MigLayout("insets 2 2 2 2", "[][grow,fill][][][][][]", "[]2[grow,fill]2[]"));

        mSearchField.setToolTipText("Search transcripts, e.g. a street name, unit or 10-code");
        mTalkgroupField.setToolTipText("Show only one talkgroup (number or alias)");
        mUrlField.setText(mPreferences.get(PREFERENCE_URL, TranscriptionClient.DEFAULT_URL));
        mUrlField.setToolTipText("Address of the transcription service (start-transcriber.bat)");
        mBrowserButton.setToolTipText("Open the call log web page, where calls can be played back");

        add(new JLabel("Search:"));
        add(mSearchField, "growx");
        add(new JLabel("Talkgroup:"));
        add(mTalkgroupField, "w 120!");
        add(mBrowserButton);
        add(new JLabel("Service:"));
        add(mUrlField, "w 220!, wrap");

        mTable.setSelectionMode(ListSelectionModel.SINGLE_SELECTION);
        mTable.setAutoCreateRowSorter(false);
        mTable.getColumnModel().getColumn(TranscriptTableModel.TIME).setPreferredWidth(140);
        mTable.getColumnModel().getColumn(TranscriptTableModel.TIME).setMaxWidth(180);
        mTable.getColumnModel().getColumn(TranscriptTableModel.TALKGROUP).setPreferredWidth(160);
        mTable.getColumnModel().getColumn(TranscriptTableModel.FROM).setPreferredWidth(140);
        mTable.getColumnModel().getColumn(TranscriptTableModel.TRANSCRIPT).setPreferredWidth(600);
        mTable.getSelectionModel().addListSelectionListener(e -> {
            if(!e.getValueIsAdjusting())
            {
                updateDetail();
            }
        });

        mDetailArea.setEditable(false);
        mDetailArea.setLineWrap(true);
        mDetailArea.setWrapStyleWord(true);

        JSplitPane split = new JSplitPane(JSplitPane.VERTICAL_SPLIT, new JScrollPane(mTable),
            new JScrollPane(mDetailArea));
        split.setResizeWeight(0.85);
        add(split, "span, grow, wrap");
        add(mStatusLabel, "span");

        DocumentListener filterListener = new DocumentListener()
        {
            @Override public void insertUpdate(DocumentEvent e) { mFilterTimer.restart(); }
            @Override public void removeUpdate(DocumentEvent e) { mFilterTimer.restart(); }
            @Override public void changedUpdate(DocumentEvent e) { mFilterTimer.restart(); }
        };
        mSearchField.getDocument().addDocumentListener(filterListener);
        mTalkgroupField.getDocument().addDocumentListener(filterListener);
        mFilterTimer.setRepeats(false);

        mUrlField.addActionListener(e -> saveUrl());
        mUrlField.addFocusListener(new FocusAdapter()
        {
            @Override
            public void focusLost(FocusEvent e)
            {
                saveUrl();
            }
        });

        mBrowserButton.addActionListener(e -> openBrowser());

        //Refresh as soon as the tab is selected rather than waiting for the next timer tick
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

    private String getUrl()
    {
        String url = mUrlField.getText().trim();
        return url.isEmpty() ? TranscriptionClient.DEFAULT_URL : url;
    }

    private void saveUrl()
    {
        if(!getUrl().equals(mPreferences.get(PREFERENCE_URL, TranscriptionClient.DEFAULT_URL)))
        {
            mPreferences.put(PREFERENCE_URL, getUrl());
            refresh();
        }
    }

    private void openBrowser()
    {
        try
        {
            Desktop.getDesktop().browse(URI.create(getUrl()));
        }
        catch(Exception e)
        {
            mLog.error("Unable to open the transcription call log in a browser", e);
            mStatusLabel.setText("Couldn't open a browser for " + getUrl());
        }
    }

    /**
     * Fetches the latest calls in the background.  Skipped while the tab isn't on screen.
     */
    private void refresh()
    {
        if(!isShowing())
        {
            return;
        }

        if(mRefreshing)
        {
            mRefreshAgain = true;
            return;
        }

        mRefreshing = true;
        String url = getUrl();

        mClient.fetchCalls(url, mSearchField.getText(), mTalkgroupField.getText(), CALL_LIMIT)
            .whenComplete((calls, error) -> SwingUtilities.invokeLater(() -> {
                mRefreshing = false;

                if(error != null)
                {
                    mStatusLabel.setText("Transcription service isn't reachable at " + url +
                        ". Start it with start-transcriber.bat in the transcriber folder.");
                }
                else
                {
                    showCalls(calls);
                    mStatusLabel.setText(calls.size() + (calls.size() == 1 ? " call" : " calls") +
                        " from " + url + (calls.size() == CALL_LIMIT ? " (newest " + CALL_LIMIT + ")" : ""));
                }

                if(mRefreshAgain)
                {
                    mRefreshAgain = false;
                    refresh();
                }
            }));
    }

    private void showCalls(List<TranscriptCall> calls)
    {
        TranscriptCall selected = getSelectedCall();

        if(!mTableModel.setCalls(calls))
        {
            return;
        }

        if(selected != null)
        {
            int row = mTableModel.indexOf(selected.id());

            if(row >= 0)
            {
                mTable.getSelectionModel().setSelectionInterval(row, row);
            }
        }

        updateDetail();
    }

    private TranscriptCall getSelectedCall()
    {
        int row = mTable.getSelectedRow();
        return row >= 0 ? mTableModel.getCall(row) : null;
    }

    private void updateDetail()
    {
        TranscriptCall call = getSelectedCall();

        if(call == null)
        {
            mDetailArea.setText("");
            return;
        }

        StringBuilder sb = new StringBuilder();
        sb.append(formatTime(call.callTime())).append("   TG ").append(call.talkgroupDisplay());
        sb.append("   From ").append(call.sourceDisplay());

        if(call.durationSeconds() != null)
        {
            sb.append(String.format("   %.1f s", call.durationSeconds()));
        }

        sb.append("\n\n").append(call.transcriptDisplay());

        if(!call.codesDisplay().isEmpty())
        {
            sb.append("\n\n").append(call.codesDisplay());
        }

        mDetailArea.setText(sb.toString());
        mDetailArea.setCaretPosition(0);
    }

    private static String formatTime(long epochSeconds)
    {
        return new SimpleDateFormat("yyyy-MM-dd HH:mm:ss").format(new Date(epochSeconds * 1000));
    }

    /**
     * Table model for the call list, newest first.
     */
    static class TranscriptTableModel extends AbstractTableModel
    {
        static final int TIME = 0;
        static final int TALKGROUP = 1;
        static final int FROM = 2;
        static final int TRANSCRIPT = 3;
        private static final String[] COLUMNS = {"Time", "Talkgroup", "From", "Transcript"};

        private List<TranscriptCall> mCalls = new ArrayList<>();

        /**
         * Replaces the calls.
         * @return true if anything changed
         */
        boolean setCalls(List<TranscriptCall> calls)
        {
            if(mCalls.equals(calls))
            {
                return false;
            }

            mCalls = new ArrayList<>(calls);
            fireTableDataChanged();
            return true;
        }

        TranscriptCall getCall(int row)
        {
            return row >= 0 && row < mCalls.size() ? mCalls.get(row) : null;
        }

        int indexOf(long id)
        {
            for(int i = 0; i < mCalls.size(); i++)
            {
                if(mCalls.get(i).id() == id)
                {
                    return i;
                }
            }

            return -1;
        }

        @Override
        public int getRowCount()
        {
            return mCalls.size();
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
            TranscriptCall call = mCalls.get(row);

            return switch(column)
            {
                case TIME -> formatTime(call.callTime());
                case TALKGROUP -> call.talkgroupDisplay();
                case FROM -> call.sourceDisplay();
                case TRANSCRIPT -> call.codesDisplay().isEmpty() ? call.transcriptDisplay() :
                    call.transcriptDisplay() + "   [" + call.codesDisplay() + "]";
                default -> "";
            };
        }
    }
}
