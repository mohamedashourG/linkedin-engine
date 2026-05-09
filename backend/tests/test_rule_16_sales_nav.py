"""
RULE 16 — Sales Nav harvest. Manual contact import (the operator's pre-
filtered Sales Nav export). Verification, not implementation: the upload
pipe already exists. This test pins the contract so a refactor can't
silently break it.

The "harvest" itself is human work outside the engine — operator filters in
Sales Nav / Apollo, exports CSV, uploads here. Engine consumes the upload
via discovery_seeds (RULE 16 lands as source='manual' in the seed pool).
"""
from __future__ import annotations

import io

from app.models.contact import (
    ContactBulkRequest,
    ContactCreate,
    parse_bulk,
    parse_csv_bytes,
    parse_excel_bytes,
)


def test_contact_create_accepts_full_record():
    c = ContactCreate(
        name="Jane Rivera",
        title="VP HR",
        company="St Example Health System",
        linkedin_url="https://www.linkedin.com/in/jane-rivera/",
    )
    assert c.name == "Jane Rivera"
    assert c.title == "VP HR"
    assert c.company == "St Example Health System"
    assert str(c.linkedin_url).startswith("https://www.linkedin.com/in/jane-rivera")


def test_contact_create_accepts_minimal_record():
    """Some Sales Nav exports lack title or company; the model must still
    accept them (the engine's enrichment step fills gaps)."""
    c = ContactCreate(name="Solo Owner", linkedin_url="https://www.linkedin.com/in/solo-owner")
    assert c.title is None and c.company is None


def test_parse_bulk_text_handles_three_shapes():
    """Bulk-paste accepts: name only, name + URL, name + title + company."""
    text = """
    # comments OK
    Alex Cofounder
    Rayan Cofounder, https://linkedin.com/in/rayan
    Michael Cofounder, VP Sales, Health Co
    https://linkedin.com/in/url-only-row
    """
    rows = parse_bulk(text)
    assert len(rows) == 4
    assert rows[0].name == "Alex Cofounder"
    assert str(rows[1].linkedin_url).rstrip("/") == "https://linkedin.com/in/rayan"
    assert rows[2].title == "VP Sales"
    assert rows[2].company == "Health Co"
    # URL-only rows get the slug back as name.
    assert rows[3].name == "url-only-row"


def test_parse_csv_with_headers():
    """The Sales Nav CSV export has 'Name' / 'Title' / 'Company' / 'LinkedIn URL'
    columns. parse_csv_bytes must detect headers and map them."""
    csv = (
        "Name,Title,Company,LinkedIn URL\n"
        "Jane Rivera,VP HR,St Example,https://www.linkedin.com/in/jane-rivera\n"
        "Bob,Director,FQHC Co,https://www.linkedin.com/in/bob\n"
    )
    rows = parse_csv_bytes(csv.encode("utf-8"))
    assert len(rows) == 2
    assert rows[0].name == "Jane Rivera"
    assert rows[0].title == "VP HR"
    assert str(rows[0].linkedin_url).rstrip("/") == "https://www.linkedin.com/in/jane-rivera"


def test_parse_csv_handles_bom():
    """UTF-8 BOM (Excel-saved CSVs) shouldn't break header detection."""
    csv = "﻿Name,LinkedIn URL\nJane,https://www.linkedin.com/in/jane\n".encode("utf-8")
    rows = parse_csv_bytes(csv)
    assert len(rows) == 1 and rows[0].name == "Jane"


def test_parse_excel_xlsx():
    """Operators occasionally export .xlsx directly. Verify the parser
    handles it."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Name", "Title", "Company", "LinkedIn URL"])
    ws.append(["Sam Tellez", "Head of Platform", "B2B SaaS Co", "https://www.linkedin.com/in/sam-tellez-staff"])
    buf = io.BytesIO()
    wb.save(buf)
    rows = parse_excel_bytes(buf.getvalue())
    assert len(rows) == 1
    assert rows[0].name == "Sam Tellez"
    assert rows[0].title == "Head of Platform"


def test_parse_csv_falls_back_to_positional_columns():
    """No headers means positional: name, title, company, linkedin_url."""
    csv = "Jane Rivera,VP HR,Co,https://www.linkedin.com/in/jane-rivera\n".encode("utf-8")
    rows = parse_csv_bytes(csv)
    assert len(rows) == 1
    assert rows[0].name == "Jane Rivera"
    assert rows[0].title == "VP HR"


def test_routes_module_imports_without_error():
    """Smoke check that the contacts routes module wires up. If a refactor
    removes parse_bulk / ContactBulkRequest etc., the route file would error
    at import time."""
    from app.routes import contacts as contacts_routes
    assert contacts_routes.router.prefix == "/api/contacts"
    paths = {r.path for r in contacts_routes.router.routes}
    # Three import paths from the module docstring (RULE 16 contract).
    assert "/api/contacts/" in paths
    assert "/api/contacts/bulk" in paths
    assert "/api/contacts/upload" in paths
