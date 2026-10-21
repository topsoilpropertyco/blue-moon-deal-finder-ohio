# Data Sources

This is the list of every free public data source the app can use to find who owns a property and how to reach them, in the order to try them. It covers the 25 Ohio counties and 3 Indiana counties in the market, and all asset classes (apartments, self-storage, warehouses, campgrounds and RV parks, mobile home parks, outdoor storage, raw land). Every link was checked on 2026-10-21. The app reads the same list from `config/sources.yaml`, so the DRILL screen opens these pages for you, already filled in with the property's parcel number, owner or address. Many county sites block computer programs but work fine in your browser, so the app gives you links to click and does not copy those sites.

## The prioritized list

Priority 1 means try it first. "Your OK" means it costs money and stays off until you approve it.

| Priority | Source | What it gives you | Cost | How the app uses it / DRILL stage | Link |
|---|---|---|---|---|---|
| 1 | Crexi export | Owner name, mailing address, sale and loan history | Your Crexi plan | Loaded on import. Records baseline | [crexi.com](https://www.crexi.com/) |
| 1 | Your own list | Other properties with the same owner name or mailing address | Free | Done for you. Portfolio match | (in the app) |
| 1 | County auditor parcel page | Owner of record, tax mailing address, last sale. In big counties, a rental flag | Free | Direct parcel link for 16 counties, search page for the rest. County auditor | See [By county](#by-county) |
| 1 | Ohio rental registration (ORC 5323.02) | For rentals, the owner's name, address and phone. For an LLC, a named member or manager with phone | Free | Online in Franklin and Montgomery (with phone), Hamilton and Delaware (no phone). By records request in Butler and Warren. Rental registration | [The law](https://codes.ohio.gov/ohio-revised-code/section-5323.02) |
| 1 | Ohio statewide parcel map | Every Ohio parcel: owner mailing address, land-use code, acreage, link to the auditor page. No owner name | Free | Absentee check, portfolio grouping, neighbors. Records baseline, portfolio match, neighbors | [Lookup map](https://geohio.maps.arcgis.com/apps/instant/lookup/index.html?appid=8bfcff44b9734007b4c8853a6a0b9e04) |
| 1 | HUD multifamily data | For HUD-assisted apartments: owner company, manager name, phone and email | Free | Link lists HUD properties in the ZIP. On-site and manager contacts | [HUD Section 8 database](https://www.hud.gov/hud-partners/multifamily-assist-section8-database) |
| 1 | EPA public water systems | Operator name, phone and email for parks and campgrounds with their own well | Free | Link lists water systems in the city. On-site contacts (campgrounds, mobile home parks) | [EPA Envirofacts](https://www.epa.gov/enviro/envirofacts-data-service-api) |
| 1 | Indiana sales disclosure files | Every Indiana sale since 2008 with buyer and seller names and addresses | Free | Indiana owner names. Records baseline (IN) | [STATS Indiana](https://www.stats.indiana.edu/about/sdf.asp) |
| 1 | Indiana park and campground rosters | Licensed mobile home communities and campgrounds with licensee | Free | LLC and trust lookup, contacts (IN) | [Parks](https://secure.in.gov/health/eph/mobile-home-community-inspection-and-licensing-program/mhc-roster), [campgrounds](https://secure.in.gov/health/eph/recreational-vehicle-campgrounds/campground-roster/) |
| 1 | Google Maps listing | Leasing or park office phone, website, reviews | Free | Link from the address. On-site contacts | [Google Maps](https://www.google.com/maps) |
| 1 | National Do Not Call Registry | Which phones you may not cold-call | First 5 area codes free | Check before any call or text | [telemarketing.donotcall.gov](https://telemarketing.donotcall.gov/) |
| 2 | Ohio Secretary of State business search | LLC's statutory agent, organizer, filings | Free | LLC and trust lookup | [businesssearch.ohiosos.gov](https://businesssearch.ohiosos.gov/) |
| 2 | Indiana INBiz | Registered agent, principals; search by a person's name to find all their LLCs | Free | LLC and trust lookup (IN) | [INBiz](https://bsd.sos.in.gov/publicbusinesssearch) |
| 2 | County recorder | Deeds and mortgages: who signed for the LLC or trust | Free to search | Recorder documents | See [By county](#by-county) |
| 2 | County clerk of courts | Evictions (owner's attorney and manager), foreclosures, liens | Free | Court records | See [By county](#by-county) |
| 2 | County probate court | Estates, heirs, executors | Free | Court records | See [By county](#by-county) |
| 2 | County parcel map (GIS) | Parcel lines and the neighbors' parcels | Free | Neighbors | See [By county](#by-county) |
| 2 | HUD tax-credit (LIHTC) list | Owner contact person, company and phone for tax-credit apartments | Free | On-site contacts (apartments) | [HUD LIHTC](https://www.huduser.gov/portal/datasets/lihtc/property.html) |
| 2 | HIFLD park list | Name, address and office phone for 1,407 Ohio mobile home and RV parks | Free | On-site contacts | [Map service](https://services.arcgis.com/0ZRg6WRC7mxSLyKX/ArcGIS/rest/services/Mobile_Home_Parks/FeatureServer) |
| 2 | Ohio mobile home park licenses | Licensed operator and lot count. A new licensee means a recent sale | Free, by email request | LLC lookup, contacts | [Commerce](https://com.ohio.gov/divisions-and-programs/manufactured-homes-program/park-operators/mh-park-operator-license) |
| 2 | Ohio campground licenses | Licensed operator and lot count, held by each county health district | Free, by request | LLC lookup, contacts | [ODH campgrounds](https://odh.ohio.gov/know-our-programs/campgrounds) |
| 2 | Self Storage Association of Ohio | About 150 member owners with contact name and phone | Free | On-site contacts (self-storage) | [Member directory](https://www.ohiossa.org/Membership/Member-Directory) |
| 2 | Google search | Company sites, news, other properties | Free | Web and social search | [google.com](https://www.google.com/) |
| 2 | Free people-search sites | Phones, relatives, past addresses | Free, by hand only | Web and social search. Never automated | [FastPeopleSearch](https://www.fastpeoplesearch.com/) |
| 3 | OpenCorporates | Officers and related companies across states | Free website | LLC and trust lookup | [opencorporates.com](https://opencorporates.com/) |
| 3 | SEC EDGAR | Form D filings name the people who run investor-funded LLCs | Free | LLC lookup, deep research | [EDGAR](https://www.sec.gov/edgar/search/) |
| 3 | Ohio real estate license lookup | Whether a manager or broker is licensed, and their brokerage | Free | On-site and manager contacts | [Lookup](https://elicense3.com.ohio.gov/Lookup/LicenseLookup.aspx) |
| 3 | Delinquent-tax lists | Owners behind on taxes (a motivated-seller sign) | Free | Court records, letters | See [By county](#by-county) |
| 3 | Sheriff's sale auctions | Foreclosure sales with case number and lender | Free | Court records | Ohio: `https://<county>.sheriffsaleauction.ohio.gov/` |
| 3 | Obituary search | Owner died: heirs, spouse, funeral home | Free | Court records, deep research | [Legacy.com](https://www.legacy.com/obituaries/search) |
| 3 | Ohio voter file | A person's full name, birth year and home address | Free download | Confirms the right person; letters | [Ohio SOS](https://www6.ohiosos.gov/ords/f?p=VOTERFTP:STWD) |
| 3 | FMCSA trucking company list | Trucking firms with yard address, phone and officer names | Free | Contacts (outdoor storage, warehouses) | [data.transportation.gov](https://data.transportation.gov/d/az4n-8mr2) |
| 3 | EPA ECHO | Businesses with permits at an address (who occupies a warehouse or yard) | Free | Contacts (warehouses, outdoor storage) | [ECHO](https://echo.epa.gov/facilities/facility-search) |
| 3 | LinkedIn, Facebook | Job, messaging | Free (logged in) | Web and social search | [LinkedIn](https://www.linkedin.com/), [Facebook](https://www.facebook.com/) |
| 3 | Apartment listing sites | Manager and leasing phone | Free | On-site contacts | (Google search link) |
| 4 | Crexi owner-contact credit | Owner phone and email | 1 of your 500 monthly credits | Crexi credit, after the free stages | (inside Crexi, by hand) |
| 4 | Ohio UCC search | Lenders and related companies | Free | LLC lookup | [ucc.ohiosos.gov](https://ucc.ohiosos.gov/) |
| 4 | Better Business Bureau | Owner or president of management companies | Free | Contacts | [bbb.org](https://www.bbb.org/) |
| 4 | CourtListener / PACER | Federal cases and bankruptcies | Free / $0.10 a page | Court records | [CourtListener](https://www.courtlistener.com/) |
| 4 | Ohio EPA documents | Permits naming the operator and a contact | Free | Contacts | [eDocument search](https://edocpub.epa.ohio.gov/publicportal/edochome.aspx) |
| 4 | MHVillage | Park list with lot counts, sometimes a manager phone | Free (by hand) | Contacts (mobile home parks) | [mhvillage.com](https://www.mhvillage.com/parks/oh) |
| 4 | Ohio Housing Finance Agency | Owners and agents of OHFA-funded apartments | Free, by request | LLC lookup | [OHFA compliance](https://www.ohiohome.org/compliance) |
| 4 | Indiana license lookup | Indiana broker and manager licenses | Free | Contacts (IN) | [mylicense.in.gov](https://mylicense.in.gov/EVerification/Search.aspx) |
| 5 | Letters, neighbors, local brokers | Owners who avoid phones; people who know the owner | Free (postage) | Letters, neighbors | (by hand) |

These sources feed the app's other steps, not the DRILL:

| Source | What it gives you | Cost | Used for | Link |
|---|---|---|---|---|
| HUD Fair Market Rents | Rent by bedroom for each county and ZIP | Free | Rent check | [HUD FMR](https://www.huduser.gov/portal/datasets/fmr.html) |
| US Census geocoder | Map points for addresses | Free, no key | Drive time, map | [Census geocoder](https://geocoding.geo.census.gov/geocoder/) |
| Google Street View | Photos of the building | 10,000 free a month | Photo check | [Docs](https://developers.google.com/maps/documentation/streetview/request-streetview) |
| Google Routes | Drive time from Springfield | 10,000 free a month | Drive-time filter | [Docs](https://developers.google.com/maps/documentation/routes/compute_route_matrix) |
| Claude Code (`claude -p`) | Labels the photos | Your Claude plan | Photo check | [Docs](https://code.claude.com/docs/en/headless) |
| Census rents (ACS B25031) | Median rent by bedroom | Free, needs a free key | Rent cross-check (not wired in) | [Key sign-up](https://api.census.gov/data/key_signup.html) |
| Zillow rent index | Typical asking rent by ZIP (278 Ohio ZIPs) | Free with credit to Zillow | Rent trend (not wired in) | [Zillow data](https://www.zillow.com/research/data/) |

## By county

"Tested" means a parcel page loaded with the right owner. "County's own link" means the county's own map or data uses that exact link, but the site blocks programs, so it was opened only through the county's record card or not at all; it should work in your browser. "Search page" means you type the parcel number yourself.

| County | Auditor parcel link works? | Rental registration online? | Recorder | Probate | Clerk |
|---|---|---|---|---|---|
| Clark, OH | Yes, tested (16 digits, no dashes) | No. Springfield has a city registry with no public search | [PublicSearch](https://clark.oh.publicsearch.us/) | [Search](https://probate.clarkcountyohio.gov/recordSearch.php) | [eServices](https://eservices.clarkcountyohiocourt.com/eservices/home.page) |
| Champaign, OH | Yes, tested (dashed number) | No (under 200,000 people) | [CountyFusion](https://countyfusion9.kofiletech.us/countyweb/login.do?countyname=ChampaignOH) | [eServices](https://eservices.champaigncountyfamilycourt.com) | [eServices](https://eservices.champaignclerk.com) |
| Greene, OH | No, search page | No (under 200,000) | [PublicSearch](https://greene.oh.publicsearch.us/) | [JWorks](https://www.greenecountyohio.gov/2087/JWorks-Case-Search) | [eServices](https://courts.greenecountyohio.gov/eservices/) |
| Madison, OH | County's own link (dashed number) | No (under 200,000) | [PublicSearch](https://madison.oh.publicsearch.us/) | [Search](https://www.madison.oh.gov/departments/court_system/juvenile_probate/index_search.php) | [Search](https://clerk.co.madison.oh.us/) |
| Montgomery, OH | Yes, tested (number with spaces) | **Yes, with phone** (per parcel and free CSVs) | [RISS](https://riss.mcrecorder.org/) | [Search](https://go.mcohio.org/applications/probate/prodcfm/casesearchx.cfm) | [PRO](https://pro.mcohio.org/) |
| Miami, OH | County's own link (dashed number) | No (under 200,000) | [Fidlar](https://rep4laredo.fidlar.com/OHMiami/DirectSearch/#/search) | [CaseLook](https://probate.miamicountyohio.gov/recordSearch.php?k=searchForm5503) | [eServices](https://courts.miamicountyohio.gov/eservices/) |
| Logan, OH | No, search page | No (under 200,000) | [CountyFusion](https://countyfusion11.kofiletech.us/countyweb/loginDisplay.action?countyname=LoganOH) | [eServices](https://eservices.logancountycourts.com/eservices/home.page.2) | [eServices](https://eservices.logancountycourts.com/eservices/home.page.2) |
| Union, OH | County's own link (29-0006444.0000 form) | No (under 200,000) | [PaxWorld](https://www2.co.union.oh.us/PaxWorld5/) | [eServices](https://eservices.co.union.oh.us/eservices) | [eServices](https://eservices.co.union.oh.us/eservices) |
| Clinton, OH | County's own link (15 digits) | No (under 200,000) | [Fidlar AVA](https://ohclinton.fidlar.com/OHClinton/AvaWeb/#!/search) | [Search](https://www.clintoncountycourts.org/recordSearch.php?k=searchForm1403) | [Search](https://www.clintoncountycourts.org/recordSearch.php?k=searchForm1404) |
| Fayette, OH | No, search page | No (under 200,000) | [CountyFusion](https://countyfusion8.govos.com/countyweb/loginDisplay.action?countyname=FayetteOH) | [Search](https://courts.fayette-co-oh.com/) | [Search](https://courts.fayette-co-oh.com/) |
| Shelby, OH | No, search page | No. Sidney has a paper registry | [CountyFusion](https://countyfusion13.govos.com/countyweb/loginDisplay.action?countyname=ShelbyOH) | [CourtView](https://search.shelbyco.net/eservices/) | [CourtView](https://search.shelbyco.net/eservices/) |
| Franklin, OH | Yes, tested (any form) | **Yes, with phone** (Rental Contact tab) | [PublicSearch](https://franklin.oh.publicsearch.us/) | [Search](https://probate.franklincountyohio.gov/Record-Search/General-Case-Search) | [Case Info](https://fcdcfcjs.co.franklin.oh.us/CaseInformationOnline/) |
| Warren, OH | Yes, tested (10 digits) | No online lookup; ask the auditor (required here) | [Fidlar AVA](https://ohwarren.fidlar.com/OHWarren/AvaWeb/) | [Search](http://probate.co.warren.oh.us/search.php) | [Benchmark](https://clerkofcourt.co.warren.oh.us/BenchmarkCP/Home.aspx/Search) |
| Preble, OH | No, search page | No (under 200,000) | [CountyFusion](https://countyfusion2.govos.com/countyweb/loginDisplay.action?countyname=PrebleOH) | [Search](http://app.prebleohiojuvenileprobate.org/recordSearch.php?k=searchForm6803) | [eServices](https://pa.preblecountyohio.net/eservices/) |
| Auglaize, OH | Yes, tested (letter + 10 digits) | No (under 200,000) | [CountyFusion](https://countyfusion9.govos.com/countyweb/loginDisplay.action?countyname=AuglaizeOH) | [CourtView](https://dockets.auglaizecounty.org/eservicesJVPR/) | [CourtView](https://dockets.auglaizecounty.org/eservicesCRTV/) |
| Darke, OH | No, search page | No (under 200,000) | [Fidlar AVA](https://rep2laredo.fidlar.com/OHDarke/AvaWeb/) | [Search](https://www.darkeprobatejuvenile.org/recordSearch.php?k=searchForm1903) | [eServices](http://70.62.18.22/eservices/home.page.2) |
| Delaware, OH | Yes, tested (14 digits) | **Yes**, no phone (search and district lists) | [PaxWorld](https://delaware.dts-central-oh.com/PaxWorld/) | [eServices](https://court.co.delaware.oh.us/eservices/home.page.2) | [eServices](https://court.co.delaware.oh.us/eservices/home.page) |
| Pickaway, OH | No, search page | No (under 200,000) | [CountyFusion](https://countyfusion2.govos.com/countyweb/loginDisplay.action?countyname=PickawayOH) | [CaseLook](https://caselook.pickawaypjcourt.com/search/6503) | [eServices](https://eservices.pickawaycountycourts.com/eservices/home.page) |
| Butler, OH | Yes, tested (letter + 13 digits) | No online lookup; ask the auditor (required here) | [PublicSearch](https://butler.oh.publicsearch.us/) | [CaseLook](https://probatecourt.bcohio.gov/recordSearch.php?k=searchForm0909) | [eServices](https://clerkservices.bcohio.gov/eservices) |
| Hamilton, OH | Yes, tested (13 digits) | **Yes**, flag on each parcel + free CSV, no phone | [Acclaim](https://acclaim-web.hamiltoncountyohio.gov/AcclaimWebLive) | [Search](https://www.probatect.org/court-records/court-record-search) | [Court Clerk](https://www.courtclerk.org/records-search/) |
| Hardin, OH | Yes, tested (12 digits) | No (under 200,000) | [CountyFusion](https://countyfusion14.kofiletech.us/countyweb/loginDisplay.action?countyname=HardinOH) | [Search](http://hardincourts.com:8888/recordSearch) (did not answer) | [CaseLook](http://www.hardincourts.com/recordSearch.php?k=searchForm3301) |
| Highland, OH | No, search page | No (under 200,000) | [CountyFusion](https://countyfusion7.kofiletech.us/countyweb/login.do?countyname=HighlandOH) | [CaseLook](https://highlandjpcourt.com/recordSearch.php?k=searchForm3602) | [eAccess](https://eaccess.hccpc.org/eservices/home.page) |
| Allen, OH | No, search page | No (under 200,000) | [PaxWorld](https://recorderexternal.allencountyohio.com/paxworld) (free account) | [CourtView](http://courtvweb.allencountyohio.com/eservices/home.page.2) | [CourtView](http://courtvweb.allencountyohio.com/eservices) |
| Marion, OH | No, search page | No (under 200,000) | [Fidlar AVA](https://rep3laredo.fidlar.com/OHMarion/AvaWeb/#/search) | [CourtView](https://courtrecords.co.marion.oh.us/eservices/home.page.9) | [CourtView](https://courtrecords.co.marion.oh.us/eservices) |
| Ross, OH | No, search page | No (under 200,000) | [CountyFusion](https://countyfusion9.kofiletech.us/countyweb/login.do?countyname=RossOH) | [eAccess](https://eaccess.co.ross.oh.us/eservices/) | [eAccess](https://eaccess.co.ross.oh.us/eservices/) |
| Wayne, IN | Yes, tested (property record card PDF) | No (Indiana has none) | [Doxpop](https://www.doxpop.com/prod/in/recorder/) (paid account) | [MyCase](https://public.courts.in.gov/mycase/) | [MyCase](https://public.courts.in.gov/mycase/) |
| Union, IN | No, county GIS search | No (Indiana has none) | [Doxpop](http://www.doxpop.com) (paid account) | [MyCase](https://public.courts.in.gov/mycase/) | [MyCase](https://public.courts.in.gov/mycase/) |
| Randolph, IN | Yes, tested (18 digits) | No (Indiana has none) | [Doxpop](http://www.doxpop.com) (paid account) | [MyCase](https://public.courts.in.gov/mycase/) | [MyCase](https://public.courts.in.gov/mycase/) |

Notes on the table:

- **Rental registration.** Ohio's law only covers counties with more than 200,000 people ([ORC 5323.01](https://codes.ohio.gov/ohio-revised-code/section-5323.01)). In this market that is Franklin, Hamilton, Montgomery, Butler, Warren and Delaware. Filings are public records, so where there is no online lookup you can email the auditor and ask for the filing for a parcel.
- **Sheriff's sales.** All 25 Ohio counties run online auctions at `https://<county>.sheriffsaleauction.ohio.gov/` (for example [Clark](https://clark.sheriffsaleauction.ohio.gov/)). Indiana counties were not checked.
- **Evictions** are usually in a city or municipal court, not the clerk of courts. `config/sources.yaml` has the municipal court link for each county where one was found.
- **Free bulk owner files.** Most counties publish a free file or map service with owner names and mailing addresses: Champaign, Greene, Madison, Miami, Logan, Union, Clinton, Fayette, Shelby, Franklin, Warren, Butler, Pickaway and Wayne (IN). Hamilton publishes a list of every registered rental (about 30,000) and Montgomery publishes rental lists with phone numbers. The links are under `bulk_data` in `config/sources.yaml`. A later step could load these automatically.
- **Best counties.** Montgomery and Franklin: direct parcel links and rental contacts with phone. **Hardest:** Clark (the main county) has no free bulk file and its auditor site blocks programs, so open the links by hand.

## Paid options (off)

None of these are turned on. Each needs your OK before the app uses it (house rule).

| Option | What it adds | Price (checked 2026-10-21) |
|---|---|---|
| Paid skip trace (BatchSkipTracing, PropStream, REISkip) | Phones and emails for owners the free stages missed | Vendor pricing; DRILL stage is off |
| Google Places phone lookup | Leasing-office phone for a property, automatically | 1,000 free a month, then $20 per 1,000 ([pricing](https://developers.google.com/maps/billing-and-pricing/pricing)) |
| Google Geocoding | Map points when the free Census geocoder misses | 10,000 free a month, then $5 per 1,000 |
| Google Maps Static | Satellite picture for the photo check | 10,000 free a month, then $2 per 1,000 |
| Anthropic API | Photo check without your Claude plan | Pay per use |
| Phone line type (Twilio Lookup, NumVerify) | Mobile or landline | Twilio $0.008 a number; NumVerify 100 free a month, then $14.99 a month |
| Email checkers (ZeroBounce, Hunter) | Whether an email works | ZeroBounce 100 free a month; Hunter 50 free credits a month |
| More Do Not Call area codes | Scrub numbers beyond 5 area codes | $85 per extra area code a year |
| Indiana Do Not Call list | Required to call Indiana consumers | $750 a year |
| FCC Reassigned Numbers Database | Was this number given to someone new? | Paid; only matters if you hold consent |
| OpenCorporates API | Automatic company lookups | From GBP 2,250 a year |
| Indiana INBiz bulk file | Every Indiana company | $9,500 plus $500 a month |
| PACER | Federal court documents | $0.10 a page, free under $30 a quarter |
| Recorder images (Fidlar Laredo, Doxpop, some CountyFusion) | Full deed and mortgage images | Varies by county |
| Brave Search API | Automatic web search | About $5 of free credit a month, card required |

## Technical notes

### Google prices and free tiers

Since March 1, 2025, Google gives a free amount for each service every month instead of a $200 credit. Source: [Google Maps pricing](https://developers.google.com/maps/billing-and-pricing/pricing), checked 2026-10-21.

| Google service | Free each month | Then |
|---|---|---|
| Geocoding | 10,000 | $5.00 per 1,000 |
| Routes, basic drive times (Essentials) | 10,000 pairs | $5.00 per 1,000 |
| Routes with live traffic (Pro) | 5,000 pairs | $10.00 per 1,000 |
| Street View image | 10,000 | $7.00 per 1,000 |
| Street View metadata ("is there a photo here?") | Unlimited | Free |
| Maps Static (satellite image) | 10,000 | $2.00 per 1,000 |
| Places phone number (Place Details Enterprise) | 1,000 | $20.00 per 1,000 |
| Places search returning only an ID | Unlimited | Free |

Example: 1,000 properties with 3 photos each is 3,000 images, well inside the free 10,000. A billing account is still required. Set a daily cap on each API in Google Cloud Console.

Google's terms let the app keep the photo ID (pano_id) and place ID forever, but other Google results (map points, phone numbers, images) only for a while (map points 30 days). That clashes with the house rule "cache everything". The safe pattern: keep Census map points forever, keep Google IDs and the photo-check labels, and let other Google data expire.

### Routes limits

- One request can hold up to 625 origin-destination pairs. With one origin (Springfield) that is 625 properties per request.
- With live traffic, the limit drops to 100 pairs per request.
- If you send addresses instead of map points, a request can only hold 50 places in total. The app sends map points.
- The speed limit is 3,000 pairs a minute.
- Basic drive times (no traffic) stay on the cheaper Essentials price. Source: [Routes usage and billing](https://developers.google.com/maps/documentation/routes/usage-and-billing).

### Street View

- Always ask the free metadata service first. It says whether a photo exists and when it was taken, and costs nothing. Source: [Street View metadata](https://developers.google.com/maps/documentation/streetview/metadata).
- Images are at most 640 x 640. Without `return_error_code=true`, a missing photo comes back as a plain gray picture you would pay for.
- Point the camera at the building: the heading is the direction from the photo spot to the property.

### HUD rents

- The newest rents are fiscal year 2027, in effect since October 1, 2026.
- The app uses HUD's free map service by default. As of 2026-10-21 it still shows fiscal year 2026 rents. The app warns when that happens.
- For 2027 rents now, download HUD's free Excel files and put them in `data/hud/` (the app's `hud_file` option): [county FMRs](https://www.huduser.gov/portal/datasets/fmr/fmr2027/FY27_FMRs.xlsx) and [ZIP-level SAFMRs](https://www.huduser.gov/portal/datasets/fmr/fmr2027/FY27_safmrs.xlsx). These downloads worked without a key earlier on 2026-10-21, but huduser.gov returned errors (503) to our server later that day, so download them in your browser.
- The HUD API needs a free account and token, allows 60 requests a minute, and requires the line "This product uses the HUD User Data API but is not endorsed or certified by HUD User." Source: [HUD FMR API](https://www.huduser.gov/portal/dataset/fmr-api.html).

### Census

- **Geocoder:** free, no key, and the results are public domain, so they can be kept forever. Batch files can hold up to 10,000 addresses and 5 MB. Results list longitude before latitude. Source: [Census geocoder](https://geocoding.geo.census.gov/geocoder/).
- **Rent data (American Community Survey):** since May 12, 2026 every request needs a free key. The newest 5-year data covers 2020-2024. Table B25031 gives median rent by bedroom count. [Get a key](https://api.census.gov/data/key_signup.html).

### Claude Code headless (the photo check)

- The app runs `claude -p "<prompt>"` on the Mac Mini. With `--output-format json --json-schema '<schema>'`, the answer comes back in a `structured_output` field. If that field is missing, the run failed or Claude declined, so the app must check for it.
- Useful options: `--tools Read` (Claude can only look at files), `--permission-mode dontAsk` (never stops to ask), `--max-turns 3`, `--no-session-persistence`, `--model sonnet`.
- **To bill your Claude plan, not an API key:** sign in once with `claude` and `/login` (or use a token from `claude setup-token`), and make sure `ANTHROPIC_API_KEY` is not set when the app calls Claude. If that key is set, Claude Code always uses it and bills the API.
- Do not use `--bare`: it skips your plan's sign-in and needs an API key.
- Sources: [headless mode](https://code.claude.com/docs/en/headless), [CLI reference](https://code.claude.com/docs/en/cli-reference), [sign-in](https://code.claude.com/docs/en/authentication).

### Do Not Call rules (plain English, not legal advice)

Have a lawyer review any calling or texting program before you start.

1. **National list.** Before calling or texting owners, scrub your numbers against the [National Do Not Call Registry](https://telemarketing.donotcall.gov/). Organizations register to download by area code. The first 5 area codes are free; each extra one is $85 a year (fiscal 2027). Use a download no older than 31 days. The market's area codes are 937/326, 614/380, 513/283, 419/567, 740/220 in Ohio and 765 in Indiana, so 5 free codes will not cover all of them; pick the target counties' codes first.
2. **Hours.** No sales calls to homes before 8 a.m. or after 9 p.m. in the owner's time zone. Keep your own do-not-call list and honor every opt-out. A landlord's personal cell phone is usually treated as a home line. ([47 CFR 64.1200](https://www.ecfr.gov/current/title-47/chapter-I/subchapter-B/part-64/subpart-L/section-64.1200))
3. **Is "I'd like to buy your property" a sales call?** Courts disagree. In June 2026 the Ninth Circuit said such calls and texts can count ([Coffey v. Fast Easy Offer](https://cdn.ca9.uscourts.gov/datastore/opinions/2026/06/04/25-4066.pdf)). Ohio and Indiana courts are not bound by it, but treat these calls as covered and scrub against the list.
4. **Robocalls and AI voices.** Prerecorded messages, ringless voicemail and AI voices need the owner's prior written consent. Texts you send by hand from a list are usually fine. Autodialers are narrowly defined since the 2021 Supreme Court case Facebook v. Duguid.
5. **Ohio.** No separate state list to buy; national-list numbers are on Ohio's list automatically. Say your name and purpose in the first 60 seconds and never block caller ID. Ohio's Telephone Solicitation Sales Act may require registration and a bond; check the exemptions. ([Ohio Attorney General](https://ohioattorneygeneral.gov/Business/Services-for-Business/Business-Guide/Telemarketing-and-Do-Not-Call-Registry))
6. **Indiana.** Indiana has its own list ($750 a year) and requires callers to register with the Attorney General first. ([Indiana Attorney General](https://www.in.gov/attorneygeneral/consumer-protection-division/id-theft-prevention/do-not-call/telephone-solicitors/))
7. **Letters are the low-risk channel.** The app's DRILL keeps a "do not contact" outcome; use it whenever someone asks.

### Sites that block programs

These work in your browser but refuse computer programs, so the app only links to them: the Ohio Secretary of State (business search, UCC, voter file), INBiz, OpenCorporates, people-search sites, Legacy.com, BBB profiles, Crexi, and the auditor sites on Pivot Point, ISSG and Beacon (Clark, Champaign, Madison, Miami, Greene, Logan, Clinton, Fayette, Shelby, Preble, Darke, Pickaway, Highland, Allen, Marion, Ross, Union). Several of their robots.txt files forbid automated collection. Never automate Crexi or people-search sites (house rule).

## What we could not verify

- **No direct parcel link** for 12 counties: Greene, Logan, Fayette, Shelby, Preble, Darke, Pickaway, Highland, Allen, Marion, Ross and Union (IN). Greene, Logan, Fayette and Shelby link parcels by an internal ID instead of the parcel number. The likely links for Preble, Darke and Pickaway are in `config/sources.yaml`; try one in your browser and tell me if it works.
- **County's own links not opened here:** Madison, Miami, Union (OH) and Clinton. The counties publish them, but their sites block programs. Open one parcel in each to confirm.
- **Parcel number format from Crexi.** Montgomery needs spaces (R72 04407 0002). Champaign, Madison, Miami and Union need the dashes and dots the county uses. If Crexi writes them differently, those links will say "No data" and you fall back to the search page. A sample Crexi export would settle this.
- **Links that will age:** Warren's link holds a tax-year code that may change next year. Wayne's record-card link has 2026 in it.
- **Not checked:** Clark's auditor map and municipal court search; Greene's probate search; Hamilton's probate search; Hardin's probate search (no answer on port 8888); whether Marion's court search includes estates; several municipal courts (Xenia, Fairborn, Beavercreek, Bellefontaine, Kettering, Vandalia, Miamisburg, Hamilton, Middletown, Fairfield, Oxford, Greenfield); Indiana sheriff sales.
- **City rental and code programs** were only researched for the county seats. Springfield and Sidney run rental registries with no public search. Other cities may have programs we did not find.
- **Statewide parcel map** data is mostly from 2023, so mailing addresses can be stale.
- **HUD USER (huduser.gov)** answered earlier in the day, then returned errors (503). HUD's map service (what the app uses by default) still answered.
- **Indiana sales disclosure** site failed a security check from our server; download it in your browser.
- **Ohio voter file** rules for business use were not checked.
- **Ohio Campground Owners Association** ([ohiocampers.com](https://ohiocampers.com/)) did not answer from our server.
- **Montgomery rental CSVs** say they refresh twice a month, but all files were dated October 1 when checked on October 21.
- **Correction to earlier notes:** Fayette's rental filings were described as "on paper under ORC 5323.02". The law only applies to counties over 200,000 people, so it does not apply in Fayette (about 29,000).
