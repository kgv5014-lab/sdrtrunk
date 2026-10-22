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

package io.github.dsheirer.service.radioreference;

import io.github.dsheirer.preference.UserPreferences;
import io.github.dsheirer.preference.radioreference.RadioReferencePreference;
import io.github.dsheirer.rrapi.RadioReferenceService;
import io.github.dsheirer.rrapi.type.Agency;
import io.github.dsheirer.rrapi.type.AgencyInfo;
import io.github.dsheirer.rrapi.type.Category;
import io.github.dsheirer.rrapi.type.CountyInfo;
import io.github.dsheirer.rrapi.type.Frequency;
import io.github.dsheirer.rrapi.type.SubCategory;
import java.util.List;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.LongFunction;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Names frequencies using the Radio Reference listings for the user's preferred county.  The county's frequency lists
 * are downloaded once in the background; {@link #describe(long)} only reads that local copy and never blocks.
 */
public class RadioReferenceFrequencyLookup implements LongFunction<String>
{
    private static final Logger mLog = LoggerFactory.getLogger(RadioReferenceFrequencyLookup.class);

    private final RadioReference mRadioReference;
    private final UserPreferences mUserPreferences;
    private final FrequencyLabelIndex mIndex = new FrequencyLabelIndex();
    private final AtomicBoolean mLoading = new AtomicBoolean();
    private volatile boolean mReady;
    private volatile String mStatus = "Not loaded";

    public RadioReferenceFrequencyLookup(RadioReference radioReference, UserPreferences userPreferences)
    {
        mRadioReference = radioReference;
        mUserPreferences = userPreferences;
    }

    /**
     * Label for the frequency, or null if unknown or the listings haven't loaded.
     */
    public String describe(long hertz)
    {
        return mIndex.describe(hertz);
    }

    @Override
    public String apply(long hertz)
    {
        return describe(hertz);
    }

    /**
     * Indicates the county listings have been loaded.
     */
    public boolean isReady()
    {
        return mReady;
    }

    /**
     * Plain-words status for display, e.g. "1,204 frequencies loaded" or why nothing was loaded.
     */
    public String getStatus()
    {
        return mStatus;
    }

    /**
     * Downloads the preferred county's frequencies on a background thread.  Does nothing if a load is running.
     */
    public void refresh()
    {
        if(mLoading.compareAndSet(false, true))
        {
            Thread thread = new Thread(this::load, "sdrtrunk rr frequency lookup");
            thread.setDaemon(true);
            thread.start();
        }
    }

    private void load()
    {
        try
        {
            int countyId = mUserPreferences.getRadioReferencePreference().getPreferredCountyId();

            if(countyId == RadioReferencePreference.INVALID_ID)
            {
                mStatus = "Choose your county in the Radio Reference tab of the playlist editor first";
                return;
            }

            if(!mRadioReference.availableProperty().get())
            {
                mStatus = "Radio Reference isn't logged in";
                return;
            }

            mStatus = "Loading...";
            RadioReferenceService service = mRadioReference.getService();
            FrequencyLabelIndex fresh = new FrequencyLabelIndex();
            CountyInfo county = service.getCountyInfo(countyId);
            addCategories(service, fresh, county.getName(), county.getCategories());

            if(county.getAgencies() != null)
            {
                for(Agency agency: county.getAgencies())
                {
                    AgencyInfo info = service.getAgencyInfo(agency.getAgencyId());
                    addCategories(service, fresh, info.getName(), info.getCategories());
                }
            }

            mIndex.clear();
            copy(fresh);
            mReady = true;
            mStatus = String.format("%,d frequencies loaded for %s", mIndex.size(), county.getName());
        }
        catch(Exception e)
        {
            mLog.warn("Unable to load Radio Reference frequencies", e);
            mStatus = "Couldn't load Radio Reference: " + e.getMessage();
        }
        finally
        {
            mLoading.set(false);
        }
    }

    private void copy(FrequencyLabelIndex source)
    {
        mIndex.addAll(source);
    }

    private void addCategories(RadioReferenceService service, FrequencyLabelIndex index, String owner,
                               List<Category> categories) throws Exception
    {
        if(categories == null)
        {
            return;
        }

        for(Category category: categories)
        {
            if(category.getSubCategories() == null)
            {
                continue;
            }

            for(SubCategory subCategory: category.getSubCategories())
            {
                for(Frequency frequency: service.getSubCategoryFrequencies(subCategory.getSubCategoryId()))
                {
                    index.add(Math.round(frequency.getDownlink() * 1E6),
                        buildLabel(owner, subCategory.getName(), frequency));
                }
            }
        }
    }

    /**
     * Builds a label such as "County Fire - Dispatch".
     */
    static String buildLabel(String owner, String subCategory, Frequency frequency)
    {
        String detail = frequency.getDescription();

        if(detail == null || detail.isBlank())
        {
            detail = frequency.getAlphaTag();
        }

        if(detail == null || detail.isBlank())
        {
            detail = subCategory;
        }

        return owner + (detail == null || detail.isBlank() ? "" : " - " + detail.trim());
    }
}
