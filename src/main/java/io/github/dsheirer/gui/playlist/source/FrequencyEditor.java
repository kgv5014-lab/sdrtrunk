/*
 * *****************************************************************************
 * Copyright (C) 2014-2022 Dennis Sheirer
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

package io.github.dsheirer.gui.playlist.source;

import io.github.dsheirer.source.config.ScanEntry;
import io.github.dsheirer.source.config.SourceConfigTuner;
import io.github.dsheirer.source.config.SourceConfigTunerMultipleFrequency;
import io.github.dsheirer.source.config.SourceConfiguration;
import io.github.dsheirer.source.tuner.channel.rotation.ChannelRotationMonitor;
import io.github.dsheirer.source.tuner.manager.TunerManager;
import io.github.dsheirer.source.tuner.manager.TunerUnavailableReason;
import javafx.beans.binding.Bindings;
import javafx.beans.value.ChangeListener;
import javafx.beans.value.ObservableValue;
import javafx.collections.FXCollections;
import javafx.collections.ObservableList;
import javafx.geometry.Insets;
import javafx.geometry.Pos;
import javafx.scene.Node;
import javafx.scene.control.Alert;
import javafx.scene.control.Button;
import javafx.scene.control.ButtonType;
import javafx.scene.control.CheckBox;
import javafx.scene.control.ComboBox;
import javafx.scene.control.Label;
import javafx.scene.control.Spinner;
import javafx.scene.control.SpinnerValueFactory;
import javafx.scene.control.TextField;
import javafx.scene.control.Tooltip;
import javafx.scene.layout.FlowPane;
import javafx.scene.layout.HBox;
import javafx.scene.layout.VBox;
import javafx.scene.paint.Color;
import jiconfont.icons.font_awesome.FontAwesome;
import jiconfont.javafx.IconNode;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * Frequency editor that supports editing single or multiple frequencies.
 */
public class FrequencyEditor extends SourceConfigurationEditor<SourceConfiguration>
{
    private final static Logger mLog = LoggerFactory.getLogger(FrequencyEditor.class);
    private static final String NONE = "(none)           ";
    private TunerManager mTunerManager;
    private FrequencyBox mPrimaryFrequencyBox;
    private ObservableList<FrequencyBox> mFrequencyBoxes = FXCollections.observableArrayList();
    private ComboBox<String> mPreferredTunerComboBox;
    private Label mCoverageLabel;
    private VBox mFrequencyBoxContainer;
    private Spinner<Integer> mChannelRotationDelaySpinner;
    private boolean mAllowMultipleFrequencies = false;
    private int mFrequencyRotationDefault = ChannelRotationMonitor.CHANNEL_ROTATION_DELAY_DEFAULT;
    private int mFrequencyRotationMinimum = ChannelRotationMonitor.CHANNEL_ROTATION_DELAY_MINIMUM;
    private int mFrequencyRotationMaximum = ChannelRotationMonitor.CHANNEL_ROTATION_DELAY_MAXIMUM;
    private boolean mScanList = false;
    private Spinner<Integer> mScanHangTimeSpinner;
    private CheckBox mScanParallelCheckBox;

    /**
     * Constructs an instance with support for multiple frequency values.  The min and max rotation delay values
     * define constraints for the channel rotation delay monitor, how long the monitor dwells on each frequency in a
     * multi-frequency configuration.
     *
     * @param tunerManager for accessing list of tuners to select a preferred tuner
     * @param minRotationDelay in milliseconds for channel/frequency dwell
     * @param maxRotationDelay in milliseconds for channel/frequency dwell
     */
    public FrequencyEditor(TunerManager tunerManager, int minRotationDelay, int maxRotationDelay, int defaultRotationDelay)
    {
        mTunerManager = tunerManager;
        mAllowMultipleFrequencies = true;
        mFrequencyRotationMinimum = minRotationDelay;
        mFrequencyRotationMaximum = maxRotationDelay;
        mFrequencyRotationDefault = defaultRotationDelay;
        init();
    }

    /**
     * Constructs an instance for a conventional (NBFM/AM) channel.  When scan list is true, the editor supports
     * multiple frequencies with a per-frequency label, priority and lockout, plus dwell time, hang time and parallel
     * monitoring settings.  A channel with more than one frequency is scanned.
     *
     * @param tunerManager for accessing list of tuners to select a preferred tuner
     * @param scanList true to enable scan list editing
     */
    public FrequencyEditor(TunerManager tunerManager, boolean scanList)
    {
        mTunerManager = tunerManager;
        mAllowMultipleFrequencies = scanList;
        mScanList = scanList;
        mFrequencyRotationDefault = ChannelRotationMonitor.CHANNEL_ROTATION_DELAY_MINIMUM;
        init();
    }

    /**
     * Constructs an instance with a default behavior of single frequency support only.
     * @param tunerManager for accessing list of tuners to select a preferred tuner
     */
    public FrequencyEditor(TunerManager tunerManager)
    {
        mTunerManager = tunerManager;
        init();
    }

    private void init()
    {
        //Wraps the tuner settings below the frequencies when the editor is narrow
        FlowPane hBox = new FlowPane();
        hBox.setPadding(new Insets(10,10,10,10));
        hBox.setHgap(10);
        hBox.setVgap(10);
        hBox.setAlignment(Pos.CENTER_LEFT);

        Label label = new Label(mAllowMultipleFrequencies ? "Frequencies (MHz)" : "Frequency (MHz)");
        label.setAlignment(Pos.BASELINE_RIGHT);
        hBox.getChildren().addAll(label, getFrequencyBoxContainer());

        Label preferredTunerLabel = new Label("Preferred Tuner");
        HBox tunerBox = new HBox();
        tunerBox.setAlignment(Pos.CENTER_LEFT);
        tunerBox.setSpacing(10);
        tunerBox.getChildren().addAll(preferredTunerLabel, getPreferredTunerComboBox());

        VBox vbox = new VBox();
        vbox.setSpacing(10);
        vbox.setPadding(new Insets(0,0,0,20));

        if(mScanList)
        {
            Label dwellLabel = new Label("Scan Dwell Time (ms)");
            HBox dwellBox = new HBox();
            dwellBox.setAlignment(Pos.CENTER_LEFT);
            dwellBox.setSpacing(10);
            dwellBox.getChildren().addAll(dwellLabel, getFrequencyRotationDelaySpinner());
            getFrequencyRotationDelaySpinner().setTooltip(new Tooltip("How long to listen to each quiet frequency " +
                "before moving to the next one"));
            getFrequencyRotationDelaySpinner().disableProperty().bind(Bindings.greaterThan(2, Bindings.size(mFrequencyBoxes)));

            Label hangLabel = new Label("Scan Hang Time (ms)");
            HBox hangBox = new HBox();
            hangBox.setAlignment(Pos.CENTER_LEFT);
            hangBox.setSpacing(10);
            hangBox.getChildren().addAll(hangLabel, getScanHangTimeSpinner());
            getScanHangTimeSpinner().disableProperty().bind(Bindings.greaterThan(2, Bindings.size(mFrequencyBoxes)));
            getScanParallelCheckBox().disableProperty().bind(Bindings.greaterThan(2, Bindings.size(mFrequencyBoxes)));

            Label scanHelp = new Label("Add more than one frequency to scan them.");
            vbox.getChildren().addAll(tunerBox, dwellBox, hangBox, getScanParallelCheckBox(), scanHelp);
        }
        else if(mAllowMultipleFrequencies)
        {
            Label frequencyRotationLabel = new Label("Frequency Rotation Delay (ms)");
            HBox frequencyBox = new HBox();
            frequencyBox.setAlignment(Pos.CENTER_LEFT);
            frequencyBox.setSpacing(10);
            frequencyBox.getChildren().addAll(frequencyRotationLabel, getFrequencyRotationDelaySpinner());
            getFrequencyRotationDelaySpinner().disableProperty().bind(Bindings.greaterThan(2, Bindings.size(mFrequencyBoxes)));

            vbox.getChildren().addAll(tunerBox, frequencyBox);
        }
        else
        {
            vbox.getChildren().addAll(tunerBox);
        }

        vbox.getChildren().add(getCoverageLabel());

        hBox.getChildren().add(vbox);

        setAlignment(Pos.TOP_LEFT);
        getChildren().add(hBox);
    }

    /**
     * Sets the disabled status for each of the frequency boxes and the preferred tuner combo box.
     * @param disable true to disable each of the controls or false to enable.
     */
    @Override
    public void disable(boolean disable)
    {
        for(FrequencyBox frequencyBox: mFrequencyBoxes)
        {
            frequencyBox.disable(disable);
        }

        getPreferredTunerComboBox().setDisable(disable);
    }

    /**
     * Saves the current state of the editor to the source configuration which can then be accessed by invoking
     * the getSourceConfiguration() method.
     *
     * Note: the source configuration will be a single frequency config if the editor has only a single frequency or
     * if the single frequency is not specified.  It will create/use a multiple frequency configuration if the editor
     * has more than one frequency listed.
     */
    @Override
    public void save()
    {
        String preferredTuner = getPreferredTunerComboBox().getSelectionModel().getSelectedItem();

        //Remove the preferred tuner value if it contains the default 'none' value
        if(preferredTuner != null && preferredTuner.contentEquals(NONE))
        {
            preferredTuner = null;
        }

        List<Long> frequencies = getFrequencies();

        if(frequencies.size() <= 1)
        {
            SourceConfigTuner sourceConfigTuner = null;

            if(getSourceConfiguration() instanceof SourceConfigTuner)
            {
                sourceConfigTuner = (SourceConfigTuner)getSourceConfiguration();
            }
            else
            {
                sourceConfigTuner = new SourceConfigTuner();
            }

            if(frequencies.size() == 1)
            {
                sourceConfigTuner.setFrequency(frequencies.get(0));
            }
            else
            {
                sourceConfigTuner.setFrequency(0);
            }

            sourceConfigTuner.setPreferredTuner(preferredTuner);
            setSourceConfiguration(sourceConfigTuner);
        }
        else
        {
            SourceConfigTunerMultipleFrequency sourceConfigMulti = null;

            if(getSourceConfiguration() instanceof SourceConfigTunerMultipleFrequency)
            {
                sourceConfigMulti = (SourceConfigTunerMultipleFrequency)getSourceConfiguration();
            }
            else
            {
                sourceConfigMulti = new SourceConfigTunerMultipleFrequency();
            }

            sourceConfigMulti.setFrequencies(frequencies);
            sourceConfigMulti.setPreferredTuner(preferredTuner);
            sourceConfigMulti.setFrequencyRotationDelay(getFrequencyRotationDelaySpinner().getValue());

            if(mScanList)
            {
                sourceConfigMulti.setScanEntries(getScanEntries());
                sourceConfigMulti.setScanHangTime(getScanHangTimeSpinner().getValue());
                sourceConfigMulti.setScanParallel(getScanParallelCheckBox().isSelected());
            }

            setSourceConfiguration(sourceConfigMulti);
        }
    }

    /**
     * Loads the source configuration into the editor.
     * @param sourceConfiguration to load
     */
    @Override
    public void setSourceConfiguration(SourceConfiguration sourceConfiguration)
    {
        super.setSourceConfiguration(sourceConfiguration);

        String preferredTuner = null;

        disable(sourceConfiguration == null);

        getFrequencyRotationDelaySpinner().getValueFactory()
            .setValue(ChannelRotationMonitor.CHANNEL_ROTATION_DELAY_MINIMUM);

        if(mScanList)
        {
            getScanHangTimeSpinner().getValueFactory().setValue(ChannelRotationMonitor.SCAN_HANG_TIME_DEFAULT);
            getScanParallelCheckBox().setSelected(true);
        }

        if(sourceConfiguration == null)
        {
            setFrequencies(Collections.emptyList());
            getPreferredTunerComboBox().setDisable(true);
            getPreferredTunerComboBox().getSelectionModel().select(null);
        }
        else if(sourceConfiguration instanceof SourceConfigTuner)
        {
            SourceConfigTuner sourceConfigTuner = (SourceConfigTuner)sourceConfiguration;
            setFrequency(sourceConfigTuner.getFrequency());
            preferredTuner = sourceConfigTuner.getPreferredTuner();
        }
        else if(sourceConfiguration instanceof SourceConfigTunerMultipleFrequency)
        {
            SourceConfigTunerMultipleFrequency sourceMulti = (SourceConfigTunerMultipleFrequency)sourceConfiguration;
            setFrequencies(sourceMulti.getFrequencies());
            preferredTuner = sourceMulti.getPreferredTuner();

            if(mScanList)
            {
                setScanEntries(sourceMulti);
                getScanHangTimeSpinner().getValueFactory().setValue(sourceMulti.getScanHangTime());
                getScanParallelCheckBox().setSelected(sourceMulti.isScanParallel());
            }

            int rotationDelay = sourceMulti.getFrequencyRotationDelay();

            if(rotationDelay < mFrequencyRotationMinimum)
            {
                rotationDelay = mFrequencyRotationMinimum;

                //For backward compatibility, we'll automatically update the value in the source config
                sourceMulti.setFrequencyRotationDelay(rotationDelay);
            }
            if(rotationDelay > mFrequencyRotationMaximum)
            {
                rotationDelay = mFrequencyRotationMaximum;

                //For backward compatibility, we'll automatically update the value in the source config
                sourceMulti.setFrequencyRotationDelay(rotationDelay);
            }

            getFrequencyRotationDelaySpinner().getValueFactory().setValue(rotationDelay);
        }
        else
        {
            setFrequencies(Collections.emptyList());
            getPreferredTunerComboBox().setDisable(false);
            getPreferredTunerComboBox().getSelectionModel().select(null);
        }

        updatePreferredTuners();

        if(preferredTuner != null)
        {
            if(!getPreferredTunerComboBox().getItems().contains(preferredTuner))
            {
                getPreferredTunerComboBox().getItems().add(preferredTuner);
            }

            getPreferredTunerComboBox().getSelectionModel().select(preferredTuner);
        }
        else
        {
            getPreferredTunerComboBox().getSelectionModel().select(NONE);
        }

        updateCoverage();
        modifiedProperty().set(false);
    }

    /**
     * Label showing which tuners can receive the channel's frequency(ies)
     */
    private Label getCoverageLabel()
    {
        if(mCoverageLabel == null)
        {
            mCoverageLabel = new Label();
            mCoverageLabel.setWrapText(true);
            mCoverageLabel.setMaxWidth(600);
        }

        return mCoverageLabel;
    }

    /**
     * Updates the tuner coverage label for the frequencies currently entered in the editor.
     */
    private void updateCoverage()
    {
        List<TunerUnavailableReason.TunerSnapshot> tuners = mTunerManager.getTunerSnapshots();
        List<String> lines = new ArrayList<>();
        TunerUnavailableReason.Coverage.Level worst = null;

        for(FrequencyBox frequencyBox: mFrequencyBoxes)
        {
            long frequency = frequencyBox.getFrequencyField().get();

            if(frequency > 0)
            {
                TunerUnavailableReason.Coverage coverage = TunerUnavailableReason.coverage(frequency, tuners);
                lines.add((mFrequencyBoxes.size() > 1 ? frequencyBox.getFrequencyField().getText() + ": " : "") +
                    coverage.description());

                if(worst == null || coverage.level().ordinal() > worst.ordinal())
                {
                    worst = coverage.level();
                }
            }
        }

        if(worst == null)
        {
            getCoverageLabel().setText(null);
            getCoverageLabel().setGraphic(null);
            return;
        }

        IconNode icon = switch(worst)
        {
            case NOW -> new IconNode(FontAwesome.CHECK_CIRCLE);
            case WHEN_FREE -> new IconNode(FontAwesome.EXCLAMATION_TRIANGLE);
            case NONE -> new IconNode(FontAwesome.TIMES_CIRCLE);
        };
        icon.setFill(switch(worst)
        {
            case NOW -> Color.GREEN;
            case WHEN_FREE -> Color.ORANGE;
            case NONE -> Color.RED;
        });
        getCoverageLabel().setGraphic(icon);
        getCoverageLabel().setText("Tuners: " + String.join("\n", lines));
    }

    /**
     * Channel rotation monitor delay value.  This dictates how long the decoder will remain on each frequency before
     * rotating to the next frequency in the list
     * @return spinner
     */
    private Spinner<Integer> getFrequencyRotationDelaySpinner()
    {
        if(mChannelRotationDelaySpinner == null)
        {
            mChannelRotationDelaySpinner = new Spinner();
            mChannelRotationDelaySpinner.setTooltip(
                new Tooltip("Delay on each frequency before rotating to next when seeking to next active channel frequency"));
            mChannelRotationDelaySpinner.getStyleClass().add(Spinner.STYLE_CLASS_SPLIT_ARROWS_HORIZONTAL);
            SpinnerValueFactory<Integer> svf = new SpinnerValueFactory.IntegerSpinnerValueFactory(mFrequencyRotationMinimum,
                mFrequencyRotationMaximum, mFrequencyRotationMinimum, 50);
            mChannelRotationDelaySpinner.setValueFactory(svf);
            mChannelRotationDelaySpinner.getValueFactory().valueProperty()
                .addListener((observable, oldValue, newValue) -> modifiedProperty().set(true));
        }

        return mChannelRotationDelaySpinner;
    }

    /**
     * Scan hang time: how long to stay on a frequency after a call ends before resuming the scan.
     */
    private Spinner<Integer> getScanHangTimeSpinner()
    {
        if(mScanHangTimeSpinner == null)
        {
            mScanHangTimeSpinner = new Spinner<>();
            mScanHangTimeSpinner.setTooltip(new Tooltip("How long to stay on a frequency after a call ends, so that " +
                "you hear the reply, before resuming the scan"));
            mScanHangTimeSpinner.getStyleClass().add(Spinner.STYLE_CLASS_SPLIT_ARROWS_HORIZONTAL);
            SpinnerValueFactory<Integer> svf = new SpinnerValueFactory.IntegerSpinnerValueFactory(
                ChannelRotationMonitor.SCAN_HANG_TIME_MINIMUM, ChannelRotationMonitor.SCAN_HANG_TIME_MAXIMUM,
                ChannelRotationMonitor.SCAN_HANG_TIME_DEFAULT, 250);
            mScanHangTimeSpinner.setValueFactory(svf);
            mScanHangTimeSpinner.getValueFactory().valueProperty()
                .addListener((observable, oldValue, newValue) -> modifiedProperty().set(true));
        }

        return mScanHangTimeSpinner;
    }

    /**
     * Parallel monitoring option for scan lists.
     */
    private CheckBox getScanParallelCheckBox()
    {
        if(mScanParallelCheckBox == null)
        {
            mScanParallelCheckBox = new CheckBox("Monitor nearby frequencies at the same time when tuners are free");
            mScanParallelCheckBox.setTooltip(new Tooltip("Each tuner hears about 2 MHz at once.  When this is on, " +
                "frequencies that fit in a free tuner's window are monitored together so no calls are missed, and " +
                "only the rest are scanned."));
            mScanParallelCheckBox.setSelected(true);
            mScanParallelCheckBox.selectedProperty()
                .addListener((observable, oldValue, newValue) -> modifiedProperty().set(true));
        }

        return mScanParallelCheckBox;
    }

    /**
     * Scan entries (label, priority, lockout) for each frequency in the editor.
     */
    private List<ScanEntry> getScanEntries()
    {
        List<ScanEntry> entries = new ArrayList<>();

        for(FrequencyBox frequencyBox: mFrequencyBoxes)
        {
            long frequency = frequencyBox.getFrequencyField().get();

            if(frequency > 0)
            {
                ScanEntry entry = new ScanEntry(frequency, frequencyBox.getLabelField().getText(),
                    frequencyBox.getLockoutCheckBox().isSelected(), frequencyBox.getPriorityCheckBox().isSelected());

                if(entry.hasSettings())
                {
                    entries.add(entry);
                }
            }
        }

        return entries;
    }

    /**
     * Loads the per-frequency scan settings into the frequency boxes.
     */
    private void setScanEntries(SourceConfigTunerMultipleFrequency config)
    {
        for(FrequencyBox frequencyBox: mFrequencyBoxes)
        {
            ScanEntry entry = config.getScanEntry(frequencyBox.getFrequencyField().get());
            frequencyBox.getLabelField().setText(entry.getLabel() != null ? entry.getLabel() : "");
            frequencyBox.getPriorityCheckBox().setSelected(entry.isPriority());
            frequencyBox.getLockoutCheckBox().setSelected(entry.isLockout());
        }
    }

    /**
     * Updates the preferred tuner's combo box to reflect the current set of tuners.
     */
    private void updatePreferredTuners()
    {
        getPreferredTunerComboBox().getItems().clear();
        getPreferredTunerComboBox().getItems().add(NONE);

        if(mTunerManager != null)
        {
            getPreferredTunerComboBox().getItems().addAll(mTunerManager.getPreferredTunerNames());
        }
    }

    /**
     * Primary frequency editor control
     */
    private FrequencyBox getPrimaryFrequencyBox()
    {
        if(mPrimaryFrequencyBox == null)
        {
            mPrimaryFrequencyBox = new FrequencyBox(mAllowMultipleFrequencies ? Buttons.ADD : Buttons.NONE);
            mFrequencyBoxes.add(mPrimaryFrequencyBox);
        }

        return mPrimaryFrequencyBox;
    }

    /**
     * Preferred tuner combo box control
     */
    private ComboBox<String> getPreferredTunerComboBox()
    {
        if(mPreferredTunerComboBox == null)
        {
            mPreferredTunerComboBox = new ComboBox<>();
            mPreferredTunerComboBox.setDisable(true);
            mPreferredTunerComboBox.getItems().add(NONE);
            mPreferredTunerComboBox.getSelectionModel().selectedItemProperty()
                .addListener((observable, oldValue, newValue) -> modifiedProperty().set(true));
        }

        return mPreferredTunerComboBox;
    }

    /**
     * Container for all frequency controls.
     */
    private VBox getFrequencyBoxContainer()
    {
        if(mFrequencyBoxContainer == null)
        {
            mFrequencyBoxContainer = new VBox();
            mFrequencyBoxContainer.setSpacing(5);
            mFrequencyBoxContainer.getChildren().addAll(getPrimaryFrequencyBox());
        }

        return mFrequencyBoxContainer;
    }

    /**
     * Updates the control with the list of frequencies
     */
    public void setFrequencies(List<Long> frequencies)
    {
        resetFrequencyBoxes();

        for(int x = 0; x < frequencies.size(); x++)
        {
            if(x == 0)
            {
                getPrimaryFrequencyBox().getFrequencyField().set(frequencies.get(0));
            }
            else
            {
                FrequencyBox frequencyBox = addFrequencyBox();
                frequencyBox.getFrequencyField().set(frequencies.get(x));
            }
        }
    }

    /**
     * Updates the control with the specified single frequency
     */
    public void setFrequency(Long frequency)
    {
        resetFrequencyBoxes();
        getPrimaryFrequencyBox().getFrequencyField().set(frequency);
    }

    /**
     * List of frequencies contained in this control
     */
    public List<Long> getFrequencies()
    {
        List<Long> frequencies = new ArrayList<>();
        for(FrequencyBox frequencyBox: mFrequencyBoxes)
        {
            long frequency = frequencyBox.getFrequencyField().get();

            if(frequency > 0)
            {
                frequencies.add(frequency);
            }
        }

        return frequencies;
    }

    /**
     * Resets the frequency boxes to a single frequency box with an empty frequency value.
     */
    private void resetFrequencyBoxes()
    {
        getFrequencyBoxContainer().getChildren().clear();
        mFrequencyBoxes.clear();
        getFrequencyBoxContainer().getChildren().add(getPrimaryFrequencyBox());
        mFrequencyBoxes.add(getPrimaryFrequencyBox());
        getPrimaryFrequencyBox().getFrequencyField().set(0);
        getPrimaryFrequencyBox().clearScanSettings();
    }

    /**
     * Adds a frequency box to the editor
     */
    private FrequencyBox addFrequencyBox()
    {
        FrequencyBox frequencyBox = new FrequencyBox(Buttons.REMOVE);
        frequencyBox.disable(false);
        mFrequencyBoxes.add(frequencyBox);
        getFrequencyBoxContainer().getChildren().add(frequencyBox);
        return frequencyBox;
    }

    /**
     * Removes the specified frequency box from this editor
     */
    private void removeFrequencyBox(FrequencyBox toRemove)
    {
        getFrequencyBoxContainer().getChildren().remove(toRemove);
        mFrequencyBoxes.remove(toRemove);
        modifiedProperty().set(true);
    }

    /**
     * Buttons enum used for styling FrequencyBox controls
     */
    public enum Buttons {ADD, REMOVE, NONE }

    /**
     * Frequency field with optional ADD or REMOVE button
     */
    public class FrequencyBox extends HBox
    {
        private FrequencyField mFrequencyField;
        private Button mAddButton;
        private Button mRemoveButton;
        private TextField mLabelField;
        private CheckBox mPriorityCheckBox;
        private CheckBox mLockoutCheckBox;

        /**
         * Constructs a frequency box with the specified button styling
         */
        public FrequencyBox(Buttons buttons)
        {
            setSpacing(10);
            setAlignment(Pos.CENTER_LEFT);
            getChildren().add(getFrequencyField());

            if(mScanList)
            {
                getChildren().addAll(getLabelField(), getPriorityCheckBox(), getLockoutCheckBox());
            }

            switch(buttons)
            {
                case ADD:
                    getChildren().add(getAddButton());
                    break;
                case REMOVE:
                    getChildren().add(getRemoveButton());
                    break;
            }
        }

        /**
         * Sets the disabled state for this frequency editor
         */
        public void disable(boolean disable)
        {
            getFrequencyField().setDisable(disable);
            getAddButton().setDisable(disable);
            getRemoveButton().setDisable(disable);
            getLabelField().setDisable(disable);
            getPriorityCheckBox().setDisable(disable);
            getLockoutCheckBox().setDisable(disable);
        }

        /**
         * Clears the scan label, priority and lockout settings
         */
        public void clearScanSettings()
        {
            getLabelField().setText("");
            getPriorityCheckBox().setSelected(false);
            getLockoutCheckBox().setSelected(false);
        }

        /**
         * Optional scan list label for this frequency
         */
        public TextField getLabelField()
        {
            if(mLabelField == null)
            {
                mLabelField = new TextField();
                mLabelField.setPromptText("Label");
                mLabelField.setPrefColumnCount(12);
                mLabelField.setDisable(true);
                mLabelField.textProperty().addListener((observable, oldValue, newValue) -> modifiedProperty().set(true));
            }

            return mLabelField;
        }

        /**
         * Scan list priority flag for this frequency
         */
        public CheckBox getPriorityCheckBox()
        {
            if(mPriorityCheckBox == null)
            {
                mPriorityCheckBox = new CheckBox("Priority");
                mPriorityCheckBox.setTooltip(new Tooltip("Check this frequency between each of the other frequencies"));
                mPriorityCheckBox.setDisable(true);
                mPriorityCheckBox.selectedProperty().addListener((observable, oldValue, newValue) -> modifiedProperty().set(true));
            }

            return mPriorityCheckBox;
        }

        /**
         * Scan list lockout flag for this frequency
         */
        public CheckBox getLockoutCheckBox()
        {
            if(mLockoutCheckBox == null)
            {
                mLockoutCheckBox = new CheckBox("Lock Out");
                mLockoutCheckBox.setTooltip(new Tooltip("Skip this frequency while scanning"));
                mLockoutCheckBox.setDisable(true);
                mLockoutCheckBox.selectedProperty().addListener((observable, oldValue, newValue) -> modifiedProperty().set(true));
            }

            return mLockoutCheckBox;
        }

        /**
         * Frequency field control for this editor
         */
        public FrequencyField getFrequencyField()
        {
            if(mFrequencyField == null)
            {
                mFrequencyField = new FrequencyField();
                mFrequencyField.setDisable(true);
                mFrequencyField.textProperty().addListener(new ChangeListener<String>()
                {
                    @Override
                    public void changed(ObservableValue<? extends String> observable, String oldValue, String newValue)
                    {
                        modifiedProperty().set(true);
                        updateCoverage();
                    }
                });
            }

            return mFrequencyField;
        }

        /**
         * Add button
         */
        private Button getAddButton()
        {
            if(mAddButton == null)
            {
                mAddButton = new Button("Add");
                mAddButton.setDisable(true);
                mAddButton.setOnAction(event -> addFrequencyBox());
            }

            return mAddButton;
        }

        /**
         * Remove button
         */
        private Button getRemoveButton()
        {
            if(mRemoveButton == null)
            {
                mRemoveButton = new Button("Remove");
                mRemoveButton.setDisable(true);
                mRemoveButton.setOnAction(event -> {
                    Alert confirm = new Alert(Alert.AlertType.CONFIRMATION,
                        "Do you want to remove this frequency?", ButtonType.YES, ButtonType.NO);
                    confirm.setTitle("Remove Frequency");
                    confirm.setHeaderText("Remove frequency?");
                    confirm.initOwner(((Node)event.getTarget()).getScene().getWindow());
                    confirm.showAndWait().ifPresent(buttonType -> {
                        if(buttonType == ButtonType.YES)
                        {
                            removeFrequencyBox(FrequencyBox.this);
                        }
                    });
                });
            }

            return mRemoveButton;
        }
    }
}
