# Power BI integration

The application stores invoice metadata and audit events in Azure Database for
PostgreSQL. Invoice PDFs remain in
SharePoint; `sharepoint_web_url` links each report row to its PDF.

Migrations `011_power_bi_reporting_views.sql` and
`013_operational_visibility.sql` create three read-only views:

| View | Grain | Contents |
| --- | --- | --- |
| `public.bi_invoice_metadata` | One row per invoice | Source, extracted fields, confidence, review, route, approval, payment, reconciliation, and SharePoint link |
| `public.bi_invoice_events` | One row per audit event | Invoice ID, IRJ, event type, message, event data, and timestamp |
| `public.bi_invoice_corrections` | One row per corrected invoice | Original extraction, human corrections, correction reason, model and prompt version, reviewer, and final routing explanation |

Join `bi_invoice_events.invoice_id` to `bi_invoice_metadata.id` as a many-to-one
relationship. Keep the invoice table on the one side. These views read current
PostgreSQL records; no second invoice dataset is maintained by the application.
Join `bi_invoice_corrections.invoice_id` to `bi_invoice_metadata.id` as a
one-to-one relationship. The original and corrected field sets are JSON text;
expand them in Power Query when building field-level accuracy measures.

## Database setup

1. Restore database connectivity for the application and for the Power BI
   Desktop or gateway machine. Azure PostgreSQL public access requires an
   allowlisted source IP; private access requires a network path into its VNet.
2. Apply migrations as the database migration identity:

   ```bash
   python -m app.postgres_migrate
   ```

3. Create a dedicated PostgreSQL reader identity for Power BI. As the database
   owner, adapt `app/postgres_power_bi_grants.sql.template` to that role and
   database name, then apply it. Grant access only to the three views. The views
   include personal and financial invoice data, so scope Power BI workspace and
   report access accordingly.
4. Verify the reporting identity can `SELECT` the three views and cannot
   modify invoice records.

## Connect Power BI

In Power BI Desktop, choose **Get Data → PostgreSQL database**. Enter the
configured `AZURE_POSTGRES_HOST` and `AZURE_POSTGRES_DATABASE`, select **Import**
for a scheduled snapshot or **DirectQuery** for live database queries, and sign
in with the dedicated reader identity. In Navigator, select the three
`public.bi_` views. Set `id` and `invoice_id` to whole numbers and treat
invoice and payment
dates as dates after checking for malformed legacy values. Preserve `currency`
in spend visuals; sums across currencies are not meaningful without exchange
rates.

Suggested first report pages: invoice volume over time, outstanding invoices
by company and status, overdue invoices using supplier terms, supplier spend by
currency, correction rate by extraction model and supplier, low confidence
fields that are corrected most often, and a drill-through page showing each
invoice's event history and SharePoint PDF link. The web app's **Reporting
metrics** section is a separate in-app dashboard, not a published Power BI
report.

After publishing, configure the semantic model's credentials and refresh or
gateway so the Power BI service can reach the same PostgreSQL host. Test a
refresh and compare invoice counts with a database `SELECT count(*) FROM
public.bi_invoice_metadata`. A successful local connection alone does not prove
that scheduled refresh from the Power BI service works.
