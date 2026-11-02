# Viking Power Studio – CRM, Proposal, Invoice & Payments MVP

## Included
- CRM leads and customer records
- Editable product catalogue
- Solar proposal PDF generator
- Solar savings model with automatic/manual overrides
- Accepted proposal → project conversion
- GST invoice creation in consolidated EPC or item-wise format
- Financial-year invoice numbering: VPE/001/2026-27
- CGST/SGST or IGST calculation per invoice line
- HSN/SAC-wise line detail, tax summary and amount in words
- Invoice PDF download based on the supplied Viking Power tax invoice
- Payment recording and outstanding balance reconciliation

## Run locally
```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

## Item-wise invoice entry
Enter one line per row:
`Description | HSN/SAC | Quantity | Unit | Rate ₹ | GST %`

The app stores each invoice line separately and calculates tax per line. Intra-state supplies split GST into CGST and SGST; inter-state supplies use IGST.

## Important before production
- Verify GST classification, HSN/SAC, rates, place-of-supply logic and invoice particulars with your CA.
- Configure and verify company bank details and seller details before issuing live invoices.
- This MVP uses local SQLite. For multi-user production, migrate to managed PostgreSQL/Supabase, add authentication, role permissions, audit logs, encrypted backups and tested restore procedures.
- This is not an e-invoicing / IRN integration and does not file GST returns.
