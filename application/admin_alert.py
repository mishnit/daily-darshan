"""Send an approved ops template inviting admins into the review conversation."""
import argparse
import csv
import os
from pathlib import Path

from adapters.whatsapp import MetaWhatsAppClient
from application.admin_whatsapp import admin_numbers
from domain.clock import today_ist


def payment_counts(rows, today):
    review = [r for r in rows if r.get("status") in {"PENDING", "SUPERSEDED"} and r.get("utr")]
    missing = [r for r in rows if r.get("status") == "PENDING" and not r.get("utr")
               and r.get("reference_id", "").startswith(f"DD{today:%y%m%d}")]
    return len(review), len(missing)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=["payments", "images"])
    args = parser.parse_args(argv)
    if args.kind == "payments":
        with Path("csv/payments.csv").open(newline="") as fh:
            review, missing = payment_counts(list(csv.DictReader(fh)), today_ist())
        if not review and not missing:
            print("No payments need attention")
            return 0
        status = f"{review} confirmed UTR(s) to review; {missing} awaiting UTR. Reply ADMIN to review"
    else:
        status = "Images awaiting source selection. Reply ADMIN to preview and approve today's image"
    numbers = admin_numbers()
    if not numbers:
        raise RuntimeError("WHATSAPP_ADMIN_NUMBERS must identify a separate authorized admin WhatsApp account")
    client = MetaWhatsAppClient()
    for number in sorted(numbers):
        result = client.send_template_params(number, "daily_darshan_ops_alert",
            ["Payment review" if args.kind == "payments" else "Daily image review", status],
            lang="en", url_button_param=os.environ["GITHUB_RUN_ID"])
        if not result.ok:
            raise RuntimeError("Admin alert was not accepted; inspect the workflow and send ADMIN manually")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
