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
package io.github.dsheirer.module.decode.twotone;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.FileTime;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * User editable list that names the station or group for each two-tone page.
 *
 * The list is a plain text CSV file with one entry per line: tone A in Hz, tone B in Hz (empty for a single long
 * tone), and a name.  Lines starting with # are comments.  The file is re-read whenever it changes, so edits apply
 * without restarting.
 */
public class TwoToneTable
{
    private static final Logger mLog = LoggerFactory.getLogger(TwoToneTable.class);
    /**
     * Tones match an entry when each is within this fraction of the listed frequency.  Quick Call II tone plans space
     * their tones about 5 percent apart, so 1.5 percent leaves room for radio and measurement error without overlap.
     */
    public static final double MATCH_FRACTION = 0.015;
    private static final long RECHECK_MILLIS = 2000;
    private static final Map<Path,Cached> CACHE = new ConcurrentHashMap<>();

    public static final String STARTER_FILE = """
            # Two-tone paging list for SDRTrunk
            #
            # One line per station or group: tone A in Hz, tone B in Hz, then the name to show.
            # Leave tone B empty for a single long tone (for example an all-call).
            # Tones match within 1.5 percent, so 349 matches a measured 349.0 or 350.2.
            # Lines that start with # are ignored.  SDRTrunk re-reads this file whenever you save it.
            #
            # Pages that are not on this list still show up in the Events tab with their measured tones,
            # for example "Unknown page 349.0 Hz + 832.5 Hz".  Copy those numbers here with the name the
            # dispatcher calls out right after the tones.
            #
            tone_a,tone_b,name
            # 349.0,832.5,Example Fire Company Station 4
            # 1084.0,,Example county all-call long tone
            """;

    private final List<Entry> mEntries;

    public TwoToneTable(List<Entry> entries)
    {
        mEntries = Collections.unmodifiableList(new ArrayList<>(entries));
    }

    public List<Entry> getEntries()
    {
        return mEntries;
    }

    /**
     * Finds the entry for a page.
     * @param toneA measured tone A in Hz
     * @param toneB measured tone B in Hz, or NaN for a single long tone
     * @return closest matching entry, or null when nothing on the list matches
     */
    public Entry match(double toneA, double toneB)
    {
        Entry best = null;
        double bestError = Double.MAX_VALUE;

        for(Entry entry : mEntries)
        {
            if(Double.isNaN(toneB) != entry.isLongTone())
            {
                continue;
            }

            double errorA = Math.abs(toneA - entry.toneA()) / entry.toneA();
            double errorB = entry.isLongTone() ? 0 : Math.abs(toneB - entry.toneB()) / entry.toneB();

            if(errorA <= MATCH_FRACTION && errorB <= MATCH_FRACTION && errorA + errorB < bestError)
            {
                best = entry;
                bestError = errorA + errorB;
            }
        }

        return best;
    }

    /**
     * Parses the CSV text.  Lines that can't be read are skipped and logged.
     */
    public static TwoToneTable parse(String text)
    {
        List<Entry> entries = new ArrayList<>();
        int lineNumber = 0;

        for(String line : text.split("\\R"))
        {
            lineNumber++;
            String trimmed = line.strip();

            if(trimmed.isEmpty() || trimmed.startsWith("#") || trimmed.toLowerCase(Locale.ROOT).startsWith("tone_a"))
            {
                continue;
            }

            //The name is everything after the second comma, so names may contain commas
            String[] parts = trimmed.split(",", 3);

            try
            {
                double toneA = Double.parseDouble(parts[0].strip());
                String toneBText = parts.length > 1 ? parts[1].strip() : "";
                double toneB = toneBText.isEmpty() ? Double.NaN : Double.parseDouble(toneBText);
                String name = parts.length > 2 ? unquote(parts[2].strip()) : "";

                if(toneA <= 0 || toneB <= 0)
                {
                    throw new NumberFormatException("tones must be above zero");
                }

                entries.add(new Entry(toneA, toneB, name.isEmpty() ? describe(toneA, toneB) : name));
            }
            catch(NumberFormatException e)
            {
                mLog.warn("Two-tone list line " + lineNumber + " skipped, expected 'tone A, tone B, name': " + trimmed);
            }
        }

        return new TwoToneTable(entries);
    }

    private static String unquote(String value)
    {
        if(value.length() >= 2 && value.startsWith("\"") && value.endsWith("\""))
        {
            return value.substring(1, value.length() - 1).replace("\"\"", "\"");
        }

        return value;
    }

    /**
     * Plain description of a pair of tones, such as "349.0 Hz + 832.5 Hz" or "1084.0 Hz long tone".
     */
    public static String describe(double toneA, double toneB)
    {
        if(Double.isNaN(toneB))
        {
            return String.format(Locale.US, "%.1f Hz long tone", toneA);
        }

        return String.format(Locale.US, "%.1f Hz + %.1f Hz", toneA, toneB);
    }

    /**
     * Returns the list stored in the file, re-reading it when it has changed.  Creates the file with instructions
     * and examples when it does not exist yet.  Returns an empty list if the file can't be read.
     */
    public static TwoToneTable get(Path path)
    {
        Cached cached = CACHE.computeIfAbsent(path, Cached::new);
        return cached.get();
    }

    /**
     * Creates the file with instructions when it doesn't exist.
     */
    public static void createIfMissing(Path path)
    {
        try
        {
            if(!Files.exists(path))
            {
                if(path.getParent() != null)
                {
                    Files.createDirectories(path.getParent());
                }
                Files.writeString(path, STARTER_FILE, StandardCharsets.UTF_8);
                mLog.info("Created two-tone paging list " + path);
            }
        }
        catch(IOException e)
        {
            mLog.warn("Couldn't create two-tone paging list " + path + ": " + e.getMessage());
        }
    }

    /**
     * Entry on the list.
     * @param toneA in Hz
     * @param toneB in Hz, or NaN for a single long tone
     * @param name of the station or group
     */
    public record Entry(double toneA, double toneB, String name)
    {
        public boolean isLongTone()
        {
            return Double.isNaN(toneB);
        }
    }

    /**
     * File backed list that reloads when the file's modified time changes.
     */
    private static class Cached
    {
        private final Path mPath;
        private TwoToneTable mTable = new TwoToneTable(List.of());
        private FileTime mModified;
        private long mLastCheck;
        private boolean mCreateAttempted;

        Cached(Path path)
        {
            mPath = path;
        }

        synchronized TwoToneTable get()
        {
            long now = System.currentTimeMillis();

            if(mModified != null && now - mLastCheck < RECHECK_MILLIS)
            {
                return mTable;
            }

            mLastCheck = now;

            if(!mCreateAttempted)
            {
                mCreateAttempted = true;
                createIfMissing(mPath);
            }

            try
            {
                FileTime modified = Files.getLastModifiedTime(mPath);

                if(!modified.equals(mModified))
                {
                    mTable = parse(Files.readString(mPath, StandardCharsets.UTF_8));
                    mModified = modified;
                    mLog.info("Loaded " + mTable.getEntries().size() + " two-tone paging entries from " + mPath);
                }
            }
            catch(IOException e)
            {
                mModified = FileTime.fromMillis(0);
                mLog.warn("Couldn't read two-tone paging list " + mPath + ": " + e.getMessage());
            }

            return mTable;
        }
    }
}
