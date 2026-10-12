# SOP: Crexi Property Records Export for Walk-Up Finder

Goal: get every multifamily property near Springfield, OH into `data/crexi/` as CSVs, without spending owner-contact credits yet.

1. In Crexi, go to **Comps & Records → Property Records** (the off-market database, not Sale or Marketplace).
2. **Location:** draw a ~55–60 mile radius around Springfield, OH, or go county by county using the Core and Edge lists in `PRD.md` Section 14.
3. **Filters:** Property type = Multifamily. Year built = 1970+. **Do not** filter on stories (the tool uses that data).
4. **Run a second export with no year-built filter.** County records often leave year built blank, and those rows would be lost.
5. **Columns to include:** address, city, state, zip, latitude, longitude, year built, units, stories, building sq ft, assessed/market value, parcel/APN, owner name, owner mailing address, Crexi ID/URL.
6. **Export CSV.** Name it `crexi_[county-or-radius]_[1970plus|allyears]_[YYYY-MM-DD].csv` and drop it in `data/crexi/`.
7. **Save the search** in Crexi so it can be re-run.
8. **Do not** spend Data Enrichment or skip-trace credits yet. Credits get spent in step 9 of the pipeline, top-down on the ranked list.
9. Note the total lead count and which of the step 5 columns were actually available. Tell Claude Code.
