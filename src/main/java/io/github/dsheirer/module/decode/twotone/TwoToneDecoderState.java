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

import io.github.dsheirer.channel.state.DecoderState;
import io.github.dsheirer.channel.state.DecoderStateEvent;
import io.github.dsheirer.identifier.Identifier;
import io.github.dsheirer.identifier.IdentifierClass;
import io.github.dsheirer.identifier.MutableIdentifierCollection;
import io.github.dsheirer.identifier.configuration.ChannelNameConfigurationIdentifier;
import io.github.dsheirer.identifier.configuration.FrequencyConfigurationIdentifier;
import io.github.dsheirer.identifier.configuration.SiteConfigurationIdentifier;
import io.github.dsheirer.identifier.configuration.SystemConfigurationIdentifier;
import io.github.dsheirer.message.IMessage;
import io.github.dsheirer.module.decode.DecoderType;
import io.github.dsheirer.module.decode.event.DecodeEvent;
import io.github.dsheirer.module.decode.event.DecodeEventType;
import io.github.dsheirer.preference.paging.PagingPreference;
import io.github.dsheirer.protocol.Protocol;
import java.text.SimpleDateFormat;
import java.util.ArrayDeque;
import java.util.Date;
import java.util.Deque;

/**
 * Names each two-tone page from the user's station list, shows it in the channel's events, and posts it to the call
 * transcriber's log.
 */
public class TwoToneDecoderState extends DecoderState
{
    private static final int RECENT_PAGES = 20;
    private final PagingPreference mPagingPreference;
    private final Deque<String> mRecentPages = new ArrayDeque<>();

    /**
     * Constructs an instance
     * @param pagingPreference for the station list and transcriber settings, or null to only show raw tones
     */
    public TwoToneDecoderState(PagingPreference pagingPreference)
    {
        mPagingPreference = pagingPreference;
    }

    @Override
    public DecoderType getDecoderType()
    {
        return DecoderType.TWO_TONE_PAGING;
    }

    @Override
    public void init()
    {
    }

    @Override
    public void receive(IMessage message)
    {
        if(message instanceof TwoTonePageMessage pageMessage)
        {
            TwoToneDetector.Page page = pageMessage.getPage();
            TwoToneTable.Entry entry = mPagingPreference != null ?
                    TwoToneTable.get(mPagingPreference.getTablePath()).match(page.toneA(), page.toneB()) : null;
            String tones = TwoToneTable.describe(page.toneA(), page.toneB());
            String details = entry != null ? "Page: " + entry.name() + " (" + tones + ")" : "Unknown page " + tones;

            //Drop the channel's talkgroup so a station toned twice in a row isn't hidden as a duplicate event
            MutableIdentifierCollection identifiers = new MutableIdentifierCollection(getIdentifierCollection().getIdentifiers());
            identifiers.remove(IdentifierClass.USER);
            broadcast(DecodeEvent.builder(DecodeEventType.PAGE, pageMessage.getTimestamp())
                    .protocol(Protocol.TWO_TONE)
                    .channel(getCurrentChannel())
                    .details(details)
                    .identifiers(identifiers)
                    .build());

            synchronized(mRecentPages)
            {
                mRecentPages.addFirst(new SimpleDateFormat("HH:mm:ss").format(new Date(pageMessage.getTimestamp())) +
                        "  " + details);
                while(mRecentPages.size() > RECENT_PAGES)
                {
                    mRecentPages.removeLast();
                }
            }

            if(mPagingPreference != null && mPagingPreference.isSendToTranscriber())
            {
                sendToTranscriber(pageMessage, entry);
            }
        }
    }

    private void sendToTranscriber(TwoTonePageMessage message, TwoToneTable.Entry entry)
    {
        String channel = null;
        String system = null;
        String site = null;
        long frequency = getCurrentFrequency();

        for(Identifier identifier : getIdentifierCollection().getIdentifiers())
        {
            if(identifier instanceof ChannelNameConfigurationIdentifier id)
            {
                channel = id.getValue();
            }
            else if(identifier instanceof SystemConfigurationIdentifier id)
            {
                system = id.getValue();
            }
            else if(identifier instanceof SiteConfigurationIdentifier id)
            {
                site = id.getValue();
            }
            else if(identifier instanceof FrequencyConfigurationIdentifier id && frequency <= 0)
            {
                frequency = id.getValue();
            }
        }

        TwoTonePageSender.send(mPagingPreference.getTranscriberUrl(), TwoTonePageSender.toJson(message.getPage(),
                message.getTimestamp(), entry, channel, system, site, frequency, mPagingPreference.getTranscriberApiKey()));
    }

    @Override
    public void receiveDecoderStateEvent(DecoderStateEvent event)
    {
        if(event.getEvent() == DecoderStateEvent.Event.REQUEST_RESET)
        {
            resetState();
        }
    }

    @Override
    public String getActivitySummary()
    {
        StringBuilder sb = new StringBuilder();
        sb.append("=============================\n");
        sb.append("Decoder:\tTwo-Tone Paging\n\n");
        sb.append("Recent Pages\n");

        synchronized(mRecentPages)
        {
            if(mRecentPages.isEmpty())
            {
                sb.append("  None\n");
            }
            else
            {
                mRecentPages.forEach(page -> sb.append("  ").append(page).append("\n"));
            }
        }

        if(mPagingPreference != null)
        {
            sb.append("\nStation list: ").append(mPagingPreference.getTablePath()).append("\n");
        }

        return sb.toString();
    }
}
