import { chromium } from 'playwright';

const companies = [
  { name: 'OCBC', url: 'https://careers.ocbc.com' },
  { name: 'UOB', url: 'https://www.uob.com/careers' },
  { name: 'DBS', url: 'https://www.dbs.com/careers' },
  { name: 'Grab', url: 'https://grab.careers' },
];

const titleFilterInclude = ['agile', 'product owner', 'product manager', 'ai product', 'head of product', 'director', 'vp', 'chief product'];
const titleFilterExclude = ['junior', 'intern', 'graduate', 'support', 'sales', 'marketing'];

function matchesFilter(title) {
  if (!title) return false;
  const lower = title.toLowerCase();
  const hasInclude = titleFilterInclude.some(word => lower.includes(word));
  const hasExclude = titleFilterExclude.some(word => lower.includes(word));
  return hasInclude && !hasExclude;
}

(async () => {
  const browser = await chromium.launch();
  const results = [];

  console.log('Scanning Singapore banking portals for Agile Product Owner roles...\n');

  for (const company of companies) {
    if (results.length >= 3) break;

    console.log(`\n>>> Scanning ${company.name}`);
    
    try {
      const page = await browser.newPage();
      page.setDefaultTimeout(15000);
      
      await page.goto(company.url, { waitUntil: 'domcontentloaded' });
      
      // Extract job data
      const jobs = await page.evaluate(() => {
        const items = [];
        
        // Collect text nodes that look like job titles
        const allText = document.body.innerText;
        const lines = allText.split('\n');
        
        for (const line of lines) {
          const trimmed = line.trim();
          if (trimmed.length > 10 && 
              (trimmed.match(/product|agile|manager|director|vp|head/i)) &&
              trimmed.length < 150) {
            items.push(trimmed);
          }
        }
        
        return items;
      });

      console.log(`  Found ${jobs.length} potential job titles`);

      for (const job of jobs) {
        if (results.length >= 3) break;
        
        if (matchesFilter(job)) {
          console.log(`  ✓ MATCH: "${job}"`);
          results.push({
            company: company.name,
            title: job,
            url: company.url
          });
        }
      }

      await page.close();
    } catch (error) {
      console.log(`  ✗ Error: ${error.message}`);
    }
  }

  await browser.close();

  console.log('\n' + '━'.repeat(70));
  console.log('RESULTS');
  console.log('━'.repeat(70) + '\n');

  if (results.length > 0) {
    results.slice(0, 3).forEach((r, i) => {
      console.log(`${i + 1}. Company: ${r.company}`);
      console.log(`   Title: ${r.title}`);
      console.log(`   Source: ${r.url}\n`);
    });
  } else {
    console.log('No Agile Product Owner roles found in this scan.');
  }

  process.exit(0);
})();
