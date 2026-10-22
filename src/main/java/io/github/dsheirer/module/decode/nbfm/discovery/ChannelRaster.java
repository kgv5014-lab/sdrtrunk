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
package io.github.dsheirer.module.decode.nbfm.discovery;

/**
 * Channel frequency raster (grid) used to snap a measured carrier frequency to the nearest valid channel frequency.
 *
 * Channel frequencies are: offset + (n * step) for any integer n.
 */
public record ChannelRaster(long step, long offset)
{
    /**
     * VHF high band (136-174 MHz).  Public safety VHF channels in the US sit on a 2.5 kHz raster after narrowbanding
     * (e.g. 155.7525, 154.4300, 151.1225 MHz).
     */
    public static final ChannelRaster VHF_HIGH = new ChannelRaster(2500, 0);

    /**
     * VHF low band (25-54 MHz).  Public safety low band channels are on a 20 kHz raster, but many users are on 5 kHz.
     */
    public static final ChannelRaster VHF_LOW = new ChannelRaster(5000, 0);

    /**
     * UHF, 220 MHz, 700 and 800 MHz bands use 6.25 kHz or 12.5 kHz rasters.  6.25 kHz covers both.
     */
    public static final ChannelRaster UHF = new ChannelRaster(6250, 0);

    /**
     * Fallback raster for any other band.
     */
    public static final ChannelRaster DEFAULT = new ChannelRaster(5000, 0);

    /**
     * Constructs an instance
     * @param step between channels in Hertz
     * @param offset of the raster from zero Hertz
     */
    public ChannelRaster
    {
        if(step <= 0)
        {
            throw new IllegalArgumentException("Channel raster step must be positive: " + step);
        }
    }

    /**
     * Default channel raster for the band that contains the frequency.
     * @param frequency in Hertz
     * @return raster for the band
     */
    public static ChannelRaster forFrequency(long frequency)
    {
        if(frequency >= 25_000_000 && frequency < 54_000_000)
        {
            return VHF_LOW;
        }
        else if(frequency >= 136_000_000 && frequency < 174_000_000)
        {
            return VHF_HIGH;
        }
        else if(frequency >= 216_000_000 && frequency < 1_000_000_000)
        {
            return UHF;
        }

        return DEFAULT;
    }

    /**
     * Snaps the frequency to the nearest channel frequency on this raster.
     * @param frequency in Hertz
     * @return nearest raster frequency in Hertz
     */
    public long snap(double frequency)
    {
        return offset + Math.round((frequency - offset) / step) * step;
    }

    /**
     * Distance from the frequency to the nearest raster frequency.
     * @param frequency in Hertz
     * @return absolute error in Hertz
     */
    public double error(double frequency)
    {
        return Math.abs(frequency - snap(frequency));
    }
}
