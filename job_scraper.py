"""
job_scraper.py — Real-time job scraping using Playwright + public ATS APIs

Job Boards  : MyCareersFuture, Indeed, JobStreet, LinkedIn, Glassdoor, Glints, Tech in Asia
Company Direct:
  Workday portal    → DBS
  Greenhouse API    → Anthropic
  Ashby API         → Airwallex, Wise, Nium, Thought Machine  (public JSON, no Playwright)
  Custom portal     → Grab, Sea Group
  Careers@Gov WD    → GovTech, HTX  (sggovterp.wd102.myworkdayjobs.com/PublicServiceCareers)
  MCF fallback      → OCBC, UOB, Standard Chartered, Citibank, HSBC, Revolut, MAS, IMDA, CSA, Singtel
"""

import asyncio
import xml.etree.ElementTree as _ET
import requests as _requests
from playwright.async_api import async_playwright

# ── Company groups shown in UI ───────────────────────────────────────────────
COMPANY_GROUPS = {
    "🏦 Banks":            ["DBS", "OCBC", "UOB", "Standard Chartered", "Citibank", "HSBC"],
    "💳 Fintechs":         ["Grab", "Sea Group", "Airwallex", "Wise", "Nium", "Revolut", "Thought Machine", "Thunes", "Stripe", "PayPal"],
    "🤖 AI Companies":     ["Anthropic"],
    "🏛️ Gov / Statutory":  ["GovTech", "HTX", "SNDGO", "DSTA", "MAS", "IMDA", "CSA",
                             "CPF Board", "HDB", "LTA", "MOF", "MOM", "ICA", "MOH Holdings", "Singtel"],
}

# ── Per-company scraping config ──────────────────────────────────────────────
COMPANY_CONFIG = {
    # Banks
    "DBS":                {"type": "dbs_workday"},
    "OCBC":               {"type": "mcf",        "mcf_name": "OCBC"},
    "UOB":                {"type": "mcf",        "mcf_name": "United Overseas Bank"},
    "Standard Chartered": {"type": "mcf",        "mcf_name": "Standard Chartered"},
    "Citibank":           {"type": "mcf",        "mcf_name": "Citi"},
    "HSBC":               {"type": "mcf",        "mcf_name": "HSBC"},
    # Fintechs / Tech
    "Grab":               {"type": "grab_direct"},
    "Sea Group":          {"type": "sea_direct"},
    "Airwallex":          {"type": "ashby",        "slug": "airwallex"},
    "Wise":               {"type": "mcf",          "mcf_name": "Wise"},
    "Nium":               {"type": "mcf",          "mcf_name": "Nium"},
    "Revolut":            {"type": "mcf",          "mcf_name": "Revolut"},
    "Thought Machine":    {"type": "ashby",        "slug": "thought-machine"},
    "Thunes":             {"type": "greenhouse",   "slug": "thunes"},
    "Stripe":             {"type": "greenhouse",   "slug": "stripe"},
    "PayPal":             {"type": "mcf",          "mcf_name": "PayPal"},
    # AI Companies
    "Anthropic":          {"type": "greenhouse",   "slug": "anthropic"},
    # Gov / Statutory Boards — Careers@Gov Workday portal
    "GovTech":            {"type": "careers_gov", "search_prefix": "GovTech"},
    "HTX":                {"type": "careers_gov", "search_prefix": "HTX"},
    "SNDGO":              {"type": "careers_gov", "search_prefix": "Smart Nation"},
    "DSTA":               {"type": "careers_gov", "search_prefix": "Defence Science and Technology Agency"},
    "CPF Board":          {"type": "careers_gov", "search_prefix": "Central Provident Fund"},
    "HDB":                {"type": "careers_gov", "search_prefix": "Housing & Development Board"},
    "LTA":                {"type": "careers_gov", "search_prefix": "Land Transport Authority"},
    "MOF":                {"type": "careers_gov", "search_prefix": "Ministry of Finance"},
    "MOM":                {"type": "careers_gov", "search_prefix": "Ministry of Manpower"},
    "ICA":                {"type": "careers_gov", "search_prefix": "Immigration & Checkpoints Authority"},
    "MOH Holdings":       {"type": "careers_gov", "search_prefix": "MOH Holdings"},
    # Gov — MCF fallback
    "MAS":                {"type": "mcf",         "mcf_name": "Monetary Authority of Singapore"},
    "IMDA":               {"type": "mcf",         "mcf_name": "Info-communications Media Development Authority"},
    "CSA":                {"type": "mcf",         "mcf_name": "Cyber Security Agency of Singapore"},
    "Singtel":            {"type": "mcf",         "mcf_name": "Singtel"},
}

# MCF positionLevel values (exact strings used in MCF URLs)
# Maps min-years-of-experience → list of MCF position levels to include
_YEARS_TO_MCF_LEVELS = {
    1:  ["Junior Executive", "Executive"],
    3:  ["Executive", "Senior Executive"],
    5:  ["Senior Executive", "Manager"],
    8:  ["Manager", "Middle Management", "Senior Management"],
    10: ["Senior Management"],
}


# ============================================================================
# MYCAREERS FUTURE — REST API (no Playwright needed)
# ============================================================================

_MCF_API = "https://api.mycareersfuture.gov.sg/v2/jobs"
_MCF_BASE = "https://www.mycareersfuture.gov.sg/job"

_MCF_EXP_MAP = {
    1: 3, 3: 3, 5: 4, 8: 5, 10: 6,   # MCF positionLevel IDs
}

def _call_mcf_api(keywords: str, company: str = None,
                  salary_min: int = 0, num_results: int = 10,
                  min_years: int = 0) -> list:
    """Call MCF REST API and return normalised job dicts."""
    import re as _re
    params = {
        "search":    keywords,
        "limit":     min(num_results, 100),
        "sortBy":    "new_posting_date",
        "employmentTypes": "Permanent",
    }
    if company:
        params["companyName"] = company
    if salary_min and salary_min > 0:
        params["salary"] = salary_min
    if min_years and min_years in _MCF_EXP_MAP:
        params["positionLevels"] = _MCF_EXP_MAP[min_years]

    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    try:
        resp = _requests.get(_MCF_API, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except Exception as e:
        print(f"  ❌ MCF API error: {e}")
        return []

    jobs = []
    for j in results:
        sal     = j.get("salary") or {}
        sal_min = sal.get("minimum", 0) or 0
        sal_max = sal.get("maximum", 0) or 0
        sal_str = (f"SGD {sal_min:,}–{sal_max:,}/mth"
                   if sal_min and sal_max else
                   f"SGD {sal_min:,}+/mth" if sal_min else "Not specified")

        meta    = j.get("metadata", {})
        url     = meta.get("jobDetailsUrl", "")
        company_name = (j.get("postedCompany") or {}).get("name", "Unknown")
        desc_html    = j.get("description", "")
        desc_clean   = _re.sub(r"<[^>]+>", " ", desc_html).strip()

        jobs.append({
            "company":     company_name,
            "title":       j.get("title", "Unknown Role"),
            "description": desc_clean,
            "salary":      sal_str,
            "location":    "Singapore",
            "url":         url,
            "source":      "MyCareersFuture",
        })
    print(f"  → {len(jobs)} jobs (MCF API{' · ' + company if company else ''})")
    return jobs


async def scrape_mycareersfuture(keywords: str, num_results: int = 10,
                                  salary_min: int = 0, salary_max: int = None,
                                  min_years: int = 0) -> list:
    """MCF general search via REST API."""
    return _call_mcf_api(keywords, salary_min=salary_min,
                         num_results=num_results, min_years=min_years)


async def _scrape_mcf_company(company: str, mcf_name: str, keywords: str,
                               num_results: int, salary_min: int = 0,
                               salary_max: int = None, min_years: int = 0) -> list:
    """MCF company-filtered search via REST API."""
    jobs = _call_mcf_api(keywords, company=mcf_name,
                         salary_min=salary_min, num_results=num_results,
                         min_years=min_years)
    for j in jobs:
        j["source"] = f"Direct:{company}"
    return jobs


def _parse_salary_range(salary_text: str) -> tuple[int, int]:
    """
    Parse a salary string into (min, max) monthly SGD integers.
    Returns (0, 0) if unparseable.
    Examples:
      '$6,500to$9,500'   → (6500, 9500)
      '$13,000to$16,000' → (13000, 16000)
      '$8,000'           → (8000, 8000)
    """
    import re
    nums = [int(n.replace(',', '')) for n in re.findall(r'[\d,]+', salary_text)
            if int(n.replace(',', '')) > 500]   # ignore noise like '30' days
    if not nums:
        return (0, 0)
    return (min(nums), max(nums))


async def _parse_mcf_page(page, num_results: int, source_label: str,
                           salary_min: int = 0, salary_max: int = None) -> list:
    jobs = []
    job_cards = await page.query_selector_all('[data-testid="job-card"]')
    if not job_cards:
        job_cards = await page.query_selector_all('.job-card, article, [class*="JobCard"]')
    print(f"  → {len(job_cards)} cards ({source_label})")

    skip_badges = ["TYPICALLY REPLIES", "ACTIVELY HIRING", "FAST RESPONSE", "NEW"]
    skip_meta   = ["Full Time", "Part Time", "Contract", "Central", "North", "South",
                   "East", "West", "Islandwide", "Years Exp", "Middle Management",
                   "Manager", "Senior", "Junior", "years experience", "Year(s)",
                   "Permanent", "Temporary", "Freelance", "months ago", "hour ago",
                   "day ago", "days ago", "week ago", "Apply Now"]

    for card in job_cards[:num_results]:
        try:
            link_el  = await card.query_selector('a[href*="/job/"]')
            all_text = await card.inner_text()
            lines    = [l.strip() for l in all_text.split('\n') if l.strip()]

            job_url = ""
            if link_el:
                href = await link_el.get_attribute("href")
                if href:
                    job_url = href if href.startswith("http") else f"https://www.mycareersfuture.gov.sg{href}"

            clean   = [l for l in lines if not any(b in l.upper() for b in skip_badges)]
            company = clean[0] if clean else "Unknown"
            title   = clean[1] if len(clean) > 1 else "Unknown"

            salary = "Not specified"
            for line in lines:
                if "$" in line or "SGD" in line.upper():
                    salary = line
                    break

            desc_lines  = [l for l in lines[2:6] if not any(k in l for k in skip_meta)]
            description = " ".join(desc_lines)[:500]

            if title and company:
                # Client-side salary filter (MCF server filter is unreliable)
                if salary != "Not specified" and (salary_min or salary_max):
                    lo, hi = _parse_salary_range(salary)
                    if lo or hi:
                        # Drop if the job's MAX salary is below what we want to earn
                        if salary_min and hi and hi < salary_min:
                            continue
                        # Drop if the job's MIN salary exceeds our budget ceiling
                        if salary_max and lo and lo > salary_max:
                            continue
                jobs.append({
                    "company":     company.strip(),
                    "title":       title.strip(),
                    "salary":      salary.strip(),
                    "location":    "Singapore",
                    "url":         job_url,
                    "description": description.strip(),
                    "source":      source_label,
                })
        except Exception as e:
            print(f"  ⚠️ MCF card error: {e}")
    return jobs


# ============================================================================
# WORKDAY — covers DBS, OCBC, UOB
# ============================================================================

async def _scrape_workday(company: str, base_url: str, keywords: str, num_results: int) -> list:
    jobs = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()
        try:
            query = keywords.replace(" ", "+")
            url = f"{base_url}?q={query}"
            print(f"  → Workday [{company}]: {url}")
            await page.goto(url, timeout=40000)

            # Workday needs extra time to hydrate React
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except:
                pass
            await page.wait_for_timeout(3000)

            # Strategy 1 — job title links (most reliable across Workday versions)
            title_links = await page.query_selector_all(
                'a[data-automation-id="jobPostingTitle"], '
                'a[href*="/job/"], '
                'li[class*="job"] a'
            )

            # Strategy 2 — fallback: any link containing /job/ in the URL
            if not title_links:
                title_links = await page.query_selector_all('a[href*="/job/"]')

            print(f"  → {len(title_links)} job links [{company}]")

            # Pass 1: collect title + url from listing page
            card_meta = []
            seen = set()
            for link in title_links[:num_results]:
                try:
                    title = (await link.inner_text()).strip()
                    if not title or len(title) < 3 or title in seen:
                        continue
                    seen.add(title)
                    href = await link.get_attribute("href") or ""
                    job_url = href if href.startswith("http") else f"{base_url.rstrip('/')}{href}"
                    location = "Singapore"
                    try:
                        parent = await link.evaluate_handle("el => el.closest('li') || el.parentElement")
                        loc_el = await parent.as_element().query_selector(
                            '[data-automation-id="city"], [data-automation-id="locations"], '
                            '[class*="location"], dd'
                        )
                        if loc_el:
                            location = (await loc_el.inner_text()).strip() or "Singapore"
                    except:
                        pass
                    card_meta.append({"title": title, "url": job_url, "location": location})
                except Exception as e:
                    print(f"  ⚠️ Workday link error [{company}]: {e}")

            # Pass 2: visit each job page to fetch full description
            print(f"  → Fetching descriptions for {len(card_meta)} {company} jobs…")
            desc_selectors = [
                '[data-automation-id="jobPostingDescription"]',
                '[class*="job-description"]',
                '[class*="jobDescription"]',
                'div[class*="description"]',
                'section[class*="description"]',
            ]
            for i, meta in enumerate(card_meta):
                description = ""
                try:
                    print(f"     [{i+1}/{len(card_meta)}] {meta['title'][:60]}")
                    detail_page = await context.new_page()
                    await detail_page.goto(meta["url"], timeout=30000)
                    try:
                        await detail_page.wait_for_load_state("networkidle", timeout=12000)
                    except:
                        pass
                    await detail_page.wait_for_timeout(2000)
                    for sel in desc_selectors:
                        el = await detail_page.query_selector(sel)
                        if el:
                            text = (await el.inner_text()).strip()
                            if len(text) > 80:
                                description = text
                                break
                    await detail_page.close()
                except Exception as e:
                    print(f"  ⚠️ Workday detail error: {e}")
                jobs.append({
                    "company":     company,
                    "title":       meta["title"],
                    "salary":      "Not specified",
                    "location":    meta["location"],
                    "url":         meta["url"],
                    "description": description,
                    "source":      f"Direct:{company}",
                })

            if not jobs:
                print(f"  ⚠️ Workday [{company}] returned 0 — falling back to MCF")

        except Exception as e:
            print(f"  ❌ Workday [{company}] error: {e}")
        finally:
            await browser.close()
    return jobs


# ============================================================================
# GRAB — grab.careers
# ============================================================================

async def _scrape_grab(keywords: str, num_results: int) -> list:
    jobs = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()
        try:
            query = keywords.replace(" ", "+")
            url = f"https://grab.careers/jobs/?country=Singapore&search={query}"
            print(f"  → Grab: {url}")
            await page.goto(url, timeout=30000)
            try:
                await page.wait_for_load_state("networkidle", timeout=12000)
            except:
                pass
            await page.wait_for_timeout(3000)

            job_cards = await page.query_selector_all(
                '[class*="JobCard"], [class*="job-card"], '
                '[data-testid*="job"], li[class*="Job"]'
            )
            if not job_cards:
                job_cards = await page.query_selector_all('li[class], div[class*="card"]')

            print(f"  → {len(job_cards)} cards (Grab)")

            for card in job_cards[:num_results]:
                try:
                    title_el    = await card.query_selector('h2, h3, [class*="title"], [class*="Title"]')
                    location_el = await card.query_selector('[class*="location"], [class*="Location"]')
                    link_el     = await card.query_selector('a')

                    title    = (await title_el.inner_text()).strip()    if title_el    else ""
                    location = (await location_el.inner_text()).strip() if location_el else "Singapore"

                    if not title:
                        continue

                    job_url = ""
                    if link_el:
                        href = await link_el.get_attribute("href") or ""
                        job_url = href if href.startswith("http") else f"https://grab.careers{href}"

                    jobs.append({
                        "company":     "Grab",
                        "title":       title,
                        "salary":      "Not specified",
                        "location":    location,
                        "url":         job_url,
                        "description": "",
                        "source":      "Direct:Grab",
                    })
                except Exception as e:
                    print(f"  ⚠️ Grab card error: {e}")

            if not jobs:
                print("  ⚠️ Grab returned 0 — site may have changed structure")

        except Exception as e:
            print(f"  ❌ Grab error: {e}")
        finally:
            await browser.close()
    return jobs


# ============================================================================
# SEA GROUP — career.sea.com
# ============================================================================

async def _scrape_sea(keywords: str, num_results: int) -> list:
    jobs = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()
        try:
            query = keywords.replace(" ", "+")
            url = f"https://career.sea.com/positions?team=&location=Singapore&search={query}"
            print(f"  → Sea Group: {url}")
            await page.goto(url, timeout=30000)
            try:
                await page.wait_for_load_state("networkidle", timeout=12000)
            except:
                pass
            await page.wait_for_timeout(3000)

            job_cards = await page.query_selector_all(
                '[class*="position-item"], [class*="PositionItem"], '
                '[class*="job-item"], div[class*="card"]'
            )
            print(f"  → {len(job_cards)} cards (Sea Group)")

            for card in job_cards[:num_results]:
                try:
                    title_el    = await card.query_selector('h3, h4, [class*="title"], [class*="Title"]')
                    location_el = await card.query_selector('[class*="location"], [class*="Location"]')
                    link_el     = await card.query_selector('a')

                    title    = (await title_el.inner_text()).strip()    if title_el    else ""
                    location = (await location_el.inner_text()).strip() if location_el else "Singapore"

                    if not title:
                        continue

                    job_url = ""
                    if link_el:
                        href = await link_el.get_attribute("href") or ""
                        job_url = href if href.startswith("http") else f"https://career.sea.com{href}"

                    jobs.append({
                        "company":     "Sea Group",
                        "title":       title,
                        "salary":      "Not specified",
                        "location":    location,
                        "url":         job_url,
                        "description": "",
                        "source":      "Direct:Sea Group",
                    })
                except Exception as e:
                    print(f"  ⚠️ Sea card error: {e}")

            if not jobs:
                print("  ⚠️ Sea Group returned 0 — site may have changed structure")

        except Exception as e:
            print(f"  ❌ Sea Group error: {e}")
        finally:
            await browser.close()
    return jobs


# ============================================================================
# DBS BANK — direct Workday portal (dbs.wd3.myworkdayjobs.com/DBS_Careers)
# Filters Singapore jobs by checking the /job/Singapore path in the href.
# Falls back to MCF if Workday returns nothing.
# ============================================================================

async def _scrape_dbs_workday(keywords: str, num_results: int) -> list:
    jobs = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()
        try:
            query = keywords.replace(" ", "+")
            url = f"https://dbs.wd3.myworkdayjobs.com/DBS_Careers?q={query}"
            print(f"  → DBS Workday: {url}")
            await page.goto(url, timeout=40000)
            try:
                await page.wait_for_load_state("networkidle", timeout=12000)
            except:
                pass
            await page.wait_for_timeout(4000)

            links = await page.query_selector_all('a[href*="/job/"]')
            print(f"  → {len(links)} total links, filtering Singapore + title relevance...")

            # Build title-relevance keywords from the search query
            kw_tokens = [t.lower() for t in keywords.split() if len(t) > 2]

            for link in links:
                href = await link.get_attribute("href") or ""
                if "Singapore" not in href:
                    continue

                title = (await link.inner_text()).strip()
                if not title:
                    continue

                # Drop jobs where none of the search keywords appear in the title
                title_lower = title.lower()
                if kw_tokens and not any(tok in title_lower for tok in kw_tokens):
                    continue

                # Extract location from parent li text
                location = "Singapore"
                try:
                    parent = await link.evaluate_handle("el => el.closest('li')")
                    parent_text = (await parent.as_element().inner_text()).strip()
                    lines = [l.strip() for l in parent_text.split("\n") if l.strip()]
                    for i, l in enumerate(lines):
                        if l == "locations" and i + 1 < len(lines):
                            location = lines[i + 1]
                            break
                except:
                    pass

                full_url = f"https://dbs.wd3.myworkdayjobs.com{href}"
                jobs.append({
                    "company":     "DBS",
                    "title":       title,
                    "salary":      "Not specified",
                    "location":    location,
                    "url":         full_url,
                    "description": "",
                    "source":      "Direct:DBS",
                })
                if len(jobs) >= num_results:
                    break

            print(f"  → {len(jobs)} Singapore title-matched jobs (DBS Workday)")

            if not jobs:
                print("  ⚠️ DBS Workday 0 Singapore results — falling back to MCF")

        except Exception as e:
            print(f"  ❌ DBS Workday error: {e}")
        finally:
            await browser.close()
    return jobs


# ============================================================================
# GOVTECH — posts primarily on MCF, but also tech.gov.sg
# ============================================================================

async def _scrape_govtech(keywords: str, num_results: int) -> list:
    """GovTech posts on MCF under 'Government Technology Agency'."""
    return await _scrape_mcf_company("GovTech", "Government Technology Agency", keywords, num_results)


# ============================================================================
# INDEED SINGAPORE
# ============================================================================

async def scrape_indeed(keywords: str, num_results: int = 10) -> list:
    """
    Indeed blocks all automated access (Cloudflare + IP-range blocking on cloud servers).
    Fall back to MCF API which covers the same Singapore market reliably.
    """
    print("  ⚠️ Indeed: blocked on cloud — falling back to MCF API")
    jobs = _call_mcf_api(keywords, num_results=num_results)
    for j in jobs:
        j["source"] = "Indeed→MCF"
    return jobs


# ============================================================================
# JOBSTREET SINGAPORE
# ============================================================================

async def scrape_jobstreet(keywords: str, num_results: int = 10) -> list:
    jobs = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        )
        page = await context.new_page()
        try:
            query = keywords.replace(" ", "-")
            url = f"https://www.jobstreet.com.sg/{query}-jobs"
            print(f"  → JobStreet: {url}")
            await page.goto(url, timeout=30000)
            await page.wait_for_timeout(3000)

            job_cards = await page.query_selector_all(
                '[data-automation="job-card-desktop"], article[class*="job"]')
            print(f"  → {len(job_cards)} cards (JobStreet)")

            for card in job_cards[:num_results]:
                try:
                    title_el   = await card.query_selector('[data-automation="jobTitle"], h1, h2, h3')
                    company_el = await card.query_selector('[data-automation="jobCompany"], [class*="company"]')
                    salary_el  = await card.query_selector('[data-automation="jobSalary"], [class*="salary"]')
                    link_el    = await card.query_selector('a[href*="jobstreet"]')
                    desc_el    = await card.query_selector('[data-automation="jobSnippet"], [class*="snippet"]')

                    title   = (await title_el.inner_text()).strip()   if title_el   else "Unknown Role"
                    company = (await company_el.inner_text()).strip() if company_el else "Unknown Company"
                    salary  = (await salary_el.inner_text()).strip()  if salary_el  else "Not specified"
                    desc    = (await desc_el.inner_text()).strip()    if desc_el    else ""

                    job_url = ""
                    if link_el:
                        href = await link_el.get_attribute("href")
                        if href:
                            job_url = href if href.startswith("http") else f"https://www.jobstreet.com.sg{href}"

                    jobs.append({
                        "company":     company,
                        "title":       title,
                        "salary":      salary,
                        "location":    "Singapore",
                        "url":         job_url,
                        "description": desc[:500],
                        "source":      "JobStreet",
                    })
                except Exception as e:
                    print(f"  ⚠️ JobStreet card error: {e}")
        except Exception as e:
            print(f"  ❌ JobStreet error: {e}")
        finally:
            await browser.close()
    return jobs


# ============================================================================
# LINKEDIN JOBS (public, no login — may be throttled)
# ============================================================================

async def scrape_linkedin(keywords: str, num_results: int = 10) -> list:
    jobs = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="en-US",
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()
        await page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        try:
            query = keywords.replace(" ", "%20")
            url = (f"https://www.linkedin.com/jobs/search/"
                   f"?keywords={query}&location=Singapore&sortBy=DD&f_TPR=r2592000")
            print(f"  → LinkedIn: {url}")
            await page.goto(url, timeout=30000)
            await page.wait_for_timeout(4000)

            job_cards = await page.query_selector_all(
                '.base-card, .base-search-card, [data-entity-urn*="jobPosting"]')
            print(f"  → {len(job_cards)} cards (LinkedIn)")

            for card in job_cards[:num_results]:
                try:
                    title_el    = await card.query_selector('.base-search-card__title, h3')
                    company_el  = await card.query_selector('.base-search-card__subtitle, h4')
                    location_el = await card.query_selector('.job-search-card__location')
                    link_el     = await card.query_selector('a.base-card__full-link, a[href*="linkedin.com/jobs"]')

                    title   = (await title_el.inner_text()).strip()    if title_el    else "Unknown Role"
                    company = (await company_el.inner_text()).strip()  if company_el  else "Unknown Company"
                    location= (await location_el.inner_text()).strip() if location_el else "Singapore"

                    job_url = ""
                    if link_el:
                        href = await link_el.get_attribute("href")
                        job_url = href.split("?")[0] if href else ""

                    jobs.append({
                        "company":     company,
                        "title":       title,
                        "salary":      "Not specified",
                        "location":    location,
                        "url":         job_url,
                        "description": "",
                        "source":      "LinkedIn",
                    })
                except Exception as e:
                    print(f"  ⚠️ LinkedIn card error: {e}")

            if not jobs:
                print("  ⚠️ LinkedIn 0 results — may be rate-limited")
        except Exception as e:
            print(f"  ❌ LinkedIn error: {e}")
        finally:
            await browser.close()
    return jobs


# ============================================================================
# GLASSDOOR SINGAPORE
# ============================================================================

async def scrape_glassdoor(keywords: str, num_results: int = 10) -> list:
    jobs = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="en-US",
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()
        await page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        try:
            query = keywords.replace(" ", "-").lower()
            url = (f"https://www.glassdoor.sg/Job/singapore-{query}-jobs"
                   f"-SRCH_IL.0,9_IC3235921.htm?sortBy=date_desc")
            print(f"  → Glassdoor: {url}")
            await page.goto(url, timeout=30000)
            await page.wait_for_timeout(4000)

            try:
                close_btn = await page.query_selector('[alt="Close"], .modal_closeIcon, button[data-test="modal-close-btn"]')
                if close_btn:
                    await close_btn.click()
                    await page.wait_for_timeout(1000)
            except:
                pass

            job_cards = await page.query_selector_all(
                '[data-test="jobListing"], .react-job-listing, li[class*="JobsList"]')
            print(f"  → {len(job_cards)} cards (Glassdoor)")

            for card in job_cards[:num_results]:
                try:
                    title_el   = await card.query_selector('[data-test="job-title"], .job-title, a[class*="jobLink"]')
                    company_el = await card.query_selector('[data-test="employer-name"], .employer-name, [class*="EmployerName"]')
                    location_el= await card.query_selector('[data-test="emp-location"], .location')
                    salary_el  = await card.query_selector('[data-test="detailSalary"], [class*="salary"]')
                    link_el    = await card.query_selector('a[href*="glassdoor"], a[href*="/job-listing/"]')

                    title   = (await title_el.inner_text()).strip()    if title_el    else "Unknown Role"
                    company = (await company_el.inner_text()).strip()  if company_el  else "Unknown Company"
                    location= (await location_el.inner_text()).strip() if location_el else "Singapore"
                    salary  = (await salary_el.inner_text()).strip()   if salary_el   else "Not specified"

                    job_url = ""
                    if link_el:
                        href = await link_el.get_attribute("href")
                        if href:
                            job_url = href if href.startswith("http") else f"https://www.glassdoor.sg{href}"

                    jobs.append({
                        "company":     company,
                        "title":       title,
                        "salary":      salary,
                        "location":    location,
                        "url":         job_url,
                        "description": "",
                        "source":      "Glassdoor",
                    })
                except Exception as e:
                    print(f"  ⚠️ Glassdoor card error: {e}")

            if not jobs:
                print("  ⚠️ Glassdoor 0 results — may require login or be blocked")
        except Exception as e:
            print(f"  ❌ Glassdoor error: {e}")
        finally:
            await browser.close()
    return jobs


# ============================================================================
# GREENHOUSE JSON API — synchronous (no Playwright)
# ============================================================================

def scrape_greenhouse_api(slug: str, company_name: str, keywords: str, num_results: int) -> list:
    """Fetch jobs from Greenhouse public API — no Playwright needed."""
    try:
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
        print(f"  → Greenhouse API [{company_name}]: {url}")
        resp = _requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            print(f"  ❌ Greenhouse [{company_name}]: HTTP {resp.status_code}")
            return []
        data = resp.json()
        jobs = []
        kw_tokens = [t.lower() for t in keywords.split() if len(t) > 2]
        for j in data.get("jobs", []):
            loc = j.get("location", {}).get("name", "")
            if "singapore" not in loc.lower():
                continue
            title = j.get("title", "").strip()
            if not title:
                continue
            # Keyword relevance filter (same as DBS Workday)
            if kw_tokens and not any(tok in title.lower() for tok in kw_tokens):
                continue
            desc = j.get("content", "") or ""
            # Strip HTML tags — full description, no truncation
            import re as _re
            desc = _re.sub(r"<[^>]+>", " ", desc)
            desc = _re.sub(r"\s+", " ", desc).strip()
            jobs.append({
                "company":     company_name,
                "title":       title,
                "salary":      "Not specified",
                "location":    loc or "Singapore",
                "url":         j.get("absolute_url", ""),
                "description": desc,
                "source":      f"Direct:{company_name}",
            })
            if len(jobs) >= num_results:
                break
        print(f"  → {len(jobs)} SG jobs (Greenhouse/{company_name})")
        return jobs
    except Exception as e:
        print(f"  ❌ Greenhouse [{company_name}] error: {e}")
        return []


# ============================================================================
# LEVER JSON API — synchronous (no Playwright)
# ============================================================================

def scrape_lever_api(slug: str, company_name: str, keywords: str, num_results: int) -> list:
    """Fetch jobs from Lever public API — no Playwright needed."""
    try:
        url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        print(f"  → Lever API [{company_name}]: {url}")
        resp = _requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            print(f"  ❌ Lever [{company_name}]: HTTP {resp.status_code}")
            return []
        postings = resp.json()
        jobs = []
        kw_tokens = [t.lower() for t in keywords.split() if len(t) > 2]
        for p in postings:
            loc = p.get("categories", {}).get("location", "") or p.get("categories", {}).get("allLocations", [""])[0]
            if loc and "singapore" not in loc.lower():
                continue  # if location specified but not SG, skip
            title = p.get("text", "").strip()
            if not title:
                continue
            if kw_tokens and not any(tok in title.lower() for tok in kw_tokens):
                continue
            desc = (p.get("descriptionPlain", "") or "")[:500].strip()
            jobs.append({
                "company":     company_name,
                "title":       title,
                "salary":      "Not specified",
                "location":    loc or "Singapore",
                "url":         p.get("hostedUrl", ""),
                "description": desc,
                "source":      f"Direct:{company_name}",
            })
            if len(jobs) >= num_results:
                break
        print(f"  → {len(jobs)} SG jobs (Lever/{company_name})")
        return jobs
    except Exception as e:
        print(f"  ❌ Lever [{company_name}] error: {e}")
        return []


# ============================================================================
# ASHBY PUBLIC API — Airwallex, Wise, Nium, Thought Machine
# No Playwright needed — clean JSON feed, zero bot risk
# ============================================================================

def scrape_ashby_api(slug: str, company_name: str, keywords: str, num_results: int) -> list:
    """Fetch jobs from Ashby public job board API."""
    try:
        url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
        print(f"  → Ashby API [{company_name}]: {url}")
        resp = _requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            print(f"  ❌ Ashby [{company_name}]: HTTP {resp.status_code}")
            return []
        data = resp.json()
        kw_tokens = [t.lower() for t in keywords.split() if len(t) > 2]
        jobs = []
        import re as _re
        for j in data.get("jobs", []):
            loc = j.get("location", "") or ""
            if "singapore" not in loc.lower():
                continue
            title = j.get("title", "").strip()
            if not title:
                continue
            if kw_tokens and not any(tok in title.lower() for tok in kw_tokens):
                continue
            desc = j.get("descriptionPlain", "") or ""
            desc = desc.strip()  # full description, no truncation
            # Salary from compensation if available
            comp = j.get("compensation", {}) or {}
            salary = "Not specified"
            if comp.get("compensationTierSummary"):
                salary = comp["compensationTierSummary"]
            elif comp.get("minValue") and comp.get("maxValue"):
                currency = comp.get("currency", "SGD")
                salary = f"{currency} {comp['minValue']:,}–{comp['maxValue']:,}"
            jobs.append({
                "company":     company_name,
                "title":       title,
                "salary":      salary,
                "location":    loc,
                "url":         j.get("jobUrl", ""),
                "description": desc,
                "source":      f"Direct:{company_name}",
            })
            if len(jobs) >= num_results:
                break
        print(f"  → {len(jobs)} SG jobs (Ashby/{company_name})")
        return jobs
    except Exception as e:
        print(f"  ❌ Ashby [{company_name}] error: {e}")
        return []


# ============================================================================
# GLINTS SINGAPORE
# ============================================================================

async def _glints_fetch_description(context, job_url: str) -> str:
    """
    Open a Glints job detail page and extract the full job description.
    Returns empty string on any failure.
    """
    if not job_url:
        return ""
    detail_page = await context.new_page()
    try:
        await detail_page.goto(job_url, timeout=30000)
        try:
            await detail_page.wait_for_load_state("networkidle", timeout=12000)
        except:
            pass
        await detail_page.wait_for_timeout(2000)

        # Glints job detail description selectors — try most-specific first
        desc_selectors = [
            '[class*="JobDescription"]',
            '[class*="job-description"]',
            '[class*="description-content"]',
            '[class*="OpportunityDescription"]',
            '[class*="opportunity-description"]',
            '[data-testid="job-description"]',
            'section[class*="description"]',
            'div[class*="description"]',
        ]
        for sel in desc_selectors:
            el = await detail_page.query_selector(sel)
            if el:
                text = (await el.inner_text()).strip()
                if len(text) > 80:
                    return text

        # Fallback: grab all visible paragraph text in main content area
        main_el = await detail_page.query_selector('main, article, [role="main"]')
        if main_el:
            text = (await main_el.inner_text()).strip()
            if len(text) > 80:
                return text

        return ""
    except Exception as e:
        print(f"  ⚠️ Glints detail fetch error ({job_url[:60]}): {e}")
        return ""
    finally:
        await detail_page.close()


async def scrape_glints(keywords: str, num_results: int = 10) -> list:
    jobs = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()
        try:
            query = keywords.replace(" ", "%20")
            url = f"https://glints.com/sg/opportunities/jobs/explore?keyword={query}&country=SG&locationName=Singapore%2C%20Central%20Singapore%2C%20Singapore"
            print(f"  → Glints: {url}")
            await page.goto(url, timeout=30000)
            try:
                await page.wait_for_load_state("networkidle", timeout=12000)
            except:
                pass
            await page.wait_for_timeout(3000)

            job_cards = await page.query_selector_all(
                '[class*="CompactOpportunityCard"], [class*="JobCard"], [data-testid="job-card"], '
                'div[class*="opportunity-card"], article[class*="job"]'
            )
            print(f"  → {len(job_cards)} cards (Glints)")

            # ── Pass 1: collect card metadata ─────────────────────────────
            card_data = []
            for card in job_cards[:num_results]:
                try:
                    title_el   = await card.query_selector('h2, h3, [class*="title"], [class*="Title"], [class*="job-name"]')
                    company_el = await card.query_selector('[class*="company"], [class*="Company"], [class*="employer"]')
                    salary_el  = await card.query_selector('[class*="salary"], [class*="Salary"], [class*="compensation"]')
                    link_el    = await card.query_selector('a')

                    title   = (await title_el.inner_text()).strip()   if title_el   else ""
                    company = (await company_el.inner_text()).strip() if company_el else "Unknown"
                    salary  = (await salary_el.inner_text()).strip()  if salary_el  else "Not specified"

                    # Keep first line only — nested elements embed location/salary below company
                    title   = title.splitlines()[0].strip()   if title   else ""
                    company = company.splitlines()[0].strip() if company else "Unknown"
                    salary  = salary.splitlines()[0].strip()  if salary  else "Not specified"

                    # Drop salary-looking noise that leaked into the title field
                    if title.startswith("$") or "SGD" in title.upper():
                        title = ""
                    if not title:
                        continue

                    job_url = ""
                    if link_el:
                        href = await link_el.get_attribute("href") or ""
                        job_url = href if href.startswith("http") else f"https://glints.com{href}"

                    card_data.append({
                        "company": company,
                        "title":   title,
                        "salary":  salary,
                        "url":     job_url,
                    })
                except Exception as e:
                    print(f"  ⚠️ Glints card error: {e}")

            # ── Pass 2: visit each job page to fetch the full description ─
            print(f"  → Fetching descriptions for {len(card_data)} Glints jobs…")
            for i, meta in enumerate(card_data):
                print(f"     [{i+1}/{len(card_data)}] {meta['company']} — {meta['title'][:50]}")
                description = await _glints_fetch_description(context, meta["url"])
                jobs.append({
                    "company":     meta["company"],
                    "title":       meta["title"],
                    "salary":      meta["salary"],
                    "location":    "Singapore",
                    "url":         meta["url"],
                    "description": description,
                    "source":      "Glints",
                })

            if not jobs:
                print("  ⚠️ Glints returned 0 — may need selector update")
        except Exception as e:
            print(f"  ❌ Glints error: {e}")
        finally:
            await browser.close()
    return jobs


# ============================================================================
# TECH IN ASIA
# ============================================================================

async def scrape_techinasia(keywords: str, num_results: int = 10) -> list:
    jobs = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()
        try:
            query = keywords.replace(" ", "+")
            url = f"https://www.techinasia.com/jobs/search?keyword={query}&location_name=Singapore"
            print(f"  → Tech in Asia: {url}")
            await page.goto(url, timeout=30000)
            try:
                await page.wait_for_load_state("networkidle", timeout=12000)
            except:
                pass
            await page.wait_for_timeout(3000)

            job_cards = await page.query_selector_all(
                '[class*="JobCard"], [class*="job-card"], article[class*="job"], '
                '[data-testid="job-listing"], li[class*="job"]'
            )
            print(f"  → {len(job_cards)} cards (Tech in Asia)")

            for card in job_cards[:num_results]:
                try:
                    title_el   = await card.query_selector('h2, h3, [class*="title"], [class*="Title"]')
                    company_el = await card.query_selector('[class*="company"], [class*="Company"]')
                    salary_el  = await card.query_selector('[class*="salary"], [class*="compensation"]')
                    link_el    = await card.query_selector('a')

                    title   = (await title_el.inner_text()).strip()   if title_el   else ""
                    company = (await company_el.inner_text()).strip() if company_el else "Unknown"
                    salary  = (await salary_el.inner_text()).strip()  if salary_el  else "Not specified"

                    # Keep first line only — nested elements may embed location/salary in company text
                    title   = title.splitlines()[0].strip()   if title   else ""
                    company = company.splitlines()[0].strip() if company else "Unknown"
                    salary  = salary.splitlines()[0].strip()  if salary  else "Not specified"

                    if not title:
                        continue

                    job_url = ""
                    if link_el:
                        href = await link_el.get_attribute("href") or ""
                        job_url = href if href.startswith("http") else f"https://www.techinasia.com{href}"

                    jobs.append({
                        "company":     company,
                        "title":       title,
                        "salary":      salary,
                        "location":    "Singapore",
                        "url":         job_url,
                        "description": "",
                        "source":      "Tech in Asia",
                    })
                except Exception as e:
                    print(f"  ⚠️ Tech in Asia card error: {e}")

            if not jobs:
                print("  ⚠️ Tech in Asia returned 0 — may need selector update")
        except Exception as e:
            print(f"  ❌ Tech in Asia error: {e}")
        finally:
            await browser.close()
    return jobs


# ============================================================================
# eFINANCIAL CAREERS — finance/fintech focused
# ============================================================================


# ============================================================================
# CAREERS@GOV — jobs.careers.gov.sg direct Playwright scraper
# ============================================================================

def _scrape_hrp_odata(agency_name: str, keywords: str, kw_tokens: list,
                      num_results: int, seen_urls: set) -> list:
    """
    Call the HRP OData API (ZGERCGS001_SRV) directly to get job listings.
    Tries common entity set names and filters by agency + keywords.
    """
    import json as _json
    import re as _re

    hrp_base  = "https://www.careers.hrp.gov.sg/sap/bc/ui5_ui5/sap/ZGERCFA004/index.html"
    odata_base = "https://www.careers.hrp.gov.sg/sap/opu/odata/sap/ZGERCGS001_SRV"
    common_params = "saml2=disabled&sap-client=800&sap-language=EN&$format=json&$top=100"
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": "https://www.careers.hrp.gov.sg/",
    }

    # Try common entity set names for SAP HR job postings
    entity_sets = [
        "JobsSet", "JobSet", "VacancySet", "JobPostingSet",
        "JobAdvertSet", "JobListSet", "ActiveJobsSet",
    ]

    agency_lower = agency_name.lower()
    candidates   = []

    for entity in entity_sets:
        try:
            url  = f"{odata_base}/{entity}?{common_params}"
            resp = _requests.get(url, headers=headers, timeout=15)
            print(f"  → OData [{entity}]: HTTP {resp.status_code}")
            if resp.status_code != 200:
                continue

            data  = resp.json()
            items = (data.get("d", {}).get("results", [])
                     or data.get("value", [])
                     or (data.get("d", []) if isinstance(data.get("d"), list) else []))

            if not items:
                print(f"  → OData [{entity}]: 0 items — trying next entity set")
                continue

            print(f"  → OData [{entity}]: {len(items)} total items — first keys: {list(items[0].keys())[:8] if items else []}")

            for item in items:
                # Try common field names for title, ID, agency
                title = (item.get("JobTitle") or item.get("Title") or item.get("PosText")
                         or item.get("ShortDesc") or item.get("Descr") or "").strip()
                job_id   = str(item.get("JobID") or item.get("Requisition") or
                               item.get("POSID") or item.get("Id") or item.get("JOBID") or "")
                job_uuid = str(item.get("JobUUID") or item.get("UUID") or
                               item.get("Guid") or item.get("GUID") or "")
                agency   = (item.get("Agency") or item.get("AgencyName") or
                            item.get("OrgUnit") or item.get("Department") or "")

                if not title:
                    continue

                # Filter by agency
                if agency_lower not in agency.lower() and agency_lower not in title.lower():
                    continue

                # Filter by keyword
                title_lower = title.lower()
                if kw_tokens and not any(tok in title_lower for tok in kw_tokens):
                    continue

                if job_id and job_uuid:
                    job_url = f"{hrp_base}#/JobDescription/{job_id}/{job_uuid}"
                elif job_id:
                    job_url = f"{hrp_base}#/JobDescription/{job_id}"
                else:
                    continue

                if job_url not in seen_urls:
                    seen_urls.add(job_url)
                    candidates.append({"title": title, "url": job_url})

            if candidates:
                print(f"  → OData [{entity}]: found {len(candidates)} matching jobs")
                break  # Found working entity set — stop trying others

        except Exception as e:
            print(f"  ⚠️ OData [{entity}] error: {e}")
            continue

    return candidates[:num_results]


async def _fetch_careers_gov_description(context, job_url: str) -> str:
    """Visit a Careers@Gov job detail page and extract the job description."""
    if not job_url:
        return ""
    detail_page = await context.new_page()
    try:
        await detail_page.goto(job_url, timeout=30000)
        try:
            await detail_page.wait_for_load_state("networkidle", timeout=12000)
        except:
            pass
        await detail_page.wait_for_timeout(2000)

        desc_selectors = [
            '[class*="job-description"]', '[class*="JobDescription"]',
            '[class*="description-content"]', '[class*="job-detail"]',
            '[class*="jobDetail"]', '[class*="content"]',
            'section[class*="description"]', 'div[class*="description"]',
            'main', 'article',
        ]
        for sel in desc_selectors:
            el = await detail_page.query_selector(sel)
            if el:
                text = (await el.inner_text()).strip()
                if len(text) > 100:
                    return text[:2000]
        return ""
    except Exception as e:
        print(f"  ⚠️ Careers@Gov detail fetch error: {e}")
        return ""
    finally:
        await detail_page.close()


async def _scrape_careers_gov(company: str, agency_name: str, keywords: str, num_results: int) -> list:
    """
    Scrape both Careers@Gov portals using Playwright:
      1. jobs.careers.gov.sg  (new portal)
      2. careers.hrp.gov.sg   (old HRP portal — SAP UI5, uses ?search-keyword=)
    Filters results by agency name, then fetches each JD.
    """
    jobs      = []
    seen_urls = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )

        kw_tokens = [t.lower() for t in keywords.split() if len(t) > 2]

        async def _collect_candidates(page, base_url: str, link_selector: str) -> list:
            """
            Collect job link candidates.
            We already searched by agency name on the portal, so results are
            pre-scoped to that agency. No keyword-title filter is applied here —
            return all agency jobs so the user sees the full picture.
            """
            candidates = []
            job_links  = await page.query_selector_all(link_selector)
            print(f"  → {len(job_links)} job links found on {base_url[:40]}")

            for link in job_links:
                try:
                    href  = await link.get_attribute("href") or ""
                    # Use only the first line of inner_text as the job title
                    raw_title = (await link.inner_text()).strip()
                    title = raw_title.split("\n")[0].strip()
                    if not title or len(title) < 3:
                        continue

                    if href.startswith("http"):
                        job_url = href
                    elif href.startswith("#"):
                        job_url = base_url + href
                    else:
                        job_url = base_url + href

                    if job_url in seen_urls:
                        continue
                    seen_urls.add(job_url)

                    candidates.append({"title": title, "url": job_url})
                    if len(candidates) >= num_results:
                        break
                except Exception as e:
                    print(f"  ⚠️ link error: {e}")
            return candidates

        # ── Portal 1: jobs.careers.gov.sg (new) — search by AGENCY NAME ─────
        try:
            page = await context.new_page()
            print(f"  → Careers@Gov NEW [{company}]: searching by agency '{agency_name}'…")
            await page.goto("https://jobs.careers.gov.sg/", timeout=60000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except:
                pass
            await page.wait_for_timeout(3000)

            search_box = None
            for sel in ['input[type="search"]', 'input[placeholder*="search" i]',
                        'input[placeholder*="keyword" i]', 'input[name="keyword"]']:
                search_box = await page.query_selector(sel)
                if search_box:
                    break

            if search_box:
                await search_box.click()
                # Search by AGENCY NAME, not keyword — filter by keyword after
                await search_box.fill(agency_name)
                await page.keyboard.press("Enter")
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except:
                    pass
                await page.wait_for_timeout(4000)

            # Accumulate ALL job URLs across pages incrementally
            # (jobs.careers.gov.sg SPA may replace results rather than append on Load More)
            accumulated_candidates = {}  # url → title, deduped

            async def _harvest_current_page():
                """Snapshot all job links currently visible on the page."""
                job_links = await page.query_selector_all('a[href*="/jobs/"]')
                for link in job_links:
                    try:
                        href = await link.get_attribute("href") or ""
                        if not href:
                            continue
                        job_url = href if href.startswith("http") else f"https://jobs.careers.gov.sg{href}"
                        if job_url in seen_urls:
                            continue
                        raw_title = (await link.inner_text()).strip()
                        title = raw_title.split("\n")[0].strip()
                        if title and len(title) >= 3:
                            if job_url not in accumulated_candidates:
                                seen_urls.add(job_url)
                                accumulated_candidates[job_url] = title
                    except:
                        pass

            # Harvest initial page results first
            await _harvest_current_page()
            print(f"  → Initial page: {len(accumulated_candidates)} jobs harvested")

            # Click "Load More" and harvest after each click
            for page_attempt in range(8):  # up to 8 extra pages = ~180 jobs max
                load_more = await page.query_selector(
                    'button:has-text("Load more"), button:has-text("Show more"), '
                    'button:has-text("Next"), [aria-label*="next" i], '
                    '[class*="load-more"], [class*="loadMore"], '
                    '[class*="pagination"] button:last-child'
                )
                if not load_more:
                    break
                # Stop if button is disabled (no more pages)
                is_disabled = await load_more.get_attribute("disabled")
                is_disabled = is_disabled is not None
                if not is_disabled:
                    is_disabled = await load_more.evaluate("el => el.disabled || el.getAttribute('aria-disabled') === 'true'")
                if is_disabled:
                    print(f"  → Load more button disabled — all pages loaded")
                    break
                try:
                    await load_more.click()
                    await page.wait_for_timeout(2500)
                    before = len(accumulated_candidates)
                    await _harvest_current_page()
                    print(f"  → Page load {page_attempt+1}: +{len(accumulated_candidates)-before} new jobs (total {len(accumulated_candidates)})")
                except Exception as e:
                    print(f"  ⚠️ Load more click error (likely end of results): {e}")
                    break

            # Convert accumulated dict to candidate list
            candidates1 = [
                {"title": title, "url": url}
                for url, title in list(accumulated_candidates.items())[:num_results]
            ]
            print(f"  → {len(candidates1)} total unique candidates from Careers@Gov")
            await page.close()
        except Exception as e:
            print(f"  ⚠️ New Careers@Gov error: {e}")
            candidates1 = []

        # ── Portal 2: careers.hrp.gov.sg — direct OData API (ZGERCGS001_SRV) ─
        candidates2 = []
        try:
            candidates2 = _scrape_hrp_odata(agency_name, keywords, kw_tokens, num_results, seen_urls)
            print(f"  → {len(candidates2)} HRP candidates from OData API")
        except Exception as e:
            print(f"  ⚠️ HRP OData error: {e}")

        all_candidates = candidates1 + candidates2
        print(f"  → {len(all_candidates)} {company} candidates total (new: {len(candidates1)}, hrp: {len(candidates2)})")

        # Pass 2: fetch JD for each candidate
        for meta in all_candidates[:num_results]:
            print(f"  → Fetching JD: {meta['title'][:60]}…")
            desc = await _fetch_careers_gov_description(context, meta["url"])
            jobs.append({
                "company":     agency_name,
                "title":       meta["title"],
                "salary":      "Not specified",
                "location":    "Singapore",
                "url":         meta["url"],
                "description": desc,
                "source":      f"Direct:{company}",
            })

        print(f"  → {len(jobs)} {company} jobs from Careers@Gov (with JDs)")
        await browser.close()

    return jobs


# ============================================================================
# COMPANY DISPATCHER
# ============================================================================

async def _scrape_company(company: str, keywords: str, num_results: int,
                          salary_min: int = 0, salary_max: int = None,
                          min_years: int = 0) -> list:
    """Route to the right scraper based on COMPANY_CONFIG."""
    cfg = COMPANY_CONFIG.get(company, {"type": "mcf", "mcf_name": company})
    t   = cfg["type"]

    if t == "dbs_workday":
        jobs = await _scrape_dbs_workday(keywords, num_results)
        if not jobs:
            jobs = await _scrape_mcf_company("DBS", "DBS", keywords, num_results,
                                              salary_min=salary_min, salary_max=salary_max,
                                              min_years=min_years)
        return jobs

    if t == "grab_direct":
        jobs = await _scrape_grab(keywords, num_results)
        if not jobs:
            jobs = await _scrape_mcf_company("Grab", "Grab", keywords, num_results,
                                              salary_min=salary_min, salary_max=salary_max,
                                              min_years=min_years)
        return jobs

    if t == "sea_direct":
        jobs = await _scrape_sea(keywords, num_results)
        if not jobs:
            jobs = await _scrape_mcf_company("Sea Group", "Sea", keywords, num_results,
                                              salary_min=salary_min, salary_max=salary_max,
                                              min_years=min_years)
        return jobs

    if t == "careers_gov":
        return await _scrape_careers_gov(company, cfg.get("search_prefix", company), keywords, num_results)

    if t == "greenhouse":
        return scrape_greenhouse_api(cfg["slug"], company, keywords, num_results)

    if t == "lever":
        return scrape_lever_api(cfg["slug"], company, keywords, num_results)

    if t == "ashby":
        return scrape_ashby_api(cfg["slug"], company, keywords, num_results)

    # Default: MCF with company filter
    return await _scrape_mcf_company(
        company, cfg.get("mcf_name", company), keywords, num_results,
        salary_min=salary_min, salary_max=salary_max, min_years=min_years
    )


# ============================================================================
# MAIN ORCHESTRATOR
# ============================================================================

def scrape_jobs(keywords: str, sources: list, num_results: int = 10,
                companies: list = None, salary_min: int = 0,
                salary_max: int = None, min_years: int = 0) -> dict:
    """
    Scrape jobs from selected job boards and/or company career pages.

    Args:
        keywords:    Job search keywords
        sources:     Job board names e.g. ["MyCareersFuture", "Indeed", "LinkedIn"]
        num_results: Max total results to return
        companies:   Company names e.g. ["DBS", "Grab"]
        salary_min:  Minimum monthly salary SGD (passed to MCF)
        salary_max:  Maximum monthly salary SGD (passed to MCF)
        min_years:   Minimum years of experience (passed to MCF as position level)
    """
    companies = companies or []

    async def run_scrapers():
        tasks      = []
        board_count = len(sources) + len(companies)
        per_source  = max(3, num_results // max(board_count, 1))

        # careers_gov scrapers search the whole agency (not keyword-filtered),
        # so give them a higher budget so deeply-listed jobs aren't cut off.
        # Other scrapers keep the per_source budget to avoid slowdowns.
        careers_gov_companies = {
            c for c in companies
            if COMPANY_CONFIG.get(c, {}).get("type") == "careers_gov"
        }
        careers_gov_per_source = max(30, num_results)

        if "MyCareersFuture" in sources:
            tasks.append(scrape_mycareersfuture(keywords, per_source,
                                                 salary_min=salary_min,
                                                 salary_max=salary_max,
                                                 min_years=min_years))
        if "Indeed" in sources:
            tasks.append(scrape_indeed(keywords, per_source))
        if "JobStreet" in sources:
            tasks.append(scrape_jobstreet(keywords, per_source))
        if "LinkedIn" in sources:
            tasks.append(scrape_linkedin(keywords, per_source))
        if "Glassdoor" in sources:
            tasks.append(scrape_glassdoor(keywords, per_source))
        if "Glints" in sources:
            tasks.append(scrape_glints(keywords, per_source))
        if "Tech in Asia" in sources:
            tasks.append(scrape_techinasia(keywords, per_source))

        for company in companies:
            # Give careers_gov companies a bigger budget — they browse all agency jobs
            budget = careers_gov_per_source if company in careers_gov_companies else per_source
            tasks.append(_scrape_company(company, keywords, budget,
                                         salary_min=salary_min,
                                         salary_max=salary_max,
                                         min_years=min_years))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_jobs = []
        for result in results:
            if isinstance(result, list):
                all_jobs.extend(result)
            elif isinstance(result, Exception):
                print(f"  ⚠️ Scraper exception: {result}")
        return all_jobs

    try:
        jobs = asyncio.run(run_scrapers())

        seen, unique = set(), []
        for job in jobs:
            key = f"{job['company'].lower()}_{job['title'].lower()}"
            if key not in seen:
                seen.add(key)
                unique.append(job)

        return {
            "status": "success",
            "jobs":   unique[:num_results],
            "count":  len(unique[:num_results]),
        }
    except Exception as e:
        return {
            "status": "error",
            "error":  str(e),
            "jobs":   [],
        }
