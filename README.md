# RecallFlag

Searchable U.S. recalls (FDA food, drugs and devices; CPSC consumer products; NHTSA vehicles), rebuilt every day from official public data.

A GitHub Actions workflow (`.github/workflows/site.yml`) runs daily: `fetch.py` downloads the latest recalls, `build.py` generates the static site, `check_site.py` validates it, and the result is deployed to GitHub Pages.

RecallFlag is independent and not affiliated with the FDA, CPSC, NHTSA or any government agency. Always check the official notice linked on each page.
