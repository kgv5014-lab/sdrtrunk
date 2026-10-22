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

import io.github.dsheirer.message.IMessage;
import io.github.dsheirer.module.decode.Decoder;
import io.github.dsheirer.module.decode.DecoderType;
import io.github.dsheirer.sample.Listener;
import io.github.dsheirer.sample.real.IRealBufferListener;

/**
 * Auxiliary decoder for two-tone sequential (Quick Call II style) fire and EMS paging in NBFM channel audio.
 */
public class TwoToneDecoder extends Decoder implements IRealBufferListener, Listener<float[]>
{
    private final TwoToneDetector mDetector = new TwoToneDetector(this::pageDetected);

    @Override
    public DecoderType getDecoderType()
    {
        return DecoderType.TWO_TONE_PAGING;
    }

    @Override
    public Listener<float[]> getBufferListener()
    {
        return this;
    }

    @Override
    public void receive(float[] samples)
    {
        mDetector.receive(samples);
    }

    @Override
    public void reset()
    {
        super.reset();
        mDetector.flush();
    }

    private void pageDetected(TwoToneDetector.Page page)
    {
        Listener<IMessage> listener = getMessageListener();

        if(listener != null)
        {
            listener.receive(new TwoTonePageMessage(page, System.currentTimeMillis()));
        }
    }
}
