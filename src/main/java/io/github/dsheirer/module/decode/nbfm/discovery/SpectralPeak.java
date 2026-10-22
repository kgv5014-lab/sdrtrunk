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
 * A carrier found in a single averaged spectrum.
 *
 * @param frequency power-weighted centre frequency in Hertz
 * @param widthHz occupied width, measured 20 dB down from the peak (or at the detection edge, if that is higher)
 * @param peakDb peak bin level in dB (relative, uncalibrated)
 * @param snrDb peak level above the local noise floor in dB
 */
public record SpectralPeak(double frequency, double widthHz, double peakDb, double snrDb)
{
}
