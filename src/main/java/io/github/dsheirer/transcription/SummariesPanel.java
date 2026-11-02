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

import javax.swing.DefaultListModel;
import javax.swing.JButton;
import javax.swing.JLabel;
import javax.swing.JList;
import javax.swing.JPanel;
import javax.swing.JScrollPane;
import javax.swing.JSplitPane;
import javax.swing.JTextArea;
import javax.swing.ListSelectionModel;
import javax.swing.SwingUtilities;
import javax.swing.Timer;
import java.awt.event.ComponentAdapter;
import java.awt.event.ComponentEvent;
import java.util.List;
import java.util.prefs.Preferences;

/**
 * The transcription service's 12-hour summaries (made at 7 AM and 7 PM): counts by type, the most important
 * incidents and a few sentences from the local AI model.  Pick one on the left to read it on the right.
 */
public class SummariesPanel extends JPanel
{
    private static final int REFRESH_INTERVAL_MS = 60000;
    private static final String PREFERENCE_URL = "transcription.service.url";

    private final Preferences mPreferences = Preferences.userNodeForPackage(TranscriptsPanel.class);
    private final TranscriptionClient mClient = new TranscriptionClient();
    private final DefaultListModel<TranscriptionClient.Recap> mListModel = new DefaultListModel<>();
    private final JList<TranscriptionClient.Recap> mList = new JList<>(mListModel);
    private final JTextArea mTextArea = new JTextArea(10, 50);
    private final JButton mMakeButton = new JButton("Make one now");
    private final JLabel mStatusLabel = new JLabel(" ");
    private final Timer mRefreshTimer = new Timer(REFRESH_INTERVAL_MS, e -> refresh());
    private boolean mRefreshing = false;

    /**
     * Constructs an instance
     */
    public SummariesPanel()
    {
        setLayout(new MigLayout("insets 2 2 2 2", "[grow][]", "[]2[grow,fill]2[]"));
        add(new JLabel("12-hour summaries, made at 7 AM and 7 PM"), "growx");
        mMakeButton.setToolTipText("Make a summary of the last 12 hours now");
        mMakeButton.addActionListener(e -> makeNow());
        add(mMakeButton, "wrap");

        mList.setSelectionMode(ListSelectionModel.SINGLE_SELECTION);
        mList.setCellRenderer((list, recap, index, selected, focused) -> {
            JLabel label = new JLabel(recap.title());
            label.setOpaque(true);
            label.setBackground(selected ? list.getSelectionBackground() : list.getBackground());
            label.setForeground(selected ? list.getSelectionForeground() : list.getForeground());
            return label;
        });
        mList.addListSelectionListener(e -> {
            if(!e.getValueIsAdjusting())
            {
                showSelected();
            }
        });

        mTextArea.setEditable(false);
        mTextArea.setLineWrap(true);
        mTextArea.setWrapStyleWord(true);

        JSplitPane split = new JSplitPane(JSplitPane.HORIZONTAL_SPLIT, new JScrollPane(mList), new JScrollPane(mTextArea));
        split.setResizeWeight(0.25);
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

    private String getUrl()
    {
        return TranscriptionClient.base(mPreferences.get(PREFERENCE_URL, TranscriptionClient.DEFAULT_URL));
    }

    private void showSelected()
    {
        TranscriptionClient.Recap recap = mList.getSelectedValue();
        mTextArea.setText(recap != null ? recap.text() : "");
        mTextArea.setCaretPosition(0);
    }

    /**
     * Fetches the summaries in the background.  Skipped while the tab isn't on screen.
     */
    private void refresh()
    {
        if(!isShowing() || mRefreshing)
        {
            return;
        }

        mRefreshing = true;
        String url = getUrl();

        mClient.fetchRecaps(url).whenComplete((recaps, error) -> SwingUtilities.invokeLater(() -> {
            mRefreshing = false;

            if(error != null)
            {
                mStatusLabel.setText("12-hour summaries aren't reachable at " + url +
                    ". Start the transcriber with start-transcriber.bat.");
                return;
            }

            show(recaps);
        }));
    }

    private void show(List<TranscriptionClient.Recap> recaps)
    {
        TranscriptionClient.Recap selected = mList.getSelectedValue();
        List<TranscriptionClient.Recap> current = java.util.Collections.list(mListModel.elements());

        if(!current.equals(recaps))
        {
            mListModel.clear();
            recaps.forEach(mListModel::addElement);
            int keep = selected == null ? -1 : indexOf(recaps, selected.id());
            mList.setSelectedIndex(keep >= 0 ? keep : recaps.isEmpty() ? -1 : 0);
        }

        mStatusLabel.setText(recaps.isEmpty() ? "No summaries yet. The first one comes at the next 7 AM or 7 PM, " +
            "or press Make one now." : recaps.size() + " summaries · from " + getUrl() + "/map");
    }

    private static int indexOf(List<TranscriptionClient.Recap> recaps, long id)
    {
        for(int i = 0; i < recaps.size(); i++)
        {
            if(recaps.get(i).id() == id)
            {
                return i;
            }
        }

        return -1;
    }

    private void makeNow()
    {
        mMakeButton.setEnabled(false);
        mStatusLabel.setText("Making a summary of the last 12 hours...");
        mClient.makeRecap(getUrl()).whenComplete((done, error) -> SwingUtilities.invokeLater(() -> {
            mMakeButton.setEnabled(true);

            if(error != null)
            {
                mStatusLabel.setText("Couldn't make a summary: " + error.getMessage());
                return;
            }

            mList.clearSelection();
            refresh();
        }));
    }
}
