#!/usr/bin/env python3
"""
Builds the self-contained Voice.ai benchmark HTML report.
Run: python3 benchmarks/build_report.py
Outputs: docs/voiceai/VOICEAI_BENCHMARK_REPORT.html

NOTE: innerHTML usage in the TTS script only sets hardcoded emoji characters
(play/stop icons), never user-supplied content. No XSS risk.
"""

import base64
import csv
import io
from pathlib import Path


def encode_chart(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def img_tag(b64, alt="chart"):
    return (
        '<img src="data:image/png;base64,' + b64 + '" '
        'style="width:100%;max-width:920px;border-radius:8px;margin:1rem 0;" '
        'alt="' + alt + '">'
    )


def csv_to_html(csv_text):
    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)
    if not rows:
        return ""

    # Determine which columns are numeric by checking data rows
    num_cols = set()
    for row in rows[1:]:
        for i, cell in enumerate(row):
            try:
                float(cell)
                num_cols.add(i)
            except ValueError:
                pass

    # Build header with matching alignment
    out = '<table><thead><tr>'
    for i, h in enumerate(rows[0]):
        if i in num_cols:
            out += '<th class="num">' + h + "</th>"
        else:
            out += "<th>" + h + "</th>"
    out += "</tr></thead><tbody>"

    # Build data rows
    for row in rows[1:]:
        out += "<tr>"
        for i, cell in enumerate(row):
            if i in num_cols:
                out += '<td class="num">' + cell + "</td>"
            else:
                out += "<td>" + cell + "</td>"
        out += "</tr>"
    out += "</tbody></table>"
    return out


def main():
    root = Path(__file__).resolve().parent.parent
    charts_dir = root / "docs" / "voiceai" / "charts"
    data_dir = root / "docs" / "voiceai" / "data"
    out_path = root / "docs" / "voiceai" / "VOICEAI_BENCHMARK_REPORT.html"

    chart_names = [
        "voiceai_chart_dashboard_final",
        "voiceai_chart_ttfb_boxplot",
        "voiceai_chart_ttfb_bars",
        "voiceai_chart_speedup",
        "voiceai_chart_published_vs_measured",
        "voiceai_chart_consistency",
        "voiceai_chart_distribution",
        "voiceai_chart_total_time",
    ]
    charts = {}
    for name in chart_names:
        p = charts_dir / (name + ".png")
        if p.exists():
            charts[name] = encode_chart(p)
            print("Encoded " + name)

    def ci(name):
        return img_tag(charts.get(name, ""), name)

    summary_csv = (data_dir / "voiceai_summary_stats.csv").read_text()
    speedup_csv = (data_dir / "voiceai_speedup_analysis.csv").read_text()
    summary_table = csv_to_html(summary_csv)
    speedup_table = csv_to_html(speedup_csv)

    # Load the template and replace placeholders
    tmpl_path = Path(__file__).parent / "report_template.html"
    html = tmpl_path.read_text()
    print("Loaded template: " + str(tmpl_path) + " (" + str(len(html) // 1024) + "KB)")

    html = html.replace("{{CHART_DASHBOARD}}", ci("voiceai_chart_dashboard_final"))
    html = html.replace("{{CHART_TTFB_BOXPLOT}}", ci("voiceai_chart_ttfb_boxplot"))
    html = html.replace("{{CHART_TTFB_BARS}}", ci("voiceai_chart_ttfb_bars"))
    html = html.replace("{{CHART_SPEEDUP}}", ci("voiceai_chart_speedup"))
    html = html.replace("{{CHART_PUBLISHED}}", ci("voiceai_chart_published_vs_measured"))
    html = html.replace("{{CHART_CONSISTENCY}}", ci("voiceai_chart_consistency"))
    html = html.replace("{{CHART_DISTRIBUTION}}", ci("voiceai_chart_distribution"))
    html = html.replace("{{CHART_TOTAL_TIME}}", ci("voiceai_chart_total_time"))
    html = html.replace("{{SUMMARY_TABLE}}", summary_table)
    html = html.replace("{{SPEEDUP_TABLE}}", speedup_table)

    out_path.write_text(html)
    size_kb = len(html) // 1024
    print("Written: " + str(out_path) + " (" + str(size_kb) + "KB)")

    # Verify
    issues = []
    if "{{" in html:
        issues.append("Unreplaced placeholders")
    if html[:20].count("\\") > 0:
        issues.append("Backslash in DOCTYPE")
    if issues:
        print("ISSUES: " + str(issues))
    else:
        print("CLEAN")


if __name__ == "__main__":
    main()
