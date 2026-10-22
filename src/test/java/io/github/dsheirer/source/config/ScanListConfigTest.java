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

package io.github.dsheirer.source.config;

import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.dataformat.xml.JacksonXmlModule;
import com.fasterxml.jackson.dataformat.xml.XmlMapper;
import io.github.dsheirer.controller.channel.Channel;
import io.github.dsheirer.module.decode.nbfm.DecodeConfigNBFM;
import io.github.dsheirer.source.tuner.channel.rotation.ChannelRotationMonitor;
import java.util.List;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Tests that scan list settings survive a playlist save and load, and that older playlists still load.
 */
public class ScanListConfigTest
{
    private static ObjectMapper mapper()
    {
        //Same configuration as the playlist manager
        JacksonXmlModule xmlModule = new JacksonXmlModule();
        xmlModule.setDefaultUseWrapper(false);
        return new XmlMapper(xmlModule).configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, false);
    }

    private static Channel scanListChannel()
    {
        Channel channel = new Channel("Fire Dispatch");
        channel.setDecodeConfiguration(new DecodeConfigNBFM());
        SourceConfigTunerMultipleFrequency config = new SourceConfigTunerMultipleFrequency();
        config.setFrequencies(List.of(154_430_000L, 154_190_000L, 155_475_000L));
        config.setFrequencyRotationDelay(300);
        config.setScanHangTime(4000);
        config.setScanParallel(false);
        config.setScanEntries(List.of(new ScanEntry(154_430_000L, "Dispatch", false, true),
            new ScanEntry(154_190_000L, null, true, false),
            new ScanEntry(155_475_000L, "  ", false, false)));
        channel.setSourceConfiguration(config);
        return channel;
    }

    @Test
    void scanSettingsRoundTrip() throws Exception
    {
        String xml = mapper().writeValueAsString(scanListChannel());
        Channel loaded = mapper().readValue(xml, Channel.class);

        assertTrue(loaded.getSourceConfiguration() instanceof SourceConfigTunerMultipleFrequency, xml);
        SourceConfigTunerMultipleFrequency config = (SourceConfigTunerMultipleFrequency)loaded.getSourceConfiguration();

        assertEquals(List.of(154_430_000L, 154_190_000L, 155_475_000L), config.getFrequencies());
        assertEquals(300, config.getFrequencyRotationDelay());
        assertEquals(4000, config.getScanHangTime());
        assertFalse(config.isScanParallel());

        //Blank label entry with no other settings is not persisted
        assertEquals(2, config.getScanEntries().size(), xml);
        assertEquals("Dispatch", config.getScanEntry(154_430_000L).getLabel());
        assertEquals(List.of(154_430_000L), config.getPriorityFrequencies());
        assertEquals(List.of(154_190_000L), config.getLockoutFrequencies());
        assertNull(config.getScanEntry(155_475_000L).getLabel());
    }

    @Test
    void copyKeepsScanSettings()
    {
        Channel copy = scanListChannel().copyOf();
        SourceConfigTunerMultipleFrequency config = (SourceConfigTunerMultipleFrequency)copy.getSourceConfiguration();

        assertEquals(300, config.getFrequencyRotationDelay());
        assertEquals(4000, config.getScanHangTime());
        assertFalse(config.isScanParallel());
        assertEquals("Dispatch", config.getScanEntry(154_430_000L).getLabel());
        assertEquals(List.of(154_190_000L), config.getLockoutFrequencies());
    }

    @Test
    void olderPlaylistLoadsWithScanDefaults() throws Exception
    {
        //Multiple frequency source config as written before scan list settings existed
        String xml = mapper().writeValueAsString(scanListChannel())
            .replaceAll(" scan_hang_time=\"[0-9]+\"", "")
            .replaceAll(" scan_parallel=\"[a-z]+\"", "")
            .replaceAll("<scan_entry[^>]*/>", "");

        assertFalse(xml.contains("scan_"), xml);

        Channel loaded = mapper().readValue(xml, Channel.class);
        SourceConfigTunerMultipleFrequency config = (SourceConfigTunerMultipleFrequency)loaded.getSourceConfiguration();

        assertEquals(3, config.getFrequencies().size());
        assertEquals(ChannelRotationMonitor.SCAN_HANG_TIME_DEFAULT, config.getScanHangTime());
        assertTrue(config.isScanParallel());
        assertTrue(config.getScanEntries().isEmpty());
        assertTrue(config.getLockoutFrequencies().isEmpty());
    }
}
