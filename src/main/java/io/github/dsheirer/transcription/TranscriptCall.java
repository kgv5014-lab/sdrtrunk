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

package io.github.dsheirer.transcription;

import java.util.List;

/**
 * One call from the local transcription service's call log (transcriber/ in this repository).
 *
 * @param id call log id
 * @param callTime call start, seconds since epoch
 * @param talkgroup talkgroup value
 * @param talkgroupLabel talkgroup alias, may be null
 * @param source radio ID, may be null
 * @param talkerAlias talker alias, may be null
 * @param transcript transcript text, may be null while the call is queued
 * @param status queued, processing, done, skipped or failed
 * @param error reason for skipped or failed calls, may be null
 * @param durationSeconds audio length, may be null
 * @param codes labels for the radio codes, call signs and times found in the transcript
 */
public record TranscriptCall(long id, long callTime, String talkgroup, String talkgroupLabel, String source,
                             String talkerAlias, String transcript, String status, String error,
                             Double durationSeconds, List<CodeLabel> codes)
{
    /**
     * A radio code, call sign or time found in a transcript, with what it means.
     */
    public record CodeLabel(String code, String meaning) {}

    /**
     * Talkgroup alias when there is one, else the talkgroup value.
     */
    public String talkgroupDisplay()
    {
        if(talkgroupLabel != null && !talkgroupLabel.isBlank())
        {
            return talkgroupLabel + " (" + talkgroup + ")";
        }

        return talkgroup != null ? talkgroup : "";
    }

    /**
     * Talker alias when there is one, else the radio ID.
     */
    public String sourceDisplay()
    {
        if(talkerAlias != null && !talkerAlias.isBlank())
        {
            return source != null && !source.isBlank() ? talkerAlias + " (" + source + ")" : talkerAlias;
        }

        return source != null ? source : "";
    }

    /**
     * Transcript, or a short note about why there isn't one yet.
     */
    public String transcriptDisplay()
    {
        if(status == null)
        {
            return "";
        }

        return switch(status)
        {
            case "done" -> transcript != null && !transcript.isBlank() ? transcript : "(no speech)";
            case "skipped" -> "(not transcribed: " + (error != null ? error : "skipped") + ")";
            case "failed" -> "(transcription failed" + (error != null ? ": " + error : "") + ")";
            default -> "(" + status + "...)";
        };
    }

    /**
     * Code labels joined for display, e.g. "10-23 Arrived on scene · 2081 Easton Fire/EMS, Ambulance"
     */
    public String codesDisplay()
    {
        if(codes == null || codes.isEmpty())
        {
            return "";
        }

        StringBuilder sb = new StringBuilder();

        for(CodeLabel label : codes)
        {
            if(!sb.isEmpty())
            {
                sb.append("  ·  ");
            }

            sb.append(label.code()).append(" ").append(label.meaning());
        }

        return sb.toString();
    }
}
