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
package io.github.dsheirer.gui.preference.paging;

import io.github.dsheirer.module.decode.twotone.TwoToneTable;
import io.github.dsheirer.preference.UserPreferences;
import io.github.dsheirer.preference.paging.PagingPreference;
import java.awt.Desktop;
import java.io.File;
import java.nio.file.Path;
import javafx.geometry.HPos;
import javafx.geometry.Insets;
import javafx.scene.control.Button;
import javafx.scene.control.CheckBox;
import javafx.scene.control.Label;
import javafx.scene.control.TextField;
import javafx.scene.layout.GridPane;
import javafx.scene.layout.HBox;
import javafx.scene.layout.Priority;
import javafx.stage.FileChooser;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Preference editor for the two-tone paging decoder.
 */
public class PagingPreferenceEditor extends HBox
{
    private final static Logger mLog = LoggerFactory.getLogger(PagingPreferenceEditor.class);
    private final PagingPreference mPagingPreference;
    private TextField mTablePathField;
    private TextField mUrlField;
    private TextField mApiKeyField;
    private CheckBox mSendCheckBox;

    public PagingPreferenceEditor(UserPreferences userPreferences)
    {
        mPagingPreference = userPreferences.getPagingPreference();

        GridPane pane = new GridPane();
        pane.setPadding(new Insets(10, 10, 10, 10));
        pane.setHgap(10);
        pane.setVgap(10);
        HBox.setHgrow(pane, Priority.ALWAYS);

        int row = 0;
        pane.add(new Label("Two-Tone Paging"), 0, row++, 3, 1);

        Label help = new Label("Turn on the Two-Tone Paging decoder in each NBFM channel's Auxiliary Decoders.  " +
                "The station list names the station or group for each pair of tones.  Pages that aren't on the list " +
                "show their measured tones in the Events tab so you can add them.");
        help.setWrapText(true);
        pane.add(help, 0, row++, 3, 1);

        Label listLabel = new Label("Station List:");
        GridPane.setHalignment(listLabel, HPos.RIGHT);
        pane.add(listLabel, 0, row);
        mTablePathField = new TextField(mPagingPreference.getTablePath().toString());
        mTablePathField.setEditable(false);
        GridPane.setHgrow(mTablePathField, Priority.ALWAYS);
        pane.add(mTablePathField, 1, row);

        Button openButton = new Button("Open");
        openButton.setOnAction(event -> openTable());
        Button changeButton = new Button("Change...");
        changeButton.setOnAction(event -> changeTable());
        Button defaultButton = new Button("Default");
        defaultButton.setOnAction(event -> {
            mPagingPreference.setTablePath(null);
            mTablePathField.setText(mPagingPreference.getTablePath().toString());
        });
        pane.add(new HBox(5, openButton, changeButton, defaultButton), 2, row++);

        mSendCheckBox = new CheckBox("Send pages to the call transcriber log");
        mSendCheckBox.setSelected(mPagingPreference.isSendToTranscriber());
        mSendCheckBox.setOnAction(event -> {
            mPagingPreference.setSendToTranscriber(mSendCheckBox.isSelected());
            updateEnabled();
        });
        pane.add(mSendCheckBox, 1, row++, 2, 1);

        Label urlLabel = new Label("Transcriber Address:");
        GridPane.setHalignment(urlLabel, HPos.RIGHT);
        pane.add(urlLabel, 0, row);
        mUrlField = new TextField(mPagingPreference.getTranscriberUrl());
        mUrlField.setPromptText(PagingPreference.DEFAULT_TRANSCRIBER_URL);
        mUrlField.setOnAction(event -> mPagingPreference.setTranscriberUrl(mUrlField.getText()));
        mUrlField.focusedProperty().addListener((observable, wasFocused, focused) -> {
            if(!focused)
            {
                mPagingPreference.setTranscriberUrl(mUrlField.getText());
                mUrlField.setText(mPagingPreference.getTranscriberUrl());
            }
        });
        pane.add(mUrlField, 1, row++, 2, 1);

        Label keyLabel = new Label("Transcriber API Key:");
        GridPane.setHalignment(keyLabel, HPos.RIGHT);
        pane.add(keyLabel, 0, row);
        mApiKeyField = new TextField(mPagingPreference.getTranscriberApiKey());
        mApiKeyField.setPromptText("Only needed when the transcriber runs on another computer");
        mApiKeyField.setOnAction(event -> mPagingPreference.setTranscriberApiKey(mApiKeyField.getText()));
        mApiKeyField.focusedProperty().addListener((observable, wasFocused, focused) -> {
            if(!focused)
            {
                mPagingPreference.setTranscriberApiKey(mApiKeyField.getText());
            }
        });
        pane.add(mApiKeyField, 1, row++, 2, 1);

        updateEnabled();
        getChildren().add(pane);
    }

    private void updateEnabled()
    {
        mUrlField.setDisable(!mSendCheckBox.isSelected());
        mApiKeyField.setDisable(!mSendCheckBox.isSelected());
    }

    /**
     * Opens the station list in the computer's default editor for CSV files, creating it first if needed.
     */
    private void openTable()
    {
        Path path = mPagingPreference.getTablePath();
        TwoToneTable.createIfMissing(path);

        Thread thread = new Thread(() -> {
            try
            {
                Desktop.getDesktop().open(path.toFile());
            }
            catch(Exception e)
            {
                mLog.error("Couldn't open two-tone station list " + path, e);
            }
        }, "open two-tone station list");
        thread.setDaemon(true);
        thread.start();
    }

    private void changeTable()
    {
        FileChooser chooser = new FileChooser();
        chooser.setTitle("Two-Tone Station List");
        chooser.getExtensionFilters().add(new FileChooser.ExtensionFilter("CSV files", "*.csv"));
        File current = mPagingPreference.getTablePath().toFile();
        if(current.getParentFile() != null && current.getParentFile().isDirectory())
        {
            chooser.setInitialDirectory(current.getParentFile());
        }
        chooser.setInitialFileName(current.getName());
        File selected = chooser.showSaveDialog(getScene() != null ? getScene().getWindow() : null);

        if(selected != null)
        {
            mPagingPreference.setTablePath(selected.toPath());
            mTablePathField.setText(mPagingPreference.getTablePath().toString());
        }
    }
}
