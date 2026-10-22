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

import io.github.dsheirer.identifier.Identifier;
import io.github.dsheirer.message.Message;
import io.github.dsheirer.protocol.Protocol;
import java.util.Collections;
import java.util.List;
import java.util.Locale;

/**
 * Two-tone page detected in channel audio.
 */
public class TwoTonePageMessage extends Message
{
    private final TwoToneDetector.Page mPage;

    public TwoTonePageMessage(TwoToneDetector.Page page, long timestamp)
    {
        super(timestamp);
        mPage = page;
    }

    public TwoToneDetector.Page getPage()
    {
        return mPage;
    }

    @Override
    public String toString()
    {
        return "TWO-TONE PAGE " + TwoToneTable.describe(mPage.toneA(), mPage.toneB()) +
            String.format(Locale.US, " (tone A %.1f s)", mPage.toneASeconds());
    }

    @Override
    public boolean isValid()
    {
        return true;
    }

    @Override
    public Protocol getProtocol()
    {
        return Protocol.TWO_TONE;
    }

    @Override
    public List<Identifier> getIdentifiers()
    {
        return Collections.emptyList();
    }
}
