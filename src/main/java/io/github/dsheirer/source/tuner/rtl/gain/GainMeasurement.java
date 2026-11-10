/*
 * *****************************************************************************
 * Copyright (C) 2014-2022 Dennis Sheirer
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
package io.github.dsheirer.source.tuner.rtl.gain;

/**
 * What the dongle heard at one gain setting during a gain sweep.
 *
 * @param gain in tenths of a dB
 * @param floorDb noise floor of the tuner's pass band, dB relative to full scale per FFT bin
 * @param clipFraction fraction of samples at the limit of the analog to digital converter, 0 to 1
 * @param snrDb signal above the noise floor for each target frequency, dB.  NaN where it couldn't be measured.
 */
public record GainMeasurement(int gain, double floorDb, double clipFraction, double[] snrDb)
{
    /**
     * Indicates if the converter was overloaded at this gain.
     */
    public boolean overloaded()
    {
        return clipFraction > GainSelector.CLIP_LIMIT;
    }
}
