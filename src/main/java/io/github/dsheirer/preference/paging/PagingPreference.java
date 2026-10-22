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
package io.github.dsheirer.preference.paging;

import io.github.dsheirer.preference.Preference;
import io.github.dsheirer.preference.PreferenceType;
import io.github.dsheirer.preference.directory.DirectoryPreference;
import io.github.dsheirer.sample.Listener;
import java.nio.file.Path;
import java.util.prefs.Preferences;

/**
 * User preferences for the two-tone paging decoder: where the station name list lives and whether pages are sent to
 * the call transcriber's log.
 */
public class PagingPreference extends Preference
{
    public static final String DEFAULT_TRANSCRIBER_URL = "http://localhost:3000/api/page";
    public static final String DEFAULT_TABLE_FILE_NAME = "paging_tones.csv";
    private static final String PREFERENCE_KEY_TABLE_PATH = "paging.table.path";
    private static final String PREFERENCE_KEY_SEND_TO_TRANSCRIBER = "paging.send.to.transcriber";
    private static final String PREFERENCE_KEY_TRANSCRIBER_URL = "paging.transcriber.url";
    private static final String PREFERENCE_KEY_TRANSCRIBER_API_KEY = "paging.transcriber.api.key";
    private Preferences mPreferences = Preferences.userNodeForPackage(PagingPreference.class);
    private final DirectoryPreference mDirectoryPreference;

    /**
     * Constructs this preference with an update listener
     * @param updateListener to receive notifications whenever these preferences change
     * @param directoryPreference for the default location of the station name list
     */
    public PagingPreference(Listener<PreferenceType> updateListener, DirectoryPreference directoryPreference)
    {
        super(updateListener);
        mDirectoryPreference = directoryPreference;
    }

    @Override
    public PreferenceType getPreferenceType()
    {
        return PreferenceType.PAGING;
    }

    /**
     * Default station name list: paging_tones.csv in the SDRTrunk application folder.
     */
    public Path getDefaultTablePath()
    {
        return mDirectoryPreference.getDirectoryApplicationRoot().resolve(DEFAULT_TABLE_FILE_NAME);
    }

    /**
     * CSV file that names the station or group for each tone pair.
     */
    public Path getTablePath()
    {
        String path = mPreferences.get(PREFERENCE_KEY_TABLE_PATH, null);
        return path == null || path.isBlank() ? getDefaultTablePath() : Path.of(path);
    }

    public void setTablePath(Path path)
    {
        if(path == null || path.equals(getDefaultTablePath()))
        {
            mPreferences.remove(PREFERENCE_KEY_TABLE_PATH);
        }
        else
        {
            mPreferences.put(PREFERENCE_KEY_TABLE_PATH, path.toString());
        }
        notifyPreferenceUpdated();
    }

    /**
     * Indicates if decoded pages are posted to the call transcriber's log.  On by default; when the transcriber isn't
     * running the post simply fails and SDRTrunk carries on.
     */
    public boolean isSendToTranscriber()
    {
        return mPreferences.getBoolean(PREFERENCE_KEY_SEND_TO_TRANSCRIBER, true);
    }

    public void setSendToTranscriber(boolean send)
    {
        mPreferences.putBoolean(PREFERENCE_KEY_SEND_TO_TRANSCRIBER, send);
        notifyPreferenceUpdated();
    }

    /**
     * Address of the transcriber's page endpoint.
     */
    public String getTranscriberUrl()
    {
        return mPreferences.get(PREFERENCE_KEY_TRANSCRIBER_URL, DEFAULT_TRANSCRIBER_URL);
    }

    public void setTranscriberUrl(String url)
    {
        if(url == null || url.isBlank())
        {
            mPreferences.remove(PREFERENCE_KEY_TRANSCRIBER_URL);
        }
        else
        {
            mPreferences.put(PREFERENCE_KEY_TRANSCRIBER_URL, url.strip());
        }
        notifyPreferenceUpdated();
    }

    /**
     * Transcriber API key.  Only needed when the transcriber runs on a different computer.
     */
    public String getTranscriberApiKey()
    {
        return mPreferences.get(PREFERENCE_KEY_TRANSCRIBER_API_KEY, "");
    }

    public void setTranscriberApiKey(String key)
    {
        mPreferences.put(PREFERENCE_KEY_TRANSCRIBER_API_KEY, key == null ? "" : key.strip());
        notifyPreferenceUpdated();
    }
}
