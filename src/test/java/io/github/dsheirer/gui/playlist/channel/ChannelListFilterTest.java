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

package io.github.dsheirer.gui.playlist.channel;

import io.github.dsheirer.controller.channel.Channel;
import io.github.dsheirer.module.decode.DecoderFactory;
import io.github.dsheirer.module.decode.DecoderType;
import io.github.dsheirer.source.config.SourceConfigTuner;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Tests the channel table search filter
 */
public class ChannelListFilterTest
{
    private static Channel createChannel(long frequency, String aliasList)
    {
        Channel channel = new Channel();
        channel.setName("Fire Dispatch");
        channel.setDecodeConfiguration(DecoderFactory.getDecodeConfiguration(DecoderType.NBFM));
        SourceConfigTuner source = new SourceConfigTuner();
        source.setFrequency(frequency);
        channel.setSourceConfiguration(source);
        channel.setAliasListName(aliasList);
        return channel;
    }

    private static boolean matches(Channel channel, String text)
    {
        ChannelEditor.ChannelListFilter filter = new ChannelEditor.ChannelListFilter();
        filter.setFilterText(text);
        return filter.test(channel);
    }

    @Test
    void matchesFrequencyInMHz()
    {
        Channel channel = createChannel(154_430_000L, null);
        assertTrue(matches(channel, "154.43"));
        assertTrue(matches(channel, "154.430"));
        assertTrue(matches(channel, "154.4300"));
        assertTrue(matches(channel, "4.43"));
        assertTrue(matches(channel, "154"));
        assertFalse(matches(channel, "155.475"));
        assertFalse(matches(channel, "154.4301"));
    }

    @Test
    void matchesAliasList()
    {
        Channel channel = createChannel(154_430_000L, "County Fire");
        assertTrue(matches(channel, "county"));
        assertFalse(matches(channel, "state"));
    }

    @Test
    void stillMatchesName()
    {
        Channel channel = createChannel(154_430_000L, null);
        assertTrue(matches(channel, "dispatch"));
    }
}
