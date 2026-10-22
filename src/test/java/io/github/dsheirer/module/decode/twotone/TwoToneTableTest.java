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

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.FileTime;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/**
 * Station list parsing and matching, and the JSON sent to the call transcriber.
 */
public class TwoToneTableTest
{
    private static final String LIST = """
            # comment
            tone_a,tone_b,name
            349.0,832.5,Station 4
            368.5, 707.3 ,"Station 7, Engine and Rescue"
            1084,,All-call long tone
            not a number,1,Broken line
            389.0,1006.9,
            """;

    @Test
    public void parsesNamesCommasAndLongTones()
    {
        TwoToneTable table = TwoToneTable.parse(LIST);
        assertEquals(4, table.getEntries().size());
        assertEquals("Station 7, Engine and Rescue", table.getEntries().get(1).name());
        assertTrue(table.getEntries().get(2).isLongTone());
        assertEquals("389.0 Hz + 1006.9 Hz", table.getEntries().get(3).name());
    }

    @Test
    public void matchesWithinTolerance()
    {
        TwoToneTable table = TwoToneTable.parse(LIST);
        assertEquals("Station 4", table.match(350.2, 830.0).name());
        assertEquals("Station 7, Engine and Rescue", table.match(368.0, 707.9).name());
        assertEquals("All-call long tone", table.match(1086.0, Double.NaN).name());
        //Tones out of order, too far off, or a two-tone page against a long tone entry
        assertNull(table.match(832.5, 349.0));
        assertNull(table.match(360.0, 832.5));
        assertNull(table.match(1084.0, 832.5));
    }

    @Test
    public void starterFileHasNoActiveEntries()
    {
        assertEquals(0, TwoToneTable.parse(TwoToneTable.STARTER_FILE).getEntries().size());
    }

    @Test
    public void fileIsCreatedAndReloadedWhenEdited(@TempDir Path folder) throws Exception
    {
        Path path = folder.resolve("paging_tones.csv");
        assertEquals(0, TwoToneTable.get(path).getEntries().size());
        assertTrue(Files.readString(path).contains("tone_a,tone_b,name"));

        Files.writeString(path, "349,832.5,Station 4\n");
        Files.setLastModifiedTime(path, FileTime.fromMillis(System.currentTimeMillis() + 60_000));
        Thread.sleep(2100);
        assertEquals("Station 4", TwoToneTable.get(path).match(349, 832.5).name());
    }

    /**
     * The transcriber's tests post exactly this text (SDRTRUNK_PAGE in transcriber/tests/test_service.py).
     */
    @Test
    public void pageJson()
    {
        TwoToneDetector.Page page = new TwoToneDetector.Page(349.04, 832.46, 1.02, 0);
        String json = TwoTonePageSender.toJson(page, 1792656843250L, new TwoToneTable.Entry(349, 832.5, "Station 4"),
                "Fire Dispatch", "Northampton", null, 154_190_000, "");
        assertEquals("{\"key\":\"\",\"dateTime\":1792656843.25,\"toneA\":349.0,\"toneB\":832.5,\"toneASeconds\":1.0," +
                "\"label\":\"Station 4\",\"system\":\"Northampton\",\"site\":null,\"channel\":\"Fire Dispatch\"," +
                "\"frequency\":154190000}", json);
    }
}
