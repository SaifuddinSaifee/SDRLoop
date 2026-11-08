# Demo Sales Navigator export

`sales_nav_demo.csv` is what the simulated Evaboot (`EVABOOT_MODE=simulated`) returns for any
Sales Navigator search URL. It uses Evaboot's export columns.

- **Companies are real** and their websites are live, so the research step reads real pages and
  real news. Checked with the app's own `fetch_page` tool on 2026-11-08.
- **People are fictional.** Names, titles, LinkedIn URLs and email addresses are made up. The
  addresses follow the `first.last@company` pattern and only ever reach the local Mailpit inbox.
- The mix is deliberate, so the funnel looks like a real search:
  - companies that are too large (CIENCE, Callbox, Morgan McKinley)
  - Evaboot's competitors (lemlist, Waalaxy)
  - excluded job titles (engineer, assistant)
  - risky, suspect, invalid or missing emails
  - a site that blocks bots (SalesRoads: no facts, so the draft is flagged)
  - a French company (non-English market flag)
