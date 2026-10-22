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

import static org.junit.jupiter.api.Assertions.assertEquals;

import io.github.dsheirer.gui.discovery.DiscoveredFrequencyTableModel.Snapshot;
import io.github.dsheirer.module.decode.nbfm.discovery.DiscoveryEvent.Type;
import org.junit.jupiter.api.Test;

/**
 * Discovered frequency table: one row per frequency, ordered by frequency, accumulating transmissions and airtime.
 */
public class DiscoveredFrequencyTableModelTest
{
    @Test
    void accumulatesTransmissionsPerFrequency()
    {
        DiscoveredFrequencyTableModel model = new DiscoveredFrequencyTableModel();
        model.setPlaylistLookup(frequency -> frequency == 154_430_000);
        model.setRecordingTime(true);

        model.update(new Snapshot(Type.STARTED, 155_752_500, false, 1_000, 250, 20, 4_000));
        model.update(new Snapshot(Type.STARTED, 154_430_000, false, 1_200, 250, 30, 5_000));
        assertEquals("ON AIR", model.getValueAt(1, DiscoveredFrequencyTableModel.COLUMN_STATUS));

        model.update(new Snapshot(Type.ENDED, 155_752_500, false, 3_000, 2_000, 25, 5_500));
        model.update(new Snapshot(Type.STARTED, 155_752_500, false, 9_000, 250, 22, 4_000));
        model.update(new Snapshot(Type.ENDED, 155_752_500, false, 10_000, 1_000, 22, 4_000));

        assertEquals(2, model.getRowCount());
        assertEquals("154.43000", model.getValueAt(0, DiscoveredFrequencyTableModel.COLUMN_FREQUENCY));
        assertEquals("Yes", model.getValueAt(0, DiscoveredFrequencyTableModel.COLUMN_PLAYLIST));
        assertEquals("155.75250", model.getValueAt(1, DiscoveredFrequencyTableModel.COLUMN_FREQUENCY));
        assertEquals("Idle", model.getValueAt(1, DiscoveredFrequencyTableModel.COLUMN_STATUS));
        assertEquals(2, model.getValueAt(1, DiscoveredFrequencyTableModel.COLUMN_TRANSMISSIONS));
        assertEquals(3.0, model.getValueAt(1, DiscoveredFrequencyTableModel.COLUMN_AIRTIME));
        assertEquals(25.0, model.getValueAt(1, DiscoveredFrequencyTableModel.COLUMN_SNR));
        assertEquals(5.5, model.getValueAt(1, DiscoveredFrequencyTableModel.COLUMN_WIDTH));
        assertEquals("+10.0 s", model.getValueAt(1, DiscoveredFrequencyTableModel.COLUMN_LAST_HEARD));

        model.setAllIdle();
        assertEquals("Idle", model.getValueAt(0, DiscoveredFrequencyTableModel.COLUMN_STATUS));

        model.remove(154_430_000);
        assertEquals(1, model.getRowCount());
    }

    @Test
    void marksBirdiesAndOffRasterCarriers()
    {
        DiscoveredFrequencyTableModel model = new DiscoveredFrequencyTableModel();
        model.update(new Snapshot(Type.STARTED, 156_000_000, false, 1_000, 250, 40, 1_200));
        model.update(new Snapshot(Type.BIRDIE, 156_000_000, false, 121_000, 120_000, 40, 1_200));
        model.update(new Snapshot(Type.STARTED, 460_140_500, true, 1_000, 250, 20, 4_000));

        assertEquals("Birdie (ignored)", model.getValueAt(0, DiscoveredFrequencyTableModel.COLUMN_STATUS));
        assertEquals("460.14050 *", model.getValueAt(1, DiscoveredFrequencyTableModel.COLUMN_FREQUENCY));
    }
}
