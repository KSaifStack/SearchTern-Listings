import html
def generate_readme(dataframe, output_path="README.md"):
    """
    Writes job listings into a README.md with an HTML table
    matching SimplifyJobs' exact format, so the existing
    target() scraper can parse it without any changes.
    
    Expects dataframe with columns: company, role, location, date, link
    """
    rows_html = []
    last_company = ""

    for _, row in dataframe.iterrows():
        company = str(row["company"]).strip()
        role = html.escape(str(row["role"]).strip())
        location = html.escape(str(row["location"]).strip())
        date = str(row["date"]).strip()
        link = str(row["link"]).strip()

        # Match SimplifyJobs' merged-cell pattern — blank company 
        # if it's the same as the previous row
        if company == last_company:
            company_cell = ""
        else:
            company_cell = html.escape(company)
            last_company = company

        location_cell = location.replace(", ", "</br>")

        row_html = f"""<tr>
<td>{company_cell}</td>
<td>{role}</td>
<td>{location_cell}</td>
<td><a href="{link}">apply</a></td>
<td>{date}</td>
</tr>"""
        rows_html.append(row_html)

    table_html = f"""<table>
<thead>
<tr>
<th>Company</th>
<th>Role</th>
<th>Location</th>
<th>Application/Link</th>
<th>Date Posted</th>
</tr>
</thead>
<tbody>
{''.join(rows_html)}
</tbody>
</table>"""

    full_readme = f"""# SearchTern Internships Dataset

Auto-generated job listing data sourced from jobhive. Updated on each scrape run.

{table_html}
"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(full_readme)

    print(f"Wrote {len(dataframe)} rows to {output_path}")
