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
import io.github.dsheirer.controller.channel.ChannelException;
import io.github.dsheirer.controller.channel.ChannelProcessingManager;
import io.github.dsheirer.eventbus.MyEventBus;
import io.github.dsheirer.gui.playlist.channel.ViewChannelRequest;
import io.github.dsheirer.util.ThreadPool;
import java.awt.Component;
import java.util.ArrayList;
import java.util.List;
import javax.swing.JMenuItem;
import javax.swing.JOptionPane;
import javax.swing.SwingUtilities;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Creates the Swing right-click menu items for controlling a channel (start, stop, restart, view/edit) so that the
 * Now Playing panel and the spectrum/waterfall display offer the same actions.
 */
public class ChannelControlMenuItems
{
    private static final Logger mLog = LoggerFactory.getLogger(ChannelControlMenuItems.class);

    private ChannelControlMenuItems()
    {
    }

    /**
     * Creates the menu items for the channel.  Start is offered when the channel is stopped, Stop and Restart when it
     * is running.  Start, stop and restart run in the background and errors are shown in a dialog.
     *
     * @param channel to control
     * @param manager to start and stop the channel
     * @param parent component for error dialogs
     * @param includeViewEdit true to also include a View/Edit item
     * @return menu items in display order
     */
    public static List<JMenuItem> create(Channel channel, ChannelProcessingManager manager, Component parent,
                                         boolean includeViewEdit)
    {
        List<JMenuItem> items = new ArrayList<>();
        String title = channel.getShortTitle();

        if(channel.isProcessing())
        {
            JMenuItem stop = new JMenuItem("Stop: " + title);
            stop.addActionListener(e -> run(channel, manager, parent, true, false));
            items.add(stop);

            JMenuItem restart = new JMenuItem("Restart: " + title);
            restart.addActionListener(e -> run(channel, manager, parent, true, true));
            items.add(restart);
        }
        else
        {
            JMenuItem start = new JMenuItem("Start: " + title);
            start.addActionListener(e -> run(channel, manager, parent, false, true));
            items.add(start);
        }

        if(includeViewEdit)
        {
            JMenuItem viewChannel = new JMenuItem("View/Edit: " + title);
            viewChannel.addActionListener(e -> MyEventBus.getGlobalEventBus().post(new ViewChannelRequest(channel)));
            items.add(viewChannel);
        }

        return items;
    }

    /**
     * Stops and/or starts the channel on a background thread.
     * @param stop true to stop the channel first
     * @param start true to start the channel afterward
     */
    private static void run(Channel channel, ChannelProcessingManager manager, Component parent, boolean stop,
                            boolean start)
    {
        ThreadPool.CACHED.execute(() -> {
            try
            {
                if(stop && channel.isProcessing())
                {
                    manager.stop(channel);
                }

                if(start && !channel.isProcessing())
                {
                    manager.start(channel);
                }
            }
            catch(ChannelException ce)
            {
                mLog.error("Error " + (start ? "starting" : "stopping") + " channel [" + channel.getName() + "] - " +
                    ce.getMessage());
                SwingUtilities.invokeLater(() -> JOptionPane.showMessageDialog(parent, ce.getMessage(),
                    "Can't " + (start ? "start" : "stop") + " channel [" + channel.getShortTitle() + "]",
                    JOptionPane.ERROR_MESSAGE));
            }
        });
    }
}
