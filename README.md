# RxBriefing

A self-updating news site for pharma, biotech, medtech, payer, health IT and health AI stocks.
No server, no database, no user accounts. A script runs every 15 minutes, pulls headlines,
and republishes a static page.

## How it works (plain English)

| Piece | What it is | Cost |
|---|---|---|
| `tickers.csv` | The list of companies we care about. Edit it any time. | — |
| `build.py` | The script. Pulls news from RSS feeds (+ Finnhub if you add a key), keeps only healthcare stories, tags them, fetches price changes, writes `site/`. | — |
| `site/` | The finished website. Just files. | — |
| GitHub Actions | A robot that runs `build.py` every 15 min. | Free (public repo) |
| Cloudflare Pages | Hosts `site/` on your domain. | Free |
| Finnhub | Stock prices + company news. Optional. | Free tier |

## Setup — about 45 minutes

### 1. Put the code on GitHub
1. Create an account at github.com if you don't have one.
2. Click **New repository**, name it `rxbriefing`, keep it **Public** (public repos get unlimited free robot minutes), click Create.
3. Upload every file in this folder (drag and drop works on the repo page → "uploading an existing file"). Make sure the `.github/workflows/build.yml` file comes along — folders starting with a dot are hidden on Mac/Windows, so unzip and check.

### 2. Get a free Finnhub key (optional but recommended — this is what gives you the +/-% next to tickers)
1. Go to finnhub.io → Get free API key → sign up.
2. Copy the key.
3. In your GitHub repo: **Settings → Secrets and variables → Actions → New repository secret**. Name: `FINNHUB_API_KEY`, value: paste the key.

### 3. Connect Cloudflare
1. Log in to dash.cloudflare.com → **Workers & Pages → Create → Pages → Upload assets**. Project name: `rxbriefing`. Upload the `site` folder once by hand (this just creates the project). 
2. **Custom domains** tab → add `rxbriefing.com` (and `www.rxbriefing.com`). Since your domain is already in Cloudflare, it sets DNS automatically.
3. Get your **Account ID**: it's shown on the right side of the Workers & Pages overview page. Add it to GitHub secrets as `CLOUDFLARE_ACCOUNT_ID`.
4. Make an API token: Cloudflare → profile icon → **My Profile → API Tokens → Create Token → "Edit Cloudflare Workers" template** → Continue → Create. Copy it, add it to GitHub secrets as `CLOUDFLARE_API_TOKEN`.

### 4. Press go
GitHub repo → **Actions** tab → "Build and deploy RxBriefing" → **Run workflow**. In 3–5 minutes your site is live. From now on it reruns itself every 15 minutes.

## Day-to-day
- **Add a company:** add a row to `tickers.csv` (`ticker,name,sector,match`). `match` = lowercase name variants separated by `|`.
- **Add a news source:** add a line to `RSS_FEEDS` in `build.py`.
- **Change tags:** edit `TAG_RULES` in `build.py`.
- **Check the robot ran:** GitHub → Actions. Green check = good. Red X = click it to see the error.

## Running it on your own computer (to test)
```
pip install -r requirements.txt
FINNHUB_API_KEY=yourkey python build.py
cd site && python -m http.server 8000     # then open http://localhost:8000
```

## Making money (no logins needed)
1. **Ads** — apply to Google AdSense once the site has ~2 weeks of content. Paste their one script tag into `site/index.html` before `</head>`.
2. **Affiliate links** — broker referral programs (Robinhood, Webull, Public), Seeking Alpha / Koyfin / TipRanks referrals. Add a short "Tools we use" box in the footer.
3. **Email digest** — Beehiiv (free tier). Create a publication, copy its form URL, and paste it into `action="https://REPLACE-WITH-YOUR-BEEHIIV-FORM-URL"` in `site/index.html` (the signup box is already in the hero). Later add a paid weekly "catalyst calendar" tier. Beehiiv handles payments; your site still has no accounts.
4. **Sponsors** — once you have traffic, sell a "presented by" slot to biotech data vendors or conferences.

## Upgrading data later
- Swap in Benzinga / paid Finnhub by adding another fetcher in `build.py` — the rest of the pipeline doesn't change.
- Add Claude summaries: set `ANTHROPIC_API_KEY` and call the API inside the loop in `main()` before `tag()`. Keep summaries short and in your own words.

## Daily morning email (free, automatic)
The script writes `https://rxbriefing.com/daily.xml` — only the stories from 5:00 AM ET yesterday to 5:00 AM ET today, FDA/M&A/trial news first, plus the biggest movers in the description. Point any RSS-to-email tool at it:

- **Beehiiv (recommended):** Create a free publication → Automations → "RSS to email" (or "Send via RSS") → feed URL `https://rxbriefing.com/daily.xml` → schedule daily 7:00 AM Eastern → pick "all items in feed". Then paste the publication's signup form URL into `site/index.html` where it says `REPLACE-WITH-YOUR-BEEHIIV-FORM-URL`.
- **Mailchimp alternative:** Create campaign → "Share blog updates" (RSS) → same feed URL → daily at 7:00 AM.

Change the send time by editing `DIGEST_SEND_HOUR` at the top of `build.py` and the time in the email tool — keep them the same.

## Company pages (nightly)
`companies.py` builds a page per ticker at `/company/TICKER/` with: pipeline by phase (ClinicalTrials.gov), approved products (Drugs@FDA), upcoming trial completion dates, SEC filings with insider forms (3/4/5/144) removed, and recent news. It runs every night at 2 AM ET via `.github/workflows/companies.yml`.

One extra secret: `SEC_USER_AGENT` = `RxBriefing your@email.com` (the SEC requires a contact in the request). Add it next to the others, then run that workflow once by hand from the Actions tab.

Also included: `/companies/` (directory of every ticker) and `/pipeline/` (the "How a drug gets made" explainer).
