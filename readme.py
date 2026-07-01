import html

def generate_readme(dataframe, output_path="README.md"):
    rows_html = []

    for _, row in dataframe.iterrows():
        company  = html.escape(str(row["company"]).strip())
        role     = html.escape(str(row["role"]).strip())
        location = html.escape(str(row["location"]).strip())
        date     = str(row["date"]).strip()
        link     = str(row["link"]).strip()

        rows_html.append(f"""<tr>
<td>{company}</td>
<td>{role}</td>
<td>{location}</td>
<td><a href="{link}">apply</a></td>
<td>{date}</td>
</tr>""")

    table = f"""<table>
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
{"".join(rows_html)}
</tbody>
</table>"""

    readme = f"""# SearchTern Job Listings

Auto-generated from jobhive. Updated each scrape run.

{table}
"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(readme)

    print(f"Wrote {len(dataframe)} rows to {output_path}")