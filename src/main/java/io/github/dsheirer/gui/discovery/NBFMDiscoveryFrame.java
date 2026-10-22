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

import io.github.dsheirer.controller.channel.ChannelModel;
import io.github.dsheirer.controller.channel.ChannelProcessingManager;
import io.github.dsheirer.module.decode.nbfm.discovery.DiscoveryChannelManager;
import io.github.dsheirer.module.decode.nbfm.discovery.DiscoveryConfiguration;
import io.github.dsheirer.module.decode.nbfm.discovery.DiscoveryEvent;
import io.github.dsheirer.module.decode.nbfm.discovery.NBFMDiscoveryFileAnalyzer;
import io.github.dsheirer.module.decode.nbfm.discovery.NBFMDiscoveryProcessor;
import io.github.dsheirer.module.decode.nbfm.discovery.TunerDiscoveryChannelHost;
import io.github.dsheirer.sample.Listener;
import io.github.dsheirer.source.SourceException;
import io.github.dsheirer.source.tuner.Tuner;
import io.github.dsheirer.source.tuner.TunerController;
import java.awt.Component;
import java.awt.EventQueue;
import java.awt.Font;
import java.awt.event.WindowAdapter;
import java.awt.event.WindowEvent;
import java.io.File;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import javax.swing.JButton;
import javax.swing.JCheckBox;
import javax.swing.JComboBox;
import javax.swing.JFileChooser;
import javax.swing.JFrame;
import javax.swing.JLabel;
import javax.swing.JOptionPane;
import javax.swing.JPanel;
import javax.swing.JScrollPane;
import javax.swing.JSpinner;
import javax.swing.JTable;
import javax.swing.JTextField;
import javax.swing.ListSelectionModel;
import javax.swing.SpinnerNumberModel;
import javax.swing.Timer;
import javax.swing.filechooser.FileNameExtensionFilter;
import javax.swing.table.DefaultTableCellRenderer;
import net.miginfocom.swing.MigLayout;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Window that runs NBFM carrier discovery on a tuner, or on a baseband recording, and lists every frequency heard.
 *
 * Opened from the spectral display's context menu.  One window per tuner; discovery stops when the window is closed.
 */
public class NBFMDiscoveryFrame extends JFrame
{
    private static final Logger mLog = LoggerFactory.getLogger(NBFMDiscoveryFrame.class);
    private static final Map<Tuner,NBFMDiscoveryFrame> FRAMES = new ConcurrentHashMap<>();

    private final Tuner mTuner;
    private final DiscoveredFrequencyTableModel mTableModel = new DiscoveredFrequencyTableModel();
    private final Set<Long> mIgnored = new HashSet<>();
    private final JTable mTable = new JTable(mTableModel);
    private final JTextField mCenterField = new JTextField(10);
    private final JButton mTuneButton = new JButton("Tune");
    private final JSpinner mThresholdSpinner = new JSpinner(new SpinnerNumberModel(10.0, 6.0, 30.0, 1.0));
    private final JButton mStartButton = new JButton("Start");
    private final JButton mStopButton = new JButton("Stop");
    private final JButton mAnalyzeButton = new JButton("Analyze Recording...");
    private final JLabel mStatusLabel = new JLabel();
    private final JCheckBox mAutoChannelsCheckBox = new JCheckBox("Open a channel for each frequency heard");
    private final JSpinner mMaximumChannelsSpinner = new JSpinner(new SpinnerNumberModel(8, 1, 32, 1));
    private final JSpinner mHangTimeSpinner = new JSpinner(new SpinnerNumberModel(5, 0, 60, 1));
    private final JComboBox<DiscoveryConfiguration.ChannelBandwidth> mBandwidthComboBox =
            new JComboBox<>(DiscoveryConfiguration.ChannelBandwidth.values());
    private final DiscoveryConfiguration mChannelSettings = new DiscoveryConfiguration();
    private final ChannelProcessingManager mChannelProcessingManager;
    private DiscoveryChannelManager mChannelManager;
    private final Timer mStatusTimer;
    private NBFMDiscoveryProcessor mLiveProcessor;
    private Thread mAnalysisThread;
    private File mLastDirectory;

    /**
     * Shows the discovery window for the tuner, creating it if needed.
     * @param tuner to run discovery on
     * @param channelModel playlist channels, used to flag frequencies that are already configured
     * @param channelProcessingManager to run temporary channels for discovered frequencies
     */
    public static void show(Tuner tuner, ChannelModel channelModel, ChannelProcessingManager channelProcessingManager)
    {
        EventQueue.invokeLater(() -> {
            NBFMDiscoveryFrame frame = FRAMES.computeIfAbsent(tuner, t -> new NBFMDiscoveryFrame(t, channelModel,
                    channelProcessingManager));
            frame.setVisible(true);
            frame.toFront();
        });
    }

    private NBFMDiscoveryFrame(Tuner tuner, ChannelModel channelModel,
                               ChannelProcessingManager channelProcessingManager)
    {
        mTuner = tuner;
        mChannelProcessingManager = channelProcessingManager;
        setTitle("NBFM Discovery - " + tuner.getPreferredName());
        setDefaultCloseOperation(DISPOSE_ON_CLOSE);

        mTableModel.setPlaylistLookup(frequency -> channelModel != null &&
                !channelModel.getChannelsInFrequencyRange(frequency - 1000, frequency + 1000).isEmpty());

        mCenterField.setToolTipText("Tuner centre frequency in MHz.  Discovery watches about 90% of the tuner's " +
                "sample rate around this frequency.");
        mTuneButton.setToolTipText("Retune the tuner.  Only available when no channels are using it.");
        mTuneButton.addActionListener(e -> tune());
        mThresholdSpinner.setToolTipText("How far above the noise floor a signal must be.  Lower finds weaker " +
                "signals but more false hits.");
        mStartButton.addActionListener(e -> startLive());

        //Channel settings apply immediately, including while discovery is running
        mAutoChannelsCheckBox.setToolTipText("Start a temporary NBFM channel for each frequency as it keys up, so " +
                "you can hear and record it.  Temporary channels aren't added to the playlist.");
        mAutoChannelsCheckBox.addActionListener(e -> {
            mChannelSettings.setAutoChannels(mAutoChannelsCheckBox.isSelected());
            updateControls();
        });
        mMaximumChannelsSpinner.setToolTipText("Most temporary channels open at once");
        mMaximumChannelsSpinner.addChangeListener(e ->
                mChannelSettings.setMaximumChannels((Integer)mMaximumChannelsSpinner.getValue()));
        mHangTimeSpinner.setToolTipText("Seconds a channel stays open after the frequency goes quiet, so replies " +
                "are heard on the same channel");
        mHangTimeSpinner.addChangeListener(e ->
                mChannelSettings.setChannelHangTimeMs((Integer)mHangTimeSpinner.getValue() * 1000));
        mBandwidthComboBox.setToolTipText("Auto picks 25 kHz for signals measured wider than 7 kHz, otherwise " +
                "12.5 kHz (most VHF public safety)");
        mBandwidthComboBox.addActionListener(e -> mChannelSettings.setChannelBandwidth(
                (DiscoveryConfiguration.ChannelBandwidth)mBandwidthComboBox.getSelectedItem()));
        mStopButton.addActionListener(e -> stopLive());
        mAnalyzeButton.setToolTipText("Run discovery on a baseband recording made from the Tuners tab");
        mAnalyzeButton.addActionListener(e -> {
            if(mAnalysisThread != null)
            {
                mAnalysisThread.interrupt();
            }
            else
            {
                analyzeRecording();
            }
        });

        mTable.setSelectionMode(ListSelectionModel.MULTIPLE_INTERVAL_SELECTION);
        mTable.setAutoCreateRowSorter(true);
        mTable.setDefaultRenderer(String.class, new StatusRenderer());
        int[] widths = {120, 100, 80, 95, 80, 95, 80, 75, 130};

        for(int x = 0; x < widths.length; x++)
        {
            mTable.getColumnModel().getColumn(x).setPreferredWidth(widths[x]);
        }

        JButton ignoreButton = new JButton("Ignore Selected");
        ignoreButton.setToolTipText("Stop reporting the selected frequencies (for example, interference or data)");
        ignoreButton.addActionListener(e -> ignoreSelected());
        JButton clearButton = new JButton("Clear List");
        clearButton.addActionListener(e -> mTableModel.clear());

        JPanel panel = new JPanel(new MigLayout("fill, insets 8", "[][][][grow]", "[][][][][grow,fill][]"));
        panel.add(new JLabel("Centre (MHz):"));
        panel.add(mCenterField, "w 110!");
        panel.add(mTuneButton, "wrap");
        panel.add(new JLabel("Threshold (dB):"));
        panel.add(mThresholdSpinner, "w 110!");
        panel.add(mStartButton, "split 2, sg run");
        panel.add(mStopButton, "sg run");
        panel.add(mAnalyzeButton, "right, wrap");
        panel.add(new JLabel("Channels:"));
        panel.add(mAutoChannelsCheckBox, "span, split 7");
        panel.add(new JLabel("Max:"), "gapleft 16");
        panel.add(mMaximumChannelsSpinner, "w 60!");
        panel.add(new JLabel("Hang (s):"), "gapleft 8");
        panel.add(mHangTimeSpinner, "w 60!");
        panel.add(new JLabel("Bandwidth:"), "gapleft 8");
        panel.add(mBandwidthComboBox, "wrap");
        panel.add(mStatusLabel, "span, wrap");
        panel.add(new JScrollPane(mTable), "span, grow, wrap");
        panel.add(new JLabel("* off the standard channel spacing"), "span 2");
        panel.add(ignoreButton, "span, split 2, right");
        panel.add(clearButton);
        setContentPane(panel);
        setSize(980, 580);
        setLocationRelativeTo(null);

        mStatusTimer = new Timer(1000, e -> updateControls());
        mStatusTimer.start();
        mCenterField.setText(String.format("%.4f", tuner.getTunerController().getFrequency() / 1e6));
        updateControls();

        addWindowListener(new WindowAdapter()
        {
            @Override
            public void windowClosed(WindowEvent e)
            {
                stopLive();

                if(mAnalysisThread != null)
                {
                    mAnalysisThread.interrupt();
                }

                mStatusTimer.stop();
                FRAMES.remove(mTuner);
            }
        });
    }

    private DiscoveryConfiguration createConfiguration()
    {
        DiscoveryConfiguration configuration = new DiscoveryConfiguration();
        configuration.setDetectionThresholdDb((Double)mThresholdSpinner.getValue());
        return configuration;
    }

    private NBFMDiscoveryProcessor createProcessor()
    {
        NBFMDiscoveryProcessor processor = new NBFMDiscoveryProcessor(createConfiguration());

        for(long frequency : mIgnored)
        {
            processor.getTracker().ignore(frequency);
        }

        processor.setListener(createTableListener());
        return processor;
    }

    /**
     * Listener that copies each event on the sample thread, then applies it to the table on the Swing thread.
     */
    private Listener<DiscoveryEvent> createTableListener()
    {
        return event -> {
            DiscoveredFrequencyTableModel.Snapshot snapshot = DiscoveredFrequencyTableModel.Snapshot.of(event);
            EventQueue.invokeLater(() -> mTableModel.update(snapshot));
        };
    }

    private void startLive()
    {
        if(mLiveProcessor == null && mAnalysisThread == null)
        {
            mTableModel.setRecordingTime(false);
            mLiveProcessor = createProcessor();

            if(mChannelProcessingManager != null)
            {
                mChannelManager = new DiscoveryChannelManager(mChannelSettings,
                        new TunerDiscoveryChannelHost(mTuner, mChannelProcessingManager), System::currentTimeMillis);
                mChannelManager.setStatusListener(status -> EventQueue.invokeLater(() ->
                        mTableModel.update(status)));
                mChannelManager.start();
                Listener<DiscoveryEvent> tableListener = createTableListener();
                DiscoveryChannelManager channelManager = mChannelManager;
                mLiveProcessor.setListener(event -> {
                    tableListener.receive(event);
                    channelManager.receive(event);
                });
            }

            mLiveProcessor.attach(mTuner.getTunerController());
            mLog.info("NBFM discovery started on tuner {}", mTuner.getPreferredName());
        }

        updateControls();
    }

    private void stopLive()
    {
        if(mLiveProcessor != null)
        {
            mLiveProcessor.detach();
            mLiveProcessor = null;

            if(mChannelManager != null)
            {
                mChannelManager.stop();
                mChannelManager = null;
            }

            mTableModel.setAllIdle();
            mLog.info("NBFM discovery stopped on tuner {}", mTuner.getPreferredName());
        }

        updateControls();
    }

    private void tune()
    {
        TunerController controller = mTuner.getTunerController();

        try
        {
            long frequency = Math.round(Double.parseDouble(mCenterField.getText().trim()) * 1e6);

            if(!controller.canTune(frequency))
            {
                JOptionPane.showMessageDialog(this, "This tuner can't tune to " + mCenterField.getText() + " MHz",
                        "NBFM Discovery", JOptionPane.WARNING_MESSAGE);
                return;
            }

            controller.setFrequency(frequency);
        }
        catch(NumberFormatException nfe)
        {
            JOptionPane.showMessageDialog(this, "Enter the centre frequency in MHz, for example 155.4",
                    "NBFM Discovery", JOptionPane.WARNING_MESSAGE);
        }
        catch(SourceException se)
        {
            mLog.error("Error tuning for NBFM discovery", se);
            JOptionPane.showMessageDialog(this, "Couldn't tune: " + se.getMessage(), "NBFM Discovery",
                    JOptionPane.ERROR_MESSAGE);
        }

        updateControls();
    }

    private void analyzeRecording()
    {
        JFileChooser chooser = new JFileChooser(mLastDirectory);
        chooser.setDialogTitle("Choose a baseband recording");
        chooser.setFileFilter(new FileNameExtensionFilter("Baseband recordings (*.wav)", "wav"));

        if(chooser.showOpenDialog(this) != JFileChooser.APPROVE_OPTION)
        {
            return;
        }

        File file = chooser.getSelectedFile();
        mLastDirectory = file.getParentFile();
        Long detected = NBFMDiscoveryFileAnalyzer.getCenterFrequency(file.getName());
        String answer = (String)JOptionPane.showInputDialog(this, "Tuner centre frequency of the recording (MHz):",
                "NBFM Discovery", JOptionPane.QUESTION_MESSAGE, null, null,
                detected != null ? String.format("%.4f", detected / 1e6) : "");

        if(answer == null)
        {
            return;
        }

        long center;

        try
        {
            center = Math.round(Double.parseDouble(answer.trim()) * 1e6);
        }
        catch(NumberFormatException nfe)
        {
            JOptionPane.showMessageDialog(this, "Enter the centre frequency in MHz, for example 155.4",
                    "NBFM Discovery", JOptionPane.WARNING_MESSAGE);
            return;
        }

        mTableModel.clear();
        mTableModel.setRecordingTime(true);
        NBFMDiscoveryProcessor processor = createProcessor();

        mAnalysisThread = new Thread(() -> {
            String result;

            try
            {
                NBFMDiscoveryFileAnalyzer.analyze(file, center, processor, createTableListener());
                result = Thread.currentThread().isInterrupted() ? "Analysis cancelled: " + file.getName() :
                        "Finished " + file.getName() + ": " + processor.getTracker().getHistory().size() +
                                " frequencies heard";
            }
            catch(Exception e)
            {
                mLog.error("Error analyzing recording " + file, e);
                result = "Couldn't analyze " + file.getName() + ": " + e.getMessage();
            }

            String status = result;
            EventQueue.invokeLater(() -> {
                mAnalysisThread = null;
                updateControls();
                mStatusLabel.setText(status);
            });
        }, "sdrtrunk nbfm discovery analyzer");

        mAnalysisThread.setDaemon(true);
        mAnalysisThread.start();
        updateControls();
        mStatusLabel.setText("Analyzing " + file.getName() + "...");
    }

    private void ignoreSelected()
    {
        int[] rows = mTable.getSelectedRows();
        long[] frequencies = new long[rows.length];

        for(int x = 0; x < rows.length; x++)
        {
            frequencies[x] = mTableModel.getFrequency(mTable.convertRowIndexToModel(rows[x]));
        }

        for(long frequency : frequencies)
        {
            mIgnored.add(frequency);
            mTableModel.remove(frequency);

            if(mLiveProcessor != null)
            {
                mLiveProcessor.getTracker().ignore(frequency);
            }

            if(mChannelManager != null)
            {
                mChannelManager.close(frequency);
            }
        }
    }

    /**
     * Enables controls for the current state and refreshes the status line.
     */
    private void updateControls()
    {
        TunerController controller = mTuner.getTunerController();
        boolean live = mLiveProcessor != null;
        boolean analyzing = mAnalysisThread != null;
        boolean tunerInUse = mTuner.getChannelSourceManager().getTunerChannelCount() > 0;

        mStartButton.setEnabled(!live && !analyzing);
        mStopButton.setEnabled(live);
        mThresholdSpinner.setEnabled(!live && !analyzing);
        mTuneButton.setEnabled(!tunerInUse);
        mCenterField.setEditable(!tunerInUse);
        mAnalyzeButton.setText(analyzing ? "Cancel Analysis" : "Analyze Recording...");
        mAnalyzeButton.setEnabled(!live);
        boolean channels = mChannelProcessingManager != null;
        mAutoChannelsCheckBox.setEnabled(channels);
        mMaximumChannelsSpinner.setEnabled(channels && mAutoChannelsCheckBox.isSelected());
        mHangTimeSpinner.setEnabled(channels && mAutoChannelsCheckBox.isSelected());
        mBandwidthComboBox.setEnabled(channels && mAutoChannelsCheckBox.isSelected());

        if(!mCenterField.isFocusOwner() && !mCenterField.isEditable())
        {
            mCenterField.setText(String.format("%.4f", controller.getFrequency() / 1e6));
        }

        if(live)
        {
            double half = controller.getSampleRate() * 0.45;
            int open = mChannelManager != null ? mChannelManager.getRunningCount() : 0;
            mStatusLabel.setText(String.format("Watching %.3f to %.3f MHz on %s%s%s", (controller.getFrequency() - half)
                    / 1e6, (controller.getFrequency() + half) / 1e6, mTuner.getPreferredName(),
                    tunerInUse ? " (centre is set by the channels using this tuner)" : "",
                    mAutoChannelsCheckBox.isSelected() ? ", " + open + " channel" + (open == 1 ? "" : "s") + " open" :
                            ""));
        }
        else if(!analyzing && !mStatusLabel.getText().startsWith("Finished") &&
                !mStatusLabel.getText().startsWith("Analysis") && !mStatusLabel.getText().startsWith("Couldn't"))
        {
            mStatusLabel.setText("Stopped.  Set the centre frequency and press Start, or analyze a recording.");
        }
    }

    /**
     * Shows carriers that are on the air in bold.
     */
    private static class StatusRenderer extends DefaultTableCellRenderer
    {
        @Override
        public Component getTableCellRendererComponent(JTable table, Object value, boolean isSelected,
                                                       boolean hasFocus, int row, int column)
        {
            Component component = super.getTableCellRendererComponent(table, value, isSelected, hasFocus, row, column);
            boolean onAir = "ON AIR".equals(table.getModel().getValueAt(table.convertRowIndexToModel(row),
                    DiscoveredFrequencyTableModel.COLUMN_STATUS));
            component.setFont(onAir ? component.getFont().deriveFont(Font.BOLD) : table.getFont());
            return component;
        }
    }
}
