"""A persisted admin decision gates image publication and all customer sends."""
from datetime import datetime
import hashlib
from pathlib import Path
from uuid import uuid4, uuid5, NAMESPACE_URL

from domain.clock import INDIA_TZ

# GitHub Pages omits dotfiles unless Jekyll is explicitly disabled.  This
# approval stamp is part of the public deployment proof, so it must have a
# normal public filename.
PUBLIC_APPROVAL_STAMP = "image-approval.json"


def required(config):
    return config.get("admin", {}).get("require_image_approval", False)


def approved(c, on_date):
    rows = [r for r in c.image_reviews.all() if r["date"] == on_date.isoformat()
            and r["status"] == "APPROVED"]
    return rows[0] if len(rows) == 1 else None


def ready(c, on_date):
    return not required(c.config) or approved(c, on_date) is not None


def require_ready(c, on_date):
    if not ready(c, on_date):
        raise RuntimeError(f"Awaiting admin image selection for {on_date}; no pages or customer sends allowed")


def require_published(c, on_date):
    require_ready(c, on_date)
    if not required(c.config):
        return
    if not deployment_ready(c.root, on_date):
        raise RuntimeError("Approved image has not been rendered; customer sends blocked")
    import requests
    row = approved(c, on_date)
    base = c.config.get("delivery", {}).get("page_base_url", "").rstrip("/")
    if not base.startswith("https://"):
        raise RuntimeError("Public page URL is required to verify image approval deployment")
    response = requests.get(base + "/" + PUBLIC_APPROVAL_STAMP, params={"approval": row["id"]},
                            headers={"Cache-Control": "no-cache"}, timeout=10)
    response.raise_for_status()
    if response.json() != {"id": row["id"], "date": row["date"], "sha256": row["sha256"]}:
        raise RuntimeError("Approved image deployment is not live; customer sends blocked")


def queue_request(c, key, reason):
    if not c.pipeline_requests.find(key):
        c.pipeline_requests.upsert(key, {"id": key, "reason": reason,
            "created_at": datetime.now(INDIA_TZ).isoformat()})


def collect_for_review(c, git, on_date, *, force_recollect: bool = False):
    from scheduler import _canonical_jpeg
    if approved(c, on_date) and not force_recollect:
        print(f"[image] {on_date} already approved; preserving selected image")
        return 0
    candidates = c.image_service.collect_daily_images(on_date)
    generation = uuid4().hex
    files = []
    for row in c.image_reviews.all():
        if row["date"] == on_date.isoformat() and row["status"] in {"PENDING", "APPROVED"}:
            row["status"] = "SUPERSEDED"
            c.image_reviews.upsert(row["id"], row)
    for image in candidates:
        path = c.image_service.candidate_path(on_date, image.source)
        data = _canonical_jpeg(image.data, on_date, image.source, append_footer=image.append_footer)
        git.write_file(path, data, f"Store review candidate {on_date} {image.source}")
        files.append(path)
        key = uuid4().hex
        c.image_reviews.upsert(key, {"id": key, "date": on_date.isoformat(),
            "generation": generation, "source": image.source, "path": path,
            "sha256": hashlib.sha256(data).hexdigest(), "status": "PENDING",
            "approved_by": "", "approved_at": ""})
    files.append(c.config["paths"].get("image_reviews_csv", "csv/image_reviews.csv"))
    git.commit(files, f"Queue image selection {on_date}")
    print(f"[image] {len(candidates)} previews stored; awaiting admin selection")
    return 0


def materialize(c, git, on_date):
    """Only approved bytes may become the canonical image."""
    row = approved(c, on_date)
    if not row:
        require_ready(c, on_date)
        return None
    data = git.read_file(row["path"])
    if not data or hashlib.sha256(data).hexdigest() != row["sha256"]:
        raise RuntimeError("Approved image bytes changed or are missing; publication blocked")
    images_dir = c.config["paths"]["images_dir"]
    existing = sorted((Path(c.root) / images_dir).glob(f"*_{on_date}.jpg"))
    name = existing[0].name if existing else f"{uuid5(NAMESPACE_URL, row['id'])}_{on_date}.jpg"
    path = str(Path(images_dir) / name)
    git.write_file(path, data, f"Publish approved image {on_date}")
    return path


def deployment_ready(root, on_date):
    """Standard-library-only gate for the deploy workflow."""
    import csv
    import json
    root = Path(root)
    config = json.loads((root / "config.json").read_text())
    if not required(config):
        return True
    path = root / config["paths"].get("image_reviews_csv", "csv/image_reviews.csv")
    if not path.exists():
        return False
    with path.open(newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if r["date"] == on_date.isoformat() and r["status"] == "APPROVED"]
    if len(rows) != 1:
        return False
    row = rows[0]
    stamp = root / config.get("delivery", {}).get("pages_dir", "docs") / PUBLIC_APPROVAL_STAMP
    if not stamp.exists():
        return False
    try:
        rendered = json.loads(stamp.read_text())
    except (ValueError, OSError):
        return False
    if rendered != {"id": row["id"], "date": row["date"], "sha256": row["sha256"]}:
        return False
    images = root / config["paths"]["images_dir"]
    candidates = list(images.glob(f"*_{on_date}.jpg"))
    return bool(candidates) and all(hashlib.sha256(p.read_bytes()).hexdigest() == row["sha256"] for p in candidates)


if __name__ == "__main__":
    import argparse
    import csv
    import json
    import os
    from domain.clock import today_ist
    parser = argparse.ArgumentParser()
    parser.add_argument("--published", action="store_true")
    args = parser.parse_args()
    config = json.loads(Path("config.json").read_text())
    if args.published:
        ok = deployment_ready(".", today_ist())
    elif not required(config):
        ok = True
    else:
        path = Path(config["paths"].get("image_reviews_csv", "csv/image_reviews.csv"))
        rows = []
        if path.exists():
            with path.open(newline="") as fh:
                rows = list(csv.DictReader(fh))
        ok = len([r for r in rows if r["date"] == today_ist().isoformat() and r["status"] == "APPROVED"]) == 1
    print(f"ready={str(ok).lower()}")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write(f"ready={str(ok).lower()}\n")
