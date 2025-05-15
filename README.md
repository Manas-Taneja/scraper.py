# Axis Max Life Term Insurance Scraper

This is a web scraper built with Playwright to extract detailed term insurance information from axismaxlife.com, including plan details, add-on riders, and quote breakdowns.

## Setup

1. Install Python 3.8 or higher
2. Install the required dependencies:
```bash
pip install -r requirements.txt
```
3. Install Playwright browsers:
```bash
playwright install
```

## Usage

Run the scraper:
```bash
python scraper.py
```

The scraper will:
1. Navigate to the term insurance plans page
2. Visit each plan page, fill out required forms, and extract detailed information
3. Save the data in JSON format as `final_parsed_term_plans.json` in the project root

## Output

The scraper generates a single file:
- `final_parsed_term_plans.json`: JSON format of the scraped data

## Data Structure

Each plan entry in the JSON includes:
- `source_url`: URL of the plan page
- `source_scrape_date`: Date of scraping
- `insurer`: Insurer name
- `plan_name`: Name of the plan
- `plan_type`: Type of plan (e.g., Term Insurance)
- `monthly_premium`: Extracted monthly premium
- `medical_required`: Whether a medical is required (bool)
- `smoker_premium_diff`: Whether smoker status affects premium (bool)
- `add_on_riders`: List of add-on riders (name, coverage, premium)
- `quote_details`: Detailed quote breakdown (equote number, policy name, life cover, cover till age, base premium, add-ons, base plus addons, GST, total amount, premium from second year)

