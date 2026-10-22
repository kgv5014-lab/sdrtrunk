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

package io.github.dsheirer.gui.channel;

import io.github.dsheirer.controller.channel.Channel;
import java.util.List;
import javax.swing.JMenuItem;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;

/**
 * Tests the channel control right-click menu items
 */
public class ChannelControlMenuItemsTest
{
    private static List<String> labels(Channel channel, boolean viewEdit)
    {
        return ChannelControlMenuItems.create(channel, null, null, viewEdit).stream().map(JMenuItem::getText).toList();
    }

    @Test
    void stoppedChannelOffersStart()
    {
        Channel channel = new Channel("Dispatch");
        channel.processingProperty().set(false);
        String title = channel.getShortTitle();
        assertEquals(List.of("Start: " + title, "View/Edit: " + title), labels(channel, true));
        assertEquals(List.of("Start: " + title), labels(channel, false));
    }

    @Test
    void runningChannelOffersStopAndRestart()
    {
        Channel channel = new Channel("Dispatch");
        channel.processingProperty().set(true);
        String title = channel.getShortTitle();
        assertEquals(List.of("Stop: " + title, "Restart: " + title, "View/Edit: " + title), labels(channel, true));
    }
}
